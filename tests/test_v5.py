"""Workout Buddy v5: the status line's "✓ done" button (an OSC 8 link to a one-shot loopback listener inside
the turn's timer; "button": "link", test suite only). A set is logged only by an explicit answer. Real HTTP
(urllib) against the test timer's listener throughout."""
import datetime as dt
import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request

from test_v3 import _now_stamp, user
from test_v4 import _zombie, asked, declined, done, is_badge, pend, say, status_raw as status, task  # noqa: F401
from test_v41 import FORBIDDEN
from test_workout import env, load_module, log, pid_alive, run, state, stop, wait_for, write_cfg  # noqa: F401
import pytest

pytestmark = pytest.mark.legacy_modes  # written against the older modes (see conftest.py)

LINK = re.compile(r"\x1b\]8;;http://127\.0\.0\.1:(\d+)/done/([A-Za-z0-9_-]+)\x1b\\✓ done\x1b\]8;;\x1b\\$")
EXS = [{"name": "push-ups", "min": 10, "max": 12, "unit": "reps"},
       {"name": "squats", "min": 15, "max": 20, "unit": "reps"}]


def setup_button(ctx, linger="0.5", **over):
    """v5 defaults (ask_mode button) on fast timings: 0.1 s polls, a short grace and click linger."""
    ctx["env"]["WORKOUT_ASK_POLL_S"] = "0.1"
    ctx["env"]["WORKOUT_CLICK_LINGER_S"] = linger
    cfg = {"ask_mode": "button", "spinner": "off", "max_lifetime_s": 30, "cooldown_s": 100, "ask_grace_s": 3,
           "exercises": EXS}
    cfg.update(over)
    write_cfg(ctx, **cfg)


def sess(ctx, sid="s1"):
    return state(ctx)["sessions"].get(sid) or {}


def link(ctx, sid="s1"):
    """Wait for the ask and its live listener; return (port, token, the asked set, the status line)."""
    assert wait_for(lambda: asked(ctx, sid) and sess(ctx, sid).get("listener"), 5)
    want = task(asked(ctx, sid)[0])
    line = status(ctx, sid)
    m = LINK.search(line)
    assert m, repr(line)
    assert line == f"💪 {want}?  " + m.group(0)
    L = sess(ctx, sid)["listener"]
    assert (int(m.group(1)), m.group(2)) == (L["port"], L["token"])
    return int(m.group(1)), m.group(2), want, line


def http(port, path, method="GET", host=None):
    """(status, body) via urllib; an HTTP error status is returned, not raised."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    if host is not None:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def click(port, token):
    return http(port, f"/done/{token}")


def refused(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return False
    except ConnectionRefusedError:
        return True


def timer_gone(ctx):
    pids = state(ctx)["timer_pids"]
    return all(not pid_alive(p) or _zombie(p) for p in pids)


# ---------------------------------------------------------------- the click

def test_click_logs_once_badge_page_and_stop_line(env):
    setup_button(env)
    say(env)
    port, token, want, _ = link(env)
    code, body = click(port, token)
    assert code == 200
    assert f"✅ {want} · " in body and "Logged. You can close this tab." in body and "window.close()" in body
    assert "<link" not in body and "src=" not in body and "http" not in body.split("</style>")[1]  # no assets
    recs = done(env)
    assert len(recs) == 1 and recs[0]["via"] == "button" and recs[0]["session_id"] == "s1"
    assert recs[0]["ask_id"] == sess(env)["listener"]["ask_id"] and recs[0]["turn_ts"] == sess(env)["turn"]["ts"]
    badge = status(env)
    assert is_badge(badge, want) and badge.split(" · ", 1)[1] in body
    code2, body2 = click(port, token)  # a repeat click: same page shape, no second row
    assert code2 == 200 and "Already logged" in body2 and len(done(env)) == 1
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert msg.startswith(f"Logged {want} · today 1 set") and "🏆 Unlocked: First Blood" in msg
    assert declined(env) == [] and len(done(env)) == 1
    assert stop(env).stdout == ""  # announced once
    assert wait_for(lambda: refused(port), 5)  # the linger (0.5 s) ran out: closed
    assert wait_for(lambda: timer_gone(env), 5)  # and the timer process is gone with it
    assert "listener" not in sess(env)


def test_listener_closes_after_click_linger_while_the_turn_runs(env):
    setup_button(env, linger="0.3")
    say(env)
    port, token, want, _ = link(env)
    assert click(port, token)[0] == 200
    assert wait_for(lambda: refused(port), 5)  # the question is answered: the listener goes
    assert is_badge(status(env), want)  # the badge stays for the rest of the turn
    assert sess(env)["token"] and "listener" not in sess(env)
    stop(env)
    assert len(done(env)) == 1


def test_localhost_host_is_accepted(env):
    setup_button(env)
    say(env)
    port, token, want, _ = link(env)
    code, body = http(port, f"/done/{token}", host=f"localhost:{port}")
    assert code == 200 and len(done(env)) == 1
    stop(env)


def test_wrong_requests_get_404_and_change_nothing(env):
    setup_button(env)
    say(env)
    port, token, want, line = link(env)
    log_before = (env["home"] / "log.jsonl").read_bytes() if (env["home"] / "log.jsonl").exists() else b""
    other = "A" * len(token) if token != "A" * len(token) else "B" * len(token)
    cases = [("GET", f"/done/{other}", None), ("GET", f"/done/{token[:-1]}", None), ("GET", "/done/", None),
             ("GET", "/done", None), ("GET", "/", None), ("GET", "/favicon.ico", None),
             ("GET", f"/done/{token}/", None), ("GET", f"/done/{token}?x=1", None), ("GET", f"/DONE/{token}", None),
             ("POST", f"/done/{token}", None), ("PUT", f"/done/{token}", None), ("DELETE", f"/done/{token}", None),
             ("OPTIONS", f"/done/{token}", None), ("PATCH", f"/done/{token}", None), ("BREW", f"/done/{token}", None),
             ("GET", f"/done/{token}", f"evil.example:{port}"), ("GET", f"/done/{token}", "127.0.0.1"),
             ("GET", f"/done/{token}", f"127.0.0.1:{port + 1}"), ("GET", f"/done/{token}", f"0.0.0.0:{port}"),
             ("GET", f"/done/{token}", f"localhost.evil.example:{port}")]
    for method, path, host in cases:
        code, _ = http(port, path, method, host)
        assert code == 404, (method, path, host, code)
    code, body = http(port, f"/done/{token}", "HEAD")
    assert code == 404 and body == ""
    assert done(env) == [] and len(asked(env)) == 1
    log_after = (env["home"] / "log.jsonl").read_bytes() if (env["home"] / "log.jsonl").exists() else b""
    assert log_after == log_before
    assert status(env) == line  # still asked, same button
    assert click(port, token)[0] == 200 and len(done(env)) == 1  # the right one still works
    stop(env)


def test_status_link_only_while_the_heartbeat_is_fresh(env):
    setup_button(env)
    say(env)
    port, token, want, line = link(env)
    assert sess(env)["listener"]["alive_until"] > time.time()
    import os
    import signal
    for pid in state(env)["timer_pids"]:
        try:
            os.kill(pid, signal.SIGKILL)  # the timer dies, its listener with it
        except ProcessLookupError:
            pass
    assert wait_for(lambda: refused(port), 5)
    st = state(env)
    st["sessions"]["s1"]["listener"]["alive_until"] = time.time() - 1  # what 10 s without a refresh looks like
    for p in st["sessions"]["s1"]["pending"]:
        p["show_until"] = time.time() + 60
    (env["home"] / "state.json").write_text(json.dumps(st))
    assert status(env) == f"💪 {want}?"  # the question, no dead link
    stop(env)


def test_status_unit_link_rules():
    mod = load_module()
    now = 1000.0
    p = {"exercise": "wall sit", "reps": 32, "unit": "seconds", "status": "asked", "id": "a1", "show_until": now + 9}
    L = {"port": 51234, "token": "abcdefghijklmnopqrstuv", "ask_id": "a1", "timer": "t", "alive_until": now + 5}
    s = {"token": "t", "pending": [p], "listener": L}
    st = {"sessions": {"s1": s}, "paused_until": 0}
    url = "http://127.0.0.1:51234/done/abcdefghijklmnopqrstuv"
    want = "💪 32s wall sit?  \x1b]8;;" + url + "\x1b\\✓ done\x1b]8;;\x1b\\"
    assert mod.osc8(url, "✓ done") == "\x1b]8;;" + url + "\x1b\\✓ done\x1b]8;;\x1b\\"
    assert mod.session_ask_text(st, "s1", now) == want
    assert mod.session_ask_text(st, "s1", now + 6) == "💪 32s wall sit?"  # heartbeat expired: plain question
    L["ask_id"] = "other"
    assert mod.session_ask_text(st, "s1", now) == "💪 32s wall sit?"  # another ask's listener: no link
    L["ask_id"] = "a1"
    for bad in ({"port": None}, {"port": True}, {"port": 70000}, {"token": "x"}, {"token": "a\x1b]8;;evil"}):
        L2 = dict(L, **bad)
        s["listener"] = L2
        assert mod.session_ask_text(st, "s1", now) == "💪 32s wall sit?", bad
    s.pop("listener")
    assert mod.session_ask_text(st, "s1", now) == "💪 32s wall sit?"
    assert mod.session_ask_text(st, "s2", now) is None  # another session never shows it


# ---------------------------------------------------------------- after Stop: the grace window

def test_click_after_stop_within_grace_is_accepted(env):
    setup_button(env, ask_grace_s=30)
    say(env)
    port, token, want, line = link(env)
    t_ts = sess(env)["turn"]["ts"]
    assert stop(env).stdout == ""  # nothing said at Stop
    assert declined(env) == [] and done(env) == []
    assert status(env) == line  # the question and its button stay up
    code, body = click(port, token)
    assert code == 200 and f"✅ {want} · " in body
    recs = done(env)
    assert len(recs) == 1 and recs[0]["via"] == "button" and recs[0]["turn_ts"] == t_ts
    assert is_badge(status(env), want)
    assert click(port, token)[1].count("Already logged") == 1 and len(done(env)) == 1
    assert stop(env).stdout == ""
    assert wait_for(lambda: refused(port), 5) and wait_for(lambda: timer_gone(env), 5)
    assert declined(env) == [] and is_badge(status(env), want)  # badge_s from the click


def test_grace_runs_out_declined_silently_and_listener_closed(env):
    setup_button(env, ask_grace_s=0.8)
    say(env)
    port, token, want, line = link(env)
    t_stop = time.time()
    assert stop(env).stdout == ""
    assert status(env) == line
    assert wait_for(lambda: refused(port), 6)
    assert time.time() >= t_stop + 0.7
    d = declined(env)
    assert len(d) == 1 and d[0]["exercise"] == want.split(" ", 1)[1] and done(env) == []
    assert status(env) == ""  # the question is gone, nothing counted
    try:
        click(port, token)
        raise AssertionError("listener still up")
    except urllib.error.URLError:
        pass
    assert wait_for(lambda: timer_gone(env), 5)
    assert "listener" not in sess(env) and "grace" not in sess(env)


def test_grace_zero_declines_at_stop(env):
    setup_button(env, ask_grace_s=0)
    say(env)
    port, token, _, _ = link(env)
    assert stop(env).stdout == ""
    assert len(declined(env)) == 1
    assert wait_for(lambda: refused(port), 5)


def test_next_prompt_declines_the_grace_question_and_closes_it(env):
    setup_button(env, ask_grace_s=60)
    say(env)
    port, token, want, line = link(env)
    stop(env)
    assert status(env) == line
    say(env, "thanks")  # the session's next prompt
    assert len(declined(env)) == 1 and done(env) == []
    assert not status(env).startswith("💪 " + want)
    assert wait_for(lambda: refused(port), 5)
    stop(env)
    assert len(declined(env)) == 1  # once


def test_esc_keeps_the_question_up_for_the_grace(env):
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text(user(_now_stamp(-1), "implement it") + "\n")
    setup_button(env, ask_grace_s=30, stale_s=1000)
    run(env, "hook-prompt", stdin=json.dumps({"session_id": "s1", "transcript_path": str(tr), "cwd": "/tmp",
                                              "prompt": "implement the retry logic and add tests for it"}))
    port, token, want, line = link(env)
    with open(tr, "a") as f:
        f.write(user(_now_stamp(), [{"type": "text", "text": "[Request interrupted by user]"}]) + "\n")
    assert wait_for(lambda: sess(env).get("nudging") is False, 15)
    assert sess(env).get("grace") and status(env) == line  # still asked, button live
    assert click(port, token)[0] == 200
    assert len(done(env)) == 1 and is_badge(status(env), want)
    assert wait_for(lambda: refused(port), 5)
    say(env, "thanks")  # closes the Esc'd turn: nothing declined, nothing doubled
    assert declined(env) == [] and len(done(env)) == 1
    stop(env)


# ---------------------------------------------------------------- /workout done, idle mode, loopback

def test_bare_done_accepts_the_button_ask(env):
    setup_button(env)
    say(env)
    port, token, want, _ = link(env)
    out = run(env, "cli", "done").stdout
    assert out.startswith(f"💪 Logged {want} · today: 1 set")
    assert len(done(env)) == 1 and done(env)[0]["via"] == "manual" and is_badge(status(env), want)
    code, body = click(port, token)  # the button right after: already logged (it lingers), no second row
    assert code == 200 and "Already logged" in body and len(done(env)) == 1
    assert wait_for(lambda: refused(port), 5)
    assert stop(env).stdout == "" and declined(env) == []
    assert run(env, "cli", "done").stdout.startswith("💪 Logged ")  # a bare done later logs the next set


def test_bare_done_in_the_grace_window_accepts_that_ask(env):
    setup_button(env, ask_grace_s=30)
    say(env)
    port, token, want, _ = link(env)
    stop(env)
    out = run(env, "cli", "done").stdout
    assert out.startswith(f"💪 Logged {want}")
    assert len(done(env)) == 1 and is_badge(status(env), want)
    assert wait_for(lambda: refused(port), 5)
    assert declined(env) == []


def test_bare_done_right_after_a_click_says_already_logged(env):
    setup_button(env)
    say(env)
    port, token, want, _ = link(env)
    click(port, token)
    stop(env)
    assert run(env, "cli", "done").stdout.startswith(f"Already logged: {want}")
    assert len(done(env)) == 1


def test_nothing_is_ever_credited_without_a_click(env):
    """In button mode nothing is logged without an answer, however long the question is up."""
    setup_button(env, ask_grace_s=0.5)
    say(env)
    port, _, _, _ = link(env)
    end = time.time() + 1.5
    while time.time() < end:
        status(env)
        time.sleep(0.1)
    assert done(env) == [] and stop(env).stdout == ""
    assert wait_for(lambda: refused(port), 5)
    assert done(env) == [] and len(declined(env)) == 1


def test_listener_is_bound_to_loopback_only(env):
    setup_button(env)
    say(env)
    port, token, _, _ = link(env)
    pid = state(env)["timer_pids"][-1]
    out = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
                         capture_output=True, text=True, timeout=10).stdout
    rows = [l for l in out.splitlines()[1:] if l.strip()]
    assert rows and all(f"127.0.0.1:{port}" in l for l in rows), out
    mod = load_module()
    lst = mod.AskListener("x", "y")
    try:
        assert lst.server.server_address[0] == "127.0.0.1" and lst.port > 0
        assert re.fullmatch(r"[A-Za-z0-9_-]{22}", lst.token)  # token_urlsafe(16)
    finally:
        lst.close()
    assert refused(lst.port)
    stop(env)


def test_token_is_written_only_to_state(env):
    setup_button(env)
    say(env)
    port, token, _, _ = link(env)
    http(port, "/nope")
    click(port, token)
    stop(env)
    assert wait_for(lambda: refused(port), 5)
    for f in env["home"].iterdir():
        if f.is_file() and f.name != "state.json":
            assert token not in f.read_text(errors="replace"), f.name


def test_button_texts_never_name_the_mechanism(env):
    setup_button(env, ask_grace_s=0.5)
    say(env)
    port, token, _, line = link(env)
    seen = [line]
    seen += [http(port, "/x")[1], click(port, token)[1], click(port, token)[1], status(env)]
    r = stop(env)
    seen += [r.stdout, r.stderr]
    mod = load_module()
    seen.append(mod.button_page("Not logged", "This question has closed. You can close this tab."))
    bad = [s for s in seen if FORBIDDEN.search(s)]
    assert bad == []


# ---------------------------------------------------------------- void rows count nowhere

def test_void_rows_count_nowhere(env):
    setup_button(env)
    mod = load_module()
    now = time.time()
    today = dt.date.today()
    voids = [{"ts": now - 60 * i, "session_id": "x", "exercise": "push-ups", "reps": 50, "unit": "reps",
              "status": "void", "via": "idle", "ask_id": f"v{i}", "void_reason": "false"} for i in range(14)]
    voids += [{"ts": now - 86400 * d, "session_id": "x", "exercise": "plank", "reps": 400, "unit": "seconds",
               "status": "void", "via": "idle"} for d in (1, 2, 3)]
    with open(env["home"] / "log.jsonl", "w") as f:
        for r in voids:
            f.write(json.dumps(r) + "\n")
    lg = log(env)
    cfg = mod.normalize_config({"exercises": EXS})
    assert mod.done_records(lg) == [] and mod.today_totals(lg, today) == (0, 0, 0)
    assert mod.streak_days(lg, today) == 0
    stats = mod.ach_stats(lg, cfg, today)
    assert (stats["sets"], stats["reps"], stats["streak"], stats["max_turn"], stats["hold_day"]) == (0, 0, 0, 0, 0)
    assert mod.unlock_achievements({}, lg, cfg) == []
    tips = {t["id"]: t["text"] for t in mod.build_tips(cfg, {}, lg, today)}
    assert tips["wb-streak"] == "No streak yet" and tips["wb-today"].startswith("Today: 0 sets")
    assert tips["wb-week"].startswith("0 reps this week · 0 all-time")
    assert mod.week_summary(lg, cfg, {}, today)["sets"] == 0
    assert "0 reps (so far) across 0 sets" in mod.share_text(lg, cfg, {}, today)
    assert mod.idle_verbs(cfg, {}, lg, now)[0] == "First set of the day is waiting"
    assert mod.current_mult(cfg, lg, today) == 1.0
    assert mod._recent_accept(now) is None
    out = run(env, "cli", "stats").stdout
    assert "Today: 0 sets" in out and "Last 7 days: 0 sets" in out and "All time: 0 sets" in out
    assert run(env, "status", "s1").stdout == ""
    assert "0/" in run(env, "cli", "achievements").stdout.split("\n")[0]
    assert "0 reps" in run(env, "cli", "share", "--text").stdout
    assert run(env, "cli", "predict-stats").returncode == 0
    assert run(env, "cli", "undo").stdout.startswith("Nothing to undo")  # a void row is never "un-done"
    assert run(env, "cli", "done").stdout.startswith("💪 Logged ")  # not "Already logged" for a void row
    assert [r["status"] for r in log(env)].count("void") == len(voids)
    assert "Today: 1 set" in run(env, "cli", "stats").stdout


def test_a_workout_prompt_in_the_grace_window_keeps_the_question(env):
    """`/workout done` is itself a prompt: the hook runs before the command, and must not close the question
    the command is about to answer. Any other prompt closes it (declined)."""
    setup_button(env, ask_grace_s=30)
    say(env)
    port, token, want, line = link(env)
    stop(env)
    say(env, "/workout done")  # the hook for the slash command
    assert declined(env) == [] and status(env) == line and not refused(port)
    out = run(env, "cli", "done").stdout  # then the command itself
    assert out.startswith(f"💪 Logged {want}") and len(done(env)) == 1
    assert is_badge(status(env), want) or status(env) == "💪 1 set today"
    stop(env)
    assert wait_for(lambda: refused(port), 5)
    assert declined(env) == [] and len(done(env)) == 1


def test_a_workout_prompt_then_the_button_still_works(env):
    setup_button(env, ask_grace_s=30)
    say(env)
    port, token, want, line = link(env)
    stop(env)
    say(env, "/workout stats")
    assert click(port, token)[0] == 200 and len(done(env)) == 1
    stop(env)
    say(env, "thanks")
    assert declined(env) == [] and len(done(env)) == 1
    assert wait_for(lambda: refused(port), 5)
    stop(env)


# ---------------------------------------------------------------- v5.1: review fixes

def _kill_timers(ctx):
    import os
    import signal
    pids = list(state(ctx)["timer_pids"])
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert wait_for(lambda: all(not pid_alive(p) for p in pids), 5)


def test_stop_after_the_timer_died_opens_no_grace(env):
    """v5.1 (review): a timer killed just before Stop (its heartbeat still fresh) must not leave a grace
    question with no listener on the status line: Stop declines it at once."""
    setup_button(env, ask_grace_s=30)
    say(env)
    port, token, want, line = link(env)
    _kill_timers(env)
    assert sess(env)["listener"]["alive_until"] > time.time()  # still inside the 10 s heartbeat
    assert stop(env).stdout == ""
    assert "grace" not in sess(env)
    assert len(declined(env)) == 1 and done(env) == []
    assert not status(env).startswith("💪 ")


def test_grace_question_shows_only_while_its_listener_is_alive():
    """v5.1 (review): after Stop the question is drawn only with its live button; once the listener's
    heartbeat lapses (a dead timer) it is gone, not left up for the rest of ask_grace_s."""
    mod = load_module()
    now = 1000.0
    p = {"exercise": "squats", "reps": 17, "unit": "reps", "status": "asked", "id": "a1", "asked_at": now - 5}
    L = {"port": 51234, "token": "abcdefghijklmnopqrstuv", "ask_id": "a1", "timer": "t", "alive_until": now + 5}
    s = {"token": None, "pending": [], "grace": {"ask": p, "until": now + 180, "turn_ts": now - 60}, "listener": L}
    st = {"sessions": {"s1": s}, "paused_until": 0}
    assert mod.session_ask_text(st, "s1", now).startswith("💪 17 squats?  \x1b]8;;")
    assert mod.session_ask_text(st, "s1", now + 6) is None  # heartbeat lapsed: no button-less question
    s.pop("listener")
    assert mod.session_ask_text(st, "s1", now) is None


def test_start_grace_refuses_a_dead_timer_pid():
    mod = load_module()
    dead = subprocess.Popen(["true"])
    dead.wait()  # reaped: the pid is gone
    now = time.time()
    p = {"exercise": "squats", "reps": 17, "unit": "reps", "status": "asked", "id": "a1", "asked_at": now - 5}
    L = {"port": 51234, "token": "abcdefghijklmnopqrstuv", "ask_id": "a1", "timer": "t",
         "alive_until": now + 9, "pid": dead.pid}
    s = {"token": "t", "pending": [p], "listener": L, "turn": {"ts": now - 60}}
    cfg = {"ask_grace_s": 180}
    assert mod.start_grace({}, cfg, s, now, "s1") is False and "grace" not in s
    import os
    L["pid"] = os.getpid()
    assert mod.start_grace({}, cfg, s, now, "s1") is True and s["grace"]["ask"] is p


def test_start_grace_settles_an_older_grace_question(env):
    """v5.1 (review): a new grace never overwrites an older one silently: the older is declined."""
    mod = load_module()
    now = time.time()
    old = {"exercise": "push-ups", "reps": 11, "unit": "reps", "status": "asked", "id": "old1", "asked_at": now - 99}
    new = {"exercise": "squats", "reps": 17, "unit": "reps", "status": "asked", "id": "new1", "asked_at": now - 5}
    import os
    L = {"port": 51234, "token": "abcdefghijklmnopqrstuv", "ask_id": "new1", "timer": "t",
         "alive_until": now + 9, "pid": os.getpid()}
    s = {"token": "t", "pending": [new], "listener": L, "turn": {"ts": now - 60},
         "grace": {"ask": old, "until": now + 50, "turn_ts": now - 200}}
    assert mod.start_grace({}, {"ask_grace_s": 180}, s, now, "s1") is True
    assert s["grace"]["ask"] is new
    d = declined(env)
    assert [r.get("ask_id") for r in d] == ["old1"] and done(env) == []


def test_no_second_ask_while_a_grace_question_is_up(env):
    """v5.1 (review): with cooldown_s below ask_grace_s, a /workout prompt in the grace window starts a turn
    whose timer must not ask again while the carried question is still up (one question at a time)."""
    setup_button(env, cooldown_s=0, ask_grace_s=4)
    say(env)
    port, token, want, line = link(env)
    first = sess(env)["listener"]["ask_id"]
    stop(env)
    say(env, "/workout stats")
    time.sleep(2.0)  # well past delay_s: the old code had asked again by now
    assert asked(env) == [] and sess(env)["listener"]["ask_id"] == first and status(env) == line
    stop(env)
    say(env, "thanks")
    assert [r.get("ask_id") for r in declined(env)][:1] == [first] and done(env) == []


def test_data_files_are_owner_only(env):
    """v5.1 (review): state.json holds the button's token; every file the add-on writes is 0600."""
    import os
    import stat
    setup_button(env)
    say(env)
    link(env)
    stop(env)
    for name in ("state.json", "log.jsonl"):
        f = env["home"] / name
        if f.exists():
            assert stat.S_IMODE(os.stat(f).st_mode) == 0o600, name
    assert stat.S_IMODE(os.stat(env["home"] / "state.json").st_mode) == 0o600
