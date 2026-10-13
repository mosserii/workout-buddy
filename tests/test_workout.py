import datetime as dt
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.legacy_modes  # written against the older modes (see conftest.py)

SCRIPT = Path(__file__).resolve().parent.parent / "plugins" / "workout-buddy" / "scripts" / "workout.py"
PY = sys.executable

# "predict": false keeps the v1/v2 timing tests on the flat delay_s; the predictor has its own tests
# (test_v3.py). WORKOUT_MIN_DELAY_S=0 (sandbox only) lifts the 10 s floor so these run in seconds.
FAST = {"delay_s": 0.3, "repeat_s": 10, "cooldown_s": 0, "stale_s": 120, "max_lifetime_s": 3,
        "sound": True, "sound_name": "Glass", "say": False, "quiet_hours": None, "predict": False,
        # v4: the v1-v3.2 tests run on the v3.2 behavior they were written for: every due set credited at
        # Stop ("ask_mode": "auto") and the spinner on, through the v3.2 key "spinner_task": true (which the
        # v4 back-compat maps onto "spinner": "progress"). The v4 defaults have their own tests (test_v4.py).
        "ask_mode": "auto", "spinner_task": True,
        # v7: every test before v7 runs on the timing it was written for, "classic" (the predictor's due
        # time, repeat_s, cooldown_s); the waiting meter (the v7 default) has its own tests (test_v7.py)
        "timing": "classic"}


# The public build's defaults differ from the personal build these tests were written against (banners,
# sound, tab title, status line and the predictor off; the band's button, "mod"). The pre-public tests run
# on the values they were written for; the public defaults have their own tests (test_public.py).
LEGACY_DEFAULTS = {"sound": True, "notification": True, "tab_title": True, "statusline": True, "button": "link",
                   "predict": True, "close_tab": True}


def load_module():
    spec = importlib.util.spec_from_file_location("workout_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A stand-in for ~/.claude/settings.json: tests write the spinner task here, never to the real file.
FAKE_SETTINGS = {"env": {"A": "1"}, "permissions": {"allow": ["Bash(ls:*)"], "defaultMode": "auto"},
                 "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x hook-stop"}]}]},
                 "spinnerTipsEnabled": True, "spinnerVerbs": {"mode": "replace", "verbs": ["Repping"]},
                 "theme": "dark", "unicode": "שלום ✓"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "wh"
    home.mkdir()
    notify = tmp_path / "notify.log"
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(FAKE_SETTINGS, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))  # nothing below may reach the real ~ (or ~/.claude)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_home / ".claude"))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setenv("WORKOUT_TEST", "1")  # the test-only knobs (legacy modes: see conftest.py)
    monkeypatch.setenv("WORKOUT_HOME", str(home))
    monkeypatch.setenv("WORKOUT_NOTIFY_LOG", str(notify))
    monkeypatch.setenv("WORKOUT_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("WORKOUT_MIN_DELAY_S", "0")
    e = dict(os.environ)
    for k in ("SSH_CONNECTION", "SSH_TTY"):  # v4: a timer over ssh never accepts by idle time
        e.pop(k, None)
    ctx = {"home": home, "notify": notify, "env": e, "tmp": tmp_path, "settings": settings}
    yield ctx
    # kill any timers we started
    try:
        st = json.loads((home / "state.json").read_text())
        for pid in st.get("timer_pids", []):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass


def write_cfg(ctx, **over):
    cfg = dict(LEGACY_DEFAULTS)
    cfg.update(FAST)
    cfg.update(over)
    (ctx["home"] / "config.json").write_text(json.dumps(cfg))


def run(ctx, *args, stdin=""):
    return subprocess.run([PY, str(SCRIPT), *args], input=stdin, capture_output=True, text=True,
                          env=ctx["env"], timeout=20)


def prompt(ctx, sid="s1", transcript=""):
    return run(ctx, "hook-prompt", stdin=json.dumps(
        {"session_id": sid, "transcript_path": transcript, "cwd": "/tmp", "prompt": "hi"}))


def stop(ctx, sid="s1"):
    return run(ctx, "hook-stop", stdin=json.dumps({"session_id": sid, "stop_hook_active": False}))


def notes(ctx, kind=None):
    p = ctx["notify"]
    if not p.exists():
        return []
    out = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return [n for n in out if kind is None or n["kind"] == kind]


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def state(ctx):
    return json.loads((ctx["home"] / "state.json").read_text())


def log(ctx):
    p = ctx["home"] / "log.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# ---------------------------------------------------------------- hooks

def test_hook_prompt_silent_and_fast(env):
    write_cfg(env)
    t = time.time()
    r = prompt(env)
    elapsed = time.time() - t
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == ""
    assert elapsed < 1.0
    s = state(env)["sessions"]["s1"]
    assert s["token"] and s["pending"] == []
    stop(env)


def test_first_run_creates_default_config(env):
    r = prompt(env)
    assert r.stdout == ""
    cfg = json.loads((env["home"] / "config.json").read_text())
    mod = load_module()
    assert list(cfg) == list(mod.PUBLIC_CONFIG_KEYS) and len(cfg["exercises"]) == 8  # the README's keys only
    assert mod.load_config()["delay_s"] == 60
    stop(env)


def test_timer_fires_after_delay_then_stop_logs(env):
    write_cfg(env)
    prompt(env)
    assert not notes(env, "fire")
    assert wait_for(lambda: len(notes(env, "fire")) == 1)
    n = notes(env, "fire")[0]
    assert n["title"] == "Workout Buddy" and "sound name \"Glass\"" in n["script"]
    st = state(env)
    assert len(st["sessions"]["s1"]["pending"]) == 1
    r = stop(env)
    assert r.returncode == 0
    msg = json.loads(r.stdout)
    assert set(msg) <= {"systemMessage", "terminalSequence"}  # v2: tab title rides along
    assert msg["systemMessage"].startswith("Logged ")
    assert "today 1 set" in msg["systemMessage"] and "/workout undo" in msg["systemMessage"]
    assert "decision" not in r.stdout
    entries = log(env)
    assert len(entries) == 1 and entries[0]["status"] == "done"
    assert entries[0]["exercise"] == st["sessions"]["s1"]["pending"][0]["exercise"]
    done = notes(env, "done")
    assert len(done) == 1 and done[0]["subtitle"] == "Claude's done" and done[0]["body"].startswith("Logged ")
    assert state(env)["sessions"]["s1"]["token"] is None


def test_stop_before_delay_no_notification(env):
    write_cfg(env, delay_s=0.6)
    prompt(env)
    r = stop(env)
    assert r.stdout == ""
    time.sleep(1.0)
    assert notes(env) == []
    assert log(env) == []


def test_new_prompt_supersedes_old_timer(env):
    write_cfg(env, delay_s=0.5)
    prompt(env)
    first_pid = state(env)["timer_pids"][-1]
    prompt(env)
    assert wait_for(lambda: len(notes(env, "fire")) >= 1)
    time.sleep(0.5)
    assert len(notes(env, "fire")) == 1
    assert wait_for(lambda: not pid_alive(first_pid), timeout=3)
    stop(env)


def test_global_cooldown_across_sessions(env):
    write_cfg(env, cooldown_s=60)
    prompt(env, "a")
    prompt(env, "b")
    assert wait_for(lambda: len(notes(env, "fire")) == 1)
    time.sleep(0.8)
    assert len(notes(env, "fire")) == 1
    st = state(env)
    pend = [len(st["sessions"][s]["pending"]) for s in ("a", "b")]
    assert sorted(pend) == [0, 1]
    stop(env, "a")
    stop(env, "b")


def test_cooldown_elapses_then_fires(env):
    write_cfg(env, cooldown_s=0.6, repeat_s=0.1)
    prompt(env)
    assert wait_for(lambda: len(notes(env, "fire")) >= 2, timeout=4)
    f = notes(env, "fire")
    assert f[1]["ts"] - f[0]["ts"] >= 0.55
    stop(env)


def test_next_set_stable_within_turn_and_never_repeats_across_stops(env):
    # The next set is decided ahead: repeat nudges in one turn ask for the same set (the one on
    # screen); after Stop a new one is picked, never the exercise just logged.
    write_cfg(env, repeat_s=0.05, max_lifetime_s=2)
    logged = []
    for _ in range(4):
        prompt(env)
        nxt = state(env)["next_set"]
        assert wait_for(lambda: len(notes(env, "fire")) >= 2, timeout=4)
        bodies = {n["body"] for n in notes(env, "fire")}
        want = f"{nxt['reps']}{'s' if nxt['unit'] == 'seconds' else ''} {nxt['exercise']}"
        assert bodies == {want}
        stop(env)
        assert wait_for(lambda: not pid_alive(state(env)["timer_pids"][-1]), timeout=4)
        logged.append(nxt["exercise"])
        env["notify"].unlink()
    assert {e["exercise"] for e in log(env)} == set(logged)
    assert all(a != b for a, b in zip(logged, logged[1:]))


def test_stale_transcript_no_fire(env):
    write_cfg(env)
    tr = env["tmp"] / "t.jsonl"
    tr.write_text("{}\n")
    old = time.time() - 1000
    os.utime(tr, (old, old))
    prompt(env, transcript=str(tr))
    assert wait_for(lambda: state(env)["sessions"]["s1"].get("idle") is True, timeout=3)
    time.sleep(0.3)
    assert notes(env) == []
    assert run(env, "status", "s1").stdout == ""  # idle: no "NOW" (and v3: never "next:")
    stop(env)


def test_fresh_transcript_fires(env):
    write_cfg(env)
    tr = env["tmp"] / "t.jsonl"
    tr.write_text("{}\n")
    prompt(env, transcript=str(tr))
    assert wait_for(lambda: len(notes(env, "fire")) == 1)
    stop(env)


def test_pause_and_resume(env):
    write_cfg(env)
    r = run(env, "cli", "pause", "5")
    assert "Paused" in r.stdout
    assert run(env, "status").stdout == "💤 paused"
    prompt(env)
    time.sleep(0.8)
    assert notes(env) == []
    stop(env)
    r = run(env, "cli", "resume")
    assert "Resumed" in r.stdout
    assert run(env, "status").stdout == ""  # v3: no "next:" before a set is due
    prompt(env)
    assert wait_for(lambda: len(notes(env, "fire")) == 1)
    stop(env)


def test_pause_day_until_midnight(env):
    write_cfg(env)
    run(env, "cli", "pause", "day")
    pu = state(env)["paused_until"]
    midnight = dt.datetime.combine(dt.date.today() + dt.timedelta(days=1), dt.time()).timestamp()
    assert abs(pu - midnight) < 1


def test_quiet_hours_blocks(env):
    now = dt.datetime.now()
    a = (now - dt.timedelta(hours=1)).strftime("%H:%M")
    b = (now + dt.timedelta(hours=1)).strftime("%H:%M")
    write_cfg(env, quiet_hours=[a, b])
    prompt(env)
    time.sleep(0.9)
    assert notes(env) == []
    stop(env)


def test_quiet_hours_function():
    mod = load_module()
    cfg = {"quiet_hours": ["22:00", "08:00"]}
    def at(h, m):
        return dt.datetime(2026, 1, 5, h, m).timestamp()
    assert mod.in_quiet_hours(cfg, at(23, 0))
    assert mod.in_quiet_hours(cfg, at(3, 0))
    assert not mod.in_quiet_hours(cfg, at(8, 0))
    assert not mod.in_quiet_hours(cfg, at(12, 0))
    assert mod.in_quiet_hours({"quiet_hours": ["09:00", "17:00"]}, at(12, 0))
    assert not mod.in_quiet_hours({"quiet_hours": None}, at(12, 0))


# ---------------------------------------------------------------- robustness

def test_malformed_config_does_not_crash(env):
    (env["home"] / "config.json").write_text("{not json")
    r = prompt(env)
    assert r.returncode == 0 and r.stdout == ""
    r = run(env, "cli", "config")
    assert r.returncode == 0 and "invalid" in r.stdout and "delay_s = 60" in r.stdout
    assert (env["home"] / "errors.log").exists()
    stop(env)


def test_partial_config_falls_back(env):
    (env["home"] / "config.json").write_text(json.dumps(
        {"delay_s": "soon", "cooldown_s": 5, "exercises": [{"name": "burpees", "min": 5, "max": 3}], "x": 1}))
    mod = load_module()
    cfg = mod.load_config()
    assert cfg["delay_s"] == 60 and cfg["cooldown_s"] == 5
    assert len(cfg["exercises"]) == 8


def test_malformed_state_does_not_crash(env):
    write_cfg(env)
    (env["home"] / "state.json").write_text("[[[garbage")
    assert run(env, "status").returncode == 0
    r = prompt(env)
    assert r.returncode == 0 and r.stdout == ""
    assert "s1" in state(env)["sessions"]
    r = stop(env)
    assert r.returncode == 0


def test_hooks_with_garbage_stdin(env):
    write_cfg(env)
    r = run(env, "hook-prompt", stdin="not json")
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    r = run(env, "hook-stop", stdin="")
    assert r.returncode == 0 and r.stdout == ""


def test_stale_sessions_pruned(env):
    mod = load_module()
    st = mod.empty_state()
    st["sessions"]["old"] = {"token": "x", "updated_at": time.time() - 7 * 3600}
    st["sessions"]["new"] = {"token": "y", "updated_at": time.time()}
    mod.prune(st, time.time())
    assert list(st["sessions"]) == ["new"]


# ---------------------------------------------------------------- cli / status

def seed_log(ctx, days_ago_list, exercise="push-ups", reps=10):
    with open(ctx["home"] / "log.jsonl", "a") as f:
        for d in days_ago_list:
            ts = (dt.datetime.now() - dt.timedelta(days=d)).timestamp()
            f.write(json.dumps({"ts": ts, "session_id": "x", "exercise": exercise, "reps": reps,
                                "unit": "reps", "status": "done"}) + "\n")


def test_undo_takes_back_last(env):
    write_cfg(env)
    run(env, "cli", "done", "push-ups", "12")
    run(env, "cli", "done", "squats", "20")
    r = run(env, "cli", "undo")
    assert "squats" in r.stdout
    entries = log(env)
    assert [e["status"] for e in entries] == ["done", "skipped"]
    run(env, "cli", "undo")
    r = run(env, "cli", "undo")
    assert "Nothing to undo" in r.stdout


def test_done_parsing(env):
    write_cfg(env)
    run(env, "cli", "done", "jumping", "jacks", "30")
    run(env, "cli", "done plank 45")  # $ARGUMENTS as one string
    run(env, "cli", "done")
    e = log(env)
    assert (e[0]["exercise"], e[0]["reps"]) == ("jumping jacks", 30)
    assert (e[1]["exercise"], e[1]["reps"], e[1]["unit"]) == ("plank", 45, "seconds")
    assert e[2]["reps"] > 0


def test_status_states(env):
    write_cfg(env)
    assert run(env, "status").stdout == ""
    seed_log(env, [0, 0, 0])
    assert run(env, "status").stdout == "💪 3 sets today"
    seed_log(env, [1, 2, 3])
    assert run(env, "status").stdout == "💪 3 sets today 🔥4d"
    prompt(env)
    assert wait_for(lambda: run(env, "status").stdout.startswith("🏋 NOW: "))
    out = run(env, "status", "s1").stdout
    assert out.startswith("🏋 NOW: ") and not out.endswith("\n")
    stop(env)
    assert run(env, "status").stdout == "💪 4 sets today 🔥4d"  # v3: no "next:"


def test_status_prefers_session(env):
    mod = load_module()
    now = time.time()
    st = mod.empty_state()
    st["sessions"]["a"] = {"token": "t", "updated_at": now,
                           "pending": [{"exercise": "squats", "reps": 20, "unit": "reps", "ts": now}]}
    st["sessions"]["b"] = {"token": "t", "updated_at": now,
                           "pending": [{"exercise": "plank", "reps": 40, "unit": "seconds", "ts": now - 5}]}
    (env["home"] / "state.json").write_text(json.dumps(st))
    write_cfg(env)  # the status line is opt-in in the public build ("statusline": true here)
    assert mod.status_text() == "🏋 NOW: 20 squats"
    assert mod.status_text("b") == "🏋 NOW: 40s plank"


def test_status_is_fast(env):
    write_cfg(env)
    seed_log(env, list(range(30)) * 5)
    mod = load_module()
    t = time.perf_counter()
    mod.status_text()
    assert time.perf_counter() - t < 0.05


def test_streak():
    mod = load_module()
    today = dt.date(2026, 3, 10)
    def rec(d):
        return {"ts": dt.datetime.combine(d, dt.time(12)).timestamp(), "status": "done"}
    days = lambda *ns: [rec(today - dt.timedelta(days=n)) for n in ns]
    assert mod.streak_days([], today) == 0
    assert mod.streak_days(days(0), today) == 1
    assert mod.streak_days(days(0, 1, 2), today) == 3
    assert mod.streak_days(days(1, 2), today) == 2  # ends yesterday
    assert mod.streak_days(days(2, 3), today) == 0
    assert mod.streak_days(days(0, 2, 3), today) == 1
    skipped = [{"ts": rec(today)["ts"], "status": "skipped"}]
    assert mod.streak_days(skipped, today) == 0


def test_stats_and_help_and_default(env):
    write_cfg(env)
    seed_log(env, [0, 1])
    r = run(env, "cli")
    assert "Today: 1 set" in r.stdout and "Streak: 2 days" in r.stdout and "Last 7 days: 2 sets" in r.stdout
    assert "stats" in run(env, "cli", "help").stdout
    assert "Unknown command" in run(env, "cli", "bogus").stdout


def test_cli_test_notification(env):
    write_cfg(env, cooldown_s=9999)
    st = {"last_fired_at": time.time()}
    (env["home"] / "state.json").write_text(json.dumps(st))
    r = run(env, "cli", "test")
    assert "Sent a test notification" in r.stdout
    assert len(notes(env, "test")) == 1


def test_osascript_escaping():
    mod = load_module()
    assert mod.applescript_quote('say "hi" \\ there') == '"say \\"hi\\" \\\\ there"'
    assert mod.applescript_quote("a\nb") == '"a b"'
    src = mod.osascript_source('T"', "S", 'B\\', {"sound": False})
    assert src == 'display notification "B\\\\" with title "T\\"" subtitle "S"'
    assert "sound name" not in src
    src = mod.osascript_source("T", "S", "B", {"sound": True, "sound_name": "Glass"})
    assert src.endswith('sound name "Glass"')


def test_osascript_escaping_compiles():
    mod = load_module()
    src = mod.osascript_source('quote " and \\ backslash', "sub 'x'", 'body "y"', {"sound": False})
    # Compile only (no notification): wrap in a handler that is never called.
    r = subprocess.run(["osacompile", "-o", "/dev/null", "-e", f"on unused()\n{src}\nend unused"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_errors_log_truncates(env):
    mod = load_module()
    p = env["home"] / "errors.log"
    p.write_text("x" * (300 * 1024))
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        mod.log_error("t", e)
    assert p.stat().st_size < 100 * 1024
    assert "boom" in p.read_text()


# ---------------------------------------------------------------- review regressions

def test_fired_set_survives_stale_transcript_and_stop_logs_it(env):
    # Finding 1: a long silent tool call made the timer drop the nudged set.
    write_cfg(env, delay_s=0.3, repeat_s=0.2, cooldown_s=60, stale_s=1, max_lifetime_s=4)
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text("{}\n")
    prompt(env, transcript=str(tr))
    assert wait_for(lambda: len(notes(env, "fire")) == 1)
    assert wait_for(lambda: state(env)["sessions"]["s1"].get("idle") is True, timeout=4)
    s = state(env)["sessions"]["s1"]
    assert s["token"] and len(s["pending"]) == 1
    assert "NOW" not in run(env, "status", "s1").stdout  # idle: no "NOW"
    r = stop(env)
    msg = json.loads(r.stdout)
    assert msg["systemMessage"].startswith("Logged ")
    assert len(log(env)) == 1 and log(env)[0]["status"] == "done"
    assert len(notes(env, "done")) == 1


def test_subagent_activity_keeps_session_live(env):
    write_cfg(env, delay_s=0.2, repeat_s=0.1, cooldown_s=0, stale_s=1, max_lifetime_s=3)
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text("{}\n")
    old = time.time() - 1000
    os.utime(tr, (old, old))
    sub = env["tmp"] / "sess" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.jsonl").write_text("{}\n")  # fresh subagent transcript
    prompt(env, transcript=str(tr))
    assert wait_for(lambda: len(notes(env, "fire")) >= 1)
    stop(env)


def test_idle_session_resumes_nudging_on_activity(env):
    write_cfg(env, delay_s=0.2, repeat_s=0.1, cooldown_s=0, stale_s=0.6, max_lifetime_s=5)
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text("{}\n")
    old = time.time() - 1000
    os.utime(tr, (old, old))
    prompt(env, transcript=str(tr))
    assert wait_for(lambda: state(env)["sessions"]["s1"].get("idle") is True, timeout=3)
    assert notes(env, "fire") == []
    tr.write_text("{}\n{}\n")  # Claude writes again
    assert wait_for(lambda: len(notes(env, "fire")) >= 1, timeout=3)
    stop(env)


def test_status_drops_now_after_timer_exits(env):
    # Finding 2: max_lifetime reached with no Stop must not leave "NOW" up for hours.
    write_cfg(env, delay_s=0.2, repeat_s=10, cooldown_s=100, max_lifetime_s=1.0)
    prompt(env)
    assert wait_for(lambda: run(env, "status", "s1").stdout.startswith("🏋 NOW: "))
    pid = state(env)["timer_pids"][-1]
    assert wait_for(lambda: not pid_alive(pid), timeout=4)
    assert run(env, "status", "other").stdout == ""
    assert run(env, "status", "s1").stdout == ""
    # the set is still credited if Stop arrives later
    r = stop(env)
    assert json.loads(r.stdout)["systemMessage"].startswith("Logged ")


def test_status_drops_now_when_paused_mid_turn(env):
    write_cfg(env, delay_s=0.2, repeat_s=0.3, cooldown_s=100, max_lifetime_s=4)
    prompt(env)
    assert wait_for(lambda: run(env, "status").stdout.startswith("🏋 NOW: "))
    run(env, "cli", "pause", "5")
    st = state(env)
    st["last_fired_at"] = 0  # let the timer reach the pause check
    (env["home"] / "state.json").write_text(json.dumps(st))
    pid = st["timer_pids"][-1]
    assert wait_for(lambda: not pid_alive(pid), timeout=4)
    assert state(env)["sessions"]["s1"]["nudging"] is False
    assert run(env, "status").stdout == "💤 paused"
    stop(env)


def test_status_hides_expired_pending_without_timer(env):
    # Timer killed / reboot: show_until bounds "NOW".
    mod = load_module()
    now = time.time()
    st = mod.empty_state()
    st["sessions"]["a"] = {"token": "t", "updated_at": now,
                           "pending": [{"exercise": "squats", "reps": 20, "unit": "reps", "ts": now - 4000,
                                        "show_until": now - 3000}]}
    (env["home"] / "state.json").write_text(json.dumps(st))
    assert mod.status_text() == ""


def test_second_prompt_carries_pending_to_next_stop(env):
    # Finding 3: a new prompt before Stop must not discard a nudged set.
    write_cfg(env, delay_s=0.2, cooldown_s=100)
    prompt(env)
    assert wait_for(lambda: len(notes(env, "fire")) == 1)
    first = state(env)["sessions"]["s1"]["pending"][0]
    prompt(env)  # Esc + new message, no Stop in between
    s = state(env)["sessions"]["s1"]
    # v3: carried (credited at the next Stop, as in v1) but no longer on screen in the new turn
    assert [p["exercise"] for p in s["carried"]] == [first["exercise"]] and s["pending"] == []
    r = stop(env)
    msg = json.loads(r.stdout)["systemMessage"]
    assert first["exercise"] in msg
    assert len(log(env)) == 1


def test_totals_wording_timed_only(env):
    # Finding 5: no "0 reps" when only timed sets were logged.
    mod = load_module()
    assert mod.fmt_totals(1, 0, 33) == "1 set / 33s"
    assert mod.fmt_totals(2, 30, 0) == "2 sets / 30 reps"
    assert mod.fmt_totals(3, 30, 45) == "3 sets / 30 reps / 45s"
    assert mod.fmt_totals(0, 0, 0) == "0 sets / 0 reps"
    write_cfg(env)
    r = run(env, "cli", "done", "plank", "40")
    assert "today: 1 set / 40s" in r.stdout and "reps" not in r.stdout
    st = mod.empty_state()
    st["sessions"]["s1"] = {"token": "t", "updated_at": time.time(),
                            "pending": [{"exercise": "wall sit", "reps": 33, "unit": "seconds", "ts": time.time()}]}
    (env["home"] / "state.json").write_text(json.dumps(st))
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert "today 2 sets / 73s" in msg and "0 reps" not in msg


def test_status_cli_process_is_fast(env):
    write_cfg(env)
    seed_log(env, list(range(30)) * 5)
    best = min(_timed_status(env) for _ in range(3))
    assert best < 0.15  # interpreter start included; ~30ms typical on this machine


def _timed_status(env):
    t = time.perf_counter()
    run(env, "status")
    return time.perf_counter() - t
