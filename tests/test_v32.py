"""Workout Buddy v3.2: "NOW: <set>" while due, and progress lines in the spinner while idle."""
import datetime as dt
import io
import json
import random
import re
import sys
import time

from test_workout import (FAKE_SETTINGS, env, load_module, log, run, state, stop, wait_for,  # noqa: F401
                          write_cfg)
import pytest

pytestmark = pytest.mark.legacy_modes  # written against the older modes (see conftest.py)

NO_INSTRUCTION = re.compile(r"\b(do|now)\b", re.I)


def settings(ctx):
    return json.loads(ctx["settings"].read_text(encoding="utf-8"))


def verbs(ctx):
    return (settings(ctx).get("spinnerVerbs") or {}).get("verbs")


def fingerprint(ctx):
    sb = ctx["settings"].stat()
    return sb.st_mtime_ns, sb.st_size, sb.st_ino, ctx["settings"].read_bytes()


def say(ctx, text, sid="s1"):
    return run(ctx, "hook-prompt", stdin=json.dumps({"session_id": sid, "transcript_path": "", "cwd": "/tmp",
                                                      "prompt": text}))


def task(p):
    return f"{p['reps']}{'s' if p['unit'] == 'seconds' else ''} {p['exercise']}"


def rec(day, hour=12, exercise="push-ups", reps=10, unit="reps"):
    ts = dt.datetime.combine(day, dt.time(hour, 0)).timestamp()
    return {"ts": ts, "session_id": "x", "exercise": exercise, "reps": reps, "unit": unit, "status": "done"}


def write_log(ctx, recs):
    with open(ctx["home"] / "log.jsonl", "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def emoji_count(s):
    import unicodedata
    return sum(1 for ch in s if ord(ch) >= 0x1F000 or unicodedata.category(ch) == "So")


def check_lines(lines):
    """The idle-line contract: 3-6 lines (1 when paused), each <= 40 chars, no trailing ellipsis, at most
    one emoji, never an instruction ("do", "now"), never a "NOW:" task."""
    assert lines == ["Workout Buddy paused"] or 3 <= len(lines) <= 6, lines
    assert len(set(lines)) == len(lines)
    for v in lines:
        assert isinstance(v, str) and 0 < len(v) <= 40, v
        assert not v.endswith(("…", "...", ".")), v
        assert emoji_count(v) <= 1, v
        assert not NO_INSTRUCTION.search(v), v
        assert "\n" not in v


class Counter:
    """Wraps the module's guarded writer and counts the writes that changed settings.json."""

    def __init__(self, mod):
        self.real = mod.write_spinner_verbs
        self.calls = []
        mod.write_spinner_verbs = self

    def __call__(self, value, target):
        r = self.real(value, target)
        self.calls.append((value, r))
        return r

    @property
    def writes(self):
        return [v for v, r in self.calls if r == "written"]


def hook_stop_inproc(mod, monkeypatch, sid="s1"):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": sid})))
    mod.cmd_hook_stop()


def live_turn(mod, sid="s1", due=None):
    """A live turn (no timer process), optionally with a due set on screen."""
    now = time.time()
    with mod.locked_state() as st:
        s = {"token": "tok", "started_at": now, "updated_at": now, "pending": [], "carried": [],
             "turn": {"ts": now, "features": {"category": "generic"}, "band": "mid", "fired": bool(due)}}
        if due:
            e, r, u = due
            s["pending"] = [{"exercise": e, "reps": r, "unit": u, "ts": now, "show_until": now + 600}]
        st["sessions"][sid] = s


# ---------------------------------------------------------------- the spinner text

def test_due_spinner_says_now_and_status_line_unchanged(env):
    write_cfg(env, delay_s=0.3, cooldown_s=100)
    run(env, "cli", "setup", "apply", "--yes")
    idle = verbs(env)
    check_lines(idle)
    say(env, "implement the retry logic and add tests for it")
    want = task(state(env)["next_set"])
    assert wait_for(lambda: verbs(env) == [f"NOW: {want}"], 4)
    assert settings(env)["spinnerVerbs"]["mode"] == "replace"
    assert run(env, "status", "s1").stdout == f"🏋 NOW: {want}"
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert msg.startswith(f"Logged {want} · ") and "🔥 " not in msg.split("·")[0]  # Stop stays plain
    after = verbs(env)
    check_lines(after)
    assert after[0].startswith("1 set today, ") and after[0].endswith("🔥")
    assert f"Up next: {task(state(env)['next_set'])}" in after
    others = {k: v for k, v in settings(env).items() if k != "spinnerVerbs"}
    assert others == {k: v for k, v in FAKE_SETTINGS.items() if k != "spinnerVerbs"}


def test_short_turns_cause_zero_settings_writes(env):
    write_cfg(env, delay_s=30, cooldown_s=100)
    run(env, "cli", "setup", "apply", "--yes")
    before, cache = fingerprint(env), state(env)["idle_spinner"]
    for text in ("amazing thank you", "ok", "תודה"):
        say(env, text)
        stop(env)
    assert fingerprint(env) == before
    assert state(env)["idle_spinner"] == cache and log(env) == []


def test_stop_due_to_idle_is_one_write_straight_to_the_new_list(env, monkeypatch):
    write_cfg(env, cooldown_s=100)
    run(env, "cli", "setup", "apply", "--yes")
    mod = load_module()
    live_turn(mod, due=("squats", 20, "reps"))
    assert verbs(env) == ["NOW: 20 squats"]
    c = Counter(mod)
    hook_stop_inproc(mod, monkeypatch)
    assert len(c.writes) == 1 and len(c.calls) == 1
    new = c.writes[0]["verbs"]
    assert verbs(env) == new
    check_lines(new)
    today = dt.date.today()
    assert new[0] == f"1 set today, {mod.hype_word(today)} 🔥"
    assert f"Up next: {task(state(env)['next_set'])}" in new
    assert "2 more days to Spark" in new  # First Blood unlocked at this Stop; 1 of 3 streak days is closest
    assert "Week: 20 reps so far" in new


def test_unchanged_idle_list_is_not_rewritten(env):
    write_cfg(env)
    run(env, "cli", "setup", "apply", "--yes")
    fp = fingerprint(env)
    run(env, "cli", "resume")  # recomputes the idle list: same lines -> no write
    run(env, "cli", "config")
    run(env, "cli", "achievements")
    assert fingerprint(env) == fp


def test_new_local_day_one_write_at_stop_never_at_prompt(env):
    write_cfg(env, delay_s=30, cooldown_s=100)
    yday = dt.date.today() - dt.timedelta(days=1)
    write_log(env, [rec(yday), rec(yday - dt.timedelta(days=1))])
    run(env, "cli", "setup", "apply", "--yes")
    st = state(env)
    st["idle_spinner"]["day"] = yday.isoformat()  # computed yesterday
    st["idle_spinner"]["verbs"] = ["1 set today, on fire 🔥", "Up next: 9 squats", "Week: 10 reps so far"]
    st["spinner_last"] = json.dumps({"mode": "replace", "verbs": st["idle_spinner"]["verbs"]}, sort_keys=True,
                                    ensure_ascii=False)
    (env["home"] / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    data = settings(env)
    data["spinnerVerbs"] = {"mode": "replace", "verbs": st["idle_spinner"]["verbs"]}
    env["settings"].write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    fp = fingerprint(env)
    say(env, "ok")
    assert fingerprint(env) == fp  # hook-prompt never refreshes
    assert state(env)["idle_spinner"]["day"] == yday.isoformat()
    stop(env)
    v = verbs(env)
    assert fingerprint(env) != fp and state(env)["idle_spinner"]["day"] == dt.date.today().isoformat()
    check_lines(v)
    assert v[0] == "First set of the day is waiting" and "2-day streak, keep it alive" in v
    fp2 = fingerprint(env)
    say(env, "ok")
    stop(env)
    assert fingerprint(env) == fp2  # once per day


def test_new_local_day_is_also_noticed_by_the_timer(env):
    write_cfg(env, delay_s=0.3, cooldown_s=10_000, max_lifetime_s=3)
    run(env, "cli", "setup", "apply", "--yes")
    st = state(env)
    st["last_fired_at"] = time.time()  # cooldown: the timer polls but nothing becomes due
    st["idle_spinner"]["day"] = "2000-01-01"
    (env["home"] / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    say(env, "implement the retry logic and add tests for it")
    assert wait_for(lambda: state(env)["idle_spinner"]["day"] == dt.date.today().isoformat(), 4)
    stop(env)


def test_cli_done_skip_pause_resume_refresh_the_list(env):
    write_cfg(env)
    run(env, "cli", "setup", "apply", "--yes")
    run(env, "cli", "done", "push-ups", "12")
    v = verbs(env)
    check_lines(v)
    assert v[0].startswith("1 set today, ") and "2 more days to Spark" in v
    assert f"Up next: {task(state(env)['next_set'])}" in v
    run(env, "cli", "undo")
    v = verbs(env)
    assert v[0] == "First set of the day is waiting" and f"Up next: {task(state(env)['next_set'])}" in v
    run(env, "cli", "pause", "5")
    assert verbs(env) == ["Workout Buddy paused"]
    run(env, "cli", "resume")
    assert verbs(env)[0] == "First set of the day is waiting"


def test_pause_running_out_is_picked_up_at_stop(env):
    write_cfg(env, delay_s=30, cooldown_s=100)
    run(env, "cli", "setup", "apply", "--yes")
    run(env, "cli", "pause", "5")
    assert verbs(env) == ["Workout Buddy paused"]
    st = state(env)
    st["paused_until"] = time.time() - 1  # ran out
    (env["home"] / "state.json").write_text(json.dumps(st, ensure_ascii=False))
    say(env, "ok")
    assert verbs(env) == ["Workout Buddy paused"]  # not at the prompt
    stop(env)
    assert verbs(env)[0] == "First set of the day is waiting"


def test_spinner_idle_claude_restores_key_removal(env):
    write_cfg(env, delay_s=0.3, cooldown_s=100, spinner_idle="claude")
    out = run(env, "cli", "setup", "apply", "--yes").stdout
    # public build: nothing due, so the person's own spinnerVerbs (from before setup) stand
    assert verbs(env) == ["Repping"] and "nothing due now" in out.split("\n")[0]
    say(env, "implement the retry logic and add tests for it")
    want = task(state(env)["next_set"])
    assert wait_for(lambda: verbs(env) == [f"NOW: {want}"], 4)
    stop(env)
    assert verbs(env) == ["Repping"]
    run(env, "cli", "done", "squats", "20")
    assert verbs(env) == ["Repping"]
    write_cfg(env, spinner_idle="progress")  # back to progress lines: picked up by the next refresh
    run(env, "cli", "config")
    check_lines(verbs(env))
    assert "spinner = \"progress\"" in run(env, "cli", "config").stdout  # v4: shown as the mapped "spinner"


def test_spinner_task_false_keeps_the_key_absent_in_every_mode(env):
    # "spinner_task": false = "spinner": "off": setup leaves a key it never managed alone (public build),
    # and nothing writes it later, even when the user changes it.
    write_cfg(env, spinner_task=False)
    run(env, "cli", "setup", "apply", "--yes")
    assert verbs(env) == ["Repping"]
    mine = dict(settings(env), spinnerVerbs={"mode": "replace", "verbs": ["Mine"]})
    env["settings"].write_text(json.dumps(mine, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    run(env, "cli", "done", "squats", "20")
    assert settings(env) == mine
    mod = load_module()
    st = mod.empty_state()
    st["idle_spinner"] = {"day": "x", "cfg": "x", "paused": False, "verbs": ["a b c"]}
    assert mod.spinner_desired(st, "off") is None


# ---------------------------------------------------------------- the lines themselves

def test_idle_variants_from_real_stats(env):
    mod = load_module()
    cfg = mod.normalize_config({})
    today = dt.date.today()
    now = dt.datetime.combine(today, dt.time(15, 0)).timestamp()
    st = mod.empty_state()
    st["next_set"] = {"exercise": "plank", "reps": 41, "unit": "seconds"}

    # nothing ever logged
    v = mod.idle_verbs(cfg, st, [], now)
    check_lines(v)
    assert v[0] == "First set of the day is waiting" and "Up next: 41s plank" in v
    assert "First set unlocks First Blood" in v and not any("Rest day" in x for x in v)

    # three sets today on a 4-day streak
    lg = [rec(today, 9), rec(today, 10, "squats", 20), rec(today, 11, "plank", 40, "seconds")]
    lg += [rec(today - dt.timedelta(days=d)) for d in (1, 2, 3)]
    st["achievements"] = {"first_blood": {"at": 0}, "streak_3": {"at": 0}}
    v = mod.idle_verbs(cfg, st, lg, now)
    check_lines(v)
    assert v[0] == f"3 sets today, {mod.hype_word(today)} 🔥"
    assert "4-day streak, keep it alive" in v and "Up next: 41s plank" in v
    assert "40 reps to Rep Machine I" in v and "Week: 60 reps so far" in v  # 10+20+10+10+10 reps

    # one set today: singular
    assert mod.idle_verbs(cfg, st, [rec(today)], now)[0] == f"1 set today, {mod.hype_word(today)} 🔥"

    # rest day: nothing logged in 2+ days
    v = mod.idle_verbs(cfg, st, [rec(today - dt.timedelta(days=3))], now)
    check_lines(v)
    assert v[0] == "Rest day? Not on Claude's watch" and not any("streak" in x for x in v)
    # yesterday only: not a rest day, the streak is alive
    v = mod.idle_verbs(cfg, st, [rec(today - dt.timedelta(days=1)), rec(today - dt.timedelta(days=2))], now)
    assert v[0] == "First set of the day is waiting" and "2-day streak, keep it alive" in v

    # paused
    st["paused_until"] = now + 600
    assert mod.idle_verbs(cfg, st, lg, now) == ["Workout Buddy paused"]


def test_hype_word_is_deterministic_per_day_and_varies():
    mod = load_module()
    d = dt.date(2026, 10, 7)
    assert mod.hype_word(d) == mod.hype_word(d)
    assert len({mod.hype_word(d + dt.timedelta(days=i)) for i in range(14)}) >= 4
    assert "on fire" in mod.HYPE_WORDS


def test_every_idle_line_meets_the_contract_on_random_data(env):
    mod = load_module()
    rng = random.Random(7)
    today = dt.date.today()
    now = dt.datetime.combine(today, dt.time(15, 0)).timestamp()
    long_cfg = mod.normalize_config({"exercises": [
        {"name": "single-leg romanian deadlift with a pause at the bottom", "min": 5, "max": 8, "unit": "reps"},
        {"name": "hollow hold", "min": 20, "max": 40, "unit": "seconds"}]})
    for cfg in (mod.normalize_config({}), long_cfg):
        for _ in range(150):
            lg = []
            for _ in range(rng.randint(0, 40)):
                e = rng.choice(cfg["exercises"])
                lg.append(rec(today - dt.timedelta(days=rng.randint(0, 12)), rng.randint(0, 23), e["name"],
                              rng.randint(1, 200), e["unit"]))
            st = mod.empty_state()
            mod.unlock_achievements(st, lg, cfg, now, today)
            if rng.random() < 0.3:
                del st["achievements"]  # an older state: nothing recorded yet
            mod.ensure_next_set(st, cfg, lg, rng=rng)
            v = mod.idle_verbs(cfg, st, lg, now)
            check_lines(v)
            assert not any(x.startswith("NOW") for x in v)


def test_config_and_cache_validation():
    mod = load_module()
    # v4: the v3.2 keys map onto "spinner" (default "off")
    assert mod.normalize_config({})["spinner"] == "off"
    assert mod.normalize_config({"spinner_idle": "progress"})["spinner"] == "progress"
    assert mod.normalize_config({"spinner_idle": "claude"})["spinner"] == "task"
    assert mod.normalize_config({"spinner_idle": "loud"})["spinner"] == "off"
    st = mod.empty_state()
    assert mod.spinner_desired(st, "progress") is None  # nothing computed yet
    st["idle_spinner"] = {"day": "x", "cfg": "x", "paused": False, "verbs": ["Up next: 9 squats", "a", "b"]}
    assert mod.spinner_desired(st, "progress") == {
        "mode": "replace", "verbs": ["Up next: 9 squats", "a", "b"]}
    assert mod.spinner_desired(st, "task") is None
    assert mod.spinner_desired(st, "off") is None
    assert not mod.valid_idle_cache({"verbs": []}) and not mod.valid_idle_cache({"verbs": [3]})
