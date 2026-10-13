"""Workout Buddy v5.2. (1) A skipped set must not come back at every later ask: every declined ask re-picks
next_set, never the declined exercise, never one declined in the last 3 asks while another is enabled,
exactly once per ask. (2) After a click on the link button (test suite only), the listener closes its own
tab (AppleScript, running browsers only, exact prefix)."""
import json
import random
import subprocess
import time

from test_v4 import asked, declined, done, say, status_raw as status, task  # noqa: F401
from test_v5 import click, link, sess, setup_button
from test_workout import PY, SCRIPT, env, load_module, log, run, state, stop, wait_for  # noqa: F401
import pytest

pytestmark = pytest.mark.legacy_modes  # written against the older modes (see conftest.py)

EX4 = [{"name": "push-ups", "min": 10, "max": 12, "unit": "reps"},
       {"name": "squats", "min": 15, "max": 20, "unit": "reps"},
       {"name": "crunches", "min": 15, "max": 20, "unit": "reps"},
       {"name": "lunges", "min": 10, "max": 12, "unit": "reps"}]
PLANK_ONLY = [{"name": "plank", "min": 60, "max": 60, "unit": "seconds"}]


def _cfg(mod, exs=EX4, **over):
    raw = {"exercises": exs, "progression": False}
    raw.update(over)
    return mod.normalize_config(raw)


def _decline(mod, st, cfg, ask_id, rng=None, ex=None):
    ns = mod.ensure_next_set(st, cfg)
    rec = {"ts": time.time(), "session_id": "s1", "exercise": ex or ns["exercise"], "reps": ns["reps"],
           "unit": ns["unit"], "status": "declined", "ask_id": ask_id}
    return rec, mod.rotate_after_declines(st, [rec], cfg, rng=rng or random.Random(ask_id))


# ---------------------------------------------------------------- rotate on decline (unit)

def test_decline_picks_a_different_exercise(env):
    mod = load_module()
    cfg = _cfg(mod)
    for seed in range(40):
        st = mod.empty_state()
        mod.ensure_next_set(st, cfg, rng=random.Random(seed))
        before = st["next_set"]["exercise"]
        rec, n = _decline(mod, st, cfg, f"a{seed}", rng=random.Random(seed + 1000))
        assert n == 1 and rec["exercise"] == before
        assert st["next_set"]["exercise"] != before


def test_three_declines_in_a_row_never_repeat_the_last_three(env):
    mod = load_module()
    cfg = _cfg(mod)
    st = mod.empty_state()
    rng = random.Random(7)
    mod.ensure_next_set(st, cfg, rng=rng)
    names = []
    for i in range(60):
        rec, n = _decline(mod, st, cfg, f"d{i}", rng=rng)
        assert n == 1
        names.append(rec["exercise"])
        assert st["next_set"]["exercise"] not in names[-3:], (i, names[-3:], st["next_set"])
    assert {r["status"] for r in st["ask_hist"]} == {"declined"} and len(st["ask_hist"]) == mod.ASK_HIST_KEEP


def test_a_taken_ask_counts_as_one_of_the_last_three(env):
    """Declined plank, then two taken asks: plank was declined within the last 3 asks, so it is still avoided;
    one more taken ask and it may come back."""
    mod = load_module()
    cfg = _cfg(mod, exs=EX4 + PLANK_ONLY)
    st = mod.empty_state()
    mod.note_ask(st, "p1", "plank", "declined")
    mod.note_ask(st, "t1", "squats", "done")
    mod.note_ask(st, "t2", "lunges", "done")
    assert mod.recently_declined(st) == {"plank"}
    for seed in range(30):
        st["last_exercise"] = "lunges"
        assert mod.ensure_next_set(st, cfg, force=True, rng=random.Random(seed))["exercise"] not in ("plank", "lunges")
    mod.note_ask(st, "t3", "crunches", "done")
    assert mod.recently_declined(st) == set()


def test_one_exercise_enabled_repeats_without_error(env):
    mod = load_module()
    cfg = _cfg(mod, exs=PLANK_ONLY)
    st = mod.empty_state()
    st["last_exercise"] = "plank"
    for i in range(4):
        rec, n = _decline(mod, st, cfg, f"x{i}")
        assert n == 1 and st["next_set"] == {"exercise": "plank", "reps": 60, "unit": "seconds"}
    assert not (env["home"] / "errors.log").exists()


def test_two_exercises_the_decline_beats_no_repeat_after_done(env):
    mod = load_module()
    cfg = _cfg(mod, exs=EX4[:2])
    st = mod.empty_state()
    st["last_exercise"] = "push-ups"
    st["next_set"] = {"exercise": "squats", "reps": 16, "unit": "reps"}
    _decline(mod, st, cfg, "z1")
    assert st["next_set"]["exercise"] == "push-ups"


def test_the_same_declined_ask_repicks_once(env):
    mod = load_module()
    cfg = _cfg(mod)
    st = mod.empty_state()
    rec, n = _decline(mod, st, cfg, "same1")
    after = dict(st["next_set"])
    assert n == 1
    for _ in range(3):  # seen again (another path, another session): nothing changes
        assert mod.rotate_after_declines(st, [rec], cfg, rng=random.Random(99)) == 0
        assert st["next_set"] == after
    assert [r["id"] for r in st["ask_hist"]] == ["same1"]


def test_rotate_never_raises(env):
    mod = load_module()
    assert mod.rotate_after_declines(None, [{"status": "declined", "exercise": "x"}]) == 0
    st = {}
    assert mod.rotate_after_declines(st, [{"status": "declined", "exercise": "x", "ask_id": "q"}],
                                     {"exercises": "broken"}) == 0  # logged, swallowed
    assert mod.rotate_after_declines(st, [{"status": "done", "exercise": "x"}, "junk", None]) == 0


def test_ask_hist_survives_a_bad_state_file(env):
    mod = load_module()
    (env["home"] / "state.json").write_text(json.dumps({"ask_hist": [1, {"exercise": 3}, {"exercise": "plank"}]}))
    assert mod.read_state()["ask_hist"] == [{"exercise": "plank"}]
    (env["home"] / "state.json").write_text(json.dumps({"ask_hist": "nope"}))
    assert mod.read_state()["ask_hist"] == []


# ---------------------------------------------------------------- rotate on decline (end to end)

def test_declined_at_stop_the_next_ask_is_another_exercise(env):
    setup_button(env, ask_grace_s=0, cooldown_s=0, exercises=EX4)
    seen = []
    for i in range(4):
        say(env)
        assert wait_for(lambda: asked(env), 5)
        seen.append(asked(env)[0]["exercise"])
        stop(env)
        assert len(declined(env)) == i + 1
        assert state(env)["next_set"]["exercise"] not in seen[-3:]
    assert len(set(seen)) == 4  # 4 exercises, never one of the last 3 declined: each came up once
    assert done(env) == []


def test_grace_running_out_rotates(env):
    setup_button(env, ask_grace_s=0.6, exercises=EX4)
    say(env)
    port, token, want, _ = link(env)
    ex = asked(env)[0]["exercise"]
    stop(env)
    assert wait_for(lambda: len(declined(env)) == 1, 6)
    st = state(env)
    assert st["next_set"]["exercise"] != ex
    assert [(r["exercise"], r["status"]) for r in st["ask_hist"]] == [(ex, "declined")]


def test_next_prompt_decline_rotates(env):
    setup_button(env, ask_grace_s=60, exercises=EX4)
    say(env)
    link(env)
    ex = asked(env)[0]["exercise"]
    stop(env)
    say(env, "thanks")  # the next prompt closes the grace question: declined
    assert len(declined(env)) == 1
    assert state(env)["next_set"]["exercise"] != ex
    stop(env)


def test_a_click_is_not_a_decline(env):
    setup_button(env, exercises=EX4)
    say(env)
    port, token, want, _ = link(env)
    assert click(port, token)[0] == 200
    stop(env)
    st = state(env)
    assert declined(env) == [] and [r["status"] for r in st["ask_hist"]] == ["done"]


def test_two_sessions_declining_concurrently_repick_once_each(env):
    setup_button(env, ask_grace_s=0, cooldown_s=0, exercises=EX4)
    say(env, sid="s1")
    say(env, sid="s2")
    assert wait_for(lambda: asked(env, "s1") and asked(env, "s2"), 6)
    ids = sorted([asked(env, "s1")[0]["id"], asked(env, "s2")[0]["id"]])
    procs = [subprocess.Popen([PY, str(SCRIPT), "hook-stop"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env["env"], text=True) for _ in ("s1", "s2")]
    for p, sid in zip(procs, ("s1", "s2")):
        p.stdin.write(json.dumps({"session_id": sid, "stop_hook_active": False}))
        p.stdin.close()
    for p in procs:
        assert p.wait(timeout=20) == 0
    d = declined(env)
    assert sorted(r["ask_id"] for r in d) == ids
    hist = state(env)["ask_hist"]
    assert sorted(r["id"] for r in hist) == ids and all(r["status"] == "declined" for r in hist)
    # seen again (e.g. a later prune): no further re-pick
    mod = load_module()
    st = state(env)
    assert mod.rotate_after_declines(st, d, mod.normalize_config({"exercises": EX4})) == 0
    assert st["next_set"] == state(env)["next_set"]
    assert state(env)["next_set"]["exercise"] not in {r["exercise"] for r in d}
    assert not (env["home"] / "errors.log").exists()


def test_another_sessions_open_ask_is_not_changed(env):
    setup_button(env, ask_grace_s=0, cooldown_s=0, exercises=EX4)
    say(env, sid="s1")
    say(env, sid="s2")
    assert wait_for(lambda: asked(env, "s1") and asked(env, "s2"), 6)
    p2 = dict(asked(env, "s2")[0])
    line2 = status(env, "s2")
    stop(env, "s1")
    assert len(declined(env)) == 1
    q2 = asked(env, "s2")[0]
    assert {k: q2[k] for k in ("id", "exercise", "reps", "unit", "asked_at")} == \
        {k: p2[k] for k in ("id", "exercise", "reps", "unit", "asked_at")}
    assert status(env, "s2").split("?", 1)[0] == line2.split("?", 1)[0]
    stop(env, "s2")


# ---------------------------------------------------------------- close the tab (unit)

SAFARI = ('tell application "Safari"\n'
          '\trepeat with w in (get every window)\n'
          '\t\ttry\n'
          '\t\t\tclose (every tab of w whose URL starts with "http://127.0.0.1:52345/")\n'
          '\t\tend try\n'
          '\tend repeat\n'
          'end tell')


def test_close_script_is_exactly_this_listeners_prefix():
    mod = load_module()
    assert mod.close_tab_prefix(52345) == "http://127.0.0.1:52345/"
    assert mod.close_tab_script("Safari", 52345) == SAFARI
    import re
    for app in mod.CLOSE_TAB_BROWSERS:
        src = mod.close_tab_script(app, 52345)
        assert src.startswith(f'tell application "{app}"\n')
        lits = re.findall(r'"([^"]*)"', src)
        assert lits == [app, "http://127.0.0.1:52345/"]  # one URL literal: the exact prefix, port and slash
        assert src.count("close ") == 1 and "starts with \"http://127.0.0.1:52345/\")" in src
    assert mod.close_tab_script("Safari", 5234).count("http://127.0.0.1:5234/\"") == 1
    for bad in (0, 65536, -1, True, "52345", "1\" or true", 52345.0, None):
        try:
            mod.close_tab_script("Safari", bad)
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass
    for app in ("Finder", "System Events", 'Safari" to quit'):
        try:
            mod.close_tab_script(app, 52345)
            raise AssertionError(f"accepted {app!r}")
        except ValueError:
            pass


class FakeRunner:
    def __init__(self, running=(), fail=()):
        self.calls, self.running, self.fail = [], set(running), set(fail)

    def __call__(self, argv):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        self.calls.append(argv)
        src = argv[2]
        app = src.split('"')[1]
        if src.endswith(" is running"):
            return subprocess.CompletedProcess(argv, 0, "true\n" if app in self.running else "false\n", "")
        if app in self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "execution error: Not authorized to send Apple events "
                                                           "to Safari. (-1743)")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_close_only_asks_running_browsers(env):
    mod = load_module()
    r = FakeRunner(running=("Safari", "Arc"))
    assert mod.close_tabs(52345, runner=r) == ["Safari", "Arc"]
    assert all(c[:2] == ["/usr/bin/osascript", "-e"] and len(c) == 3 for c in r.calls)
    checks = [c[2] for c in r.calls if c[2].endswith(" is running")]
    assert checks == [f'application "{a}" is running' for a in mod.CLOSE_TAB_BROWSERS]
    closes = [c[2] for c in r.calls if not c[2].endswith(" is running")]
    assert closes == [SAFARI, mod.close_tab_script("Arc", 52345)]
    # each close comes right after its own browser's running check: nothing is ever launched
    idx = [i for i, c in enumerate(r.calls) if c[2] in closes]
    assert all(r.calls[i - 1][2] == f'application "{c[2].split(chr(34))[1]}" is running'
               for i, c in zip(idx, [r.calls[i] for i in idx]))
    r2 = FakeRunner(running=())
    assert mod.close_tabs(52345, runner=r2) == [] and len(r2.calls) == len(mod.CLOSE_TAB_BROWSERS)
    assert not (env["home"] / "errors.log").exists()


def test_close_failures_are_swallowed_and_logged_once(env):
    mod = load_module()
    r = FakeRunner(running=("Safari", "Google Chrome"), fail=("Safari", "Google Chrome"))
    assert mod.close_tabs(52345, runner=r) == ["Safari", "Google Chrome"]
    errs = (env["home"] / "errors.log").read_text()
    assert errs.count("[close-tab]") == 1 and "-1743" in errs

    def boom(argv):
        raise subprocess.TimeoutExpired(argv, 5)
    assert mod.close_tabs(52345, runner=boom) == []
    assert (env["home"] / "errors.log").read_text().count("[close-tab]") == 2  # one more line, for that call
    assert mod.close_tabs("nope", runner=FakeRunner(running=("Safari",))) == []


def test_osascript_runner_has_a_timeout_and_list_args(monkeypatch):
    mod = load_module()
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, "false\n", "")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod._osascript(["/usr/bin/osascript", "-e", 'application "Arc" is running'])
    assert seen["timeout"] == 5.0 and isinstance(seen["argv"], list) and not seen.get("shell")


def test_close_tab_config_default_off_and_false_never_runs(env, monkeypatch):
    mod = load_module()
    assert mod.DEFAULT_CONFIG["close_tab"] is False and mod.normalize_config({})["close_tab"] is False  # public
    assert mod.normalize_config({"close_tab": True})["close_tab"] is True
    assert mod.normalize_config({"close_tab": "no"})["close_tab"] is False
    called = []
    monkeypatch.setattr(mod, "_close_tab_later", lambda port: called.append(port))
    assert mod.schedule_close_tab(52345, {"close_tab": False}) is None
    time.sleep(0.1)
    assert called == []
    monkeypatch.setenv("WORKOUT_PLATFORM", "linux")  # not macOS: never, whatever the config says
    assert mod.schedule_close_tab(52345, {"close_tab": True}) is None
    monkeypatch.setenv("WORKOUT_PLATFORM", "darwin")
    t = mod.schedule_close_tab(52345, {"close_tab": True})
    t.join(2)
    assert called == [52345] and not t.daemon


def test_sandbox_never_touches_a_real_browser(env, monkeypatch):
    mod = load_module()
    monkeypatch.setenv("WORKOUT_CLOSE_TAB_DELAY_S", "0")
    monkeypatch.delenv("WORKOUT_CLOSE_TAB_LOG", raising=False)
    monkeypatch.setattr(mod, "close_tabs", lambda *a, **k: (_ for _ in ()).throw(AssertionError("real close")))
    monkeypatch.setattr(mod, "_osascript", lambda *a, **k: (_ for _ in ()).throw(AssertionError("osascript")))
    mod._close_tab_later(52345)
    assert mod.close_tab_delay_s() == 0.0
    monkeypatch.delenv("WORKOUT_TEST")
    assert mod.close_tab_delay_s() == 1.5  # the override is test-run only


# ---------------------------------------------------------------- close the tab (end to end)

def _close_log(ctx):
    p = ctx["tmp"] / "close.log"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_click_is_logged_before_the_tab_is_closed(env):
    env["env"]["WORKOUT_CLOSE_TAB_LOG"] = str(env["tmp"] / "close.log")
    env["env"]["WORKOUT_CLOSE_TAB_DELAY_S"] = "0.3"
    setup_button(env)
    say(env)
    port, token, want, _ = link(env)
    t0 = time.time()
    code, body = click(port, token)
    assert code == 200 and "You can close this tab." in body and "window.close()" in body
    assert _close_log(env) == []  # answered first; the close comes after the delay
    if env["env"].get("WORKOUT_PLATFORM") != "darwin":
        # the Linux path: AppleScript does not exist there, so nothing is ever closed; the click still logs
        time.sleep(0.8)
        assert _close_log(env) == [] and len(done(env)) == 1
        stop(env)
        return
    assert wait_for(lambda: _close_log(env), 5)
    c = _close_log(env)
    assert len(c) == 1 and c[0]["port"] == port and c[0]["prefix"] == f"http://127.0.0.1:{port}/"
    rec = done(env)[0]
    assert rec["ts"] <= c[0]["ts"] and c[0]["ts"] >= t0 + 0.3
    stop(env)


def test_close_tab_false_never_closes(env):
    env["env"]["WORKOUT_CLOSE_TAB_LOG"] = str(env["tmp"] / "close.log")
    env["env"]["WORKOUT_CLOSE_TAB_DELAY_S"] = "0"
    setup_button(env, close_tab=False)
    say(env)
    port, token, want, _ = link(env)
    assert click(port, token)[0] == 200 and len(done(env)) == 1
    time.sleep(0.8)
    assert _close_log(env) == []
    stop(env)


def test_wrong_token_never_closes(env):
    env["env"]["WORKOUT_CLOSE_TAB_LOG"] = str(env["tmp"] / "close.log")
    env["env"]["WORKOUT_CLOSE_TAB_DELAY_S"] = "0"
    setup_button(env)
    say(env)
    port, token, want, _ = link(env)
    from test_v5 import http
    assert http(port, "/done/" + "x" * 22)[0] == 404
    time.sleep(0.6)
    assert _close_log(env) == [] and done(env) == []
    stop(env)
