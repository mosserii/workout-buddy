"""Workout Buddy v2: spinner, tips, coach, achievements, level-ups, share card, tab title."""
import datetime as dt
import json
import random
import re
import time

from test_workout import (FAKE_SETTINGS, env, load_module, log, notes, pid_alive, prompt, run, state,  # noqa: F401
                          stop, wait_for, write_cfg)
import pytest

pytestmark = pytest.mark.legacy_modes  # written against the older modes (see conftest.py)

TIP_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def inject_pending(ctx, sets, sid="s1", extra_state=None):
    """A live turn with nudged sets waiting for Stop (no timer process)."""
    now = time.time()
    st = {"sessions": {sid: {"token": "t", "updated_at": now, "started_at": now,
                             "pending": [dict(exercise=e, reps=r, unit=u, ts=now) for e, r, u in sets]}}}
    st.update(extra_state or {})
    (ctx["home"] / "state.json").write_text(json.dumps(st))


def rec(day, hour=12, exercise="push-ups", reps=10, unit="reps", sid="x", minute=0):
    ts = dt.datetime.combine(day, dt.time(hour, minute)).timestamp()
    return {"ts": ts, "session_id": sid, "exercise": exercise, "reps": reps, "unit": unit, "status": "done"}


def write_log(ctx, recs):
    with open(ctx["home"] / "log.jsonl", "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def validate_tips_file(p):
    """The spinnerTipsOverride tipsFile format (settings reference): a JSON array of tip entries, or an
    object with a `tips` array; entry = string or {id (req, <=64 of [A-Za-z0-9._-]), text (req, one line
    <=500), cooldownSessions 0..1000, priority -10..10}; at most 200 tips; file <= 256 KB."""
    assert p.stat().st_size <= 256 * 1024
    data = json.loads(p.read_text())
    tips = data["tips"] if isinstance(data, dict) else data
    assert isinstance(tips, list) and 1 <= len(tips) <= 200
    ids = set()
    for t in tips:
        assert isinstance(t, dict) and set(t) <= {"id", "text", "cooldownSessions", "priority"}
        assert TIP_ID.match(t["id"]) and t["id"] not in ids
        ids.add(t["id"])
        assert isinstance(t["text"], str) and 0 < len(t["text"]) <= 100
        assert "\n" not in t["text"] and not re.search(r"[\x00-\x1f\x7f]", t["text"])
        assert isinstance(t["cooldownSessions"], int) and 0 <= t["cooldownSessions"] <= 1000
        assert isinstance(t["priority"], int) and -10 <= t["priority"] <= 10
    return tips


# ---------------------------------------------------------------- spinner + tips

def task(p):
    return f"{p['reps']}{'s' if p['unit'] == 'seconds' else ''} {p['exercise']}"


def test_spinner_config_leaves_spinner_verbs_to_setup(env):
    # v3: spinnerVerbs is managed live (present only while a set is due), so the JSON to merge by hand
    # is the tips part only; the next set is never printed before it is due.
    write_cfg(env)
    r = run(env, "spinner-config")
    assert r.returncode == 0
    cfg = json.loads(r.stdout)
    assert set(cfg) == {"spinnerTipsEnabled", "spinnerTipsOverride"}
    assert task(state(env)["next_set"]) not in r.stdout
    assert json.loads(env["settings"].read_text()) == FAKE_SETTINGS  # spinner-config only prints


def test_spinner_config_shape_and_verbs(env):
    # v4: spinner-config prints the tips only (spinnerVerbs is the add-on's with "task"/"progress", and
    # absent with "off"); the joke verbs are still built by spinner_verbs(), checked here directly.
    write_cfg(env, spinner_task=False)
    r = run(env, "spinner-config")
    assert r.returncode == 0
    cfg = json.loads(r.stdout)
    assert set(cfg) == {"spinnerTipsEnabled", "spinnerTipsOverride"}
    mod = load_module()
    verbs = mod.spinner_verbs(mod.load_config())
    assert 35 <= len(verbs) <= 60
    assert len({v.lower() for v in verbs}) == len(verbs)
    for v in verbs:
        assert isinstance(v, str) and 0 < len(v) <= 45, v
        assert not v.endswith(("…", ".")), v
        assert v == v.strip()
    joined = " ".join(verbs)
    for word in ("push-ups", "Squatting", "Planking", "Lunging", "Jumping jacks"):
        assert word in joined
    assert cfg["spinnerTipsEnabled"] is True
    assert cfg["spinnerTipsOverride"] == {"tipsFile": str(env["home"] / "tips.json"), "label": "Workout Buddy",
                                          "excludeDefault": True}
    assert len(cfg["spinnerTipsOverride"]["label"]) <= 40
    validate_tips_file(env["home"] / "tips.json")  # created so excludeDefault never empties the spinner


def test_spinner_verbs_follow_configured_exercises(env):
    ex = [{"name": "burpees", "min": 5, "max": 10, "unit": "reps"}]
    write_cfg(env, exercises=ex, spinner_task=False)
    mod = load_module()
    verbs = mod.spinner_verbs(mod.load_config())  # v4: no longer printed by spinner-config
    assert "Doing burpees between diffs" in verbs
    assert not any("Planking" in v for v in verbs)
    write_cfg(env, exercises=ex)
    assert "spinnerVerbs" not in json.loads(run(env, "spinner-config").stdout)  # v3: managed live


def test_cli_setup_off_leaves_a_key_it_never_managed(env):
    # public build: "off" never touches a spinnerVerbs that Workout Buddy did not set itself
    write_cfg(env, spinner_task=False)
    out = run(env, "cli", "setup", "apply", "--yes").stdout
    first, rest = out.split("\n", 1)
    assert "settings.json" in first and "never set spinnerVerbs there" in first
    assert "spinnerVerbs" not in json.loads(rest.split("\n", 1)[1])
    assert json.loads(env["settings"].read_text()) == FAKE_SETTINGS


def test_tips_created_on_first_prompt_and_valid(env):
    write_cfg(env, delay_s=30)
    assert not (env["home"] / "tips.json").exists()
    r = prompt(env)
    assert r.stdout == "" and r.returncode == 0
    tips = validate_tips_file(env["home"] / "tips.json")
    ids = {t["id"] for t in tips}
    assert {"wb-streak", "wb-week", "wb-today", "wb-form-push-ups"} <= ids
    assert "wb-next" not in ids  # v3: tips show on every turn; the set only once due
    stop(env)


def test_tips_refresh_on_stop_and_cli(env):
    write_cfg(env)
    inject_pending(env, [("squats", 20, "reps")])
    stop(env)
    tips = {t["id"]: t["text"] for t in validate_tips_file(env["home"] / "tips.json")}
    assert tips["wb-week"].startswith("20 reps this week · 20 all-time")
    assert tips["wb-today"].startswith("Today: 1 set · yesterday: 0")
    run(env, "cli", "done", "push-ups", "15")
    tips = {t["id"]: t["text"] for t in validate_tips_file(env["home"] / "tips.json")}
    assert tips["wb-week"].startswith("35 reps this week")
    assert "🔥" in tips["wb-streak"]
    assert tips["wb-form-push-ups"].startswith("Push-ups: tight core")
    run(env, "cli", "undo")
    tips = {t["id"]: t["text"] for t in validate_tips_file(env["home"] / "tips.json")}
    assert tips["wb-week"].startswith("20 reps this week")


def test_tips_next_achievement_progress(env):
    mod = load_module()
    cfg = mod.normalize_config({})
    today = dt.date.today()
    lg = [rec(today, reps=412)]
    st = mod.empty_state()
    mod.unlock_achievements(st, lg, cfg, today=today)
    texts = {t["id"]: t["text"] for t in mod.build_tips(cfg, st, lg, today)}
    assert texts["wb-next-achievement"] == "88 more reps to 🏅 Rep Machine II (500)"


# ---------------------------------------------------------------- coach

def test_coach_pools_sizes_and_templates():
    mod = load_module()
    names = [e["name"] for e in mod.DEFAULT_EXERCISES]
    hype = sum(len(v) for v in mod.COACH["hype"]["fire"].values())
    assert hype >= 30
    for tone in ("chill", "drill-sergeant"):
        assert sum(len(v) for v in mod.COACH[tone]["fire"].values()) >= 12
        assert len(mod.COACH[tone]["fire"]["generic"]) >= 12
    p = {"exercise": "plank", "reps": 40, "unit": "seconds"}
    for tone, pack in mod.COACH.items():
        assert set(pack) == {"fire"}  # no stop / title pools: nothing cheers on screen
        for theme, lines in pack["fire"].items():
            assert theme in mod.THEMES
            for line in lines:
                assert "{set}" in line or "{SET}" in line or "{reps}" in line
                out = mod.render_fire(line, p)
                assert "40s plank" in out or "40" in out
                # never name an exercise literally: the set does it (keeps nudges unambiguous)
                assert not any(n in line.lower() for n in names), line


def test_all_three_tones_load(env):
    mod = load_module()
    for tone in ("hype", "chill", "drill-sergeant"):
        cfg = mod.normalize_config({"coach": tone})
        assert cfg["coach"] == tone
        assert mod.coach_pack(cfg) is mod.COACH[tone]
        write_cfg(env, coach=tone, cooldown_s=9999)
        r = run(env, "cli", "test")
        n = notes(env, "test")[-1]
        assert n["body"] == task(state(env)["next_set"])  # the task is the body
        sub = n["subtitle"]
        pool = [l for ls in mod.COACH[tone]["fire"].values() for l in ls]
        assert any(sub == mod.render_fire(l, _parse_set(mod, sub, l)) for l in pool if _parse_set(mod, sub, l))
        assert "Sent a test notification" in r.stdout
        assert run(env, "cli", "config").stdout.count(f'coach = "{tone}"') == 1
    assert mod.normalize_config({"coach": "yoga-guru"})["coach"] == "hype"


def _parse_set(mod, body, tmpl):
    """Recover the set a body was rendered from, if it came from tmpl."""
    upper = "{SET}" in tmpl
    pre, _, post = tmpl.partition("{SET}" if upper else "{set}")
    if not (body.startswith(pre) and body.endswith(post)):
        return None
    what = body[len(pre):len(body) - len(post)] if post else body[len(pre):]
    m = re.match(r"^(\d+)([sS]?) (.+)$", what)
    if not m:
        return None
    ex = m.group(3).lower() if upper else m.group(3)
    return {"exercise": ex, "reps": int(m.group(1)), "unit": "seconds" if m.group(2) else "reps"}


def test_coach_rotation_never_repeats():
    mod = load_module()
    rng = random.Random(7)
    for tone in mod.COACH_TONES:
        cfg = mod.normalize_config({"coach": tone})
        st = mod.empty_state()
        for kind in ("fire",):
            for theme in mod.THEMES:
                prev = None
                for _ in range(200):
                    line = mod.coach_pick(st, cfg, kind, theme, rng)
                    assert line != prev
                    prev = line
                assert st["coach_last"][kind] == prev


def test_coach_rotation_persists_across_processes(env):
    write_cfg(env, cooldown_s=9999)
    seen = []
    for _ in range(12):
        run(env, "cli", "test")
        seen.append(state(env)["coach_last"]["fire"])
    assert all(a != b for a, b in zip(seen, seen[1:]))


def test_theme_classification():
    mod = load_module()
    cases = {
        "add pytest coverage for the parser": "tests",
        "the build crashes with a traceback, please fix": "bug",
        "refactor this module and rename the helpers": "refactor",
        "deploy to prod and ship the release": "deploy",
        "review my PR and leave feedback": "review",
        "update the README docs": "docs",
        "what's the weather like": "generic",
        "": "generic",
        None: "generic",
    }
    for text, want in cases.items():
        assert mod.classify_prompt(text) == want, text
    cfg = mod.normalize_config({})
    assert mod.fire_pool(cfg, "tests") == mod.COACH["hype"]["fire"]["tests"]
    assert mod.fire_pool(cfg, "generic") == mod.COACH["hype"]["fire"]["generic"]
    chill = mod.normalize_config({"coach": "chill"})
    assert set(mod.COACH["chill"]["fire"]["tests"]) <= set(mod.fire_pool(chill, "tests"))
    assert len(mod.fire_pool(chill, "tests")) > 1
    # a theme with a single line still falls back to generic + that line
    tiny = {"fire": {"generic": ["a {set}", "b {set}"], "tests": ["t {set}"]}}
    orig = mod.COACH["hype"]
    try:
        mod.COACH["hype"] = tiny
        assert mod.fire_pool(cfg, "tests") == ["a {set}", "b {set}", "t {set}"]
    finally:
        mod.COACH["hype"] = orig


def test_prompt_theme_stored_but_text_never_persisted(env):
    write_cfg(env, delay_s=0.2, repeat_s=10, cooldown_s=0)
    marker = "ZQXMARKER981"
    payload = {"session_id": "s1", "transcript_path": "", "cwd": "/tmp",
               "prompt": f"please add pytest coverage for the {marker} parser"}
    r = run(env, "hook-prompt", stdin=json.dumps(payload))
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert state(env)["sessions"]["s1"]["theme"] == "tests"
    assert wait_for(lambda: len(notes(env, "fire")) == 1)
    sub = notes(env, "fire")[0]["subtitle"]
    mod = load_module()
    assert any(_parse_set(mod, sub, l) for l in mod.COACH["hype"]["fire"]["tests"])  # themed line used
    stop(env)
    for f in env["home"].rglob("*"):
        if f.is_file():
            assert marker not in f.read_text(errors="ignore"), f
            assert "parser" not in f.read_text(errors="ignore"), f


# ---------------------------------------------------------------- achievements

CFG = None


def _ach(mod, lg, today, st=None):
    cfg = mod.normalize_config({})
    st = st if st is not None else mod.empty_state()
    return mod.unlock_achievements(st, lg, cfg, now=1000.0, today=today), st


def test_achievement_first_blood_and_rep_tiers():
    mod = load_module()
    today = dt.date(2026, 3, 10)
    assert _ach(mod, [], today)[0] == []
    new, _ = _ach(mod, [rec(today, reps=10)], today)
    assert "first_blood" in new and "reps_100" not in new
    new, _ = _ach(mod, [rec(today, reps=100)], today)
    assert {"first_blood", "reps_100"} <= set(new) and "reps_500" not in new
    new, _ = _ach(mod, [rec(today, reps=5000)], today)
    assert {"reps_100", "reps_500", "reps_1000", "reps_5000"} <= set(new)
    # seconds never count as reps; skipped sets never count
    new, _ = _ach(mod, [rec(today, reps=999, unit="seconds", exercise="plank")], today)
    assert "reps_100" not in new
    skipped = dict(rec(today, reps=500), status="skipped")
    assert _ach(mod, [skipped], today)[0] == []


def test_achievement_streaks():
    mod = load_module()
    today = dt.date(2026, 3, 10)
    days = lambda n: [rec(today - dt.timedelta(days=i)) for i in range(n)]
    assert "streak_3" not in _ach(mod, days(2), today)[0]
    new = _ach(mod, days(3), today)[0]
    assert "streak_3" in new and "streak_7" not in new
    new = _ach(mod, days(7), today)[0]
    assert {"streak_3", "streak_7"} <= set(new) and "streak_30" not in new
    assert "streak_30" in _ach(mod, days(30), today)[0]


def test_achievement_marathon():
    mod = load_module()
    today = dt.date(2026, 3, 10)
    turn = [rec(today, sid="sess") for _ in range(5)]  # one Stop = same session + ts
    assert "marathon" in _ach(mod, turn, today)[0]
    assert "marathon" not in _ach(mod, turn[:4], today)[0]
    manual = [rec(today, sid="manual") for _ in range(6)]
    assert "marathon" not in _ach(mod, manual, today)[0]
    spread = [rec(today, sid="sess", minute=i) for i in range(5)]  # five different turns
    assert "marathon" not in _ach(mod, spread, today)[0]


def test_achievement_night_owl_and_early_bird():
    mod = load_module()
    today = dt.date(2026, 3, 10)
    for hour, minute, night, early in [(22, 0, True, False), (23, 59, True, False), (3, 59, True, False),
                                       (4, 0, False, False), (5, 0, False, True), (7, 59, False, True),
                                       (8, 0, False, False), (12, 0, False, False)]:
        new = _ach(mod, [rec(today, hour=hour, minute=minute)], today)[0]
        assert ("night_owl" in new) is night, (hour, minute)
        assert ("early_bird" in new) is early, (hour, minute)


def test_achievement_variety_pack():
    mod = load_module()
    today = dt.date(2026, 3, 10)
    names = [e["name"] for e in mod.DEFAULT_EXERCISES]
    all_ex = [rec(today - dt.timedelta(days=i % 7), exercise=n) for i, n in enumerate(names)]
    assert "variety_pack" in _ach(mod, all_ex, today)[0]
    assert "variety_pack" not in _ach(mod, all_ex[:-1], today)[0]
    stale = all_ex[:-1] + [rec(today - dt.timedelta(days=8), exercise=names[-1])]
    assert "variety_pack" not in _ach(mod, stale, today)[0]


def test_achievement_plank_master():
    mod = load_module()
    today = dt.date(2026, 3, 10)
    same_day = [rec(today, exercise="plank", reps=200, unit="seconds"),
                rec(today, hour=13, exercise="wall sit", reps=100, unit="seconds")]
    assert "plank_master" in _ach(mod, same_day, today)[0]
    split = [rec(today, exercise="plank", reps=200, unit="seconds"),
             rec(today - dt.timedelta(days=1), exercise="plank", reps=200, unit="seconds")]
    assert "plank_master" not in _ach(mod, split, today)[0]
    reps_only = [rec(today, reps=400)]
    assert "plank_master" not in _ach(mod, reps_only, today)[0]


def test_achievements_unlock_once_with_timestamp():
    mod = load_module()
    today = dt.date(2026, 3, 10)
    new, st = _ach(mod, [rec(today)], today)
    assert new == ["first_blood"] and st["achievements"]["first_blood"]["at"] == 1000.0
    cfg = mod.normalize_config({})
    assert mod.unlock_achievements(st, [rec(today)], cfg, now=2000.0, today=today) == []
    assert st["achievements"]["first_blood"]["at"] == 1000.0


def test_stop_announces_achievement_and_cli_lists(env):
    write_cfg(env)
    inject_pending(env, [("squats", 20, "reps")])
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert msg.startswith("Logged 20 squats · ")
    # Night Owl (22:00-04:00) and Early Bird (05:00-08:00) depend on the local hour of the logged set,
    # which workout.py takes from the wall clock; expect them exactly when that hour is in their window.
    h = dt.datetime.fromtimestamp(log(env)[0]["ts"]).hour
    timed = {"Night Owl": ("a set between 22:00 and 04:00", h >= 22 or h < 4),
             "Early Bird": ("a set between 05:00 and 08:00", 5 <= h < 8)}
    want = ["First Blood (first set logged)"] + [f"{n} ({d})" for n, (d, on) in timed.items() if on]
    lines = msg.split("\n")
    assert lines[1:1 + len(want)] == [f"🏆 Unlocked: {w}" for w in want], (h, msg)
    ach = notes(env, "achievement")
    assert len(ach) == 1 and "First Blood" in ach[0]["body"]
    assert set(state(env)["achievements"]) == {"first_blood"} | {
        i for i, n in (("night_owl", "Night Owl"), ("early_bird", "Early Bird")) if timed[n][1]}
    out = run(env, "cli", "achievements").stdout
    assert f"{len(want)}/13 unlocked" in out and "✅ First Blood" in out, (h, out)
    assert "🔒 Rep Machine I — 100 reps · 20/100 reps" in out
    for n, (_, on) in timed.items():
        assert f"{'✅' if on else '🔒'} {n}" in out, (h, n, out)
    # a second set does not re-announce it
    inject_pending(env, [("lunges", 12, "reps")], extra_state={"achievements": state(env)["achievements"]})
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert "First Blood" not in msg


# ---------------------------------------------------------------- level-ups

def test_multiplier_math_and_cap():
    mod = load_module()
    expect = {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.1, 5: 1.1, 6: 1.2, 9: 1.3, 12: 1.4, 15: 1.5, 18: 1.5, 300: 1.5}
    for streak, m in expect.items():
        assert mod.multiplier(streak) == m, streak
    assert mod.multiplier(30, enabled=False) == 1.0
    assert [mod.level_for(s) for s in (0, 3, 6, 15, 99)] == [1, 2, 3, 6, 6]
    assert mod.level_for(15, enabled=False) == 1
    assert mod.fmt_level(3, 1.2) == "Level 3 · ×1.2"
    cfg = mod.normalize_config({"exercises": [{"name": "push-ups", "min": 10, "max": 20, "unit": "reps"}]})
    rng = random.Random(1)
    reps = [mod.pick_exercise(cfg, rng=rng, mult=1.5)["reps"] for _ in range(300)]
    assert min(reps) >= 15 and max(reps) <= 30 and max(reps) > 20
    assert mod.normalize_config({"progression": False})["progression"] is False
    assert mod.current_mult({"progression": False}, [rec(dt.date.today() - dt.timedelta(days=i))
                                                      for i in range(20)]) == 1.0


def test_level_shown_in_stats_and_level_up_in_stop(env):
    write_cfg(env)
    today = dt.date.today()
    write_log(env, [rec(today - dt.timedelta(days=i)) for i in (1, 2)])  # 2-day streak: Level 1
    inject_pending(env, [("squats", 20, "reps")], extra_state={"level": 1})
    msg = json.loads(stop(env).stdout)["systemMessage"]  # now 3 days: Level 2
    assert msg.split("\n")[-1] == "Level 2 · ×1.1 reps"
    assert state(env)["level"] == 2
    assert "Level 2 · ×1.1" in run(env, "cli", "stats").stdout
    inject_pending(env, [("lunges", 12, "reps")], extra_state={"level": 2, "achievements": state(env)["achievements"]})
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert "Level" not in msg  # unchanged level, no noise


# ---------------------------------------------------------------- share card

def test_display_width_helper():
    mod = load_module()
    assert mod.display_width("abc") == 3
    assert mod.display_width("💪") == 2 and mod.display_width("🔥🏆") == 4
    assert mod.display_width("⏱️") == 1  # narrow base + VS16 (zero width)
    assert mod.display_width("╔═█░×–·") == 7
    assert mod.display_width(mod.fit("💪 hello", 5)) == 5
    assert mod.display_width(mod.fit("x" * 80, 48)) == 48


def _assert_card(mod, card):
    lines = card.split("\n")
    widths = {mod.display_width(l) for l in lines}
    assert len(widths) == 1, [(mod.display_width(l), l) for l in lines]
    assert widths.pop() <= 52
    assert lines[0].startswith("╔") and lines[-1].startswith("╚")
    return lines


def test_share_card_alignment_full_and_empty(env):
    mod = load_module()
    cfg = mod.normalize_config({})
    today = dt.date.today()
    lg = []
    for i in range(7):
        d = today - dt.timedelta(days=i)
        lg += [rec(d, exercise="push-ups", reps=15), rec(d, hour=13, exercise="plank", reps=45, unit="seconds"),
               rec(d, hour=14, exercise="a very very long custom exercise name 💪", reps=12345)]
    st = mod.empty_state()
    mod.unlock_achievements(st, lg, cfg, today=today)
    card = mod.share_card(lg, cfg, st, today)
    lines = _assert_card(mod, card)
    text = "\n".join(lines)
    for want in ("WORKOUT BUDDY · for Claude Code", "🔥 7-day streak", "█", "Best day:", "🏆 Latest:",
                 "while Claude coded, I trained 💪", "#ClaudeCode", "21 sets", "5m 15s held"):
        assert want in text, want
    empty = mod.share_card([], cfg, mod.empty_state(), today)
    assert "No sets yet" in _assert_card(mod, empty)[7]


def test_share_cli_and_text(env):
    write_cfg(env)
    run(env, "cli", "done", "push-ups", "20")
    run(env, "cli", "done", "plank", "60")
    mod = load_module()
    card = run(env, "cli", "share").stdout.rstrip("\n")
    _assert_card(mod, card)
    txt = run(env, "cli", "share", "--text").stdout.strip()
    assert 2 <= len(txt.splitlines()) <= 3
    assert len(re.findall(r"#\w+", txt)) <= 2 and "#ClaudeCode" in txt
    assert "20 reps" in txt and "1m of holds" in txt


# ---------------------------------------------------------------- tab title

def test_terminal_sequence_only_when_set_logged(env):
    write_cfg(env)
    inject_pending(env, [("push-ups", 15, "reps"), ("plank", 40, "seconds")])
    out = json.loads(stop(env).stdout)
    seq = out["terminalSequence"]
    assert seq == "\x1b]2;✅ +15 push-ups +40s plank · 🔥1d\x07"
    # no set this turn: Stop prints nothing at all
    inject_pending(env, [])
    assert stop(env).stdout == ""
    # hook-prompt never emits anything, terminalSequence included
    r = prompt(env)
    assert r.stdout == ""
    stop(env)
    # disabled by config
    write_cfg(env, tab_title=False)
    inject_pending(env, [("squats", 20, "reps")])
    out = json.loads(stop(env).stdout)
    assert "terminalSequence" not in out and out["systemMessage"].startswith("Logged ")


def test_terminal_sequence_strips_control_chars():
    mod = load_module()
    seq = mod.tab_title_sequence([{"exercise": "evil\x1b]0;x\x07name", "reps": 3, "unit": "reps"}], 0)
    body = seq[len("\x1b]2;"):-1]
    assert seq.startswith("\x1b]2;") and seq.endswith("\x07")
    assert not re.search(r"[\x00-\x1f\x7f-\x9f]", body)


# ---------------------------------------------------------------- review-fix regressions

# Praise from the old stop pools (v2 coach lines); none of it may reach the screen any more.
PRAISE = ("Legend", "champ", "🎉", "Gains committed", "Merge approved", "You shipped", "and so are you",
          "Nice work", "in the bank", "easy and steady", "Lovely", "Acceptable", "Barely", "Dismissed",
          "Tomorrow, more", "Don't get comfortable", "Level up!", "paying off", "Rebuild it", "earned by",
          "Set complete", "Reps shipped", "Back to the desk", "Well moved", "AT EASE",
          "Claude's done —", "💪 Logged", "Achievement unlocked!", "Overachiever", "misses you",
          "Absolute unit", "Honesty")


def test_stop_system_message_is_one_plain_line(env):
    """The user does not watch the screen: Stop says what was logged, nothing else."""
    mod = load_module()
    for tone in mod.COACH_TONES:
        write_cfg(env, coach=tone)
        (env["home"] / "log.jsonl").unlink(missing_ok=True)
        inject_pending(env, [("squats", 24, "reps"), ("plank", 34, "seconds")],
                       extra_state={"achievements": {a[0]: {"at": 1} for a in mod.ACHIEVEMENTS}, "level": 1})
        msg = json.loads(stop(env).stdout)["systemMessage"]
        assert msg == ("Logged 24 squats + 34s plank · today 2 sets / 24 reps / 34s · 🔥1d · "
                       "/workout undo to take it back"), (tone, msg)
        # v3: the next set is never shown before it is due. The exact match above already proves it; a
        # substring check on the random next set flaked whenever it was "24 squats" again (~4% a run).
        assert "next" not in msg.lower()
        assert not any(w.lower() in msg.lower() for w in PRAISE)
        done = notes(env, "done")[-1]
        assert done["subtitle"] == "Claude's done" and done["body"] == "Logged 24 squats + 34s plank"


def test_no_praise_anywhere_on_stop_with_unlocks_and_level(env):
    write_cfg(env)
    today = dt.date.today()
    write_log(env, [rec(today - dt.timedelta(days=i), reps=50) for i in (1, 2)])
    inject_pending(env, [("squats", 20, "reps")], extra_state={"level": 1})
    msg = json.loads(stop(env).stdout)["systemMessage"]
    lines = msg.split("\n")
    assert lines[0] == "Logged 20 squats · today 1 set / 20 reps · 🔥3d · /workout undo to take it back"
    assert "🏆 Unlocked: Rep Machine I (100 reps)" in lines and "🏆 Unlocked: Spark (3-day streak)" in lines
    assert lines[-1] == "Level 2 · ×1.1 reps"
    assert not any(w.lower() in msg.lower() for w in PRAISE)
    for n in notes(env):
        assert not any(w.lower() in (n["body"] + n["subtitle"]).lower() for w in PRAISE), n
    tips = " ".join(t["text"] for t in validate_tips_file(env["home"] / "tips.json"))
    assert not any(w.lower() in tips.lower() for w in PRAISE + ("don't break it", "ahead", "Today's the day"))


def test_timed_sets_read_right_in_every_line():
    """Finding 3: ALL-CAPS lines use {SET}; no line assumes a countable set ("them")."""
    mod = load_module()
    plank = {"exercise": "plank", "reps": 40, "unit": "seconds"}
    assert mod.render_fire("I WANT {SET}.", plank) == "I WANT 40S PLANK."
    for pack in mod.COACH.values():
        for lines in pack["fire"].values():
            for line in lines:
                out = mod.render_fire(line, plank)
                assert not re.search(r"\bthem\b", out, re.I), line
                assert "then one more" not in out and "Cover 40s" not in out
                letters = re.sub(r"\{\w+\}", "", line)
                if sum(c.isupper() for c in letters) > 0.8 * max(1, sum(c.isalpha() for c in letters)):
                    assert "{set}" not in line, line  # caps line must shout the set too
    for gone in ("READ-ME-A-REP", "Rename yourself", "Ctrl+Sweat", "10x biceps", "Docs pay off later"):
        assert not any(gone in l for p in mod.COACH.values() for ls in p["fire"].values() for l in ls), gone


def test_every_tone_has_three_lines_per_theme():
    """Finding 5: themes matter in every tone (fire_pool uses the themed pool, not generic + 1)."""
    mod = load_module()
    for tone in mod.COACH_TONES:
        cfg = mod.normalize_config({"coach": tone})
        for theme in mod.THEMES:
            assert len(mod.COACH[tone]["fire"].get(theme, [])) >= 3, (tone, theme)
            if theme != "generic":
                assert mod.fire_pool(cfg, theme) == mod.COACH[tone]["fire"][theme]


def test_spinner_verbs_no_stem_padding():
    """Finding 6: duplicate-stem and filler verbs were swapped out."""
    mod = load_module()
    verbs = mod.spinner_verbs(mod.normalize_config({}))
    for gone in ("Squatting the bugs", "Lunging at the backlog", "Stretching before the merge",
                 "Spotting off-by-one errors", "Pushing up the PR", "Gains-driven developing",
                 "Doing reps on the repo", "Flexing"):
        assert gone not in verbs, gone
    for want in ("Squat-merging", "Rebasing my glutes", "Burpee-driven development", "git push-up --force"):
        assert want in verbs, want
    for stem in ("spotting", "stretching", "squatting"):
        assert sum(v.lower().startswith(stem) for v in verbs) == 1, stem


def test_share_bar_label_names_the_set_count(env):
    """Finding 7: the bar is scaled by sets, so the label leads with the set count."""
    mod = load_module()
    cfg = mod.normalize_config({})
    today = dt.date.today()
    lg = [rec(today, hour=9, minute=i, exercise="squats", reps=28) for i in range(6)]
    lg += [rec(today, hour=10, minute=i, exercise="lunges", reps=37) for i in range(5)]
    card = mod.share_card(lg, cfg, mod.empty_state(), today)
    _assert_card(mod, card)
    sq = next(l for l in card.split("\n") if "squats" in l)
    lu = next(l for l in card.split("\n") if "lunges" in l)
    assert "6× · 168" in sq and "5× · 185" in lu
    assert sq.count("█") > lu.count("█")


def test_no_next_tip_even_with_a_next_set(env):
    """v3: tips show under the spinner on every turn, so no tip names the next set (it is decided ahead,
    shown only once due)."""
    mod = load_module()
    cfg = mod.normalize_config({})
    today = dt.date.today()
    st = mod.empty_state()
    st["next_set"] = {"exercise": "plank", "reps": 45, "unit": "seconds"}
    tips = mod.build_tips(cfg, st, [], today)
    assert "wb-next" not in {t["id"] for t in tips}
    assert not any("45s plank" in t["text"] or t["text"].startswith("Next") for t in tips)


def test_tip_ids_stay_unique_for_non_latin_names(env):
    """Finding 10: two Hebrew exercise names must not collapse to one id."""
    mod = load_module()
    a, b = mod._tip_id("form-סקוואט"), mod._tip_id("form-שכיבות סמיכה")
    assert a != b and TIP_ID.match(a) and TIP_ID.match(b)
    assert mod._tip_id("form-push-ups") == "wb-form-push-ups"  # ASCII ids unchanged
    assert TIP_ID.match(mod._tip_id("x" * 200 + "ש"))
    write_cfg(env, exercises=[{"name": "סקוואט", "min": 10, "max": 20},
                              {"name": "שכיבות סמיכה", "min": 10, "max": 20}])
    run(env, "cli", "done", "סקוואט", "12")
    validate_tips_file(env["home"] / "tips.json")


# ---------------------------------------------------------------- v3: banners off, task on screen

def test_new_bool_keys_validate():
    mod = load_module()
    d = mod.normalize_config({})
    # v4: "spinner_task" is a v3.2 key mapped onto "spinner" (off by default), no longer a bool key
    # public build: banners and the status line are off by default
    assert d["notification"] is False and d["statusline"] is False and d["spinner"] == "off"
    off = mod.normalize_config({"notification": False, "statusline": False, "spinner_task": False})
    assert (off["notification"], off["statusline"], off["spinner"]) == (False, False, "off")
    on = mod.normalize_config({"notification": True, "statusline": True})
    assert (on["notification"], on["statusline"]) == (True, True)
    bad = mod.normalize_config({"notification": "no", "statusline": 0, "spinner_task": None})
    assert (bad["notification"], bad["statusline"], bad["spinner"]) == (False, False, "off")
    for k in ("notification", "statusline"):
        assert k in mod.BOOL_KEYS


def test_notification_off_sends_nothing_but_timer_still_records(env):
    """Banners off: zero osascript/say calls for nudge, done and achievement; the set still shows
    as NOW in the status line and Stop still logs it."""
    write_cfg(env, notification=False, say=True, repeat_s=0.1, max_lifetime_s=3)
    prompt(env)
    nxt = state(env)["next_set"]
    assert wait_for(lambda: run(env, "status", "s1").stdout == f"🏋 NOW: {task(nxt)}")
    assert len(state(env)["sessions"]["s1"]["pending"]) >= 1
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert msg.startswith(f"Logged {task(nxt)}")
    assert "🏆 Unlocked: First Blood (first set logged)" in msg  # unlocked, only not bannered
    assert log(env) and all(e["exercise"] == nxt["exercise"] for e in log(env))
    run(env, "cli", "done", "squats", "20")
    assert not env["notify"].exists() or notes(env) == []


def test_cli_test_with_banners_off_says_what_would_show(env):
    write_cfg(env, notification=False)
    out = run(env, "cli", "test").stdout
    assert "Banners are off" in out and "/workout config" in out
    assert task(state(env)["next_set"]) in out
    assert notes(env) == []
    cfg_out = run(env, "cli", "config").stdout
    assert "notification = false" in cfg_out and "/workout setup banners --yes" in cfg_out


def test_statusline_off_prints_nothing(env):
    mod = load_module()
    now = time.time()
    st = mod.empty_state()
    st["sessions"]["a"] = {"token": "t", "updated_at": now,
                           "pending": [{"exercise": "squats", "reps": 20, "unit": "reps", "ts": now}]}
    st["next_set"] = {"exercise": "squats", "reps": 20, "unit": "reps"}
    (env["home"] / "state.json").write_text(json.dumps(st))
    write_log(env, [rec(dt.date.today())])
    write_cfg(env, statusline=False)
    assert run(env, "status", "a").stdout == "" and mod.status_text("a") == ""
    write_cfg(env)
    assert run(env, "status", "a").stdout == "🏋 NOW: 20 squats"


def test_status_shows_nothing_then_now_then_totals(env):
    write_cfg(env, delay_s=0.8, cooldown_s=100)
    prompt(env)
    nxt = task(state(env)["next_set"])
    assert run(env, "status", "s1").stdout == ""  # v3: nothing before the set is due
    assert wait_for(lambda: run(env, "status", "s1").stdout == f"🏋 NOW: {nxt}", timeout=4)
    stop(env)
    assert run(env, "status", "s1").stdout == "💪 1 set today"  # no "next:"


def test_done_without_args_logs_the_next_set_and_skip_repicks(env):
    write_cfg(env)
    prompt(env)
    stop(env)
    nxt = state(env)["next_set"]
    out = run(env, "cli", "done").stdout
    e = log(env)[-1]
    assert (e["exercise"], e["reps"], e["unit"]) == (nxt["exercise"], nxt["reps"], nxt["unit"])
    new = state(env)["next_set"]
    assert new["exercise"] != nxt["exercise"] and task(new) not in out and "next" not in out.lower()
    out = run(env, "cli", "undo").stdout
    assert out.startswith("↩️ Undone: ") and task(state(env)["next_set"]) not in out


def test_next_set_repicked_when_config_drops_it(env):
    write_cfg(env)
    prompt(env)
    stop(env)
    write_cfg(env, exercises=[{"name": "burpees", "min": 5, "max": 6, "unit": "reps"}])
    prompt(env)
    assert state(env)["next_set"]["exercise"] == "burpees"
    stop(env)


# ---------------------------------------------------------------- v3: the spinner task in settings.json

def settings_json(env):
    return json.loads(env["settings"].read_text(encoding="utf-8"))


def assert_only_spinner_changed(env):
    """Every other key is equal after a JSON round-trip and keeps its position."""
    now = settings_json(env)
    before = dict(FAKE_SETTINGS)
    assert list(now) == list(before)
    for k in before:
        if k != "spinnerVerbs":
            assert json.dumps(now[k], ensure_ascii=False) == json.dumps(before[k], ensure_ascii=False), k
    return now["spinnerVerbs"]


def test_setup_manages_the_key_only_while_a_set_is_due(env):
    """"spinner_idle": "claude" (the v3 behavior): after setup, spinnerVerbs is absent while nothing is
    due (Claude Code's own verbs), "NOW: <set>" while a set is due, and absent again after Stop. Only
    that key ever changes."""
    write_cfg(env, delay_s=0.4, cooldown_s=100, spinner_idle="claude")
    assert settings_json(env) == FAKE_SETTINGS
    prompt(env)
    stop(env)
    assert settings_json(env) == FAKE_SETTINGS  # nothing is written before /workout setup
    out = run(env, "cli", "setup", "apply", "--yes").stdout
    # public build: the person's own spinnerVerbs (from before setup) stand while nothing is due
    assert "unchanged" in out.split("\n")[0] and "nothing due now" in out.split("\n")[0]
    assert "Backup of settings.json" in out
    now = settings_json(env)
    assert now == FAKE_SETTINGS and list(now) == list(FAKE_SETTINGS)
    raw = env["settings"].read_bytes()
    assert raw.endswith(b"}\n") and "שלום ✓".encode() in raw  # newline kept, no \\u escapes
    mtime = env["settings"].stat().st_mtime_ns
    assert "unchanged" in run(env, "cli", "setup", "apply", "--yes").stdout.split("\n")[0]
    assert env["settings"].stat().st_mtime_ns == mtime  # same value: no write
    prompt(env)
    assert wait_for(lambda: (settings_json(env).get("spinnerVerbs") or {}).get("verbs", [""])[0].startswith("NOW: "),
                    timeout=4)  # due -> the task
    sv = settings_json(env)["spinnerVerbs"]
    assert sv == {"mode": "replace", "verbs": ["NOW: " + task(state(env)["sessions"]["s1"]["pending"][-1])]}
    assert run(env, "status", "s1").stdout == f"🏋 {sv['verbs'][0]}"
    stop(env)
    assert settings_json(env) == FAKE_SETTINGS  # logged -> the earlier value again
    run(env, "cli", "done", "lunges", "12")
    run(env, "cli", "undo")
    assert settings_json(env) == FAKE_SETTINGS
    assert not list(env["tmp"].glob("settings.json.workout-tmp*"))


def test_spinner_task_off_keeps_the_key_absent(env):
    write_cfg(env, delay_s=0.3, cooldown_s=100)
    run(env, "cli", "setup", "apply", "--yes")
    write_cfg(env, delay_s=0.3, cooldown_s=100, spinner_task=False)
    prompt(env)
    assert wait_for(lambda: run(env, "status", "s1").stdout.startswith("🏋 NOW: "), timeout=4)
    assert settings_json(env)["spinnerVerbs"] == FAKE_SETTINGS["spinnerVerbs"]  # the earlier value, not a task
    stop(env)
    assert settings_json(env) == FAKE_SETTINGS


def test_settings_missing_or_broken_is_skipped_never_created(env):
    write_cfg(env)
    env["settings"].unlink()
    out = run(env, "cli", "setup", "apply", "--yes").stdout
    assert "skipped" in out.split("\n")[0] and not env["settings"].exists()
    assert "spinner-settings-read" in (env["home"] / "errors.log").read_text()
    env["settings"].write_text('{"theme": "dark",')  # truncated JSON
    run(env, "cli", "done", "squats", "20")
    assert env["settings"].read_text() == '{"theme": "dark",'


def test_tests_never_touch_the_real_settings_file(env, monkeypatch):
    """The suite never reaches a real settings.json: HOME and CLAUDE_CONFIG_DIR are temp dirs (conftest),
    so settings_path() falls inside the test's tmp dir even without WORKOUT_SETTINGS_PATH."""
    from pathlib import Path
    mod = load_module()
    monkeypatch.delenv("WORKOUT_SETTINGS_PATH")
    sp = Path(mod.settings_path())
    assert sp == Path(env["env"]["CLAUDE_CONFIG_DIR"]) / "settings.json"
    assert str(sp).startswith(str(env["tmp"]))
    e = {k: v for k, v in env["env"].items() if k != "WORKOUT_SETTINGS_PATH"}
    sandbox = dict(env, env=e)
    write_cfg(env)
    out = run(sandbox, "cli", "setup", "apply", "--yes").stdout
    assert "skipped: could not read" in out.split("\n")[0] and not sp.exists()  # never created
    run(sandbox, "hook-prompt", stdin=json.dumps({"session_id": "s1", "prompt": "hi"}))
    run(sandbox, "hook-stop", stdin=json.dumps({"session_id": "s1"}))
    run(sandbox, "cli", "done", "squats", "20")
    run(sandbox, "cli", "undo")
    assert not sp.exists()
    assert settings_json(env) == FAKE_SETTINGS