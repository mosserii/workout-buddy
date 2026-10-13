"""Workout Buddy v7: the waiting meter ("timing": "meter", the default) and cooldown_s 300."""
import json
import os
import random
import time

import pytest

from test_v3 import _now_stamp, user
from test_workout import env, load_module, log, run, state, stop, wait_for, write_cfg  # noqa: F401

pytestmark = pytest.mark.legacy_modes  # written against the older modes (see conftest.py)


# ---------------------------------------------------------------- helpers

def setup_meter(ctx, **over):
    """Meter timing on fast test numbers: the timer polls every 0.1 s (sandbox only)."""
    ctx["env"]["WORKOUT_METER_POLL_S"] = "0.1"
    ctx["env"]["WORKOUT_ASK_POLL_S"] = "0.1"
    cfg = {"timing": "meter", "ask_mode": "button", "button": "link", "spinner": "off",
           "meter_base_s": 100, "meter_jitter_s": 0, "meter_min_turn_s": 0.5, "max_lifetime_s": 30,
           "exercises": [{"name": "push-ups", "min": 10, "max": 12, "unit": "reps"},
                         {"name": "squats", "min": 15, "max": 20, "unit": "reps"}]}
    cfg.update(over)
    write_cfg(ctx, **cfg)


def say(ctx, sid="s1", transcript="", text="implement the retry logic and add tests for it"):
    return run(ctx, "hook-prompt", stdin=json.dumps({"session_id": sid, "transcript_path": transcript,
                                                      "cwd": "/tmp", "prompt": text}))


def pend(ctx, sid="s1"):
    return (state(ctx)["sessions"].get(sid) or {}).get("pending") or []


def asked(ctx, sid="s1"):
    return [p for p in pend(ctx, sid) if p.get("status") == "asked"]


def meter_s(ctx):
    return state(ctx).get("meter_s") or 0.0


def cli(ctx, *args):
    return run(ctx, "cli", *args)


CFG = {"meter_base_s": 300, "meter_jitter_s": 60, "meter_min_turn_s": 20, "max_lifetime_s": 3600,
       "quiet_hours": None, "ask_mode": "button", "button": "link", "timing": "meter"}


def busy(ts):
    return {"token": "t", "turn": {"ts": ts, "fired": False}, "pending": []}


def st0(mod, **sessions):
    st = mod.empty_state()
    st["sessions"] = dict(sessions)
    return st


# ---------------------------------------------------------------- config

def test_v7_defaults_cooldown_300_and_meter_timing():
    mod = load_module()
    assert mod.DEFAULT_CONFIG["cooldown_s"] == 300
    assert mod.DEFAULT_CONFIG["timing"] == "meter"
    assert (mod.DEFAULT_CONFIG["meter_base_s"], mod.DEFAULT_CONFIG["meter_jitter_s"],
            mod.DEFAULT_CONFIG["meter_min_turn_s"]) == (300, 60, 20)
    cfg = mod.normalize_config({"timing": "classic", "meter_base_s": 120, "meter_jitter_s": -1})
    assert cfg["timing"] == "classic" and cfg["meter_base_s"] == 120 and cfg["meter_jitter_s"] == 60
    assert mod.normalize_config({"timing": "fast"})["timing"] == "meter"


def test_first_run_config_is_meter(env):
    say(env)
    cfg = json.loads((env["home"] / "config.json").read_text())
    assert "timing" not in cfg and cfg["meter_base_s"] == 300  # the README's keys only
    assert load_module().load_config()["timing"] == "meter"
    stop(env)


# ---------------------------------------------------------------- the meter (unit)

def test_union_counting_across_overlapping_sessions(env):
    """A busy 0-90, B busy 30-120: the meter is 120 s (the union), not 150 s (the sum)."""
    mod = load_module()
    st = st0(mod)
    t0 = 1_000_000.0
    st["sessions"]["A"] = busy(t0)
    mod.meter_tick(st, CFG, t0)
    mod.meter_tick(st, CFG, t0 + 30)
    st["sessions"]["B"] = busy(t0 + 30)
    mod.meter_tick(st, CFG, t0 + 30)
    mod.meter_tick(st, CFG, t0 + 60)
    mod.meter_tick(st, CFG, t0 + 90)
    st["sessions"]["A"]["token"] = None  # A's Stop
    mod.meter_tick(st, CFG, t0 + 90)
    assert st["meter_s"] == 90 and st["meter_on"] is True  # B still busy
    mod.meter_tick(st, CFG, t0 + 120)
    st["sessions"]["B"]["token"] = None
    mod.meter_tick(st, CFG, t0 + 120)
    assert st["meter_s"] == 120 and st["meter_on"] is False
    mod.meter_tick(st, CFG, t0 + 200)  # nothing busy: nothing added
    assert st["meter_s"] == 120


def test_a_long_gap_counts_at_most_the_cap(env):
    mod = load_module()
    st = st0(mod, A=busy(1000.0))
    mod.meter_tick(st, CFG, 1000.0)
    mod.meter_tick(st, CFG, 1000.0 + 3000)  # a closed laptop
    assert st["meter_s"] == mod.METER_GAP_CAP_S


def test_esc_and_stale_and_pause_stop_counting(env):
    mod = load_module()
    t0 = 5000.0
    for stop_it in ("esc", "idle", "pause", "lifetime"):
        st = st0(mod, A=busy(t0))
        mod.meter_tick(st, CFG, t0)
        mod.meter_tick(st, CFG, t0 + 10)
        if stop_it == "esc":
            st["sessions"]["A"]["turn"]["over_at"] = t0 + 10
        elif stop_it == "idle":
            st["sessions"]["A"]["idle"] = True
        elif stop_it == "pause":
            st["paused_until"] = t0 + 10_000
        mod.meter_tick(st, CFG, t0 + 10)
        if stop_it == "lifetime":
            mod.meter_tick(st, dict(CFG, max_lifetime_s=15), t0 + 20)  # 10 + 10 (counted up to this tick)
            mod.meter_tick(st, dict(CFG, max_lifetime_s=15), t0 + 40)
            assert st["meter_s"] == 20, stop_it
            continue
        mod.meter_tick(st, CFG, t0 + 50)
        assert st["meter_s"] == 10 and st["meter_on"] is False, stop_it


def test_an_open_ask_freezes_the_meter_and_a_dead_timers_ask_does_not(env):
    mod = load_module()
    t0 = 9000.0
    a = busy(t0)
    a["pending"] = [{"exercise": "squats", "reps": 15, "unit": "reps", "ts": t0, "status": "asked",
                     "id": "abcd1234", "show_until": t0 + 45}]
    st = st0(mod, A=a, B=busy(t0))
    mod.meter_tick(st, CFG, t0)
    assert st["meter_on"] is False  # asked, heartbeat live: frozen
    mod.meter_tick(st, CFG, t0 + 30)
    assert st["meter_s"] == 0
    mod.meter_tick(st, CFG, t0 + 50)  # the heartbeat ran out (the timer died): no longer open
    assert st["meter_on"] is True
    mod.meter_tick(st, CFG, t0 + 60)
    assert st["meter_s"] == 10
    # a question in its grace window (after Stop) also freezes it
    st["sessions"]["A"] = {"token": None, "grace": {"ask": dict(a["pending"][0]), "until": t0 + 200}}
    mod.meter_tick(st, CFG, t0 + 60)
    assert st["meter_on"] is False
    mod.meter_tick(st, CFG, t0 + 100)
    assert st["meter_s"] == 10


def test_jitter_bounds_and_floor():
    mod = load_module()
    rng = random.Random(7)
    ts = [mod.meter_new_target(CFG, rng) for _ in range(500)]
    assert all(240 <= t <= 360 for t in ts)
    assert min(ts) < 250 and max(ts) > 350  # both sides are used
    assert len(set(ts)) > 100
    assert mod.meter_new_target(dict(CFG, meter_base_s=10, meter_jitter_s=60), random.Random(1)) >= 0
    assert mod.meter_new_target(dict(CFG, meter_jitter_s=0), rng) == 300


def test_reset_draws_a_new_target_and_a_changed_config_redraws(env):
    mod = load_module()
    st = st0(mod)
    st["meter_s"] = 400
    mod.meter_reset(st, CFG, random.Random(3))
    assert st["meter_s"] == 0 and 240 <= st["meter_target_s"] <= 360
    st["meter_target_s"] = 300
    assert mod.meter_target(st, dict(CFG, meter_base_s=60, meter_jitter_s=10), random.Random(2)) <= 70


def test_ask_rule_needs_meter_full_and_turn_old_enough(env):
    mod = load_module()
    now = 50_000.0
    cfg = dict(CFG, meter_jitter_s=0)
    st = st0(mod, A=busy(now - 25))
    st["meter_target_s"] = 300
    st["meter_s"] = 299
    assert not mod.meter_should_ask(st, cfg, "A", st["sessions"]["A"], now)  # meter short
    st["meter_s"] = 300
    assert mod.meter_should_ask(st, cfg, "A", st["sessions"]["A"], now)
    st["sessions"]["A"]["turn"]["ts"] = now - 19  # the turn has run 19 s < 20 s
    assert not mod.meter_should_ask(st, cfg, "A", st["sessions"]["A"], now)
    st["sessions"]["A"]["turn"]["ts"] = now - 25
    st["sessions"]["A"]["turn"]["fired"] = True  # one set per turn
    assert not mod.meter_should_ask(st, cfg, "A", st["sessions"]["A"], now)
    st["sessions"]["A"]["turn"]["fired"] = False
    st["sessions"]["A"]["pending"] = [{"exercise": "squats", "reps": 15, "unit": "reps", "ts": now - 5,
                                       "status": "declined", "id": "abcd1234"}]
    assert not mod.meter_should_ask(st, cfg, "A", st["sessions"]["A"], now)  # asked (and skipped) this turn
    st["sessions"]["A"]["pending"] = []
    b = busy(now - 100)
    b["pending"] = [{"exercise": "push-ups", "reps": 10, "unit": "reps", "ts": now - 5, "status": "asked",
                     "id": "ffff0000", "show_until": now + 40}]
    st["sessions"]["B"] = b  # another session's question is up: never two open asks
    assert not mod.meter_should_ask(st, cfg, "A", st["sessions"]["A"], now)


def test_prefers_the_session_whose_band_is_drawing(env):
    mod = load_module()
    now = time.time()
    cfg = dict(CFG, meter_jitter_s=0, button="mod", statusline=True)
    beats = env["home"] / "mod-seen"
    beats.mkdir()
    (beats / "band").write_text("")
    st = st0(mod, plain=busy(now - 30), band=busy(now - 30))
    st["meter_s"], st["meter_target_s"] = 300, 300
    st["sessions"]["band"]["meter_poll_at"] = now - 1
    assert not mod.meter_should_ask(st, cfg, "plain", st["sessions"]["plain"], now)  # leaves it to the band
    assert mod.meter_should_ask(st, cfg, "band", st["sessions"]["band"], now)
    st["sessions"]["band"]["meter_poll_at"] = now - 60  # its timer is gone: the link session asks
    assert mod.meter_should_ask(st, cfg, "plain", st["sessions"]["plain"], now)
    st["sessions"]["band"]["meter_poll_at"] = now - 1
    st["sessions"]["band"]["turn"]["ts"] = now - 5  # the band's turn does not qualify yet: no waiting on it
    assert mod.meter_should_ask(st, cfg, "plain", st["sessions"]["plain"], now)
    st["sessions"]["band"]["turn"]["ts"] = now - 30
    assert mod.meter_should_ask(st, dict(cfg, button="link"), "plain", st["sessions"]["plain"], now)
    # public build: with no status line, a session whose band is not drawing never asks (it could not be seen)
    st["sessions"]["band"]["meter_poll_at"] = now - 60  # even with the band's timer gone
    assert not mod.meter_should_ask(st, dict(cfg, statusline=False), "plain", st["sessions"]["plain"], now)
    st["sessions"]["band"]["meter_poll_at"] = now - 1
    assert mod.meter_should_ask(st, dict(cfg, statusline=False), "band", st["sessions"]["band"], now)


# ---------------------------------------------------------------- the meter, end to end

def test_meter_mode_has_no_predictor_but_records_the_turn(env):
    setup_meter(env, predict=True)  # public build: turns are recorded only with "predict": true
    say(env)
    st = state(env)
    turn = st["sessions"]["s1"]["turn"]
    assert turn["band"] == "meter" and turn["p_long"] is None and st["decisions"] == []
    assert st["meter_on"] is True
    time.sleep(0.3)
    stop(env)
    st = state(env)
    recs = [json.loads(line) for line in (env["home"] / "turns.jsonl").read_text().splitlines()]
    assert len(recs) == 1 and recs[0]["band"] == "meter" and recs[0]["duration_s"] >= 0.25
    assert st["decisions"] == [] and (st.get("predict") or {}).get("new_since_train", 0) == 0
    assert st["meter_on"] is False and 0.25 <= st["meter_s"] <= 1.5


def test_meter_mode_with_the_predictor_off_keeps_no_turn_records(env):
    setup_meter(env, predict=False)
    say(env)
    time.sleep(0.3)
    stop(env)
    assert not (env["home"] / "turns.jsonl").exists()
    assert 0.25 <= state(env)["meter_s"] <= 1.5  # the meter still counted the turn


def test_classic_mode_is_unchanged(env):
    setup_meter(env, timing="classic", delay_s=30, cooldown_s=0)
    say(env)
    st = state(env)
    assert st["sessions"]["s1"]["turn"]["band"] == "flat" and len(st["decisions"]) == 1
    assert "meter_s" not in st and "meter_on" not in st
    stop(env)
    assert "meter_s" not in state(env)


def test_short_turns_never_ask_but_fill_the_meter(env):
    setup_meter(env, meter_base_s=0.8, meter_min_turn_s=2.0)
    for _ in range(2):
        say(env)
        time.sleep(0.6)
        stop(env)
    assert [r for r in log(env) if r.get("status") in ("done", "declined")] == []
    st = state(env)
    assert st["meter_s"] >= 1.0 and st["meter_on"] is False  # full, but no turn ran 2 s
    m = st["meter_s"]
    time.sleep(0.5)
    assert meter_s(env) == m  # nothing busy: it does not grow
    # the next turn that runs 2 s gets the set
    say(env)
    assert wait_for(lambda: asked(env), 6)
    p = asked(env)[0]
    st = state(env)
    assert st["sessions"]["s1"]["turn"]["fired_at_s"] >= 1.9
    assert st["meter_s"] < 0.5 and st["meter_target_s"] == 0.8  # reset at the ask, a new target (no jitter)
    stop(env)


def test_the_ask_waits_for_the_meter(env):
    setup_meter(env, meter_base_s=2.0, meter_min_turn_s=0.3)
    say(env)
    time.sleep(1.0)
    assert asked(env) == []  # the turn is old enough, the meter is not full
    assert wait_for(lambda: asked(env), 6)
    assert state(env)["sessions"]["s1"]["turn"]["fired_at_s"] >= 1.9
    stop(env)


def test_one_ask_per_turn_frozen_while_open_counts_again_after_done(env):
    setup_meter(env, meter_base_s=0.5, meter_min_turn_s=0.3)
    say(env)
    assert wait_for(lambda: asked(env), 6)
    time.sleep(0.8)
    st = state(env)
    assert st["meter_on"] is False and st["meter_s"] < 0.3  # frozen while the question is up
    assert cli(env, "done").returncode == 0
    assert len([r for r in log(env) if r.get("status") == "done"]) == 1
    assert wait_for(lambda: meter_s(env) > 0.7, 6)  # counting again, past the target
    time.sleep(0.6)
    assert len([p for p in pend(env) if p.get("status")]) == 1  # still one ask this turn
    stop(env)
    # the next turn asks at once (the meter is already full)
    say(env)
    assert wait_for(lambda: asked(env), 6)
    stop(env)


def test_skip_resets_and_the_next_ask_is_another_exercise(env):
    setup_meter(env, meter_base_s=0.5, meter_min_turn_s=0.3)
    say(env)
    assert wait_for(lambda: asked(env), 6)
    first = asked(env)[0]["exercise"]
    assert cli(env, "decline").returncode == 0
    assert wait_for(lambda: meter_s(env) > 0.6, 6)
    stop(env)
    say(env)
    assert wait_for(lambda: asked(env), 6)
    assert asked(env)[0]["exercise"] != first  # a set skipped rotates
    assert [r["status"] for r in log(env)] == ["declined"]  # nothing logged without done
    stop(env)


def test_esc_stops_the_meter(env):
    setup_meter(env)
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text("")
    say(env, transcript=str(tr))
    time.sleep(0.5)
    with open(tr, "a") as f:
        f.write(user(_now_stamp(), [{"type": "text", "text": "[Request interrupted by user]"}]) + "\n")
    assert wait_for(lambda: (state(env)["sessions"]["s1"].get("turn") or {}).get("over_at"), 5)
    st = state(env)
    assert st["meter_on"] is False
    m = st["meter_s"]
    assert 0.4 <= m <= 2.0
    time.sleep(0.6)
    assert meter_s(env) == m


def test_two_overlapping_sessions_count_once(env):
    setup_meter(env)
    say(env, sid="a")
    time.sleep(1.0)
    say(env, sid="b")
    time.sleep(1.0)
    stop(env, sid="a")
    time.sleep(1.0)
    st_b = state(env)  # b still busy
    stop(env, sid="b")
    st = state(env)
    ts_a = st["hists"]["a"]["prev_stop_ts"] - st["hists"]["a"]["prev_duration_s"]
    ts_b = st_b["sessions"]["b"]["turn"]["ts"]
    union = st["hists"]["b"]["prev_stop_ts"] - ts_a
    overlap = st["hists"]["a"]["prev_stop_ts"] - ts_b
    assert overlap > 0.9
    assert abs(st["meter_s"] - union) < 0.02, (st["meter_s"], union)  # the union, never the sum (+ overlap)


def test_a_tick_never_moves_the_clock_back(env):
    mod = load_module()
    st = st0(mod, A=busy(100.0))
    mod.meter_tick(st, CFG, 100.0)
    mod.meter_tick(st, CFG, 110.0)  # a timer tick
    mod.meter_tick(st, CFG, 105.0)  # a hook that read its clock before it got the lock
    mod.meter_tick(st, CFG, 120.0)
    assert st["meter_s"] == 20 and st["last_tick_ts"] == 120.0


# ---------------------------------------------------------------- what it says

def test_stats_config_and_status_show_the_meter(env):
    setup_meter(env, meter_base_s=300, meter_jitter_s=60)
    st = load_module().empty_state()
    st.update(meter_s=192.0, meter_target_s=300.0, meter_on=False, last_tick_ts=time.time())
    (env["home"] / "state.json").write_text(json.dumps(st))
    out = cli(env, "stats").stdout
    assert "Waiting meter: 3m 12s of ~5m" in out
    out = cli(env, "config").stdout
    assert 'timing = "meter"' in out and "meter_base_s = 300" in out and "waiting meter: 3m 12s of ~5m" in out
    assert cli(env, "done", "squats", "15").returncode == 0
    assert run(env, "status", "s1").stdout.endswith(" ▰▰▰▱▱")
    out = cli(env, "why").stdout
    assert '"timing" is "meter"' in out
    write_cfg(env, timing="classic")
    assert "meter" not in cli(env, "stats").stdout
    assert "▰" not in run(env, "status", "s1").stdout and "▱" not in run(env, "status", "s1").stdout


@pytest.mark.parametrize("words", ["idle", "away", "keyboard", "mouse"])
def test_meter_text_names_no_mechanism(env, words):
    mod = load_module()
    st = st0(mod)
    assert words not in mod.meter_line(st, CFG, 1.0)


# ---------------------------------------------------------------- fixer round: an ignored question, a dead timer

def test_an_ignored_question_expires_and_frees_the_other_sessions(env):
    mod = load_module()
    t0 = time.time()
    cfg = mod.normalize_config(dict(CFG, meter_jitter_s=0, meter_ask_ttl_s=180,
                                    exercises=[{"name": "squats", "min": 15, "max": 15, "unit": "reps"},
                                               {"name": "push-ups", "min": 10, "max": 10, "unit": "reps"}]))
    a = busy(t0 - 400)
    a["turn"]["fired"] = True
    a["pending"] = [{"exercise": "squats", "reps": 15, "unit": "reps", "ts": t0 - 179, "status": "asked",
                     "asked_at": t0 - 179, "id": "abcd1234", "show_until": t0 + 40}]
    a["listener"] = {"ask_id": "abcd1234", "alive_until": t0 + 30, "pid": 1}
    st = st0(mod, A=a, B=busy(t0 - 60))
    st["meter_s"], st["meter_target_s"] = 0.0, 300.0
    st["next_set"] = {"exercise": "squats", "reps": 15, "unit": "reps"}
    assert not mod.expire_meter_ask(st, cfg, "A", a, t0)  # 179 s: still up
    assert mod.any_ask_open(st, t0)
    assert mod.expire_meter_ask(st, cfg, "A", a, t0 + 1)  # 180 s: declined
    p = a["pending"][0]
    assert p["status"] == "declined" and p["expired"] is True and "listener" not in a
    assert not mod.any_ask_open(st, t0 + 1) and mod.open_ask(a) is None
    assert st["next_set"]["exercise"] == "push-ups"  # rotated away from the ignored set
    rows = log(env)
    assert [(r["status"], r.get("expired"), r["ask_id"]) for r in rows] == [("declined", True, "abcd1234")]
    assert not mod.meter_turn_qualifies(cfg, a, t0 + 1)  # A's turn keeps its one ask
    st["meter_s"] = 300.0
    assert mod.meter_should_ask(st, cfg, "B", st["sessions"]["B"], t0 + 1)  # the other session may ask now
    assert not mod.expire_meter_ask(st, mod.normalize_config(dict(CFG, meter_ask_ttl_s=0)), "A", a, t0 + 9999)


def test_the_grace_window_ends_with_the_questions_lifetime(env):
    mod = load_module()
    now = time.time()
    for asked_ago, timing, until in ((100, "meter", now + 80), (200, "meter", None), (200, "classic", now + 180)):
        cfg = mod.normalize_config(dict(CFG, timing=timing, ask_grace_s=180, meter_ask_ttl_s=180))
        s = busy(now - 300)
        s["pending"] = [{"exercise": "squats", "reps": 15, "unit": "reps", "ts": now - asked_ago,
                         "status": "asked", "asked_at": now - asked_ago, "id": "abcd1234", "show_until": now + 40}]
        s["listener"] = {"ask_id": "abcd1234", "alive_until": now + 30, "pid": os.getpid()}
        st = st0(mod, A=s)
        started = mod.start_grace(st, cfg, s, now, "A")
        if until is None:
            assert started is False and s["pending"][0]["status"] == "asked"  # Stop declines it as usual
        else:
            assert started is True and abs(s["grace"]["until"] - until) < 1e-6, (timing, asked_ago)


def test_an_ended_or_dead_timer_takes_its_turn_out_of_the_count(env):
    mod = load_module()
    t0 = 7000.0
    s = busy(t0)
    assert mod.turn_busy(s, t0 + 100, CFG)  # never polled yet (unit states, the first 5 s)
    s["meter_poll_at"] = t0 + 80
    assert mod.turn_busy(s, t0 + 100, CFG)
    assert not mod.turn_busy(s, t0 + 80 + mod.METER_TIMER_DEAD_S + 0.1, CFG)  # killed: no poll for 30 s
    s["meter_poll_at"] = t0 + 99
    s["nudging"] = False  # the timer ended (lifetime, an error)
    assert not mod.turn_busy(s, t0 + 100, CFG)
    st = st0(mod, A=s)
    mod.meter_tick(st, CFG, t0 + 100)
    mod.meter_tick(st, CFG, t0 + 150)
    assert st["meter_on"] is False and st["meter_s"] == 0


def test_an_unanswered_question_expires_and_another_session_asks(env):
    setup_meter(env, meter_base_s=0.5, meter_min_turn_s=0.3, meter_ask_ttl_s=1.0)
    say(env, sid="a")
    assert wait_for(lambda: asked(env, "a"), 6)
    asked_at = asked(env, "a")[0]["asked_at"]
    say(env, sid="b")
    time.sleep(0.6)
    assert asked(env, "b") == []  # a's question is up: nothing else is asked
    assert wait_for(lambda: asked(env, "b"), 8)  # a's expired after 1 s, the meter refilled, b asks
    b_at = asked(env, "b")[0]["asked_at"]
    assert b_at - asked_at >= 1.0 + 0.5 - 0.05
    pa = pend(env, "a")
    assert [(p["status"], p.get("expired")) for p in pa] == [("declined", True)]  # a: still one ask this turn
    assert [(r["status"], r.get("expired")) for r in log(env)] == [("declined", True)]
    stop(env, sid="a")
    assert [r["status"] for r in log(env)] == ["declined"]  # Stop does not decline it twice
    stop(env, sid="b")


def test_esc_after_a_pause_and_resume_still_stops_the_meter(env):
    setup_meter(env)
    tr = env["tmp"] / "sess.jsonl"
    tr.write_text("")
    say(env, transcript=str(tr))
    time.sleep(0.4)
    assert cli(env, "pause", "1").returncode == 0
    paused_at = time.time()
    st = state(env)  # the pause cli ticks the meter under the lock before it returns: it is off now
    assert st["meter_on"] is False
    m = st["meter_s"]
    # the timer still polls while paused: a poll that started after the pause returned (no wall-clock bound)
    assert wait_for(lambda: (state(env)["sessions"]["s1"].get("meter_poll_at") or 0) > paused_at, 10)
    time.sleep(0.4)
    assert meter_s(env) == m  # paused: the meter holds, through those polls
    assert state(env)["sessions"]["s1"].get("nudging") is not False
    assert cli(env, "resume").returncode == 0
    assert wait_for(lambda: meter_s(env) > m + 0.2, 10)  # counting again
    with open(tr, "a") as f:
        f.write(user(_now_stamp(), [{"type": "text", "text": "[Request interrupted by user]"}]) + "\n")
    assert wait_for(lambda: (state(env)["sessions"]["s1"].get("turn") or {}).get("over_at"), 10)
    st = state(env)
    assert st["meter_on"] is False
    m = st["meter_s"]
    # Another session's hooks tick the meter: the Esc'd turn adds nothing. That session's own say..stop
    # turn does count (the union of busy time) for as long as it is open, which is two subprocess spawns:
    # load-dependent, so it is bounded by the wall time the test held it open, not by a constant.
    for _ in range(2):
        t0 = time.time()
        say(env, sid="other", text="ok")
        stop(env, sid="other")
        held = time.time() - t0
        st = state(env)
        assert st["meter_on"] is False  # nothing busy: the Esc'd turn is not running
        assert st["meter_s"] - m <= held + 0.01, (st["meter_s"] - m, held)
        m = st["meter_s"]
        time.sleep(0.4)
        assert meter_s(env) == m  # idle: ticks in this gap (the Esc'd turn's timer, if alive) add nothing
