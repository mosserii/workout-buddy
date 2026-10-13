"""Workout Buddy v3: the task only when a set is due, and a turn-length predictor that decides when."""
import json
import os
import stat
import time

import pytest

from test_workout import (FAKE_SETTINGS, env, load_module, log, notes, pid_alive, run, state,  # noqa: F401
                          stop, wait_for, write_cfg)

pytestmark = pytest.mark.legacy_modes  # written against the older modes (see conftest.py)


def say(ctx, text, sid="s1", transcript=""):
    return run(ctx, "hook-prompt", stdin=json.dumps(
        {"session_id": sid, "transcript_path": transcript, "cwd": "/tmp", "prompt": text}))


def settings(ctx):
    return json.loads(ctx["settings"].read_text(encoding="utf-8"))


def due_shown(ctx):
    """v3.2: the spinner shows a due set ("NOW: <set>"); otherwise it holds the idle progress lines."""
    v = (settings(ctx).get("spinnerVerbs") or {}).get("verbs") or []
    return len(v) == 1 and v[0].startswith("NOW: ")


def fingerprint(ctx):
    sb = ctx["settings"].stat()
    return sb.st_mtime_ns, sb.st_size, sb.st_ino, ctx["settings"].read_bytes()


def task(p):
    return f"{p['reps']}{'s' if p['unit'] == 'seconds' else ''} {p['exercise']}"


def turns(ctx):
    p = ctx["home"] / "turns.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def set_up(ctx, **cfg):
    write_cfg(ctx, **cfg)
    out = run(ctx, "cli", "setup", "apply", "--yes").stdout
    assert not due_shown(ctx) and settings(ctx)["spinnerVerbs"]["verbs"], out  # nothing due: progress lines
    return out


LONG_ASK = ("implement the retry logic in the uploader, add tests for the backoff and the jitter, "
            "then refactor the client so the session is reused across calls")


# ---------------------------------------------------------------- part 1: the task only when due

def test_short_turn_writes_nothing_and_shows_nothing(env):
    set_up(env, predict=True, early_delay_s=1.0, delay_s=1.0, late_delay_s=1.0, cooldown_s=0)
    before = fingerprint(env)
    for text in ("amazing thank you", LONG_ASK, "תודה"):
        say(env, text)
        assert run(env, "status", "s1").stdout == ""
        stop(env)
    time.sleep(1.3)  # past every due time: the ended turns must stay quiet
    assert fingerprint(env) == before  # zero settings writes
    assert not due_shown(env)
    assert run(env, "status", "s1").stdout == "" and notes(env) == [] and log(env) == []
    assert all(t["fired"] is False and t["duration_s"] < 1.0 for t in turns(env))


def test_due_set_writes_the_task_and_stop_removes_it(env):
    set_up(env, predict=True, early_delay_s=0.3, delay_s=0.3, late_delay_s=0.3, cooldown_s=100)
    say(env, LONG_ASK)
    want = task(state(env)["next_set"])
    assert wait_for(lambda: settings(env).get("spinnerVerbs") == {"mode": "replace", "verbs": [f"NOW: {want}"]}, 4)
    assert run(env, "status", "s1").stdout == f"🏋 NOW: {want}"
    others = {k: v for k, v in settings(env).items() if k != "spinnerVerbs"}
    assert others == {k: v for k, v in FAKE_SETTINGS.items() if k != "spinnerVerbs"}
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert msg.startswith(f"Logged {want} · ") and "next" not in msg
    assert not due_shown(env)
    assert run(env, "status", "s1").stdout == "💪 1 set today"
    t = turns(env)[-1]
    assert t["fired"] is True and 0.2 < t["fired_at_s"] < 2 and t["decided_delay_s"] == 0.3


def test_two_sessions_one_due_one_short(env):
    set_up(env, delay_s=0.3, cooldown_s=100)
    say(env, LONG_ASK, sid="a")
    want = task(state(env)["next_set"])
    assert wait_for(lambda: due_shown(env), 4)
    fp = fingerprint(env)
    say(env, "amazing thank you", sid="b")  # a short turn elsewhere
    assert settings(env)["spinnerVerbs"]["verbs"] == [f"NOW: {want}"]
    assert stop(env, "b").stdout == ""
    assert fingerprint(env) == fp  # the short session caused no write and did not clear a's task
    assert settings(env)["spinnerVerbs"]["verbs"] == [f"NOW: {want}"]
    stop(env, "a")
    assert not due_shown(env)


def test_interrupted_turn_is_resolved_on_the_next_prompt(env):
    """Esc (no Stop): the next prompt takes the old task off screen at once, records the old turn as
    censored, and the set is credited at the next Stop (the v1 carry-over rule)."""
    set_up(env, delay_s=0.3, cooldown_s=100)
    say(env, LONG_ASK)
    first = wait_due_state(env)["sessions"]["s1"]["pending"][0]
    say(env, "amazing thank you")  # Esc + a new message
    assert not due_shown(env)
    assert run(env, "status", "s1").stdout == ""
    s = state(env)["sessions"]["s1"]
    assert s["pending"] == [] and [p["exercise"] for p in s["carried"]] == [first["exercise"]]
    cens = [t for t in turns(env) if t["censored"]]
    assert len(cens) == 1 and cens[0]["duration_s"] is None and cens[0]["fired"] is True
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert msg.startswith(f"Logged {task(first)} · ")
    assert len(log(env)) == 1 and not due_shown(env)
    assert [t["censored"] for t in turns(env)] == [True, False]


def test_pause_and_timer_exit_take_the_task_down(env):
    set_up(env, delay_s=0.2, repeat_s=10, cooldown_s=100, max_lifetime_s=1.0)
    say(env, LONG_ASK)
    assert wait_for(lambda: due_shown(env), 4)
    pid = state(env)["timer_pids"][-1]
    assert wait_for(lambda: not pid_alive(pid), 4)
    assert not due_shown(env)  # the timer gave up: off screen, still credited at Stop
    assert json.loads(stop(env).stdout)["systemMessage"].startswith("Logged ")
    # Second turn: lift the cooldown through config.json (written while no timer runs) instead of a
    # read-modify-write of state.json under a live timer, which could lose the timer's own write. A
    # 60 s lifetime so the timer cannot give up before it fires under load.
    assert not pid_alive(pid)
    write_cfg(env, delay_s=0.2, repeat_s=10, cooldown_s=0, max_lifetime_s=60)
    say(env, LONG_ASK)
    assert wait_for(lambda: due_shown(env), 10)
    run(env, "cli", "pause", "5")
    assert not due_shown(env) and run(env, "status").stdout == "💤 paused"
    assert settings(env)["spinnerVerbs"]["verbs"] == ["Workout Buddy paused"]
    stop(env)


def test_stale_sessions_pruned_clear_the_spinner_and_record_censored(env):
    set_up(env)
    now = time.time()
    st = state(env)
    st["sessions"]["old"] = {"token": "t", "updated_at": now - 7 * 3600, "started_at": now - 7 * 3600,
                             "pending": [{"exercise": "squats", "reps": 20, "unit": "reps", "ts": now - 7 * 3600,
                                          "show_until": now + 600}],
                             "turn": {"ts": now - 7 * 3600, "features": {"category": "generic"}, "band": "mid"}}
    st["spinner_last"] = json.dumps({"mode": "replace", "verbs": ["20 squats"]}, sort_keys=True)
    (env["home"] / "state.json").write_text(json.dumps(st))
    data = settings(env)
    data["spinnerVerbs"] = {"mode": "replace", "verbs": ["20 squats"]}
    env["settings"].write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    run(env, "cli", "resume")  # any locked_state prunes
    assert "old" not in state(env)["sessions"] and not due_shown(env)
    assert [t["censored"] for t in turns(env)] == [True]


# ---------------------------------------------------------------- the guarded writer

def test_writer_keeps_indent_order_newline_and_mode(env, monkeypatch):
    mod = load_module()
    p = env["settings"]
    for indent, nl in (("\t", True), ("    ", False), (None, False)):
        body = json.dumps({"a": 1, "spinnerVerbs": {"x": 1}, "z": [1, 2], "u": "שלום"}, indent=indent,
                          ensure_ascii=False)
        p.write_text(body + ("\n" if nl else ""), encoding="utf-8")
        os.chmod(p, 0o600)
        assert mod.write_spinner_verbs(None, str(p)) == "written"
        out = p.read_text(encoding="utf-8")
        assert json.loads(out) == {"a": 1, "z": [1, 2], "u": "שלום"}
        assert out.endswith("\n") == nl and "שלום" in out
        if indent:
            assert out.split("\n")[1].startswith(indent + '"a"')
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert mod.write_spinner_verbs(None, str(p)) == "unchanged"
        assert mod.write_spinner_verbs({"mode": "replace", "verbs": ["9 squats"]}, str(p)) == "written"
        assert list(json.loads(p.read_text())) == ["a", "z", "u", "spinnerVerbs"]
    assert not list(env["tmp"].glob("settings.json.workout-tmp*"))


def test_writer_compare_and_swap_retries_once_then_skips(env):
    mod = load_module()
    p = env["settings"]
    calls = []

    def meddle(real, attempt):  # another writer (Claude Code, the user) changes the file mid-write
        calls.append(attempt)
        if attempt == 1:
            d = json.loads(open(real).read())
            d["theme"] = "light"
            open(real, "w").write(json.dumps(d, indent=2, ensure_ascii=False) + "\n")

    mod._CAS_TEST_HOOK = meddle
    assert mod.write_spinner_verbs({"mode": "replace", "verbs": ["12 lunges"]}, str(p)) == "written"
    assert calls == [1, 2]
    d = json.loads(p.read_text())
    assert d["theme"] == "light" and d["spinnerVerbs"]["verbs"] == ["12 lunges"]  # both changes kept

    def always(real, attempt):
        d = json.loads(open(real).read())
        d["n"] = d.get("n", 0) + 1
        open(real, "w").write(json.dumps(d, indent=2) + "\n")

    mod._CAS_TEST_HOOK = always
    r = mod.write_spinner_verbs(None, str(p))
    assert r.startswith("skipped") and "kept changing" in r
    d = json.loads(p.read_text())
    assert d["n"] == 2 and d["spinnerVerbs"]["verbs"] == ["12 lunges"]  # the other writer won; we backed off
    assert "spinner-settings-cas" in (env["home"] / "errors.log").read_text()
    assert not list(env["tmp"].glob("settings.json.workout-tmp*"))
    mod._CAS_TEST_HOOK = None


def test_writer_never_creates_or_truncates(env):
    mod = load_module()
    env["settings"].unlink()
    sp = str(env["settings"])
    assert mod.write_spinner_verbs(None, sp).startswith("skipped") and not env["settings"].exists()
    env["settings"].write_text("[1, 2]")
    assert mod.write_spinner_verbs({"mode": "replace", "verbs": ["x"]}, sp).startswith("skipped")
    assert mod.write_spinner_verbs({"mode": "replace", "verbs": ["x"]}, None).startswith("skipped")
    assert env["settings"].read_text() == "[1, 2]"


# ---------------------------------------------------------------- part 2: features and privacy

def test_ack_go_and_hebrew_classification():
    mod = load_module()
    ack = ["amazing thank you", "thanks!", "thank you so much", "lgtm", "👍", "🙏🙏", "great job", "perfect",
           "תודה", "תודה רבה", "סבבה תודה", "מעולה", "looks good to me", "cool, thanks"]
    go = ["yes", "ok", "continue", "pls continue! lets gooo", "313 approved lets gooooo", "yes do it", "כן",
          "יאללה תמשיך", "go ahead", "done"]
    other = ["great, now fix the tests", "thanks, now deploy it to prod", "/workout stats",
             "what should we do now?", "keep me updated", LONG_ASK, "", None]
    for t in ack:
        assert mod.classify_prompt(t) == "ack", t
    for t in go:
        assert mod.classify_prompt(t) == "go", t
    for t in other:
        assert mod.classify_prompt(t) not in ("ack", "go"), t
    assert "ack" in mod.CATEGORIES and "go" in mod.CATEGORIES
    assert mod.fire_pool(mod.normalize_config({}), "ack") == mod.COACH["hype"]["fire"]["generic"]


def test_text_features_shape():
    mod = load_module()
    f = mod.text_features("```py\nx = 1\n```\nwhy does this fail?")
    assert f == {"char_len": 35, "word_count": 7, "line_count": 4, "has_code_block": True, "is_slash_command": False,
                 "question_mark": True, "category": "generic", "build_verb": False, "build_verb_first": False}
    f = mod.text_features("/workout done squats 20")
    assert f["is_slash_command"] and not f["build_verb"]
    f = mod.text_features("Implement the parser")
    assert f["build_verb"] and f["build_verb_first"]
    f = mod.text_features("can you please refactor it")
    assert f["build_verb"] and not f["build_verb_first"]


def test_features_never_contain_prompt_text(env):
    write_cfg(env, predict=True, early_delay_s=0.2, delay_s=0.2, late_delay_s=0.2, cooldown_s=0)
    sentinel = "QXZSENTINEL7741"
    say(env, f"please implement the {sentinel} parser and tests for {sentinel}")
    assert wait_for(lambda: len(notes(env, "fire")) == 1, 4)
    stop(env)
    say(env, f"thanks {sentinel}")
    stop(env)
    run(env, "cli", "why")
    run(env, "cli", "train")
    assert len(turns(env)) == 2
    for f in env["home"].rglob("*"):
        if f.is_file():
            txt = f.read_text(errors="ignore")
            assert sentinel not in txt and "parser" not in txt, f
    t = turns(env)[0]
    assert set(t["features"]) == {"char_len", "word_count", "line_count", "has_code_block", "is_slash_command",
                                  "question_mark", "category", "build_verb", "build_verb_first",
                                  "session_prev_duration_s", "session_ema_duration_s",
                                  "seconds_since_session_prev_stop", "hour_of_day"}
    assert all(not isinstance(v, str) or v in load_module().CATEGORIES for v in t["features"].values())
    second = turns(env)[1]["features"]
    assert second["session_prev_duration_s"] is not None and second["seconds_since_session_prev_stop"] >= 0


# ---------------------------------------------------------------- prior, blending, policy

def test_prior_values():
    mod = load_module()
    P = mod.prior_p
    assert P({"category": "ack", "char_len": 500, "build_verb": True}) == 0.02
    assert P({"category": "generic", "is_slash_command": True, "char_len": 200, "build_verb": True}) == 0.1
    assert P({"category": "generic", "char_len": 39, "build_verb": False}) == 0.1
    assert P({"category": "bug", "char_len": 81, "build_verb": True}) == 0.7
    assert P({"category": "bug", "char_len": 80, "build_verb": True}) == 0.35
    assert P({"category": "generic", "char_len": 39, "build_verb": True}) == 0.35
    assert P({"category": "generic", "char_len": 60, "build_verb": False}) == 0.35
    assert P({"category": "go", "char_len": 3}) == 0.5


def _model(mod, n, b=0.0, long_s=60):
    d = len(mod.FEATURE_NAMES)
    return {"version": mod.MODEL_VERSION, "features": list(mod.FEATURE_NAMES), "mean": [0.0] * d, "std": [1.0] * d,
            "w": [0.0] * d, "b": b, "n": n, "long_s": long_s}


def test_blending_weight():
    mod = load_module()
    cfg = mod.normalize_config({})
    f = {"category": "ack", "char_len": 10}
    r = mod.predict_long(cfg, f, None)
    assert r["p_long"] == 0.02 and r["w"] == 0 and r["p_model"] is None
    r = mod.predict_long(cfg, f, _model(mod, 40, b=0.0))  # p_model = 0.5, w = 40 / 80
    assert r["w"] == 0.5 and r["p_model"] == 0.5 and r["p_long"] == pytest.approx(0.5 * 0.5 + 0.5 * 0.02)
    r = mod.predict_long(cfg, f, _model(mod, 360, b=10.0))
    assert r["w"] == 0.9 and r["p_long"] == pytest.approx(0.9 * mod.sigmoid(10.0) + 0.1 * 0.02, abs=1e-4)
    assert mod.predict_long(cfg, f, _model(mod, 360, long_s=90))["p_model"] is None  # other long_s: ignored


def test_policy_thresholds_and_floor(monkeypatch):
    monkeypatch.delenv("WORKOUT_TEST", raising=False)
    mod = load_module()
    cfg = mod.normalize_config({"predict": True})  # public build: the predictor is off by default
    # hi 0.9, not the brief's 0.75: replayed with the cooldown on the corrected labels, 0.75 showed a task
    # on 14 of 211 short turns (flat rule: 0) for +10 sets, 0.85 on 5 for +2, 0.9 on 0 for +0 (see README)
    assert (cfg["predict_hi"], cfg["predict_lo"], cfg["early_delay_s"], cfg["delay_s"], cfg["late_delay_s"]) == \
        (0.9, 0.15, 15, 60, 120)
    hi = 0.75
    assert mod.choose_delay(cfg, 0.75, hi) == ("early", 15.0)
    assert mod.choose_delay(cfg, 0.749, hi) == ("mid", 60.0)
    assert mod.choose_delay(cfg, 0.151, hi) == ("mid", 60.0)
    assert mod.choose_delay(cfg, 0.15, hi) == ("late", 120.0)
    assert mod.choose_delay(dict(cfg, predict=False), 0.99, hi) == ("flat", 60.0)
    tiny = dict(cfg, early_delay_s=2, delay_s=0, late_delay_s=5)
    for p in (0.99, 0.5, 0.01):
        assert mod.choose_delay(tiny, p, hi)[1] == 10.0  # never earlier than 10 s
    assert mod.choose_delay(dict(tiny, predict=False), 0.5, hi)[1] == 10.0
    monkeypatch.setenv("WORKOUT_MIN_DELAY_S", "0")  # ignored outside a test run (WORKOUT_TEST=1)
    assert mod.min_delay_s() == 10.0
    bad = mod.normalize_config({"predict_hi": 0.1, "predict_lo": 0.5, "long_s": 0, "retrain_every": 0,
                                "predict": "yes"})
    assert (bad["predict_hi"], bad["predict_lo"], bad["long_s"], bad["retrain_every"], bad["predict"]) == \
        (0.9, 0.15, 60, 25, False)  # public build: the predictor is off by default
    ok = mod.normalize_config({"predict_hi": 0.8, "predict_lo": 0.3, "long_s": 90, "predict": False})
    assert (ok["predict_hi"], ok["predict_lo"], ok["long_s"], ok["predict"]) == (0.8, 0.3, 90, False)


def test_safety_net_turn_ending_before_due_never_shows(env):
    set_up(env, predict=True, early_delay_s=0.8, delay_s=0.8, late_delay_s=0.8, cooldown_s=0)
    before = fingerprint(env)
    say(env, LONG_ASK)
    pid = state(env)["timer_pids"][-1]
    time.sleep(0.4)
    assert stop(env).stdout == ""
    assert wait_for(lambda: not pid_alive(pid), 3)
    time.sleep(0.2)
    assert notes(env) == [] and log(env) == [] and fingerprint(env) == before
    assert state(env)["sessions"]["s1"]["pending"] == []


def test_hook_prompt_decides_the_delay(env):
    write_cfg(env, predict=True, early_delay_s=11, delay_s=22, late_delay_s=33, cooldown_s=0)
    say(env, "amazing thank you")
    s = state(env)["sessions"]["s1"]
    assert s["turn"]["band"] == "late" and s["turn"]["decided_delay_s"] == 33
    assert s["theme"] == "ack" and s["turn"]["p_long"] == 0.02
    stop(env)
    say(env, LONG_ASK)
    assert state(env)["sessions"]["s1"]["turn"]["band"] == "mid"  # prior 0.7 < hi 0.75
    stop(env)
    write_cfg(env, predict=True, early_delay_s=11, delay_s=22, late_delay_s=33, cooldown_s=0, predict_hi=0.6,
              predict_lo=0.05)
    say(env, LONG_ASK)
    t = state(env)["sessions"]["s1"]["turn"]
    assert t["band"] == "early" and t["decided_delay_s"] == 11
    stop(env)
    out = run(env, "cli", "why").stdout
    assert "Latest prompt" in out and "early: due at 11s" in out and "prior 0.70 (no model yet)" in out
    assert "Prompt before it" in out and "outcome: ran" in out and "no task shown" in out


# ---------------------------------------------------------------- self-correction

def test_adaptive_threshold_up_and_down():
    mod = load_module()
    cfg = mod.normalize_config({"predict_hi": 0.75, "predict_lo": 0.15})
    st = mod.empty_state()
    early = {"band": "early", "decided_delay_s": 15, "fired_at_s": 15.2}
    for _ in range(9):
        assert mod.adapt_after_turn(st, cfg, early, 20.0) is None  # < 10 samples: no change yet
    assert mod.adapt_after_turn(st, cfg, early, 20.0) == "up"
    assert mod.effective_hi(cfg, st) == 0.8 and st["predict"]["early"] == []
    assert mod.adapt_after_turn(st, cfg, {"band": "mid", "decided_delay_s": 60, "fired_at_s": 61}, 5) is None
    assert mod.adapt_after_turn(st, cfg, dict(early, fired_at_s=200), 5) is None  # held by cooldown: not early
    for _ in range(4):  # push it to the cap
        for _ in range(10):
            mod.adapt_after_turn(st, cfg, early, 20.0)
    assert mod.effective_hi(cfg, st) == 0.95
    for _ in range(10):
        assert mod.adapt_after_turn(st, cfg, early, 20.0) is None  # capped
    st["predict"]["early"] = []
    changes = [mod.adapt_after_turn(st, cfg, early, 400.0) for _ in range(30)]
    assert changes[-1] == "down" and changes.count("down") == 1 and mod.effective_hi(cfg, st) == 0.9
    for _ in range(3 * 30):
        mod.adapt_after_turn(st, cfg, early, 400.0)
    assert mod.effective_hi(cfg, st) == 0.75  # back to the configured value, never below
    for _ in range(30):
        mod.adapt_after_turn(st, cfg, early, 400.0)
    assert mod.effective_hi(cfg, st) == 0.75
    # a 10% false-early window (>= 5%, <= 15%) changes nothing
    st["predict"]["early"] = []
    outs = [mod.adapt_after_turn(st, cfg, early, 20.0 if i % 10 == 9 else 400.0) for i in range(30)]
    assert set(outs) == {None}


def test_adaptive_threshold_persists_through_stop(env):
    write_cfg(env, predict=True, predict_hi=0.6, predict_lo=0.05, early_delay_s=0.2, delay_s=5, late_delay_s=5,
              cooldown_s=0)
    st = state(env) if (env["home"] / "state.json").exists() else {}
    st["predict"] = {"hi_bump": 0.0, "early": [1] * 9, "new_since_train": 0}
    (env["home"] / "state.json").write_text(json.dumps(st))
    say(env, LONG_ASK)
    assert wait_for(lambda: len(notes(env, "fire")) == 1, 4)
    stop(env)  # fired early, ended within early_delay_s + 30 s: the 10th false early
    pr = state(env)["predict"]
    assert pr["hi_bump"] == 0.05 and pr["early"] == []
    say(env, LONG_ASK)
    assert state(env)["decisions"][-1]["hi"] == 0.65
    stop(env)


# ---------------------------------------------------------------- training and evaluation

def _rec(mod, ts, cat, dur, chars=100, verb=True, source="live", censored=False):
    f = {"char_len": chars, "word_count": chars // 5, "line_count": 1, "has_code_block": False,
         "is_slash_command": False, "question_mark": False, "category": cat, "build_verb": verb,
         "build_verb_first": verb, "session_prev_duration_s": None, "session_ema_duration_s": None,
         "seconds_since_session_prev_stop": None, "hour_of_day": 10}
    return {"ts": ts, "sid": "x", "source": source, "features": f, "duration_s": None if censored else dur,
            "censored": censored, "decided_delay_s": None, "p_long": None, "band": None, "fired": False,
            "fired_at_s": None}


def synthetic(mod, n=200):
    """Separable: every ack ends in 5 s, every long build ask runs 200 s, alternating."""
    out = []
    for i in range(n):
        if i % 2:
            out.append(_rec(mod, 1_700_000_000 + i * 600, "ack", 5.0, chars=12, verb=False))
        else:
            out.append(_rec(mod, 1_700_000_000 + i * 600, "bug", 200.0, chars=300))
    out.append(_rec(mod, 1_800_000_000, "bug", None, censored=True))
    return out


def test_predict_stats_on_a_synthetic_set_with_a_known_answer(env):
    mod = load_module()
    cfg = mod.normalize_config({})
    ev = mod.evaluate(synthetic(mod), cfg)
    assert (ev["n_total"], ev["n_finished"], ev["n_censored"], ev["n_train"], ev["n_test"]) == (201, 200, 1, 160, 40)
    assert ev["base_rate"] == 0.5 and ev["auc_model"] == 1.0 and ev["auc_blend"] == 1.0
    assert ev["brier_model"] < 0.01 and ev["brier_base"] == pytest.approx(0.25)
    assert ev["bands"] == {"early": 20, "late": 20}
    assert ev["short_shown_pct"] == 0.0 and ev["short_shown_flat_pct"] == 0.0 and ev["tiny_shown_pct"] == 0.0
    assert ev["long_early_pct"] == 100.0 and ev["false_early_pct"] == 0.0 and ev["n_early_fired"] == 20
    assert ev["gain_median_long_s"] == 45.0  # due at 15 s instead of 60 s on a 200 s turn
    assert ev["gain_total_min"] == pytest.approx(20 * 45 / 60)
    assert ev["missed_vs_flat_pct"] == 0.0
    with open(env["home"] / "turns.jsonl", "w") as f:
        for r in synthetic(mod):
            f.write(json.dumps(r) + "\n")
    write_cfg(env, delay_s=60)
    out = run(env, "cli", "predict-stats").stdout
    assert "Turns: 201 (201 live); 200 finished, 1 interrupted" in out
    assert "AUC    model 1.000" in out and "false early" in out and "+45s per long turn" in out
    assert "short turns (<60s) that showed a task: 0.0%" in out


def test_predict_stats_with_little_data(env):
    write_cfg(env)
    assert "Turns: 0" in run(env, "cli", "predict-stats").stdout
    mod = load_module()
    assert "not enough" in mod.evaluate(synthetic(mod, 10), mod.normalize_config({}))["error"]


def test_auc_and_brier_helpers():
    mod = load_module()
    assert mod.auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert mod.auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
    assert mod.auc([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1]) == 0.5
    assert mod.auc([0.3, 0.3], [1, 1]) is None
    assert mod.brier([1.0, 0.0], [1, 0]) == 0.0 and mod.brier([0.5], [1]) == 0.25


def test_training_is_deterministic_and_learns(env):
    mod = load_module()
    cfg = mod.normalize_config({})
    recs = synthetic(mod)
    m1, msg = mod.train_model(cfg, recs)
    m2, _ = mod.train_model(cfg, recs)
    assert m1["w"] == m2["w"] and m1["b"] == m2["b"] and m1["n"] == 200 and "trained on 200" in msg
    assert mod.valid_model(json.loads((env["home"] / "model.json").read_text()))
    m = mod.load_model()
    ack = mod.predict_long(cfg, synthetic(mod)[1]["features"], m)
    bug = mod.predict_long(cfg, synthetic(mod)[0]["features"], m)
    assert ack["p_long"] < 0.1 and bug["p_long"] > 0.75 and bug["top"]
    few, msg = mod.train_model(cfg, synthetic(mod, 10))
    assert few is None and "not enough data" in msg


def test_stop_triggers_background_retrain(env):
    write_cfg(env, retrain_every=3)
    mod = load_module()
    with open(env["home"] / "turns.jsonl", "w") as f:
        for r in synthetic(mod, 60):
            f.write(json.dumps(r) + "\n")
    for _ in range(3):
        say(env, "amazing thank you")
        stop(env)
    assert wait_for(lambda: (env["home"] / "model.json").exists(), 8)
    m = json.loads((env["home"] / "model.json").read_text())
    assert m["n"] == 63 and state(env)["predict"]["new_since_train"] == 0
    say(env, "amazing thank you")
    d = state(env)["decisions"][-1]
    assert d["n"] == 63 and d["p_model"] is not None
    stop(env)


def test_turns_file_is_capped(env, monkeypatch):
    write_cfg(env, predict=True)  # turns are recorded only where the predictor can use them
    mod = load_module()
    monkeypatch.setattr(mod, "TURNS_MAX", 50)
    monkeypatch.setattr(mod, "TURNS_SLACK", 5)
    recs = [_rec(mod, 1_700_000_000 + i, "bug", 100.0, chars=4000) for i in range(300)]
    mod.append_turns(recs)
    got = mod.read_turns()
    assert len(got) == 50 and got[0]["ts"] == 1_700_000_250 and got[-1]["ts"] == 1_700_000_299


# ---------------------------------------------------------------- transcripts

def _e(kind, ts, **kw):
    base = {"parentUuid": None, "isSidechain": False, "type": kind, "sessionId": "sess-1", "userType": "external",
            "timestamp": ts}
    base.update(kw)
    return json.dumps(base)


def user(ts, content, **kw):
    return _e("user", ts, message={"role": "user", "content": content}, **kw)


def asst(ts, **kw):
    return _e("assistant", ts, message={"role": "assistant", "content": [{"type": "text", "text": "ok"}]}, **kw)


def tool_result(ts):
    return _e("user", ts, message={"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
              toolUseResult={"stdout": "x"})


T = "2026-10-01T10:00:{:02d}.000Z"


def write_fixture(tmp_path, lines, name="sess-1.jsonl", age=3600):
    proj = tmp_path / "projects" / "-Users-x-repo"
    proj.mkdir(parents=True, exist_ok=True)
    fp = proj / name
    fp.write_text("\n".join(lines) + "\n")
    old = time.time() - age
    os.utime(fp, (old, old))
    return fp


def test_transcript_parser_rules(env):
    mod = load_module()
    lines = [
        _e("queue-operation", T.format(0)),
        user(T.format(1), "implement the parser please", origin={"kind": "human"}),   # turn 1: 1 -> 9
        asst(T.format(3)),
        tool_result(T.format(5)),
        asst(T.format(6), isSidechain=True),            # sidechain: ignored
        user(T.format(7), "skill body", isMeta=True),    # meta: ignored
        asst(T.format(9)),
        "{not json",                                     # malformed: skipped
        user(T.format(12), "<local-command-caveat>Caveat: ...</local-command-caveat>", isMeta=True),
        user(T.format(13), "<command-name>/model</command-name>\n<command-args></command-args>"),  # no activity
        user(T.format(15), "<command-message>workout</command-message>\n<command-name>/workout</command-name>"
                           "\n<command-args>stats</command-args>"),               # turn 2 (slash): 15 -> 18
        asst(T.format(18)),
        user(T.format(20), "<task-notification>\n<task-id>1</task-id>"),       # boundary
        asst(T.format(25)),                                                    # belongs to no turn
        user(T.format(30), "amazing thank you", origin={"kind": "human"}),     # turn 3: 30 -> 31
        asst(T.format(31)),
        user(T.format(33), "peer says hi", origin={"kind": "peer"}),           # boundary
        asst(T.format(34)),
        _e("user", None, message={"role": "user", "content": "no timestamp"}),  # missing ts: not a turn start
        asst(T.format(35)),
        user(T.format(40), "fix the flaky test", origin={"kind": "human"}),    # turn 4: interrupted
        asst(T.format(42)),
        user(T.format(44), [{"type": "text", "text": "[Request interrupted by user]"}]),
        user(T.format(45), [{"type": "text", "text": "summary"}], isCompactSummary=True),
        user(T.format(50), [{"type": "text", "text": "now deploy it"}, {"type": "image"}]),  # turn 5: 50 -> 58
        tool_result(T.format(58)),
        asst(T.format(57)),
    ]
    fp = write_fixture(env["tmp"], lines)
    recs = mod.parse_transcript(str(fp))
    got = [(r["features"]["category"], r["features"]["is_slash_command"], r["duration_s"], r["censored"]) for r in recs]
    assert got == [("generic", False, 8.0, False), ("generic", True, 3.0, False), ("ack", False, 1.0, False),
                   ("tests", False, None, True), ("deploy", False, 8.0, False)]
    assert all(r["source"] == "transcript" and r["sid"] == mod.sid_hash("sess-1") for r in recs)
    f2, f3, f5 = recs[1]["features"], recs[2]["features"], recs[4]["features"]
    assert f2["session_prev_duration_s"] == 8.0 and f2["seconds_since_session_prev_stop"] == 6.0
    assert f3["session_prev_duration_s"] == 3.0 and f3["session_ema_duration_s"] == pytest.approx(0.3 * 3 + 0.7 * 8)
    assert f5["session_prev_duration_s"] == 1.0  # the interrupted turn leaves the history alone
    for r in recs:
        assert "parser" not in json.dumps(r) and "deploy it" not in json.dumps(r)


def test_transcript_last_turn_of_a_live_file_is_censored(env):
    mod = load_module()
    lines = [user(T.format(1), "write the docs"), asst(T.format(9))]
    fp = write_fixture(env["tmp"], lines, age=10)  # touched 10 s ago: may still be running
    assert [r["censored"] for r in mod.parse_transcript(str(fp))] == [True]
    os.utime(fp, (time.time() - 3600, time.time() - 3600))
    assert [r["duration_s"] for r in mod.parse_transcript(str(fp))] == [8.0]


def test_bootstrap_from_transcripts_end_to_end(env):
    root = env["tmp"] / "projects"
    turns_ = []
    for i in range(30):
        long_ = i % 2 == 0
        s = i * 1000
        turns_ += [user(f"2026-09-20T10:{(s // 60) % 60:02d}:{s % 60:02d}.000Z",
                        LONG_ASK if long_ else "thanks!"),
                   asst(f"2026-09-20T{10 + (s + (200 if long_ else 3)) // 3600:02d}:"
                        f"{((s + (200 if long_ else 3)) // 60) % 60:02d}:{(s + (200 if long_ else 3)) % 60:02d}.000Z")]
    write_fixture(env["tmp"], ["garbage"] + turns_)
    sub = root / "-Users-x-repo" / "sess-1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text(user(T.format(1), "subagent prompt") + "\n")  # never read
    write_cfg(env)
    out = run(env, "cli", "train", "--from-transcripts", "--days", "0", "--root", str(root)).stdout
    assert "Read 1 transcript files" in out and "turns.jsonl now holds" in out
    assert "Model trained on" in out, out
    recs = turns(env)
    assert all(r["source"] == "transcript" for r in recs)
    n = len(recs)
    out = run(env, "cli", "train", "--from-transcripts", "--days", "0", "--root", str(root)).stdout
    assert len(turns(env)) == n  # re-running replaces, never duplicates
    assert (env["home"] / "model.json").exists()
    assert "Bad --days" in run(env, "cli", "train", "--from-transcripts", "--days", "x").stdout


# ---------------------------------------------------------------- speed

def test_hook_prompt_latency_with_full_history_and_a_model(env):
    mod = load_module()
    write_cfg(env, predict=True)
    recs = synthetic(mod, 400)
    mod.train_model(mod.normalize_config({}), recs)
    with open(env["home"] / "turns.jsonl", "w") as f:
        for i in range(5000):
            f.write(json.dumps(recs[i % len(recs)]) + "\n")
    cfg = mod.normalize_config({"predict": True})
    m = mod.load_model()
    f = dict(mod.text_features(LONG_ASK), **mod.session_features({"prev_duration_s": 30, "ema_duration_s": 40,
                                                                   "prev_stop_ts": time.time() - 9}, time.time()))
    t = time.perf_counter()
    for _ in range(100):
        mod._model_cache.clear()
        mod.predict_long(cfg, dict(mod.text_features(LONG_ASK), **f), mod.load_model())
    per = (time.perf_counter() - t) / 100
    assert per < 0.010, per  # inference (incl. reading model.json) well under 10 ms
    assert m is not None
    best = 9.0
    for _ in range(4):
        t = time.perf_counter()
        r = say(env, LONG_ASK)
        best = min(best, time.perf_counter() - t)
        assert r.stdout == "" and r.stderr == "" and r.returncode == 0
        stop(env)
    assert best < 0.15, best  # whole hook process, interpreter start included
    assert state(env)["decisions"][-1]["p_model"] is not None


def test_esc_seen_in_the_transcript_stops_the_set_from_becoming_due(env):
    """Esc writes "[Request interrupted by user]" to the transcript and no Stop follows: the timer reads
    the transcript's tail before marking a set due, and gives up instead."""
    write_cfg(env, delay_s=0.6, cooldown_s=0)
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text(user("2020-01-01T00:00:00.000Z", "old prompt") + "\n")
    say(env, LONG_ASK, transcript=str(tr))
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    with open(tr, "a") as f:
        f.write(asst(stamp) + "\n")
        f.write(user(stamp, [{"type": "text", "text": "[Request interrupted by user]"}]) + "\n")
    pid = state(env)["timer_pids"][-1]
    assert wait_for(lambda: not pid_alive(pid), 4)
    assert notes(env) == [] and state(env)["sessions"]["s1"]["pending"] == []
    mod = load_module()
    assert mod.transcript_interrupted(str(tr), time.time() - 30)
    assert not mod.transcript_interrupted(str(tr), time.time() + 3600)  # an older interrupt does not count
    with open(tr, "a") as f:
        f.write(user(stamp, "a new prompt") + "\n")
    assert not mod.transcript_interrupted(str(tr), time.time() - 30)
    assert not mod.transcript_interrupted(str(env["tmp"] / "missing.jsonl"), 0)
    stop(env)


# ---------------------------------------------------------------- review fixes (v3.1)

def _now_stamp(offset=0.0):
    t = time.time() + offset
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"


def wait_due_state(env, sid="s1", timeout=10):
    """The first state snapshot in which session `sid` has a pending set AND the spinner shows it.

    workout.locked_state() writes settings.json (sync_spinner) *before* it writes state.json, so
    "NOW: ..." on screen does not yet mean the pending set is readable from state.json; under load that
    gap is wide enough to read an empty pending list. Waiting on the state snapshot itself (and reading
    the set from that same snapshot) closes the gap."""
    seen = {}

    def ready():
        st = state(env)  # atomic_write_json: a read is always one whole snapshot
        pend = ((st.get("sessions") or {}).get(sid) or {}).get("pending") or []
        if pend and settings(env)["spinnerVerbs"]["verbs"] == [f"NOW: {task(pend[0])}"]:
            seen["st"] = st
            return True
        return False

    assert wait_for(ready, timeout)
    return seen["st"]


def sets_of(pending):
    """Pending sets without show_until, which the live timer's heartbeat legitimately moves on every poll."""
    return [{k: v for k, v in p.items() if k != "show_until"} for p in pending]


def _due_turn(env, transcript="", **cfg):
    """A running turn in session s1 with a set on screen. Returns (state, task). A transcript is touched
    first so its staleness clock starts at the prompt, not at whenever the test wrote it."""
    cfg.setdefault("max_lifetime_s", 60)  # FAST's 3 s lifetime would end the timer mid-test under load
    set_up(env, delay_s=0.2, cooldown_s=100, **cfg)
    if transcript:
        os.utime(transcript)
    say(env, LONG_ASK, transcript=transcript)
    st = wait_due_state(env)
    return st, task(st["sessions"]["s1"]["pending"][0])


NOTIFICATIONS = ["<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>",
                 '<agent-message from="other-session">hi</agent-message>',
                 "Another Claude session sent a message:\n<agent-message from=\"x\">hi</agent-message>",
                 "3 background agents were stopped by the user: a, b, c"]


def test_injected_message_mid_turn_leaves_the_running_turn_alone(env):
    """Finding 1: a task notification or another session's message fires UserPromptSubmit mid-turn. It
    must not censor the turn, take the due set off screen, start a timer or write a turn row."""
    st0, want = _due_turn(env)
    s0 = st0["sessions"]["s1"]
    fp = fingerprint(env)
    for text in NOTIFICATIONS:
        assert say(env, text).stdout == ""
    st = state(env)
    s = st["sessions"]["s1"]
    assert s["token"] == s0["token"] and s["turn"] == s0["turn"] and sets_of(s["pending"]) == sets_of(s0["pending"])
    assert s.get("carried", []) == [] and st["timer_pids"] == st0["timer_pids"]
    assert fingerprint(env) == fp and settings(env)["spinnerVerbs"]["verbs"] == [f"NOW: {want}"]
    assert turns(env) == [] and len(st["decisions"]) == 1
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert msg.startswith(f"Logged {want} · ")
    assert [(t["censored"], t["source"]) for t in turns(env)] == [(False, "live")]


def test_injected_message_while_idle_starts_nothing(env):
    set_up(env, delay_s=0.2, cooldown_s=0)
    for text in NOTIFICATIONS:
        say(env, text)
    st = state(env) if (env["home"] / "state.json").exists() else {"sessions": {}, "timer_pids": []}
    assert not (st["sessions"].get("s1") or {}).get("token") and st["timer_pids"] == []
    assert stop(env).stdout == ""
    time.sleep(0.4)
    assert turns(env) == [] and not due_shown(env) and log(env) == []


def test_human_message_mid_turn_joins_the_running_turn(env):
    """Finding 1: a message typed while Claude works is handed to the running turn (one Stop ends both).
    With the transcript showing no Esc, the turn, its timer and its due set stay."""
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text(user(_now_stamp(-1), "older") + "\n")
    st0, want = _due_turn(env, transcript=str(tr))
    with open(tr, "a") as f:
        f.write(asst(_now_stamp()) + "\n")
    fp = fingerprint(env)
    say(env, "also update the README while you're at it", transcript=str(tr))
    s = state(env)["sessions"]["s1"]
    assert s["token"] == st0["sessions"]["s1"]["token"] and sets_of(s["pending"]) == sets_of(st0["sessions"]["s1"]["pending"])
    assert fingerprint(env) == fp and turns(env) == []
    stop(env)
    t = turns(env)
    assert len(t) == 1 and not t[0]["censored"] and t[0]["ts"] == st0["sessions"]["s1"]["turn"]["ts"]
    assert t[0]["features"]["char_len"] == len(LONG_ASK)  # the turn is the first prompt's


def test_human_message_after_esc_still_censors_and_carries(env):
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text("")
    _, want = _due_turn(env, transcript=str(tr))
    with open(tr, "a") as f:
        f.write(user(_now_stamp(), [{"type": "text", "text": "[Request interrupted by user]"}]) + "\n")
    say(env, "amazing thank you", transcript=str(tr))
    s = state(env)["sessions"]["s1"]
    assert s["pending"] == [] and [task(p) for p in s["carried"]] == [want]
    assert not due_shown(env)
    assert [t["censored"] for t in turns(env)] == [True]
    assert json.loads(stop(env).stdout)["systemMessage"].startswith(f"Logged {want} · ")


def test_injected_message_after_esc_closes_the_open_turn(env):
    """An Esc'd turn followed by a task notification (no human prompt in between): the notification's own
    turn must not be recorded as the Esc'd turn finishing late. The Esc'd turn is censored and its set is
    carried and credited at the notification turn's Stop (the v1 carry rule)."""
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text("")
    _, want = _due_turn(env, transcript=str(tr))
    with open(tr, "a") as f:
        f.write(user(_now_stamp(), [{"type": "text", "text": "[Request interrupted by user]"}]) + "\n")
    assert say(env, NOTIFICATIONS[0], transcript=str(tr)).stdout == ""
    s = state(env)["sessions"]["s1"]
    assert not s["token"] and s["pending"] == [] and [task(p) for p in s["carried"]] == [want]
    assert not due_shown(env)
    assert [t["censored"] for t in turns(env)] == [True]
    assert json.loads(stop(env).stdout)["systemMessage"].startswith(f"Logged {want} · ")
    assert [t["censored"] for t in turns(env)] == [True]  # the notification turn is not a turn


def test_missed_stop_is_resolved_by_the_transcripts_turn_end(env):
    """If Claude Code wrote turn_duration after the turn began (Stop ran, our hook did not), the next
    prompt starts a new turn instead of joining a dead one."""
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text("")
    _due_turn(env, transcript=str(tr))
    time.sleep(0.6)
    with open(tr, "a") as f:
        f.write(_e("system", _now_stamp(), subtype="turn_duration", durationMs=900) + "\n")
    old = state(env)["sessions"]["s1"]["token"]
    say(env, "next question", transcript=str(tr))
    assert state(env)["sessions"]["s1"]["token"] != old and [t["censored"] for t in turns(env)] == [True]
    mod = load_module()
    assert mod.transcript_turn_over(str(tr), time.time() - 30) == "finished"
    assert mod.transcript_turn_over(str(tr), time.time() + 5) is None  # an older end does not count
    assert mod.transcript_turn_over(str(env["tmp"] / "missing.jsonl"), 0) is None
    stop(env)


def test_bootstrap_prefers_a_finished_transcript_row_over_a_censored_live_one(env):
    mod = load_module()
    lines = [user("2026-09-20T10:00:00.000Z", LONG_ASK), asst("2026-09-20T10:05:00.000Z")]
    write_fixture(env["tmp"], lines)
    ts = mod.parse_ts("2026-09-20T10:00:01.000Z")
    live = [_rec(mod, ts, "generic", None, censored=True), _rec(mod, ts + 9999, "generic", 30.0)]
    for r in live:
        r["sid"] = mod.sid_hash("sess-1")
    mod.write_turns(live)
    out = mod.bootstrap_from_transcripts(mod.normalize_config({}), days=0, roots=[str(env["tmp"] / "projects")])
    rows = sorted(turns(env), key=lambda r: r["ts"])
    assert [(r["source"], r["censored"], r["duration_s"]) for r in rows] == \
        [("transcript", False, 300.0), ("live", False, 30.0)]
    assert "1 live rows marked interrupted replaced" in out


def test_transcript_parser_peer_bash_turn_end_and_local_commands(env):
    """Finding 2: an isMeta peer message ends the turn; a "!" shell command ends it; Claude Code's
    turn_duration entry closes it; a local command (/model, /compact) is never a turn."""
    mod = load_module()
    lines = [
        user(T.format(1), "please continue!"),                                           # turn 1: 1 -> 6
        asst(T.format(6)),
        user(T.format(8), "Another Claude session sent a message:\n<agent-message from=\"x\">",
             isMeta=True, origin={"kind": "peer"}),                                      # boundary (isMeta)
        asst(T.format(30)),
        user(T.format(31), "I trust you, do it"),                                        # turn 2: 31 -> 33
        asst(T.format(33)),
        user(T.format(35), "<bash-input>git status</bash-input>"),                       # boundary
        user(T.format(35), "<bash-stdout>clean</bash-stdout><bash-stderr></bash-stderr>"),
        asst(T.format(39)),
        user(T.format(40), "fix the bug"),                                               # turn 3: 40 -> 44
        asst(T.format(43)),
        _e("system", T.format(44), subtype="turn_duration", durationMs=4000),
        _e("system", T.format(45), subtype="away_summary"),
        asst(T.format(50)),                                                              # after the end
        user(T.format(51), "<command-name>/model</command-name>\n<command-args></command-args>"),
        user(T.format(51), "<local-command-stdout>Set model to opus</local-command-stdout>"),
        asst(T.format(53)),
        user(T.format(54), "<command-name>/compact</command-name>"),
        _e("system", T.format(55), subtype="local_command", content="<local-command-stdout>x"),
        asst(T.format(59)),
    ]
    fp = write_fixture(env["tmp"], lines)
    got = [(r["features"]["char_len"], r["duration_s"]) for r in mod.parse_transcript(str(fp))]
    assert got == [(len("please continue!"), 5.0), (len("I trust you, do it"), 2.0), (len("fix the bug"), 4.0)]


def test_transcript_duplicates_across_resumed_files_are_dropped(env):
    lines = [user(T.format(1), "write the docs"), asst(T.format(9))]
    write_fixture(env["tmp"], lines, name="sess-1.jsonl")
    write_fixture(env["tmp"], lines + [user(T.format(20), "thanks"), asst(T.format(21))], name="sess-2.jsonl")
    mod = load_module()
    out = mod.bootstrap_from_transcripts(mod.normalize_config({}), days=0, roots=[str(env["tmp"] / "projects")])
    assert sorted(r["duration_s"] for r in turns(env)) == [1.0, 8.0] and "1 repeated across resumed files" in out


def test_simulated_cooldown_replay():
    mod = load_module()
    # three overlapping 400 s turns 100 s apart, due at 15 s, cooldown 600 s: only the first gets a set
    assert mod.simulate_fires([0, 100, 200], [400, 400, 400], [15, 15, 15], 600, 300) == [[15], [], []]
    # one long turn: repeats every max(repeat, cooldown)
    assert mod.simulate_fires([0], [1300], [60], 600, 300) == [[60, 660, 1260]]
    # a turn shorter than its delay never fires; the next one is not held back by it
    assert mod.simulate_fires([0, 10], [30, 100], [60, 60], 600, 300) == [[], [60]]


def test_predict_stats_reports_the_cooldown_replay(env):
    mod = load_module()
    ev = mod.evaluate(synthetic(mod), mod.normalize_config({"cooldown_s": 0, "repeat_s": 1000}))
    # 20 long test turns (200 s) each fire once at 15 s (policy) or 60 s (flat); 20 acks never fire
    assert (ev["cd_sets_policy"], ev["cd_sets_flat"], ev["cd_short_shown_policy"], ev["n_short"]) == (20, 20, 0, 20)
    assert ev["cd_onscreen_min_policy"] == pytest.approx(20 * 185 / 60)
    assert ev["cd_false_early"] == 0 and ev["cd_early_fires"] == 20 and ev["cd_brief"] == 0
    with open(env["home"] / "turns.jsonl", "w") as f:
        for r in synthetic(mod):
            f.write(json.dumps(r) + "\n")
    write_cfg(env, delay_s=60, cooldown_s=600)
    out = run(env, "cli", "predict-stats").stdout
    assert "with the global cooldown (600s)" in out and "sets shown: " in out
    assert "screen time, not exercise" in out and "extra exercise" not in out


def test_rare_yes_no_features_get_no_weight(env):
    """Finding 4: 8 slash commands must not earn a large weight; nor 1 ack."""
    mod = load_module()
    recs = synthetic(mod, 200)
    for i, r in enumerate(recs[:8]):
        r["features"]["is_slash_command"] = True
    m, _ = mod.train_model(mod.normalize_config({}), recs)
    j = mod.FEATURE_NAMES.index("slash_command")
    assert m["w"][j] == 0.0 and "slash_command" in m["masked"] and "cat_ack" not in m["masked"]
    assert "code_block" not in m["masked"]  # never seen at all: constant, not masked (no weight anyway)


def test_workout_command_uses_the_prior_only(env):
    mod = load_module()
    write_cfg(env, predict=True)
    mod.train_model(mod.normalize_config({}), synthetic(mod, 200))
    say(env, "/workout stats")
    d = state(env)["decisions"][-1]
    assert d["p_model"] is None and d["p_long"] == 0.1 and d["band"] == "late"
    stop(env)
    say(env, "/review the diff")
    assert state(env)["decisions"][-1]["p_model"] is not None
    stop(env)


def test_esc_while_the_task_is_on_screen_takes_it_down(env):
    """Finding 5: the timer checks for Esc on every poll while a set is up, not only before it fires."""
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text("")
    # stale_s=2: on-screen poll 1 s; the timer would live on for 60 s if it only checked before firing
    _, want = _due_turn(env, transcript=str(tr), stale_s=2, max_lifetime_s=60)
    pid = state(env)["timer_pids"][-1]
    t0 = time.time()
    with open(tr, "a") as f:
        f.write(user(_now_stamp(), [{"type": "text", "text": "[Request interrupted by user]"}]) + "\n")
    assert wait_for(lambda: not due_shown(env), 8)
    assert wait_for(lambda: not pid_alive(pid), 5) and time.time() - t0 < 12  # well under the 60 s lifetime
    assert state(env)["sessions"]["s1"]["nudging"] is False
    assert [task(p) for p in state(env)["sessions"]["s1"]["pending"]] == [want]  # still credited later


def test_a_dead_timer_releases_the_spinner_after_the_heartbeat(env):
    env["env"]["WORKOUT_HEARTBEAT_S"] = "0.6"
    _due_turn(env)
    pid = state(env)["timer_pids"][-1]
    os.kill(pid, 9)
    assert wait_for(lambda: not pid_alive(pid), 3)
    # the dead timer refreshes nothing more: wait out the last show_until it wrote, then some
    last = state(env)["sessions"]["s1"]["pending"][-1]["show_until"]
    assert wait_for(lambda: time.time() > last + 0.3, 5)
    assert due_shown(env)  # nothing has re-synced the spinner yet: the stale set is still in settings
    say(env, "amazing thank you", sid="b")  # any state change in another session
    assert not due_shown(env)
    stop(env, "b")


def test_heartbeat_keeps_a_live_set_up(env):
    """A live timer refreshes show_until on every on-screen poll, so the set outlives one heartbeat.
    Heartbeat 2 s vs an on-screen poll of 0.2 s (stale_s 0.4; no transcript, so staleness never
    applies) leaves ~1.8 s of scheduling slack per refresh under load."""
    env["env"]["WORKOUT_HEARTBEAT_S"] = "2.0"
    st0, want = _due_turn(env, stale_s=0.4)
    first = st0["sessions"]["s1"]["pending"][-1]["show_until"]
    # Outlive the first heartbeat: without a refresh the set would now be past its show_until.
    assert wait_for(lambda: time.time() > first + 0.2, 6)
    s = state(env)["sessions"]["s1"]
    assert s["pending"][-1]["show_until"] > first and s.get("nudging") is not False  # refreshed, alive
    say(env, "amazing thank you", sid="b")  # any state change re-syncs the spinner from due_set
    assert settings(env)["spinnerVerbs"]["verbs"] == [f"NOW: {want}"]
    stop(env, "b")
    stop(env)


def test_more_acks():
    mod = load_module()
    for t in ("ok thanks", "yes thanks", "no", "nope", "לא", "שיגעון תודה", "ok, thank you!", "כן תודה"):
        assert mod.classify_prompt(t) == "ack", t
    for t in ("ok continue thanks", "yes", "ok", "thanks, go ahead and push"):
        assert mod.classify_prompt(t) != "ack", t
    for t in NOTIFICATIONS + ["  <task-notification>x"]:
        assert mod.is_injected(t), t
    for t in ("fix the <task-notification> parser", "another claude session is stuck", LONG_ASK, None):
        assert not mod.is_injected(t), t


def test_session_history_outlives_the_session_prune(env):
    """Finding 8: as in the transcripts, a session resumed after the 6 h prune is not a first turn."""
    write_cfg(env)
    say(env, "write the docs")
    stop(env)
    st = state(env)
    st["sessions"]["s1"]["updated_at"] = time.time() - 7 * 3600
    (env["home"] / "state.json").write_text(json.dumps(st))
    run(env, "cli", "resume")  # prunes s1
    assert "s1" not in state(env)["sessions"] and "s1" in state(env)["hists"]
    say(env, "now the tests")
    f = state(env)["sessions"]["s1"]["turn"]["features"]
    assert f["session_prev_duration_s"] is not None and f["seconds_since_session_prev_stop"] >= 0
    stop(env)
    mod = load_module()
    st = {"hists": {"old": {"prev_stop_ts": time.time() - 31 * 86400}, "bad": 3,
                    **{f"s{i}": {"prev_stop_ts": time.time() - i} for i in range(250)}}}
    mod.prune_hists(st, time.time())
    assert "old" not in st["hists"] and "bad" not in st["hists"] and len(st["hists"]) == 200
    assert "s0" in st["hists"] and "s249" not in st["hists"]


def test_a_rare_feature_leaves_the_prompt_to_the_prior(env):
    """An ack seen on 1 training turn must not be scored by a model that knows nothing about acks: the
    blend weight for that prompt is capped at k/(k+40), so "amazing thank you" keeps the prior's 0.02
    (late band) even with a model trained on hundreds of turns."""
    mod = load_module()
    recs = [r for r in synthetic(mod, 200) if not r["censored"]]
    acks = [r for r in recs if r["features"]["category"] == "ack"]
    for r in acks[1:]:
        r["features"]["category"] = "generic"
    m = mod.fit_logreg([r["features"] for r in recs], [1 if r["duration_s"] >= 60 else 0 for r in recs])
    m["long_s"] = 60
    assert m["support"]["cat_ack"] == 1
    cfg = mod.normalize_config({"predict": True})
    f = mod.text_features("amazing thank you")
    f.update(mod.session_features({}, time.time()))
    p = mod.predict_long(cfg, f, m)
    assert p["w"] == pytest.approx(1 / 41, abs=1e-4) and p["p_long"] <= 0.05
    assert mod.choose_delay(cfg, p["p_long"], cfg["predict_hi"])[0] == "late"
    g = dict(recs[0]["features"])  # a "bug" ask, well supported
    assert mod.predict_long(cfg, g, m)["w"] == pytest.approx(m["n"] / (m["n"] + 40), abs=1e-4)
    old = dict(m)
    old.pop("support")  # a model.json from before this field: the plain n/(n+40) blend
    assert mod.predict_long(cfg, f, old)["w"] == pytest.approx(m["n"] / (m["n"] + 40), abs=1e-4)
