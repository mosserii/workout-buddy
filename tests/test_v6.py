"""Workout Buddy v6: the mod. ui.json (the mod's cheap feed), `cli done <ask_id>` / `cli decline [ask_id]`
(the band's [1] done / [2] skip), and "button": "mod" (no listener; v6.1: the status line shows neither the
question nor the badge where the band does; a socket-less keeper keeps the grace window working)."""
import json
import os
import stat
import time

from test_v4 import asked, declined, done, is_badge, say, status, task  # noqa: F401
from test_v5 import click, link, refused, sess, setup_button, timer_gone
from test_workout import env, load_module, log, run, state, stop, wait_for, write_cfg  # noqa: F401
import pytest

pytestmark = pytest.mark.legacy_modes  # written against the older modes (see conftest.py)


def beat(ctx, sid="s1", age=0.0):
    """What the mod does while its band runs: touch <home>/mod-seen/<sid> (age: that many seconds ago)."""
    d = ctx["home"] / "mod-seen"
    d.mkdir(parents=True, exist_ok=True)
    f = d / sid
    f.write_text(str(time.time()))
    t = time.time() - age
    os.utime(f, (t, t))
    return f


def setup_mod(ctx, beat_sid="s1", **over):
    setup_button(ctx, button="mod", **over)
    if beat_sid:
        beat(ctx, beat_sid)


def ui(ctx):
    p = ctx["home"] / "ui.json"
    return json.loads(p.read_text()) if p.exists() else None


def ui_s(ctx, sid="s1"):
    u = ui(ctx)
    return ((u or {}).get("sessions") or {}).get(sid) or {}


def wait_ask(ctx, sid="s1"):
    assert wait_for(lambda: asked(ctx, sid) and ui_s(ctx, sid).get("mode") == "ask", 5)
    return asked(ctx, sid)[0]


def cli(ctx, *args):
    return run(ctx, "cli", *args)


# ---------------------------------------------------------------- config

def test_button_config_default_mod_and_link_accepted():
    mod = load_module()
    assert mod.normalize_config({})["button"] == "mod"  # public build: the band is the button
    assert mod.normalize_config({"button": "link"})["button"] == "link"
    assert mod.normalize_config({"button": "bogus"})["button"] == "mod"
    assert mod.normalize_config({"button": True})["button"] == "mod"


# ---------------------------------------------------------------- ui.json

def test_mod_mode_ask_shows_in_ui_json_and_bare_status(env):
    setup_mod(env)
    say(env)
    p = wait_ask(env)
    want = task(p)
    u = ui(env)
    e = u["sessions"]["s1"]
    assert u["version"] == 1 and isinstance(u["updated_at"], float)
    assert e["mode"] == "ask" and e["ask_id"] == p["id"] and e["text"] == want
    assert e["exercise"] == p["exercise"] and e["reps"] == p["reps"] and e["unit"] == p["unit"]
    assert e["until_ts"] > time.time() - 1
    assert status(env) == ""  # v6.1: the band shows the question; nothing in the status line (no sets yet)
    L = sess(env)["listener"]
    assert L["kind"] == "mod" and "port" not in L  # the keeper: nothing listens
    raw = (env["home"] / "ui.json").read_text()
    assert L["token"] not in raw and "token" not in raw and "port" not in raw
    assert stat.S_IMODE(os.stat(env["home"] / "ui.json").st_mode) == 0o600


def test_link_mode_also_writes_ui_json_without_secrets(env):
    setup_button(env)
    say(env)
    port, token, want, _ = link(env)
    assert wait_for(lambda: ui_s(env).get("mode") == "ask", 5)
    raw = (env["home"] / "ui.json").read_text()
    assert token not in raw and str(port) not in raw.replace(str(ui(env)["updated_at"]), "")
    assert ui_s(env)["text"] == want
    click(port, token)
    assert wait_for(lambda: ui_s(env).get("mode") == "badge", 5)


def test_ui_json_unchanged_state_is_not_rewritten(env):
    mod_env = dict(env)
    setup_mod(mod_env)
    m = load_module()
    os.environ["WORKOUT_HOME"] = str(env["home"])
    st = m.empty_state()
    assert m.write_ui(st, time.time()) is True
    assert m.write_ui(st, time.time()) is False  # same content: no write
    assert ui(env)["sessions"] == {}


def test_ui_entry_unit_views():
    m = load_module()
    now = 1000.0
    p = {"exercise": "squats", "reps": 20, "unit": "reps", "status": "asked", "id": "ab12cd34", "show_until": now + 9}
    s = {"token": "t", "pending": [p]}
    st = {"sessions": {"s1": s, "s2": {"token": None, "pending": []}}, "paused_until": 0}
    e = m.ui_entry(st, "s1", now)
    assert e == {"mode": "ask", "ask_id": "ab12cd34", "exercise": "squats", "reps": 20, "unit": "reps",
                 "text": "20 squats", "cheer": "", "until_ts": now + 9}
    assert m.ui_entry(st, "s2", now)["mode"] == "none"
    assert m.ui_entry(st, "s1", now + 10)["mode"] == "none"  # heartbeat lapsed
    st["paused_until"] = now + 60
    assert m.ui_entry(st, "s1", now)["mode"] == "none"
    st["paused_until"] = 0
    # the badge (old record without cheer / set: parsed from the text)
    p["status"] = "accepted"
    s["badge"] = {"text": "✅ 20 squats · kudos! 💪", "ask_id": "ab12cd34", "accepted_at": now, "token": "t"}
    e = m.ui_entry(st, "s1", now)
    assert e["mode"] == "badge" and e["text"] == "20 squats" and e["cheer"] == "kudos! 💪"
    assert e["until_ts"] == now + m.BADGE_S
    # seconds
    q = {"exercise": "plank", "reps": 45, "unit": "seconds", "status": "asked", "id": "ffffffff", "show_until": now + 5}
    st2 = {"sessions": {"s1": {"token": "t", "pending": [q]}}, "paused_until": 0}
    assert m.ui_entry(st2, "s1", now)["text"] == "45s plank"


def test_grace_question_in_mod_mode_shows_while_keeper_alive():
    m = load_module()
    now = 1000.0
    p = {"exercise": "squats", "reps": 17, "unit": "reps", "status": "asked", "id": "a1", "asked_at": now - 5}
    L = {"kind": "mod", "token": "x" * 22, "ask_id": "a1", "timer": "t", "alive_until": now + 5}
    s = {"token": None, "pending": [], "grace": {"ask": p, "until": now + 180, "turn_ts": now - 60}, "listener": L}
    st = {"sessions": {"s1": s}, "paused_until": 0}
    assert m.session_ask_text(st, "s1", now) == "💪 17 squats?"
    e = m.ui_entry(st, "s1", now)
    assert e["mode"] == "ask" and e["until_ts"] == now + 5  # min(grace end, keeper heartbeat)
    assert m.session_ask_text(st, "s1", now + 6) is None  # keeper lapsed (dead timer)
    L["ask_id"] = "other"
    assert m.session_ask_text(st, "s1", now) is None


# ---------------------------------------------------------------- cli done <ask_id>

def test_done_by_ask_id_accepts_exactly_that_ask(env):
    setup_mod(env)
    say(env)
    p = wait_ask(env)
    r = cli(env, "done", p["id"])
    assert r.returncode == 0, r.stdout
    assert r.stdout.startswith(f"💪 Logged {task(p)} · today: ") and r.stdout.count("\n") == 1
    recs = done(env)
    assert len(recs) == 1 and recs[0]["via"] == "mod" and recs[0]["ask_id"] == p["id"]
    assert status(env) == "💪 1 set today"  # v6.1: the badge is the band's; the daily segment stays
    e = ui_s(env)
    assert e["mode"] == "badge" and e["ask_id"] == p["id"] and e["text"] == task(p)
    assert e["cheer"] in load_module().CHEERS
    again = cli(env, "done", p["id"])  # a repeat press
    assert again.returncode == 0 and again.stdout.strip() == f"Already logged: {task(p)}."
    assert len(done(env)) == 1
    msg = json.loads(stop(env).stdout)["systemMessage"]  # announced at Stop, as a click is
    assert msg.startswith(f"Logged {task(p)}")


def test_done_with_a_closed_or_unknown_ask_id_is_a_noop(env):
    setup_mod(env)
    say(env)
    p = wait_ask(env)
    r = cli(env, "done", "deadbeef")
    assert r.returncode == 1 and r.stdout.strip() == "That question has closed — nothing logged."
    assert done(env) == [] and asked(env)  # the real ask is untouched
    assert cli(env, "done", p["id"]).returncode == 0


def test_done_by_id_while_paused_logs_nothing(env):
    setup_mod(env)
    say(env)
    p = wait_ask(env)
    cli(env, "pause", "5")
    r = cli(env, "done", p["id"])
    assert r.returncode == 1 and "paused" in r.stdout and done(env) == []


def test_ask_id_argument_parsing(env):
    setup_mod(env)
    m = load_module()
    os.environ["WORKOUT_HOME"] = str(env["home"])
    assert m.ask_id_arg(["ab12cd34"]) == "ab12cd34"
    assert m.ask_id_arg(["--ask", "12345678"]) == "12345678"
    assert m.ask_id_arg(["12345678"]) is None  # all digits, unknown: reps, as before (a human's bare word)
    assert m.ask_id_arg(["--ask", "zz"]) is None
    assert m.ask_id_arg(["20"]) is None
    assert m.ask_id_arg(["squats", "20"]) is None
    assert m.ask_id_arg(["AB12CD34"]) is None
    assert m.ask_id_arg([]) is None


def test_plain_done_still_works(env):
    setup_mod(env)
    r = cli(env, "done", "squats", "15")
    assert r.returncode == 0 and "Logged 15 squats" in r.stdout


# ---------------------------------------------------------------- cli decline

def test_decline_by_id_declines_rotates_and_is_silent(env):
    setup_mod(env)
    say(env)
    p = wait_ask(env)
    n_notes = len([l for l in (env["notify"].read_text().splitlines() if env["notify"].exists() else [])])
    r = cli(env, "decline", p["id"])
    assert r.returncode == 0 and r.stdout.strip() == f"Skipped {task(p)} — next one will be different."
    d = declined(env)
    assert len(d) == 1 and d[0]["ask_id"] == p["id"] and d[0]["exercise"] == p["exercise"]
    st = state(env)
    assert st["next_set"]["exercise"] != p["exercise"]  # rotated away
    assert st["ask_hist"][-1]["status"] == "declined"
    assert status(env) == "" and ui_s(env)["mode"] == "none"
    assert "listener" not in sess(env)
    after = len([l for l in (env["notify"].read_text().splitlines() if env["notify"].exists() else [])])
    assert after == n_notes  # no banner
    assert stop(env).stdout == "" and len(declined(env)) == 1  # Stop does not decline it again
    assert done(env) == []
    assert wait_for(lambda: timer_gone(env), 6)


def test_decline_twice_and_nothing_open(env):
    setup_mod(env)
    r = cli(env, "decline")
    assert r.returncode == 1 and r.stdout.strip() == "No open question to skip."
    say(env)
    p = wait_ask(env)
    assert cli(env, "decline").returncode == 0  # bare: the newest open ask
    r = cli(env, "decline", p["id"])
    assert r.returncode == 1 and "closed" in r.stdout and len(declined(env)) == 1
    assert cli(env, "decline", "squats").returncode == 1


def test_no_second_ask_after_a_decline_in_the_same_turn(env):
    setup_mod(env, cooldown_s=0, repeat_s=0.2)
    say(env)
    p = wait_ask(env)
    cli(env, "decline", p["id"])
    time.sleep(1.0)
    assert asked(env) == [] and len([q for q in sess(env)["pending"] if q.get("status")]) == 1


def test_decline_in_link_mode_closes_the_listener(env):
    setup_button(env)
    say(env)
    port, token, want, _ = link(env)
    p = asked(env)[0]
    assert cli(env, "decline", p["id"]).returncode == 0
    assert wait_for(lambda: refused(port), 5)
    assert click_closed(port, token)
    assert done(env) == [] and len(declined(env)) == 1


def click_closed(port, token):
    try:
        code, _ = click(port, token)
        return code == 410
    except Exception:
        return True


# ---------------------------------------------------------------- mod mode after Stop: the grace window

def test_mod_mode_grace_keeps_the_question_and_done_works(env):
    setup_mod(env, ask_grace_s=30)
    say(env)
    p = wait_ask(env)
    t_ts = sess(env)["turn"]["ts"]
    assert stop(env).stdout == ""
    assert status(env) == "" and ui_s(env)["mode"] == "ask"  # v6.1: the grace question is the band's
    assert declined(env) == [] and done(env) == []
    r = cli(env, "done", p["id"])
    assert r.returncode == 0
    recs = done(env)
    assert len(recs) == 1 and recs[0]["via"] == "mod" and recs[0]["turn_ts"] == t_ts
    assert status(env) == "💪 1 set today" and ui_s(env)["mode"] == "badge"
    assert wait_for(lambda: timer_gone(env), 6)
    assert "listener" not in sess(env)


def test_mod_mode_grace_runs_out_declined(env):
    setup_mod(env, ask_grace_s=0.8)
    say(env)
    p = wait_ask(env)
    stop(env)
    assert ui_s(env)["mode"] == "ask"
    assert wait_for(lambda: len(declined(env)) == 1, 6)
    assert wait_for(lambda: ui_s(env).get("mode") == "none", 3)
    assert status(env) == "" and done(env) == []
    assert wait_for(lambda: timer_gone(env), 6)


def test_mod_mode_decline_in_grace(env):
    setup_mod(env, ask_grace_s=30)
    say(env)
    p = wait_ask(env)
    stop(env)
    r = cli(env, "decline", p["id"])
    assert r.returncode == 0 and len(declined(env)) == 1
    assert "grace" not in sess(env) and status(env) == "" and ui_s(env)["mode"] == "none"
    assert wait_for(lambda: timer_gone(env), 6)


def test_mod_mode_never_listens(env):
    setup_mod(env)
    say(env)
    wait_ask(env)
    L = sess(env)["listener"]
    assert set(L) == {"kind", "token", "ask_id", "timer", "alive_until", "pid"}
    import subprocess
    out = subprocess.run(["lsof", "-a", "-nP", "-iTCP", "-sTCP:LISTEN", "-p", str(L["pid"])],
                         capture_output=True, text=True).stdout
    assert out.strip() == ""


# ---------------------------------------------------------------- fixer: the mod's argv form and the beat

def test_unknown_all_digit_id_with_ask_flag_logs_nothing(env):
    """The mod sends `cli done --ask <id>`: an all-digit id that has dropped off the record must never be
    read as a rep count for whatever ask is open."""
    setup_mod(env)
    say(env)
    p = wait_ask(env)
    for action in ("done", "decline"):
        r = cli(env, action, "--ask", "12345670")
        assert r.returncode == 1, (action, r.stdout)
        assert "closed" in r.stdout and r.stdout.count("\n") == 1
    assert done(env) == [] and declined(env) == [] and asked(env)[0]["id"] == p["id"]
    assert not (env["home"] / "log.jsonl").exists() or "12345670" not in (env["home"] / "log.jsonl").read_text()


def test_ask_flag_with_a_bad_id_does_nothing(env):
    setup_mod(env)
    say(env)
    wait_ask(env)
    for action in ("done", "decline"):
        for bad in (["--ask", "20"], ["--ask", "squats"], ["--ask"]):
            r = cli(env, action, *bad)
            assert r.returncode == 1, (action, bad, r.stdout)
    assert done(env) == [] and declined(env) == []


def test_done_and_decline_by_ask_flag(env):
    setup_mod(env)
    say(env)
    p = wait_ask(env)
    r = cli(env, "done", "--ask", p["id"])
    assert r.returncode == 0 and len(done(env)) == 1 and done(env)[0]["ask_id"] == p["id"]


def test_mod_mode_without_a_beat_keeps_the_link(env):
    """button=mod in the shared config, but this session's profile has no mod loaded (no beat): v5 link."""
    setup_mod(env, beat_sid=None)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    assert wait_for(lambda: isinstance(sess(env).get("listener"), dict) and sess(env)["listener"].get("port"), 5)
    L = sess(env)["listener"]
    assert L.get("kind") != "mod" and L["port"]
    link(env)  # the status line carries the OSC 8 link


def test_mod_mode_stale_beat_or_other_session_keeps_the_link(env):
    setup_mod(env, beat_sid="other")
    beat(env, "s1", age=600)
    say(env)
    assert wait_for(lambda: isinstance(sess(env).get("listener"), dict) and sess(env)["listener"].get("port"), 5)
    assert sess(env)["listener"].get("kind") != "mod"


def test_mod_seen_unit(env):
    m = load_module()  # WORKOUT_HOME is the sandbox (the env fixture sets it)
    assert m.mod_seen("s1") is False  # no dir yet
    beat(env, "s1")
    assert m.mod_seen("s1") is True
    assert m.mod_seen("s2") is False
    assert m.mod_seen("../s1") is False and m.mod_seen("") is False and m.mod_seen(None) is False
    old = beat(env, "gone", age=2 * 86400)
    beat(env, "s3", age=m.MOD_SEEN_S + 5)
    assert m.mod_seen("s3") is False
    assert not old.exists()  # pruned


# ---------------------------------------------------------------- v6.1: the status line leaves the ask to the band

def test_band_draws_unit(env):
    m = load_module()
    now = time.time()
    beat(env, "s1")
    assert m.band_draws({"button": "mod"}, "s1", now) is True
    assert m.band_draws({"button": "link"}, "s1", now) is False  # link mode: the status line keeps it
    assert m.band_draws({}, "s1", now) is False
    assert m.band_draws({"button": "mod"}, "s2", now) is False  # no beat for this session
    assert m.band_draws({"button": "mod"}, None, now) is False
    beat(env, "s3", age=m.MOD_SEEN_S + 5)
    assert m.band_draws({"button": "mod"}, "s3", now) is False  # stale beat: the mod is not drawing


def test_mod_mode_status_line_keeps_the_daily_segment_not_the_ask(env):
    setup_mod(env)
    assert cli(env, "done", "squats", "15").returncode == 0  # a set earlier today
    say(env)
    p = wait_ask(env)
    line = status(env)
    assert line == "💪 1 set today", repr(line)
    assert "?" not in line and "✅" not in line and "\x1b]8" not in line
    assert cli(env, "done", "--ask", p["id"]).returncode == 0
    assert ui_s(env)["mode"] == "badge"
    assert status(env) == "💪 2 sets today"  # no "✅ … · cheer" badge


def test_mod_mode_other_session_without_a_beat_keeps_question_and_badge(env):
    """s1's mod beats, s2's does not (another config dir): s2's status line keeps the v5.2 link and badge."""
    setup_mod(env, beat_sid="s1")
    say(env, sid="s2")
    port, token, want, line = link(env, "s2")
    assert line.startswith(f"💪 {want}?")
    click(port, token)
    assert wait_for(lambda: is_badge(status(env, "s2"), want), 5)


def test_link_mode_with_a_beat_keeps_question_and_badge(env):
    """"button": "link" ignores the beat: the status line is v5.2 unchanged."""
    setup_button(env)
    beat(env, "s1")
    say(env)
    port, token, want, line = link(env)
    assert line.startswith(f"💪 {want}?")
    click(port, token)
    assert wait_for(lambda: is_badge(status(env), want), 5)


def test_mod_mode_stale_beat_status_line_shows_the_question(env):
    setup_mod(env, beat_sid=None)
    beat(env, "s1", age=600)
    say(env)
    port, token, want, line = link(env)
    assert line.startswith(f"💪 {want}?")
