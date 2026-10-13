"""Workout Buddy v4: a set is asked ("💪 10 push-ups?"), and logged only when it is answered: the band's
[1] done (`cli done --ask <id>`) or /workout done. An unanswered ask is declined at Stop."""
import datetime as dt
import json
import re
import time

import pytest

from test_workout import (FAKE_SETTINGS, env, load_module, log, notes, run, state, stop, wait_for,  # noqa: F401
                          write_cfg)

pytestmark = pytest.mark.legacy_modes  # written against the older modes (see conftest.py)


def setup_ask(ctx, **over):
    """v4 defaults (ask_mode button, spinner off) on the fast test timings."""
    ctx["env"]["WORKOUT_ASK_POLL_S"] = "0.1"
    cfg = {"ask_mode": "button", "spinner": "off", "max_lifetime_s": 20, "cooldown_s": 100, "ask_grace_s": 0,
           "exercises": [{"name": "push-ups", "min": 10, "max": 12, "unit": "reps"},
                         {"name": "squats", "min": 15, "max": 20, "unit": "reps"}]}
    cfg.update(over)
    write_cfg(ctx, **cfg)


def accept(ctx, sid="s1"):
    """Answer the open ask the way the band's [1] does; returns the set as text."""
    p = asked(ctx, sid)[0]
    r = run(ctx, "cli", "done", "--ask", p["id"])
    assert r.returncode == 0 and r.stdout.startswith("💪 Logged "), r.stdout
    return task(p)


def say(ctx, text="implement the retry logic and add tests for it", sid="s1"):
    return run(ctx, "hook-prompt", stdin=json.dumps({"session_id": sid, "transcript_path": "", "cwd": "/tmp",
                                                      "prompt": text}))


def pend(ctx, sid="s1"):
    return (state(ctx)["sessions"].get(sid) or {}).get("pending") or []


def asked(ctx, sid="s1"):
    return [p for p in pend(ctx, sid) if p.get("status") == "asked"]


def task(p):
    return f"{p['reps']}{'s' if p['unit'] == 'seconds' else ''} {p['exercise']}"


_LINK = re.compile(r"  \x1b\]8;;[^\x1b]*\x1b\\✓ done\x1b\]8;;\x1b\\")


def status_raw(ctx, sid="s1"):
    return run(ctx, "status", sid).stdout


def status(ctx, sid="s1"):
    """The status line without the link button's OSC 8 link (test_v5 tests the link itself)."""
    return _LINK.sub("", status_raw(ctx, sid))


def done(ctx):
    return [r for r in log(ctx) if r.get("status") == "done"]


def declined(ctx):
    return [r for r in log(ctx) if r.get("status") == "declined"]


def is_badge(text, want):
    """'✅ <set> · <cheer>' (v4.1), the cheer one of the pool."""
    mod = load_module()
    head = f"✅ {want} · "
    return text.startswith(head) and text[len(head):] in mod.CHEERS


def settings_fp(ctx):
    sb = ctx["settings"].stat()
    return sb.st_mtime_ns, sb.st_size, sb.st_ino, ctx["settings"].read_bytes()


# ---------------------------------------------------------------- an answer logs the set

def test_answering_the_ask_logs_the_set_once(env):
    setup_ask(env)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    p = asked(env)[0]
    want = task(p)
    assert status(env) == f"💪 {want}?"
    time.sleep(0.5)
    assert done(env) == []  # nothing is logged without an answer
    accept(env)
    recs = done(env)
    assert len(recs) == 1 and recs[0]["via"] == "mod" and task(recs[0]) == want
    assert recs[0]["session_id"] == "s1" and recs[0]["turn_ts"] == state(env)["sessions"]["s1"]["turn"]["ts"]
    badge = status(env)
    assert is_badge(badge, want)  # for the rest of the turn
    time.sleep(0.4)
    assert len(done(env)) == 1  # polling on does not log it again
    r = run(env, "cli", "done", "--ask", p["id"])
    assert r.stdout.startswith(f"Already logged: {want}") and len(done(env)) == 1
    r = stop(env)
    msg = json.loads(r.stdout)["systemMessage"]
    assert msg.startswith(f"Logged {want} · today 1 set")
    assert "🏆 Unlocked: First Blood (first set logged)" in msg
    for word in ("great", "nice", "awesome", "!"):
        assert word not in msg.split("\n")[0].lower()
    assert len(done(env)) == 1 and declined(env) == []
    assert status(env) == badge  # v4.1: the badge stays badge_s (120 s) after Stop
    assert stop(env).stdout == ""  # announced once


def test_an_unanswered_ask_logs_nothing(env):
    setup_ask(env)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    time.sleep(0.8)
    assert done(env) == [] and status(env).endswith("?")
    assert stop(env).stdout == ""  # declined: silence
    d = declined(env)
    assert len(d) == 1 and d[0]["session_id"] == "s1" and d[0]["asked_at"]
    assert done(env) == []
    assert "Today: 0 sets" in run(env, "cli", "stats").stdout


def test_no_second_ask_while_one_is_open(env):
    """v4.1: one task per turn, so an open ask is never superseded by a repeat, even with no cooldown and a
    short repeat_s."""
    setup_ask(env, cooldown_s=0, repeat_s=0.2)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    p = asked(env)[0]
    end = time.time() + 1.5
    while time.time() < end:  # many repeat_s periods, the question drawn throughout
        assert status(env) == f"💪 {task(p)}?"
        time.sleep(0.1)
    assert [q.get("id") for q in pend(env)] == [p["id"]] and pend(env)[0]["status"] == "asked"
    assert stop(env).stdout == ""
    d = declined(env)
    assert len(d) == 1 and d[0]["ask_id"] == p["id"] and done(env) == []


def test_status_line_is_per_session(env):
    setup_ask(env)
    today = dt.datetime.combine(dt.date.today(), dt.time(9, 0)).timestamp()
    with open(env["home"] / "log.jsonl", "a") as f:
        f.write(json.dumps({"ts": today, "session_id": "x", "exercise": "squats", "reps": 15, "unit": "reps",
                            "status": "done"}) + "\n")
    say(env, sid="a")
    assert wait_for(lambda: asked(env, "a"), 5)
    say(env, sid="b")  # the global cooldown (100 s) keeps b from asking
    time.sleep(0.5)
    assert status(env, "a").endswith("?") and status(env, "a").startswith("💪 ")
    assert status(env, "b") == "💪 1 set today"
    assert run(env, "status").stdout == "💪 1 set today"  # no session id: the daily segment
    stop(env, "a")
    stop(env, "b")


def test_pause_hides_the_ask_and_blocks_answering(env):
    setup_ask(env)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    p = asked(env)[0]
    run(env, "cli", "pause", "10")
    r = run(env, "cli", "done", "--ask", p["id"])
    assert r.returncode == 1 and r.stdout.startswith("Workout Buddy is paused")
    assert done(env) == [] and status(env) == "💤 paused"
    stop(env)
    run(env, "cli", "resume")


# ---------------------------------------------------------------- manual override

def test_bare_done_logs_the_asked_set_and_stop_stays_silent(env):
    setup_ask(env)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    want = task(asked(env)[0])
    out = run(env, "cli", "done").stdout
    assert out.startswith(f"💪 Logged {want} · today: 1 set")
    recs = done(env)
    assert len(recs) == 1 and recs[0]["via"] == "manual" and recs[0]["session_id"] == "s1"
    assert is_badge(status(env), want)
    assert stop(env).stdout == ""  # already said by /workout done; not declined either
    assert declined(env) == [] and len(done(env)) == 1


def test_bare_done_without_an_ask_logs_the_next_set(env):
    setup_ask(env)
    out = run(env, "cli", "done").stdout
    assert out.startswith("💪 Logged ")
    assert done(env)[0]["session_id"] == "manual"


def test_undo_after_an_answered_set_takes_it_back_and_clears_the_tick(env):
    setup_ask(env)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    accept(env)
    assert status(env).startswith("✅ ")
    assert run(env, "cli", "undo").stdout.startswith("↩️ Undone: ")
    assert done(env) == [] and status(env) == ""
    assert stop(env).stdout == ""  # nothing left to announce


def test_skip_dismisses_the_open_ask_and_never_touches_the_log(env):
    """/workout skip is the band's [2]: the question goes (declined), and no logged set is taken back."""
    setup_ask(env)
    run(env, "cli", "done", "squats", "15")  # a set logged earlier, by hand
    say(env)
    assert wait_for(lambda: asked(env), 5)
    p = asked(env)[0]
    out = run(env, "cli", "skip").stdout
    assert out.startswith(f"Skipped {task(p)}")
    assert asked(env) == [] and status(env) == "💪 1 set today"
    assert [r["status"] for r in log(env)] == ["done", "declined"] and log(env)[1]["ask_id"] == p["id"]
    r = run(env, "cli", "skip")
    assert r.returncode == 1 and r.stdout.startswith("No open question to skip")
    assert [r["status"] for r in log(env)] == ["done", "declined"]  # still nothing undone
    assert stop(env).stdout == ""


# ---------------------------------------------------------------- spinner off

def test_spinner_off_removes_the_key_once_then_never_writes(env):
    setup_ask(env, spinner="progress")
    run(env, "cli", "setup", "apply", "--yes")
    assert "spinnerVerbs" in json.loads(env["settings"].read_text())  # progress lines
    setup_ask(env)  # now "spinner": "off"
    say(env, "thanks")  # a short turn: the guarded writer puts the earlier value back, once
    stop(env)
    s = json.loads(env["settings"].read_text())
    assert s == FAKE_SETTINGS
    fp = settings_fp(env)
    say(env, "thanks")
    stop(env)  # a short turn: zero writes
    say(env)
    assert wait_for(lambda: asked(env), 5)  # an ask: still nothing in the spinner
    assert status(env).endswith("?")
    accept(env)
    stop(env)
    run(env, "cli", "undo")
    run(env, "cli", "pause", "5")
    run(env, "cli", "resume")
    assert settings_fp(env) == fp
    out = run(env, "cli", "setup", "apply", "--yes").stdout
    assert "already held its earlier value" in out.split("\n")[0] and settings_fp(env)[3] == fp[3]
    assert run(env, "cli", "setup", "apply", "--yes").stdout.startswith("Spinner off → ")
    assert "never set spinnerVerbs" in run(env, "cli", "setup", "apply", "--yes").stdout  # no longer managed


def test_cli_setup_off_removes_only_its_own_key_and_says_so(env):
    # with no earlier value, "off" removes the key Workout Buddy wrote
    s0 = {k: v for k, v in FAKE_SETTINGS.items() if k != "spinnerVerbs"}
    env["settings"].write_text(json.dumps(s0, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    setup_ask(env, spinner="progress")
    run(env, "cli", "setup", "apply", "--yes")
    assert "spinnerVerbs" in json.loads(env["settings"].read_text())
    setup_ask(env)
    out = run(env, "cli", "setup", "apply", "--yes").stdout
    first = out.split("\n")[0]
    assert first.startswith("Spinner off → ") and "removed spinnerVerbs" in first
    assert json.loads(env["settings"].read_text()) == s0
    assert "spinnerVerbs" not in json.loads(out.split("Spinner settings", 1)[1].split("\n", 1)[1])


def test_default_config_is_v4():
    """The default ask_mode is "button": nothing is logged without an answer."""
    mod = load_module()
    d = mod.normalize_config({})
    assert (d["spinner"], d["ask_mode"], d["ask_grace_s"]) == ("off", "button", 180)
    assert mod.ASK_MODES == ("button", "auto") and "idle_accept_s" not in d
    assert mod.normalize_config({"ask_mode": "idle"})["ask_mode"] == "button"  # gone: never accepted
    assert "spinner_task" not in d and "spinner_idle" not in d


def test_old_spinner_keys_map_onto_spinner():
    mod = load_module()
    n = mod.normalize_config
    assert n({"spinner_task": True})["spinner"] == "progress"
    assert n({"spinner_task": True, "spinner_idle": "progress"})["spinner"] == "progress"
    assert n({"spinner_task": True, "spinner_idle": "claude"})["spinner"] == "task"
    assert n({"spinner_idle": "claude"})["spinner"] == "task"
    assert n({"spinner_task": False, "spinner_idle": "progress"})["spinner"] == "off"
    assert n({"spinner_task": True, "spinner": "off"})["spinner"] == "off"  # the v4 key wins
    assert n({"spinner": "loud"})["spinner"] == "off"
    assert n({"ask_mode": "auto"})["ask_mode"] == "auto" and n({"ask_mode": "x"})["ask_mode"] == "button"


# ---------------------------------------------------------------- auto mode (v3.2)

def test_auto_mode_credits_every_due_set_at_stop(env):
    setup_ask(env, ask_mode="auto")
    say(env)
    assert wait_for(lambda: pend(env), 5)
    p = pend(env)[0]
    assert "status" not in p and status(env) == f"🏋 NOW: {task(p)}"
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert msg.startswith(f"Logged {task(p)}")
    assert len(done(env)) == 1 and "via" not in done(env)[0] and declined(env) == []


def test_short_turn_shows_nothing(env):
    setup_ask(env, delay_s=5)
    say(env, "thanks")
    assert status(env) == ""
    assert stop(env).stdout == ""
    assert log(env) == []


def test_injected_message_alone_never_asks(env):
    """A turn with no human prompt (only an injected message) never asks, so nothing can be answered."""
    setup_ask(env)
    say(env, text="<task-notification>agent finished</task-notification>")
    time.sleep(1.0)
    st = env["home"] / "state.json"
    assert not st.exists() or (asked(env) == [] and pend(env) == [])  # no turn, no timer, no ask
    assert done(env) == [] and status(env) == ""


# ---------------------------------------------------------------- fix round (review findings)

from test_v3 import _now_stamp, user  # noqa: E402


def test_badge_after_esc_stays_badge_s_then_goes(env):
    """v4.1: after Esc the answered set stays logged and the badge stays badge_s more; then the normal
    segment. The timer exits."""
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text(user(_now_stamp(-1), "implement it") + "\n")
    setup_ask(env, stale_s=1000, badge_s=1.5, ask_grace_s=0)
    run(env, "hook-prompt", stdin=json.dumps({"session_id": "s1", "transcript_path": str(tr), "cwd": "/tmp",
                                              "prompt": "implement the retry logic and add tests for it"}))
    assert wait_for(lambda: asked(env), 5)
    assert status(env).endswith("?")
    want = accept(env)
    badge = status(env)
    assert is_badge(badge, want)
    time.sleep(1.6)  # past badge_s since the acceptance: the live turn keeps it up
    assert status(env) == badge
    with open(tr, "a") as f:
        f.write(user(_now_stamp(), [{"type": "text", "text": "[Request interrupted by user]"}]) + "\n")
    assert wait_for(lambda: state(env)["sessions"]["s1"].get("nudging") is False, 15)
    ended = state(env)["sessions"]["s1"]["badge"]["ended_at"]
    if time.time() < ended + 1.3:
        assert status(env) == badge  # after Esc: still up for badge_s
    assert len(done(env)) == 1  # still logged
    assert wait_for(lambda: status(env) == "💪 1 set today", 5)
    assert time.time() >= ended + 1.5
    pid = state(env)["timer_pids"][-1]
    from test_workout import pid_alive
    assert wait_for(lambda: not pid_alive(pid) or _zombie(pid), 15)


def _zombie(pid):
    import subprocess as sp
    out = sp.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return out == "" or out.startswith("Z")


def test_badge_timing_from_timestamps_only():
    """v4.1 (was test_tick_needs_a_live_timer): during the live turn the timer's heartbeat keeps the badge
    up, and at least badge_s after the acceptance even if the timer died; after the turn ended (Stop, Esc,
    the timer's end) badge_s past the later of acceptance and that end; no timer is consulted then."""
    mod = load_module()
    now = 1000.0
    text = "✅ 10 push-ups · bravo! 🔥"
    p = {"exercise": "push-ups", "reps": 10, "unit": "reps", "status": "accepted", "id": "a1",
         "show_until": now + 500}
    s = {"token": "t", "pending": [p], "badge": {"text": text, "ask_id": "a1", "accepted_at": now, "token": "t"}}
    st = {"sessions": {"s1": s}, "paused_until": 0}
    assert mod.session_ask_text(st, "s1", now, 120) == text
    assert mod.session_ask_text(st, "s1", now + 499, 120) == text  # heartbeat: the rest of the turn
    assert mod.session_ask_text(st, "s1", now + 501, 120) is None  # the timer stopped refreshing it
    s["nudging"] = False  # timer gone without ending the badge: badge_s after acceptance
    assert mod.session_ask_text(st, "s1", now + 119, 120) == text
    assert mod.session_ask_text(st, "s1", now + 121, 120) is None
    s.pop("nudging")
    assert mod.session_ask_text(st, "s2", now, 120) is None  # another session never shows it
    # Stop at now + 300: token gone, pending cleared; up to Stop + badge_s
    s.update(token=None, pending=[])
    s["badge"]["ended_at"] = now + 300
    assert mod.session_ask_text(st, "s1", now + 419, 120) == text
    assert mod.session_ask_text(st, "s1", now + 421, 120) is None
    # Stop right after acceptance: badge_s after acceptance (the later of the two)
    s["badge"]["ended_at"] = now + 1
    assert mod.session_ask_text(st, "s1", now + 120.5, 120) == text
    assert mod.session_ask_text(st, "s1", now + 121.5, 120) is None
    st["paused_until"] = now + 10_000
    assert mod.session_ask_text(st, "s1", now + 2, 120) is None  # paused: hidden
    st["paused_until"] = 0
    s["token"], s["badge"]["token"] = "new", "old"  # a badge from an earlier turn that never ended
    del s["badge"]["ended_at"]
    assert mod.session_ask_text(st, "s1", now + 2, 120) is None


def test_named_done_while_that_set_is_asked_closes_the_ask(env):
    """`/workout done push-ups` for the set being asked logs it once, via the ask."""
    setup_ask(env)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    p = asked(env)[0]
    out = run(env, "cli", "done", p["exercise"]).stdout
    assert out.startswith(f"💪 Logged {task(p)}")
    assert asked(env) == [] and is_badge(status(env), task(p))
    r = run(env, "cli", "done", "--ask", p["id"])  # the band's [1] afterwards logs nothing more
    assert r.stdout.startswith("Already logged")
    time.sleep(0.4)
    recs = done(env)
    assert len(recs) == 1 and recs[0]["via"] == "manual" and recs[0]["session_id"] == "s1"
    assert stop(env).stdout == "" and declined(env) == []


def test_named_done_for_another_exercise_leaves_the_ask_open(env):
    setup_ask(env)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    p = asked(env)[0]
    other = "squats" if p["exercise"] == "push-ups" else "push-ups"
    run(env, "cli", "done", other, "7")
    assert len(asked(env)) == 1 and done(env)[0]["session_id"] == "manual"
    stop(env)


def test_bare_done_right_after_the_band_answered_says_already_logged(env):
    """A bare /workout done just after the band's [1] must not log a second, different set."""
    setup_ask(env)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    want = accept(env)
    out = run(env, "cli", "done").stdout
    assert out.startswith(f"Already logged: {want}") and len(done(env)) == 1
    assert "/workout undo" in out and "/workout skip" not in out
    stop(env)
    assert run(env, "cli", "done").stdout.startswith("Already logged: ")  # also after Stop, within 2 min
    assert run(env, "cli", "done", "squats", "15").stdout.startswith("💪 Logged 15 squats")  # by name: logs
    assert len(done(env)) == 2
    assert run(env, "cli", "done").stdout.startswith("💪 Logged ")  # a manual set since: bare logs again


def test_pruned_session_records_its_open_ask_as_declined(env):
    """A session dropped by the 6 h prune with an open ask leaves a declined record."""
    setup_ask(env)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    p = asked(env)[0]
    for pid in state(env)["timer_pids"]:
        try:
            import os
            import signal
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    st = state(env)
    st["sessions"]["s1"]["updated_at"] = time.time() - 7 * 3600
    (env["home"] / "state.json").write_text(json.dumps(st))
    run(env, "cli", "resume")  # any locked_state prunes
    d = declined(env)
    assert "s1" not in state(env)["sessions"]
    assert len(d) == 1 and d[0]["ask_id"] == p["id"] and done(env) == []


def test_setup_off_after_the_hook_removed_the_key_says_when(env):
    """The hook may remove spinnerVerbs before `/workout setup` runs; setup says so."""
    setup_ask(env, spinner="progress")
    run(env, "cli", "setup", "apply", "--yes")
    setup_ask(env)  # "spinner": "off"
    say(env, "thanks")
    stop(env)
    assert json.loads(env["settings"].read_text()) == FAKE_SETTINGS  # the earlier value is back
    first = run(env, "cli", "setup", "apply", "--yes").stdout.split("\n")[0]
    assert "already held its earlier value" in first and "nothing written" in first
