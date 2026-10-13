"""Workout Buddy v4.1: one ask per turn, and a success badge ("✅ 20 crunches · bravo! 🔥") once taken."""
import json
import re
import time

from test_v4 import accept as answer, asked, declined, done, is_badge, pend, say, setup_ask, status, task  # noqa: F401
from test_workout import env, load_module, log, notes, run, state, stop, wait_for  # noqa: F401
import pytest

pytestmark = pytest.mark.legacy_modes  # written against the older modes (see conftest.py)


def accept(ctx):
    """Wait for the open ask, see it drawn, answer it (the band's [1]). Returns the set text."""
    assert wait_for(lambda: asked(ctx), 5)
    want = task(asked(ctx)[0])
    n = len(done(ctx))
    assert status(ctx) == f"💪 {want}?"  # drawn
    assert answer(ctx) == want
    assert len(done(ctx)) == n + 1
    return want


# ---------------------------------------------------------------- one ask per turn

def test_no_second_ask_in_the_same_turn_after_accept_even_with_cooldown_zero(env):
    """The live-test bug: cooldown_s 0 asked '💪 30 calf raises?' right after '20 crunches' was taken,
    replacing the ✅ before it was ever seen."""
    setup_ask(env, cooldown_s=0, repeat_s=0.2)
    say(env)
    want = accept(env)
    badge = status(env)
    assert is_badge(badge, want)
    end = time.time() + 1.5
    while time.time() < end:  # many repeat_s periods, no cooldown: still nothing new
        assert status(env) == badge
        time.sleep(0.1)
    assert [p.get("status") for p in pend(env)] == ["accepted"] and asked(env) == []
    assert len(done(env)) == 1
    assert json.loads(stop(env).stdout)["systemMessage"].startswith(f"Logged {want} · ")
    assert declined(env) == []


def test_a_new_ask_needs_a_new_turn_and_the_cooldown(env):
    setup_ask(env, cooldown_s=100)
    say(env)
    accept(env)
    stop(env)
    say(env)  # a new turn, inside the global cooldown: no ask
    time.sleep(0.8)
    assert asked(env) == [] and pend(env) == []
    stop(env)
    setup_ask(env, cooldown_s=0)  # no cooldown: the next turn asks
    say(env)
    assert wait_for(lambda: asked(env), 5)
    stop(env)


def test_auto_mode_still_repeats_within_a_turn(env):
    """"ask_mode": "auto" is unchanged: a due set repeats every repeat_s (after the cooldown)."""
    setup_ask(env, ask_mode="auto", cooldown_s=0, repeat_s=0.3)
    say(env)
    assert wait_for(lambda: len(pend(env)) >= 2, 5)
    assert all("status" not in p for p in pend(env))
    stop(env)
    assert len(done(env)) >= 2


# ---------------------------------------------------------------- the badge

def test_cheer_pool_and_rotation_never_repeats():
    mod = load_module()
    assert len(mod.CHEERS) >= 5 and len(set(mod.CHEERS)) == len(mod.CHEERS)
    for c in mod.CHEERS:
        assert len(c) <= 16 and mod._emoji_count(c) == 1, c
    st = {}
    seen = []
    for _ in range(300):
        seen.append(mod.pick_cheer(st))
        assert st["last_cheer"] == seen[-1]
    assert all(a != b for a, b in zip(seen, seen[1:]))
    assert set(seen) == set(mod.CHEERS)
    p = {"exercise": "crunches", "reps": 20, "unit": "reps"}
    assert mod.badge_text(p, "bravo! 🔥") == "✅ 20 crunches · bravo! 🔥"
    assert mod.badge_text({"exercise": "plank", "reps": 45, "unit": "seconds"}, "kudos! 💪") == \
        "✅ 45s plank · kudos! 💪"


def test_badges_rotate_across_turns_in_state(env):
    setup_ask(env, cooldown_s=0)
    texts = []
    for _ in range(4):
        say(env)
        want = accept(env)
        b = status(env)
        assert is_badge(b, want)
        texts.append(b.split(" · ", 1)[1])
        assert state(env)["last_cheer"] == texts[-1]
        stop(env)
    assert all(a != b for a, b in zip(texts, texts[1:]))


def test_badge_during_the_turn_after_stop_for_badge_s_then_gone(env):
    setup_ask(env, badge_s=1.0)
    say(env)
    want = accept(env)
    badge = status(env)
    assert is_badge(badge, want)
    time.sleep(1.3)  # past badge_s since the acceptance, the turn still running: still up
    assert status(env) == badge
    msg = json.loads(stop(env).stdout)["systemMessage"]
    assert msg.split("\n")[0].startswith(f"Logged {want} · today 1 set")  # Stop: the plain fact line
    assert "·" not in badge.split(" · ", 1)[1] and badge.split(" · ", 1)[1] not in msg
    t_stop = time.time()
    # the timer is not needed after Stop: kill it, the badge still shows (timestamps only)
    import os
    import signal
    for pid in state(env)["timer_pids"]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if time.time() < t_stop + 0.8:
        assert status(env) == badge
    assert wait_for(lambda: status(env) == "💪 1 set today", 3)
    assert time.time() >= t_stop + 0.9


def test_badge_goes_at_the_next_prompt(env):
    setup_ask(env, cooldown_s=100)  # badge_s default 120
    say(env)
    want = accept(env)
    stop(env)
    assert is_badge(status(env), want)
    say(env, "thanks")  # the next prompt in that session
    assert status(env) == "💪 1 set today"
    stop(env)
    assert status(env) == "💪 1 set today"


def test_badge_is_per_session(env):
    setup_ask(env, cooldown_s=100)
    say(env, sid="a")
    assert wait_for(lambda: asked(env, "a"), 5)
    want = task(asked(env, "a")[0])
    status(env, "a")
    answer(env, "a")
    assert is_badge(status(env, "a"), want)
    assert status(env, "b") == "💪 1 set today"
    stop(env, "a")


def test_undo_takes_the_badge_down_after_stop_too(env):
    setup_ask(env)
    say(env)
    accept(env)
    stop(env)
    assert status(env).startswith("✅ ")
    assert run(env, "cli", "undo").stdout.startswith("↩️ Undone: ")
    assert status(env) == ""


def test_open_ask_at_stop_is_declined_silently_and_no_badge(env):
    setup_ask(env)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    status(env)
    assert stop(env).stdout == ""
    assert len(declined(env)) == 1 and status(env) == ""
    assert "badge" not in state(env)["sessions"]["s1"]


def test_manual_done_on_the_ask_also_gets_the_badge(env):
    setup_ask(env)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    want = task(asked(env)[0])
    run(env, "cli", "done")
    assert is_badge(status(env), want)
    stop(env)


def test_badge_s_config():
    mod = load_module()
    n = mod.normalize_config
    assert n({})["badge_s"] == 120
    assert n({"badge_s": 30})["badge_s"] == 30 and n({"badge_s": -1})["badge_s"] == 120
    assert n({"badge_s": "x"})["badge_s"] == 120


# ---------------------------------------------------------------- the words on screen

FORBIDDEN = re.compile(r"idle|away|keyboard|mouse|stepp?(ed|ing)? (away|out)", re.I)


def _texts_of_module(mod):
    """Every fixed user-facing text pool: help, cheers, coach lines, idle-spinner fillers, form cues."""
    out = [mod.HELP, *mod.CHEERS, *mod.IDLE_FILLERS, *mod.PAUSED_VERBS, *mod.HYPE_WORDS]

    def walk(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
    walk(mod.COACH)
    walk(mod.FORM_CUES)
    walk(mod.GENERIC_VERBS)
    walk(mod.EXERCISE_VERBS)
    return out


def test_user_visible_text_never_names_the_mechanism(env):
    """Status line, Stop messages, every /workout command's output, help, tips, banners: never a word
    about idle time, being away, the keyboard or the mouse."""
    mod = load_module()
    seen = _texts_of_module(mod)
    setup_ask(env, cooldown_s=0, badge_s=0.3, notification=True)
    # an accepted set: the ask, the badge, the Stop line, after Stop, the normal segment
    say(env)
    assert wait_for(lambda: asked(env), 5)
    seen.append(status(env))
    seen.append(run(env, "cli", "done", "--ask", asked(env)[0]["id"]).stdout)
    seen.append(status(env))
    r = stop(env)
    seen += [r.stdout, r.stderr]
    seen.append(status(env))
    time.sleep(0.4)
    seen.append(status(env))
    # an open ask at Stop (declined, silent)
    say(env)
    assert wait_for(lambda: asked(env), 5)
    seen.append(status(env))
    r = stop(env)
    seen += [r.stdout, r.stderr]
    # every command
    for args in (["stats"], ["achievements"], ["share"], ["share", "--text"], ["setup"], ["pause", "5"],
                 ["resume"], ["done"], ["done"], ["done", "squats", "15"], ["skip"], ["undo"], ["why"], ["predict-stats"],
                 ["test"], ["config"], ["help"], ["bogus"], []):
        r = run(env, "cli", *args)
        seen += [r.stdout, r.stderr]
    r = run(env, "spinner-config")
    seen += [r.stdout, r.stderr]
    seen.append(run(env).stdout)  # no subcommand: help
    tips = env["home"] / "tips.json"
    if tips.exists():
        seen += [t["text"] for t in json.loads(tips.read_text())["tips"]]
    for n in notes(env):
        seen += [str(v) for v in n.values()]
    st = state(env)
    if isinstance(st.get("idle_spinner"), dict):
        seen += st["idle_spinner"].get("verbs") or []
    seen = [s for s in seen if s]
    assert len(seen) > 40
    bad = [s for s in seen if FORBIDDEN.search(s.replace(str(env["tmp"]), "<tmp>"))]
    assert bad == []
