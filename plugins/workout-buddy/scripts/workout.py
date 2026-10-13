#!/usr/bin/env python3
"""Workout Buddy for Claude Code: Claude thinks. You rep.

While Claude works on a long turn, a band above the prompt asks for a short set ("15 push-ups?"):
press 1 when done, 2 to skip. When to ask is decided by a waiting meter: every second some session is
busy fills one meter (the union across sessions); about every meter_base_s (300 s, +- meter_jitter_s)
of waiting, the next turn that has run meter_min_turn_s (20 s) asks. Short turns never ask. One ask per
turn; an unanswered question goes away after meter_ask_ttl_s (180 s).

Stdlib only, macOS and Linux. Everything is kept in one local data dir (see home()); nothing is sent
anywhere. Subcommands:
  hook-prompt                        UserPromptSubmit hook (prints NOTHING)
  hook-stop                          Stop hook (prints optional {"systemMessage", "terminalSequence"})
  _timer SID TOKEN TRANSCRIPT DELAY  detached turn timer (internal)
  _train                             background retrain of model.json (internal; "timing": "classic")
  status [SID]                       one-line status segment (for your own status line, optional)
  spinner-config                     JSON spinner settings (prints only)
  where                              the data dir this process resolves (one line)
  cli [stats|achievements|share [--text]|setup [...]|pause [min|day]|resume|skip|undo|done [exercise] [reps]|
       config|help]

Env:
  WORKOUT_HOME           data dir override (else $CLAUDE_PLUGIN_DATA, else ~/.workout-buddy)
  CLAUDE_CONFIG_DIR      where Claude Code keeps settings.json (default ~/.claude)
  WORKOUT_SETTINGS_PATH  the settings.json that /workout setup may change (default
                         $CLAUDE_CONFIG_DIR/settings.json)
Test-only knobs, honoured only with WORKOUT_TEST=1 (never on an install, WORKOUT_HOME or not): WORKOUT_NOTIFY_LOG,
WORKOUT_MIN_DELAY_S, WORKOUT_ASK_POLL_S, WORKOUT_CLICK_LINGER_S, WORKOUT_CLOSE_TAB_LOG, WORKOUT_CLOSE_TAB_DELAY_S,
WORKOUT_METER_POLL_S, WORKOUT_HEARTBEAT_S, WORKOUT_PLATFORM, WORKOUT_TEST_LEGACY_MODES.
"""
import sys

MIN_PY = (3, 9)
if sys.version_info < MIN_PY:  # before anything that needs 3.9; this file still parses on 3.8
    sys.stderr.write("Workout Buddy: needs Python %d.%d or later (found %d.%d); it does nothing until then.\n"
                     % (MIN_PY + tuple(sys.version_info[:2])))
    sys.exit(0)

import collections
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import operator
import os
import random
import re
import secrets
import stat
import subprocess
import time
import unicodedata

SESSION_TTL_S = 6 * 3600
ERRORS_MAX_BYTES = 256 * 1024
ERRORS_KEEP_BYTES = 64 * 1024

DEFAULT_EXERCISES = [
    {"name": "push-ups", "min": 10, "max": 20, "unit": "reps"},
    {"name": "jumping jacks", "min": 20, "max": 40, "unit": "reps"},
    {"name": "crunches", "min": 15, "max": 25, "unit": "reps"},
    {"name": "squats", "min": 15, "max": 25, "unit": "reps"},
    {"name": "plank", "min": 30, "max": 60, "unit": "seconds"},
    {"name": "lunges", "min": 10, "max": 16, "unit": "reps"},
    {"name": "calf raises", "min": 20, "max": 30, "unit": "reps"},
    {"name": "wall sit", "min": 30, "max": 45, "unit": "seconds"},
]

COACH_TONES = ("hype", "chill", "drill-sergeant")
SPINNER_IDLE_MODES = ("progress", "claude")  # v3.2 key, mapped onto "spinner"
SPINNER_MODES = ("off", "task", "progress")  # off: Claude Code's own verbs | task: "NOW: <set>" while due | + idle lines
# A set is logged only by an explicit action: the band's [1] done, or /workout done. ("auto", which credits
# every due set at Stop, is kept for the test suite only, see legacy_modes_allowed.)
ASK_MODES = ("button", "auto")
PUBLIC_ASK_MODES = ("button",)
# How the button is drawn. "mod" (the public build's only one): the band above the prompt draws 1: done /
# 2: skip and answers through `cli done --ask <id>` / `cli decline --ask <id>`; nothing listens on any port.
# "link" (test suite only): an OSC 8 link in a status line to a loopback listener on 127.0.0.1.
BUTTON_MODES = ("link", "mod")
PUBLIC_BUTTON_MODES = ("mod",)
# When a set is asked. "meter" (the public build's only one): a waiting meter fills with every second some
# session is busy (the union across sessions); once it reaches meter_target_s (meter_base_s +- meter_jitter_s,
# drawn fresh after every ask), the next turn that has run meter_min_turn_s asks. "classic" (test suite
# only): a per-prompt due time, repeat_s and a global cooldown_s.
TIMING_MODES = ("meter", "classic")
PUBLIC_TIMING_MODES = ("meter",)

DEFAULT_CONFIG = {
    "timing": "meter",
    "meter_base_s": 300,
    "meter_jitter_s": 60,
    "meter_min_turn_s": 20,
    "meter_ask_ttl_s": 180,
    "delay_s": 60,
    "repeat_s": 300,
    "cooldown_s": 300,
    "stale_s": 120,
    "max_lifetime_s": 3600,
    "sound": False,
    "sound_name": "Glass",
    "say": False,
    "quiet_hours": None,
    "coach": "hype",
    "progression": True,
    "tab_title": False,
    "notification": False,
    "statusline": False,
    "spinner": "off",
    "ask_mode": "button",
    "button": "mod",
    "ask_grace_s": 180,
    "badge_s": 120,
    "predict": False,
    "close_tab": False,
    "long_s": 60,
    "early_delay_s": 15,
    "late_delay_s": 120,
    "predict_hi": 0.9,
    "predict_lo": 0.15,
    "retrain_every": 25,
    "exercises": DEFAULT_EXERCISES,
}

NUMERIC_KEYS = ("delay_s", "repeat_s", "cooldown_s", "stale_s", "max_lifetime_s", "early_delay_s", "late_delay_s",
                "badge_s", "ask_grace_s", "meter_base_s", "meter_jitter_s", "meter_min_turn_s",
                "meter_ask_ttl_s")
POSITIVE_KEYS = ("long_s",)  # > 0
BOOL_KEYS = ("sound", "say", "progression", "tab_title", "notification", "statusline", "predict", "close_tab")
# The keys a fresh config.json is written with (the README's settings table). Every other key keeps its
# default unless you add it by hand.
PUBLIC_CONFIG_KEYS = ("exercises", "meter_base_s", "meter_jitter_s", "meter_min_turn_s", "meter_ask_ttl_s",
                      "quiet_hours", "progression", "coach", "badge_s", "notification", "sound", "sound_name",
                      "spinner")


def public_config(cfg):
    """The README's keys of cfg, in that order: what a fresh config.json holds."""
    return {k: json.loads(json.dumps(cfg[k])) for k in PUBLIC_CONFIG_KEYS}

# --------------------------------------------------------------------------
# coach personality
#
# Fire lines are templates: {set} = "15 push-ups" / "40s plank", plus {reps},
# {exercise}, {unit}, and {SET} (upper-cased, for ALL-CAPS lines). They never
# name an exercise literally (the set does). A fire line is only ever the
# nudge notification's subtitle; the body is the set itself. Nothing on screen
# cheers: the Stop message and the done notification are plain facts.

THEMES = ("tests", "bug", "refactor", "deploy", "review", "docs", "generic")

THEME_KEYWORDS = {
    "tests": ("test", "tests", "pytest", "jest", "vitest", "spec", "specs", "coverage", "unittest", "e2e",
              "assert", "flaky"),
    "bug": ("bug", "bugs", "fix", "fixes", "broken", "error", "errors", "crash", "crashes", "debug",
            "traceback", "exception", "stacktrace", "regression", "failing", "fails"),
    "refactor": ("refactor", "refactoring", "cleanup", "clean", "rename", "simplify", "restructure",
                 "extract", "dedupe", "tidy", "reorganize"),
    "deploy": ("deploy", "deployment", "ship", "release", "publish", "prod", "production", "rollout",
               "merge", "push", "rollback"),
    "review": ("review", "pr", "prs", "diff", "feedback", "audit", "approve", "lgtm", "critique"),
    "docs": ("doc", "docs", "readme", "documentation", "docstring", "docstrings", "changelog", "explain",
             "writeup", "guide", "tutorial"),
}

COACH = {
    "hype": {
        "fire": {
            "generic": [
                "Compiling… 🧱 Drop and give me {set}.",
                "Claude's thinking. You're sweating: {set} 💦",
                "Tokens are flowing. So is your blood: {set} 🔥",
                "While the model cooks, you cook: {set} 🍳",
                "Context window's filling up. Fill your lungs: {set}!",
                "Claude's on it. You're on the floor: {set} 💪",
                "No doomscrolling. {set}. Go.",
                "Your chair misses you. Make it wait: {set}",
                "Free gains while Claude grinds: {set} 🏋️",
                "Your Apple Watch thinks you died. Prove it wrong: {set}",
                "Waiting is a rest day. This isn't one: {set} 🔥",
                "Agents in parallel, you in motion: {set} ⚡",
                "Every token is a rep. Match it: {set}",
                "git commit -m \"{set}\"",
                "Stack Overflow can't do this for you: {set}",
                "Claude's chewing tokens. You chew through {set}.",
            ],
            "tests": [
                "Tests are running. So are you: {set} 🏃",
                "Red, green, rep: {set} ✅",
                "Assert yourself: {set} 💪",
                "Coverage at 80%? Your core's at 0%: {set}.",
                "The suite is slow. You don't have to be: {set}",
            ],
            "bug": [
                "Claude's squashing bugs. You squash doubt: {set} 🐛",
                "Stack trace? Stack reps: {set}",
                "Off-by-one? Do {set}. Then do it right.",
                "While Claude hunts the bug, you hunt gains: {set}",
                "Debugging takes patience. And {set}.",
            ],
            "refactor": [
                "Claude's refactoring your mess. You refactor your abs: {set}",
                "Extract method: {set} 🧹",
                "Clean code, clean form: {set}",
                "Rename a variable, rename your PR: {set}",
            ],
            "deploy": [
                "Shipping to prod? Ship {set} to your muscles 🚀",
                "Deploy pending. Gains deploying now: {set}",
                "Pipeline warming up. You warm up too: {set}",
                "Release notes: added {set}. 📦",
            ],
            "review": [
                "Claude's reviewing the diff. Review your form: {set} 👀",
                "LGTM = Let's Get The Muscles: {set}",
                "Requested changes: {set}.",
            ],
            "docs": [
                "Claude's writing docs. You write history: {set} 📝",
                "Claude's writing the README. You write a PR (personal record): {set}",
                "Docs are rotting. Your quads don't have to: {set}",
            ],
        },
    },
    "chill": {
        "fire": {
            "generic": [
                "No rush. When you're ready: {set} 🌿",
                "Claude's working. Maybe ease into {set}?",
                "Gentle nudge: {set}. Then a sip of water 💧",
                "Little movement break: {set} 🙂",
                "Your back would love {set} right about now.",
                "Breathe in, breathe out, {set}.",
                "Claude's got the code. You've got {set}, if you like.",
                "A tiny set for a tiny break: {set}",
                "Easy does it: {set} 🍵",
                "Keep the blood moving: {set}",
                "Soft reminder: {set}. No pressure.",
                "Stand up, shake it out, {set} ☀️",
            ],
            "tests": [
                "Tests are running. Take your time with {set}.",
                "Green or red, the suite takes a while. {set}, gently 🌿",
                "Let the tests do the worrying. You do {set}.",
            ],
            "bug": [
                "While the bug hunt goes on, maybe {set}?",
                "Bugs can wait a minute. Your spine can't: {set} 🙂",
                "Claude's untangling it. You untangle your shoulders: {set}",
            ],
            "refactor": [
                "Claude's tidying the code. Tidy your posture: {set} 🌿",
                "Small refactor, small set: {set}",
                "Clean code takes time. Spend some of it on {set}.",
            ],
            "deploy": [
                "The deploy can wait a minute. So can you, after {set}.",
                "Pipelines are slow. Stretch it out: {set} ☀️",
                "Shipping is stressful. {set} helps, a little.",
            ],
            "review": [
                "Claude's reading the diff. You read your body: {set}",
                "Review in progress. Movement in progress? {set} 🙂",
                "No comments on your form. Just {set}, when you like.",
            ],
            "docs": [
                "Claude's writing docs. You could write off some stiffness: {set}",
                "Docs are a slow craft. So is {set} 🍵",
                "While the README grows, you grow a little: {set}",
            ],
        },
    },
    "drill-sergeant": {
        "fire": {
            "generic": [
                "ON THE FLOOR. {SET}. NOW. 🪖",
                "Did I stutter? {set}!",
                "Claude works 24/7. You can manage {set}.",
                "YOUR CODE COMPILES FASTER THAN YOU MOVE. {SET}!",
                "That chair is not your friend. {set}, recruit.",
                "I WANT {SET}. CLEAN FORM. NO CHEATING.",
                "No excuses, no snoozing. {set}!",
                "Pain is weakness leaving the codebase. {set}!",
                "You call that posture? Fix it: {set}",
                "Move it, move it, MOVE IT: {set}",
                "While Claude debugs, you DO NOT sit. {set}!",
                "Recruit! {set}. Sound off when you're done!",
            ],
            "tests": [
                "Tests failing? So is your cardio. {set}!",
                "THE SUITE RUNS. YOU RUN. {SET}!",
                "Green checks are earned, recruit. {set}!",
            ],
            "bug": [
                "Bugs fear me. So should you. {set}!",
                "That bug didn't write itself. Neither do abs. {set}!",
                "While Claude hunts the bug, you hunt excuses to quit. DENIED. {set}!",
            ],
            "refactor": [
                "Clean that code AND that form. {set}!",
                "Refactor THIS, recruit: {set}!",
                "Dead code gets deleted. Dead weight gets {set}!",
            ],
            "deploy": [
                "Nobody ships until you've done {set}!",
                "PROD DOES NOT WAIT. NEITHER DO I. {SET}!",
                "You want to ship? Earn it. {set}!",
            ],
            "review": [
                "Code review is not a nap, recruit. {set}!",
                "Changes requested: {set}. NOW!",
                "I reviewed your posture. REJECTED. {set}!",
            ],
            "docs": [
                "Docs are written standing up today. {set}!",
                "You read the manual? Here's mine: {set}!",
                "README says: {SET}. NO EXCEPTIONS.",
            ],
        },
    },
}


# Prompt categories for the turn-length predictor: the coach themes plus
#   "ack" a short acknowledgement that ends the exchange: thanks / great / amazing / lgtm / an emoji,
#         English or Hebrew ("amazing thank you", "תודה", "👍");
#   "go"  a short go-ahead that starts work: yes / ok / continue / approved / lets go / pls resume.
# The two are split because a go-ahead usually starts a long turn, while a true ack ends quickly. Neither has coach lines; fire_pool falls back to generic.
CATEGORIES = THEMES + ("ack", "go")

ACK_CORE = frozenset("""
thanks thank thx ty tysm tnx lgtm cool great amazing
awesome nice perfect good excellent wonderful fantastic sweet gotcha sure fine alright wow haha hahaha lol
brilliant beautiful neat super agreed exactly correct noted understood cheers np appreciate appreciated
love loved bravo kudos superb splendid lovely magnificent incredible impressive gorgeous sick dope legit
no nope nah
תודה תודהה מעולה אחלה סבבה יופי מדהים מושלם נהדר הכבוד יפה וואו חחח חחחח
מצוין מצויין תותח אש נחמד אהבתי לגמרי נכון בדיוק פצצה מהמם מטורף שיגעון לא
""".split())
# "ok thanks", "yes thanks": a soft yes next to a thanks is still an ack. Action words (continue, go, run,
# push, ...) are not soft: "ok continue thanks" is a go-ahead.
ACK_SOFT = frozenset("""
ok okay okey k kk yes yep yeah yup ya sure alright
כן אוקיי אוקי טוב בסדר
""".split())
ACK_FILLER = frozenset("""
you so much very a lot it that this is was looks look sounds sound makes sense works worked all the to me
for i we us
really just again man mate buddy dude bro sir job work well done got thank's oh ah great's indeed totally
רבה ממש כל על זה עובד נראה לך
""".split())

GO_CORE = frozenset("""
yes yep yeah yup ya ok okay okey k kk sure go continue resume proceed approved approve merged retry again
ready done next lets let's do send post ship run push publish keep going start
כן יאללה תמשיך המשך קדימה סע אוקיי אוקי טוב בסדר לך תתחיל אושר מוזג
""".split())
GO_FILLER = frozenset("""
please pls plz now on ahead with it this that and then yes go do again more one the a ok so let us
""".split())

BUILD_VERBS = frozenset("""
implement build create refactor migrate write rewrite fix add run rerun deploy test review investigate debug
update change make port remove delete rename check analyze analyse audit install setup configure convert
generate design plan find search explore research compare verify validate ship merge push publish draft
prepare clean optimize improve integrate scaffold upgrade bump rebase commit open summarize explain
document read trace profile benchmark measure evaluate train wire extend split move replace edit modify
adjust tweak polish finish complete continue execute launch start restart dig rebuild redo resolve
תבנה בנה תכתוב כתוב תתקן תקן תוסיף הוסף תריץ הרץ תבדוק בדוק תעשה עשה תמשיך המשך תעדכן עדכן תמחק מחק
תשנה שנה תסדר סדר תחקור חקור תפרסם פרסם תעלה תדחוף תמזג תכין הכן תממש ממש תייצר צור תריץ תנתח נתח
""".split())

_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?|\d+")


def _words(text):
    return _WORD.findall(text.lower())


def is_ack(text):
    """A short acknowledgement: only ack words (at least one core word) and fillers, at most 10 words,
    or emoji / punctuation only. "great, now fix the tests" is not an ack."""
    if not isinstance(text, str):
        return False
    t = text.strip()
    if not t or len(t) > 120 or "```" in t or t.startswith("/"):
        return False
    words = _words(t)
    if not words:
        return len(t) <= 24  # emoji / punctuation only: "👍", "🙏🙏", "!!"
    if len(words) > 10:
        return False
    return (any(_tok_in(w, ACK_CORE) for w in words)
            and all(_tok_in(w, ACK_CORE, ACK_FILLER, ACK_SOFT, GO_FILLER) for w in words))


def _tok_in(w, *sets):
    """w (or w with a trailing letter run collapsed: "gooooo" -> "go", "yesss" -> "yes") is in a set."""
    if any(w in s for s in sets):
        return True
    c = re.sub(r"(\w)\1+$", r"\1", w)
    return c != w and any(c in s for s in sets)


def is_go(text):
    """A short go-ahead: at most 8 words, all go / ack / filler words or numbers, at least one go word
    ("yes", "continue", "pls resume", "313 approved lets gooo", "כן")."""
    if not isinstance(text, str):
        return False
    t = text.strip()
    if not t or len(t) > 80 or "```" in t or t.startswith("/"):
        return False
    words = _words(t)
    if not words or len(words) > 8:
        return False
    return (any(_tok_in(w, GO_CORE) for w in words)
            and all(w.isdigit() or _tok_in(w, GO_CORE, GO_FILLER, ACK_CORE, ACK_FILLER) for w in words))


# Messages Claude Code itself submits (UserPromptSubmit fires for them too, mid-turn or not): background
# task notifications, messages from other Claude sessions, "N background agents were stopped". None of
# them is a human turn: mid-turn they belong to the running turn, and when Claude is idle they start a
# turn nobody asked for (no task is shown, nothing is recorded). Matched on the prefixes seen on disk.
_INJECTED = re.compile(r"(<task-notification>|<agent-message[\s>]|<teammate-message[\s>]|"
                       r"Another Claude session sent a message:|\d+ background agents? (?:were|was) stopped|"
                       r'Background agent ")')


def is_injected(text):
    return isinstance(text, str) and bool(_INJECTED.match(text.lstrip()))


def classify_prompt(text):
    """Category label for a prompt (only the label is ever stored, never the text): "ack" for a short
    acknowledgement, "go" for a short go-ahead, else the coach theme with the most keyword hits (tests, bug, refactor, deploy,
    review, docs), else "generic"."""
    if not isinstance(text, str) or not text:
        return "generic"
    if is_ack(text):
        return "ack"
    if is_go(text):
        return "go"
    words = re.findall(r"[a-z0-9]+", text.lower()[:4000])
    best, best_n = "generic", 0
    for theme, kws in THEME_KEYWORDS.items():
        n = sum(1 for w in words if w in kws)
        if n > best_n:
            best, best_n = theme, n
    return best


def coach_pack(cfg):
    return COACH.get(cfg.get("coach"), COACH["hype"])


def pick_line(pool, last=None, rng=random):
    """Random line from pool, never `last` (unless the pool has one line)."""
    choices = [p for p in pool if p != last] or list(pool)
    return rng.choice(choices)


def fire_pool(cfg, theme):
    """Themed lines when the tone has at least two for the theme, else generic (+ any themed one)."""
    fire = coach_pack(cfg)["fire"]
    themed = fire.get(theme) if theme and theme != "generic" else None
    return themed if themed and len(themed) >= 2 else (fire["generic"] + (themed or []))


def render_fire(tmpl, p):
    what = fmt_set(p["exercise"], p["reps"], p["unit"])
    return tmpl.format(set=what, SET=what.upper(), reps=p["reps"],
                       exercise=p["exercise"], unit=p["unit"])


def coach_pick(st, cfg, kind, theme=None, rng=random):
    """Pick a line of `kind` (only "fire" is left), avoiding the last one; records it in st."""
    last = st.get("coach_last")
    if not isinstance(last, dict):
        last = st["coach_last"] = {}
    pool = fire_pool(cfg, theme) if kind == "fire" else coach_pack(cfg)[kind]
    line = pick_line(pool, last.get(kind), rng)
    last[kind] = line
    return line


# --------------------------------------------------------------------------
# paths / errors


DEFAULT_HOME = "~/.workout-buddy"


def home():
    """The data dir, first match wins: WORKOUT_HOME (an explicit override), CLAUDE_PLUGIN_DATA (the plugin's
    own data dir, which Claude Code sets for plugin hooks), ~/.workout-buddy."""
    for var in ("WORKOUT_HOME", "CLAUDE_PLUGIN_DATA"):
        v = os.environ.get(var)
        if v and v.strip():
            return os.path.expanduser(v)
    return os.path.expanduser(DEFAULT_HOME)


def sandboxed():
    """A test run (WORKOUT_TEST=1): the test-only knobs are honoured. Never keyed on WORKOUT_HOME, which is a
    user setting (and the band passes its data dir that way on every press)."""
    return os.environ.get("WORKOUT_TEST") == "1"


def legacy_modes_allowed():
    """The older modes ("ask_mode": "auto", "button": "link", "timing": "classic"), kept for the test suite
    only: a test run with WORKOUT_TEST_LEGACY_MODES=1 too."""
    return sandboxed() and os.environ.get("WORKOUT_TEST_LEGACY_MODES") == "1"


def is_macos():
    """macOS-only calls (banners, speech) run only here. WORKOUT_PLATFORM (test runs only) lets the tests take
    the Linux path on a Mac."""
    forced = os.environ.get("WORKOUT_PLATFORM") if sandboxed() else None
    return (forced or sys.platform) == "darwin"


def claude_config_dir():
    """Where Claude Code keeps settings.json and transcripts: CLAUDE_CONFIG_DIR, else ~/.claude."""
    v = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.expanduser(v) if v and v.strip() else os.path.expanduser("~/.claude")


def path(name):
    return os.path.join(home(), name)


def ensure_home():
    os.makedirs(home(), exist_ok=True)


def log_error(where, exc=None):
    try:
        ensure_home()
        p = path("errors.log")
        try:
            if os.path.getsize(p) > ERRORS_MAX_BYTES:
                with open(p, "rb") as f:
                    f.seek(-ERRORS_KEEP_BYTES, os.SEEK_END)
                    tail = f.read()
                atomic_write_bytes(p, b"[truncated]\n" + tail)
        except OSError:
            pass
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        import traceback  # only on an error: importing it costs ~25 ms on Python 3.14, on every hook
        detail = traceback.format_exc() if exc is not None else ""
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{stamp} [{where}] {exc!r}\n{detail}\n" if exc is not None else f"{stamp} [{where}]\n")
    except Exception:
        pass


def atomic_write_bytes(p, data):
    """Write via a temp file and os.replace. The file is created owner-only (0600, whatever the umask):
    state.json holds the button's one-time token, and the rest is personal data."""
    tmp = f"{p}.tmp.{os.getpid()}.{secrets.token_hex(3)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, p)


def atomic_write_json(p, obj):
    atomic_write_bytes(p, (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------
# config

_config_error = None


def _valid_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def normalize_config(raw):
    """Merge a user dict over defaults, dropping invalid values."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(raw, dict):
        return cfg
    for k in NUMERIC_KEYS:
        if _valid_num(raw.get(k)):
            cfg[k] = raw[k]
    for k in POSITIVE_KEYS:
        if _valid_num(raw.get(k)) and raw[k] > 0:
            cfg[k] = raw[k]
    for k in BOOL_KEYS:
        if isinstance(raw.get(k), bool):
            cfg[k] = raw[k]
    ri = raw.get("retrain_every")
    if isinstance(ri, int) and not isinstance(ri, bool) and ri >= 1:
        cfg["retrain_every"] = ri
    hi, lo = raw.get("predict_hi", cfg["predict_hi"]), raw.get("predict_lo", cfg["predict_lo"])
    if _valid_num(hi) and _valid_num(lo) and 0 < lo < hi < 1:  # both or neither
        cfg["predict_hi"], cfg["predict_lo"] = hi, lo
    if isinstance(raw.get("sound_name"), str) and raw["sound_name"].strip():
        cfg["sound_name"] = raw["sound_name"]
    if raw.get("coach") in COACH_TONES:
        cfg["coach"] = raw["coach"]
    if raw.get("spinner") in SPINNER_MODES:
        cfg["spinner"] = raw["spinner"]
    elif "spinner_task" in raw or "spinner_idle" in raw:
        # v3.2 keys: "spinner_task": false -> off; "spinner_idle": "claude" -> task; else progress
        if raw.get("spinner_task") is False:
            cfg["spinner"] = "off"
        elif raw.get("spinner_idle") == "claude":
            cfg["spinner"] = "task"
        elif raw.get("spinner_task") is True or raw.get("spinner_idle") in SPINNER_IDLE_MODES:
            cfg["spinner"] = "progress"
    legacy = legacy_modes_allowed()
    if raw.get("ask_mode") in (ASK_MODES if legacy else PUBLIC_ASK_MODES):
        cfg["ask_mode"] = raw["ask_mode"]
    if raw.get("button") in (BUTTON_MODES if legacy else PUBLIC_BUTTON_MODES):
        cfg["button"] = raw["button"]
    if raw.get("timing") in (TIMING_MODES if legacy else PUBLIC_TIMING_MODES):
        cfg["timing"] = raw["timing"]
    qh = raw.get("quiet_hours")
    if qh is None or (isinstance(qh, list) and len(qh) == 2 and all(parse_hhmm(x) is not None for x in qh)):
        cfg["quiet_hours"] = qh
    exs = raw.get("exercises")
    if isinstance(exs, list):
        good = []
        for e in exs:
            if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"].strip():
                continue
            lo, hi = e.get("min"), e.get("max")
            if not (isinstance(lo, int) and isinstance(hi, int)) or isinstance(lo, bool) or isinstance(hi, bool):
                continue
            if lo < 1 or hi < lo:
                continue
            unit = e.get("unit") if e.get("unit") in ("reps", "seconds") else "reps"
            good.append({"name": e["name"].strip(), "min": lo, "max": hi, "unit": unit})
        if good:
            cfg["exercises"] = good
    return cfg


def load_config():
    global _config_error
    _config_error = None
    p = path("config.json")
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("config.json must be a JSON object")
        return normalize_config(raw)
    except FileNotFoundError:
        try:
            ensure_home()
            atomic_write_json(p, public_config(DEFAULT_CONFIG))
        except Exception as e:
            log_error("config-create", e)
        return normalize_config({})
    except Exception as e:
        _config_error = f"{type(e).__name__}: {e}"
        log_error("config-load", e)
        return normalize_config({})


def parse_hhmm(s):
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return None


def in_quiet_hours(cfg, now=None):
    qh = cfg.get("quiet_hours")
    if not qh:
        return False
    start, end = parse_hhmm(qh[0]), parse_hhmm(qh[1])
    if start is None or end is None or start == end:
        return False
    t = dt.datetime.fromtimestamp(now if now is not None else time.time())
    cur = t.hour * 60 + t.minute
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


# --------------------------------------------------------------------------
# state

def empty_state():
    return {"sessions": {}, "last_fired_at": 0, "last_exercise": None, "paused_until": 0, "timer_pids": [],
            "achievements": {}, "level": None, "coach_last": {}, "next_set": None, "spinner_setup": False,
            "cfg_hash": None, "spinner_last": None, "predict": {}, "decisions": [], "hists": {},
            "idle_spinner": None, "ask_hist": []}


def read_state():
    try:
        with open(path("state.json"), encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            raise ValueError("state.json must be a JSON object")
    except FileNotFoundError:
        return empty_state()
    except Exception as e:
        log_error("state-load", e)
        return empty_state()
    base = empty_state()
    base.update(st)
    if not isinstance(base.get("sessions"), dict):
        base["sessions"] = {}
    for k in ("last_fired_at", "paused_until"):
        if not _valid_num(base.get(k)):
            base[k] = 0
    if not isinstance(base.get("timer_pids"), list):
        base["timer_pids"] = []
    for k in ("achievements", "coach_last", "predict", "hists"):
        if not isinstance(base.get(k), dict):
            base[k] = {}
    if not isinstance(base.get("decisions"), list):
        base["decisions"] = []
    if not isinstance(base.get("level"), int) or isinstance(base.get("level"), bool):
        base["level"] = None
    if not valid_set(base.get("next_set")):
        base["next_set"] = None
    if not isinstance(base.get("spinner_setup"), bool):
        base["spinner_setup"] = False
    if not valid_idle_cache(base.get("idle_spinner")):
        base["idle_spinner"] = None
    base["ask_hist"] = _ask_hist(base)
    for sid in list(base["sessions"]):
        if not isinstance(base["sessions"][sid], dict):
            del base["sessions"][sid]
    return base


def prune(st, now, declined_out=None):
    """Drop sessions idle for SESSION_TTL_S. Returns turn records for any open turn dropped with them
    (no Stop ever came: recorded as interrupted). An ask still open in a dropped session is appended to
    declined_out (a list) as a "declined" log record, as Stop would have done."""
    gone = []
    for sid in list(st["sessions"]):
        s = st["sessions"][sid]
        ts = s.get("updated_at") or s.get("started_at") or 0
        if not _valid_num(ts) or now - ts > SESSION_TTL_S:
            if s.get("token") and isinstance(s.get("turn"), dict):
                gone.append(turn_record(s["turn"], sid, None, True))
            if declined_out is not None:
                try:
                    _c, _a, dec = split_pending((s.get("carried") or []) + (s.get("pending") or []))
                    declined_out += [log_rec(p, sid, now, "declined", asked_at=p.get("asked_at"),
                                             ask_id=p.get("id")) for p in dec]
                    declined_out += settle_grace(s, sid, now)
                except Exception as e:
                    log_error("prune-declined", e)
            del st["sessions"][sid]
    st["timer_pids"] = st["timer_pids"][-20:]
    prune_hists(st, now)
    return gone


HIST_TTL_S = 30 * 86400
HIST_KEEP = 200


def prune_hists(st, now):
    """Per-session turn history (prev / EMA duration, last Stop) outlives the 6 h session prune, as in the
    transcript bootstrap, where a session's history runs across its whole file: a session resumed the
    next day is not a first turn. Kept 30 days, newest 200 sessions."""
    h = st.get("hists")
    if not isinstance(h, dict):
        st["hists"] = {}
        return
    for sid in list(h):
        v = h[sid]
        if not isinstance(v, dict) or not _valid_num(v.get("prev_stop_ts")) or now - v["prev_stop_ts"] > HIST_TTL_S:
            del h[sid]
    if len(h) > HIST_KEEP:
        for sid in sorted(h, key=lambda k: h[k]["prev_stop_ts"])[: len(h) - HIST_KEEP]:
            del h[sid]


@contextlib.contextmanager
def locked_state():
    """Exclusive read-modify-write of state.json."""
    ensure_home()
    with open(path(".lock"), "a") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            st = read_state()
            yield st
            now = time.time()
            declined = []
            gone = prune(st, now, declined)
            if declined:
                try:
                    log_declines(st, declined)
                except Exception as e:
                    log_error("prune-declined-log", e)
            if gone:
                try:
                    append_turns(gone)
                except Exception as e:
                    log_error("turns-censored", e)
            try:
                sync_spinner(st, now=now)  # the spinner follows every state change (no-op when unchanged)
            except Exception as e:
                log_error("spinner-sync", e)
            atomic_write_json(path("state.json"), st)
            write_ui(st, now)  # v6: the mod's feed follows every state change (no write when unchanged)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock():
    ensure_home()
    with open(path(".lock"), "a") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


def is_paused(st, now=None):
    return (st.get("paused_until") or 0) > (now if now is not None else time.time())


# --------------------------------------------------------------------------
# log

def read_log():
    out = []
    try:
        with open(path("log.jsonl"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        out.append(rec)
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return out


def append_log(records):
    ensure_home()
    with open(path("log.jsonl"), "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def local_date(ts):
    return dt.datetime.fromtimestamp(ts).date()


def done_records(log):
    return [r for r in log if r.get("status") == "done" and _valid_num(r.get("ts"))]


def _amount(r):
    try:
        return int(r.get("reps") or 0)
    except (TypeError, ValueError):
        return 0


def streak_days(log, today=None):
    today = today or dt.date.today()
    days = {local_date(r["ts"]) for r in done_records(log)}
    if today in days:
        d = today
    elif today - dt.timedelta(days=1) in days:
        d = today - dt.timedelta(days=1)
    else:
        return 0
    n = 0
    while d in days:
        n += 1
        d -= dt.timedelta(days=1)
    return n


def today_totals(log, today=None):
    today = today or dt.date.today()
    recs = [r for r in done_records(log) if local_date(r["ts"]) == today]
    reps = sum(_amount(r) for r in recs if r.get("unit", "reps") != "seconds")
    secs = sum(_amount(r) for r in recs if r.get("unit") == "seconds")
    return len(recs), reps, secs


# --------------------------------------------------------------------------
# progression (level-ups)

LEVEL_EVERY_DAYS = 3
LEVEL_STEP = 0.1
LEVEL_CAP = 1.5


def multiplier(streak, enabled=True):
    """1.0 base, +0.1 per 3 streak days, capped at 1.5."""
    if not enabled or not isinstance(streak, int) or streak < 0:
        return 1.0
    return round(min(LEVEL_CAP, 1.0 + LEVEL_STEP * (streak // LEVEL_EVERY_DAYS)), 2)


def level_for(streak, enabled=True):
    if not enabled:
        return 1
    return int(round((multiplier(streak, True) - 1.0) / LEVEL_STEP)) + 1


def fmt_level(level, mult):
    return f"Level {level} · ×{mult:.1f}"


def current_mult(cfg, log=None, today=None):
    if not cfg.get("progression", True):
        return 1.0
    return multiplier(streak_days(log if log is not None else read_log(), today), True)


# --------------------------------------------------------------------------
# achievements

ACHIEVEMENTS = [
    # id, name, desc, stat key, target (None = number of configured exercises), unit word
    ("first_blood", "First Blood", "first set logged", "sets", 1, "set"),
    ("reps_100", "Rep Machine I", "100 reps", "reps", 100, "reps"),
    ("reps_500", "Rep Machine II", "500 reps", "reps", 500, "reps"),
    ("reps_1000", "Rep Machine III", "1,000 reps", "reps", 1000, "reps"),
    ("reps_5000", "Rep Machine IV", "5,000 reps", "reps", 5000, "reps"),
    ("streak_3", "Spark", "3-day streak", "streak", 3, "days"),
    ("streak_7", "Week Warrior", "7-day streak", "streak", 7, "days"),
    ("streak_30", "Unbreakable", "30-day streak", "streak", 30, "days"),
    ("marathon", "Marathon", "5+ sets in one Claude turn", "max_turn", 5, "sets"),
    ("night_owl", "Night Owl", "a set between 22:00 and 04:00", "night", 1, ""),
    ("early_bird", "Early Bird", "a set between 05:00 and 08:00", "early", 1, ""),
    ("variety_pack", "Variety Pack", "every exercise within 7 days", "variety", None, "exercises"),
    ("plank_master", "Plank Master", "5+ min of holds in one day", "hold_day", 300, "s"),
]
ACH_BY_ID = {a[0]: a for a in ACHIEVEMENTS}


def ach_stats(log, cfg, today=None):
    today = today or dt.date.today()
    done = done_records(log)
    reps = sum(_amount(r) for r in done if r.get("unit", "reps") != "seconds")
    turns = {}
    hold_by_day = {}
    night = early = 0
    for r in done:
        if r.get("session_id") != "manual":
            key = (r.get("session_id"), r.get("turn_ts", r.get("ts")))  # sets of one turn (v4: logged one by one)
            turns[key] = turns.get(key, 0) + 1
        h = dt.datetime.fromtimestamp(r["ts"]).hour
        if h >= 22 or h < 4:
            night += 1
        if 5 <= h < 8:
            early += 1
        if r.get("unit") == "seconds":
            d = local_date(r["ts"])
            hold_by_day[d] = hold_by_day.get(d, 0) + _amount(r)
    week_start = today - dt.timedelta(days=6)
    names = {e["name"] for e in cfg["exercises"]}
    recent = {r.get("exercise") for r in done if week_start <= local_date(r["ts"]) <= today}
    return {"sets": len(done), "reps": reps, "streak": streak_days(log, today),
            "max_turn": max(turns.values()) if turns else 0, "night": night, "early": early,
            "variety": len(names & recent), "variety_target": len(names),
            "hold_day": max(hold_by_day.values()) if hold_by_day else 0}


def _ach_target(a, stats):
    return stats["variety_target"] if a[4] is None else a[4]


def ach_label(a):
    return f"{a[1]} ({a[2]})"


def unlock_achievements(st, log, cfg, now=None, today=None):
    """Unlock newly earned achievements into st; return their ids (in definition order)."""
    now = now if now is not None else time.time()
    stats = ach_stats(log, cfg, today)
    got = st.get("achievements")
    if not isinstance(got, dict):
        got = st["achievements"] = {}
    new = []
    for a in ACHIEVEMENTS:
        if a[0] in got:
            continue
        if stats[a[3]] >= _ach_target(a, stats):
            got[a[0]] = {"at": now, "name": a[1]}
            new.append(a[0])
    return new


def next_achievement(st, stats):
    """(achievement, remaining) for the locked countable one closest to done, or None."""
    got = st.get("achievements") or {}
    best = None
    for a in ACHIEVEMENTS:
        if a[0] in got or a[3] in ("night", "early"):
            continue
        target = _ach_target(a, stats)
        cur = stats[a[3]]
        if target <= 0 or cur >= target:
            continue
        frac = cur / target
        if best is None or frac > best[0]:
            best = (frac, a, target - cur)
    return (best[1], best[2]) if best else None


def fmt_remaining(a, remaining, target):
    if a[3] == "streak":
        return f"{remaining} more day{'s' if remaining != 1 else ''} to 🏅 {a[1]} ({target}-day streak)"
    if a[3] == "hold_day":
        return f"{fmt_secs(remaining)} more holding in a day to 🏅 {a[1]} (5 min)"
    if a[3] == "variety":
        return f"{remaining} more exercise{'s' if remaining != 1 else ''} this week to 🏅 {a[1]}"
    if a[3] == "sets":
        return f"Log one set to unlock 🏅 {a[1]}"
    return f"{remaining:,} more {a[5]} to 🏅 {a[1]} ({target:,})"


# --------------------------------------------------------------------------
# formatting / notifications

def fmt_totals(n, reps, secs):
    """'3 sets / 45 reps / 60s' -- reps omitted when 0 and only timed sets exist."""
    parts = [f"{n} set{'s' if n != 1 else ''}"]
    if reps or not secs:
        parts.append(f"{reps} reps")
    if secs:
        parts.append(f"{secs}s")
    return " / ".join(parts)


def fmt_set(exercise, reps, unit):
    if unit == "seconds":
        return f"{reps}s {exercise}"
    return f"{reps} {exercise}"


def fmt_secs(s):
    s = int(s)
    if s < 60:
        return f"{s}s"
    m, r = divmod(s, 60)
    return f"{m}m {r}s" if r else f"{m}m"


def applescript_quote(s):
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\r", " ").replace("\n", " ")
    return f'"{s}"'


def osascript_source(title, subtitle, body, cfg):
    src = f"display notification {applescript_quote(body)} with title {applescript_quote(title)}"
    if subtitle:
        src += f" subtitle {applescript_quote(subtitle)}"
    if cfg.get("sound"):
        src += f" sound name {applescript_quote(cfg.get('sound_name') or 'Glass')}"
    return src


def notify(title, subtitle, body, cfg, kind="info", speech=None):
    """macOS banner (+ optional sound / speech). With "notification": false nothing is shown or said; off
    macOS nothing is shown either (the band is the only signal). In a test run, WORKOUT_NOTIFY_LOG records
    every banner that "notification" allows instead of showing it, with "suppressed": true where it would
    not show (not macOS), so the tests see the same events on either platform."""
    if not cfg.get("notification", False):
        return
    mac = is_macos()
    log_p = os.environ.get("WORKOUT_NOTIFY_LOG") if sandboxed() else None
    if log_p:
        rec = {"ts": time.time(), "kind": kind, "title": title, "subtitle": subtitle, "body": body,
               "sound": cfg.get("sound"), "say": speech if cfg.get("say") else None,
               "script": osascript_source(title, subtitle, body, cfg), "suppressed": not mac}
        with open(log_p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return
    if not mac:
        return
    try:
        subprocess.run(["osascript", "-e", osascript_source(title, subtitle, body, cfg)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=False)
    except Exception as e:
        log_error("notify-osascript", e)
    if cfg.get("say") and speech:
        try:
            subprocess.Popen(["say", speech], start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        except Exception as e:
            log_error("notify-say", e)


def notify_achievements(ids, cfg):
    if not ids:
        return
    names = [ach_label(ACH_BY_ID[i]) for i in ids]
    body = " + ".join(names) if len(names) <= 2 else f"{names[0]} + {len(names) - 1} more"
    notify("Workout Buddy", "🏆 Unlocked", body, cfg, kind="achievement",
           speech=f"Unlocked: {ACH_BY_ID[ids[0]][1]}")


def ach_lines(ids):
    """Factual unlock lines for the terminal, e.g. '🏆 Unlocked: Rep Machine I (100 reps)'."""
    return [f"🏆 Unlocked: {ach_label(ACH_BY_ID[i])}" for i in ids]


def pick_exercise(cfg, last=None, rng=random, mult=1.0, exclude=(), avoid=()):
    """A random set from the enabled exercises. Never `last` (the exercise just done) and, v5.2, never one
    in `exclude` (the one just declined) nor in `avoid` (declined in the last 3 asks), as long as an
    alternative is left. When none is, the rules give way in this order: `avoid`, then `last`, then
    `exclude` (one exercise enabled: it repeats)."""
    exs = cfg["exercises"]
    hard, soft = set(exclude), set(avoid)
    pool = exs
    for banned in (hard | soft | {last}, hard | {last}, hard):
        pool = [e for e in exs if e["name"] not in banned]
        if pool:
            break
    pool = pool or exs
    e = rng.choice(pool)
    lo = max(1, int(round(e["min"] * mult)))
    hi = max(lo, int(round(e["max"] * mult)))
    return {"exercise": e["name"], "reps": rng.randint(lo, hi), "unit": e["unit"]}


# --------------------------------------------------------------------------
# the next set (decided ahead of time) + the spinner task
#
# One set is chosen before it is due and kept in state["next_set"], so the
# spinner, the status line and the nudge all name the same task. It is
# re-picked after a set is logged (Stop, `done`) or undone (`skip`), when it
# no longer fits the configured exercises, and (v5.2) after an ask is declined
# (see "rotate on decline" below).

def valid_set(p):
    return bool(isinstance(p, dict) and isinstance(p.get("exercise"), str) and p["exercise"].strip()
                and isinstance(p.get("reps"), int) and not isinstance(p.get("reps"), bool) and p["reps"] >= 1
                and p.get("unit") in ("reps", "seconds"))


def set_fits_config(p, cfg):
    return valid_set(p) and any(e["name"] == p["exercise"] and e["unit"] == p["unit"] for e in cfg["exercises"])


def ensure_next_set(st, cfg, log=None, force=False, rng=random, exclude=()):
    """Return st["next_set"], picking a fresh one (multiplier, never the last exercise, v5.2 never one
    declined in the last 3 asks, nor one in `exclude`, while alternatives exist) when forced or when the
    stored one is missing or no longer fits the config."""
    ns = st.get("next_set")
    if force or not set_fits_config(ns, cfg):
        p = pick_exercise(cfg, st.get("last_exercise"), rng=rng, mult=current_mult(cfg, log), exclude=exclude,
                          avoid=recently_declined(st))
        ns = st["next_set"] = {"exercise": p["exercise"], "reps": p["reps"], "unit": p["unit"]}
    return ns


# --------------------------------------------------------------------------
# rotate on decline (v5.2)
#
# A skipped set must not come back at every later ask. Every declined ask, wherever it is
# declined (Stop, the grace window running out, the session's next prompt or Esc, the 6 h prune), re-picks
# next_set under the .lock, never the declined exercise, and never one declined in the last 3 asks while
# another is enabled. state["ask_hist"] remembers the last ASK_HIST_KEEP ask outcomes by ask id: a declined
# ask re-picks exactly once, however many sessions or code paths see it. An ask already on screen keeps
# its own copy of the set (its pending entry), so a re-pick never changes a question that is up.

ASK_HIST_KEEP = 20
DECLINE_AVOID_ASKS = 3


def _ask_hist(st):
    h = st.get("ask_hist") if isinstance(st, dict) else None
    return [r for r in h if isinstance(r, dict) and isinstance(r.get("exercise"), str)] if isinstance(h, list) else []


def recently_declined(st, n=DECLINE_AVOID_ASKS):
    """The exercises declined in the last n asks (taken asks count as asks too)."""
    return {r["exercise"] for r in _ask_hist(st)[-n:] if r.get("status") == "declined"}


def note_ask(st, ask_id, exercise, status, now=None):
    """Remember one ask outcome. False if this ask id is already on record (nothing changes)."""
    h = _ask_hist(st)
    if ask_id and any(r.get("id") == ask_id for r in h):
        return False
    h.append({"id": ask_id, "exercise": exercise, "status": status,
              "ts": round(now if now is not None else time.time(), 3)})
    st["ask_hist"] = h[-ASK_HIST_KEEP:]
    return True


def rotate_after_declines(st, recs, cfg=None, rng=random):
    """Under the lock, for declined log records just written: re-pick next_set once per ask not already
    on record, never the declined exercise. Returns how many re-picks were made. Never raises."""
    n = 0
    try:
        if not isinstance(st, dict):
            return 0
        for r in recs or []:
            if not (isinstance(r, dict) and r.get("status") == "declined" and isinstance(r.get("exercise"), str)):
                continue
            if not note_ask(st, r.get("ask_id"), r["exercise"], "declined", r.get("ts")):
                continue
            if cfg is None:
                cfg = load_config()
            ensure_next_set(st, cfg, force=True, rng=rng, exclude={r["exercise"]})
            n += 1
    except Exception as e:
        log_error("decline-rotate", e)
    return n


def log_declines(st, recs, cfg=None):
    """Write declined log records, then rotate next_set away from them (under the lock)."""
    if not recs:
        return
    append_log(recs)
    rotate_after_declines(st, recs, cfg)


def task_text(p):
    """'15 push-ups' / '45s plank'."""
    return fmt_set(p["exercise"], p["reps"], p["unit"])


def config_hash(cfg):
    return hashlib.sha1(json.dumps(cfg, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def settings_path():
    """The settings.json that `/workout setup` may change (only after an explicit yes): WORKOUT_SETTINGS_PATH,
    else $CLAUDE_CONFIG_DIR/settings.json, else ~/.claude/settings.json."""
    p = os.environ.get("WORKOUT_SETTINGS_PATH")
    if p:
        return os.path.expanduser(p)
    return os.path.join(claude_config_dir(), "settings.json")


OPEN_STATUSES = (None, "asked")  # a pending entry still on screen: v3.2 "due" (no status) or v4 "asked"


def is_open(p):
    return valid_pending(p) and p.get("status") in OPEN_STATUSES


def due_set(st, now=None, prefer_sid=None, skip_idle=False, asked=None):
    """The set on screen right now, or None: the most recent open set (due and not logged, or asked and
    not yet accepted) of a live turn (token set) whose timer has not given up (nudging is not False),
    within its show_until, while not paused. prefer_sid wins ties for the status line; skip_idle hides an
    idle-looking turn; asked=False only looks at v3.2-style due sets (ask_mode "auto")."""
    now = now if now is not None else time.time()
    if is_paused(st, now):
        return None
    sessions = st.get("sessions") if isinstance(st.get("sessions"), dict) else {}
    best = None
    for sid, s in sessions.items():
        if not isinstance(s, dict) or not s.get("token") or s.get("nudging") is False:
            continue
        if skip_idle and s.get("idle"):
            continue
        ts = s.get("updated_at") or 0
        if not _valid_num(ts) or now - ts > SESSION_TTL_S:
            continue
        pend = [p for p in (s.get("pending") or []) if is_open(p)
                and (asked is None or (p.get("status") == "asked") == asked)]
        if not pend:
            continue
        last = pend[-1]
        show_until = last.get("show_until")
        if not _valid_num(show_until):
            ts0 = last.get("ts")
            show_until = (ts0 + 900) if _valid_num(ts0) else 0
        if now > show_until:
            continue
        key = (1 if prefer_sid and sid == prefer_sid else 0, last.get("ts") or 0)
        if best is None or key > best[0]:
            best = (key, last)
    return best[1] if best else None


def valid_pending(p):
    return (isinstance(p, dict) and isinstance(p.get("exercise"), str) and p["exercise"]
            and _valid_num(p.get("reps")) and p.get("unit", "reps") in ("reps", "seconds"))


def spinner_desired(st, spinner="off", now=None):
    """spinnerVerbs, globally. "off": None (the key absent: Claude Code's own verbs). "task": while a set is
    due (or asked) anywhere, {"mode": "replace", "verbs": ["NOW: 41s plank"]} (Claude Code adds the "…"),
    else None. "progress": the same while due, else the idle progress lines cached in state["idle_spinner"]
    (see idle_verbs; ["Workout Buddy paused"] while paused), None before they were first computed.
    Pure: never reads the log, so a prompt costs nothing; the idle lines change only at refresh points."""
    if spinner not in ("task", "progress"):
        return None
    p = due_set(st, now)
    if p:
        return {"mode": "replace", "verbs": ["NOW: " + fmt_set(p["exercise"], int(p["reps"]), p.get("unit", "reps"))]}
    if spinner != "progress":
        return None
    c = st.get("idle_spinner")
    if not valid_idle_cache(c):
        return None
    return {"mode": "replace", "verbs": list(c["verbs"])}


# --------------------------------------------------------------------------
# the idle spinner: a few progress lines while no set is due
#
# The spinner is what you look at while waiting, so a little hype is welcome here (and only here: the Stop
# message stays a plain fact). Never an instruction ("Up next" is a preview), never a set owed.
# Computed only at refresh points (a set logged at Stop, `/workout done|skip|pause|resume|setup|config`,
# an achievement unlock) or when the cache is stale (a new local day, a changed config, a pause that
# began or ran out), which is checked at Stop and by the timer, never at hook-prompt. Cached in
# state["idle_spinner"]; sync_spinner writes settings.json only when the value differs.

IDLE_MAX = 40
IDLE_MIN_LINES = 3
IDLE_MAX_LINES = 6
PAUSED_VERBS = ["Workout Buddy paused"]
HYPE_WORDS = ("on fire", "heating up", "unstoppable", "locked in", "in the zone", "rolling")
IDLE_FILLERS = ("Workout Buddy standing by", "Every rep counts", "Claude codes, you get strong")


def valid_idle_cache(c):
    return bool(isinstance(c, dict) and isinstance(c.get("verbs"), list) and c["verbs"]
                and all(isinstance(v, str) and v for v in c["verbs"]))


def hype_word(day):
    """One hype word per local day (deterministic, so the list does not churn within a day)."""
    return HYPE_WORDS[day.toordinal() % len(HYPE_WORDS)]


def _emoji_count(s):
    return sum(1 for ch in s if ord(ch) >= 0x1F000 or unicodedata.category(ch) == "So")


def _idle_ach_line(a, remaining, target):
    kind, name = a[3], a[1]
    if kind == "sets":
        return f"First set unlocks {name}"
    if kind == "reps":
        return f"{remaining:,} reps to {name}"
    if kind == "streak":
        return f"{remaining} more day{'s' if remaining != 1 else ''} to {name}"
    if kind == "hold_day":
        return f"{fmt_secs(remaining)} more holding to {name}"
    if kind == "variety":
        return f"{remaining} more exercise{'s' if remaining != 1 else ''} to {name}"
    if kind == "max_turn":
        return f"{name}: {target} sets in one turn"
    return f"{remaining:,} {a[5]} to {name}".replace("  ", " ")


def idle_verbs(cfg, st, log, now=None):
    """3-6 short progress lines (<= 40 chars, no trailing ellipsis, at most one emoji each) from real
    stats; ["Workout Buddy paused"] while paused."""
    now = now if now is not None else time.time()
    if is_paused(st, now):
        return list(PAUSED_VERBS)
    today = local_date(now)
    done = done_records(log)
    n_today = today_totals(log, today)[0]
    streak = streak_days(log, today)
    last_day = max((local_date(r["ts"]) for r in done), default=None)
    lines = []
    if n_today:
        lines.append(f"{n_today} set{'s' if n_today != 1 else ''} today, {hype_word(today)} 🔥")
    elif last_day is not None and (today - last_day).days >= 2:
        lines.append("Rest day? Not on Claude's watch")
    else:
        lines.append("First set of the day is waiting")
    if streak >= 2:
        lines.append(f"{streak}-day streak, keep it alive")
    ns = st.get("next_set")
    if set_fits_config(ns, cfg):
        lines.append(f"Up next: {task_text(ns)}")
    stats = ach_stats(log, cfg, today)
    na = next_achievement(st, stats)
    if na:
        lines.append(_idle_ach_line(na[0], na[1], _ach_target(na[0], stats)))
    week_start = today - dt.timedelta(days=6)
    w_reps = sum(_amount(r) for r in done
                 if week_start <= local_date(r["ts"]) <= today and r.get("unit", "reps") != "seconds")
    if w_reps:
        lines.append(f"Week: {w_reps:,} reps so far")
    out, seen = [], set()
    for v in list(lines) + list(IDLE_FILLERS):
        if len(out) >= IDLE_MIN_LINES and v in IDLE_FILLERS:
            break
        v = " ".join(_CTRL.sub("", v).split()).rstrip(".…").strip()
        if v and len(v) <= IDLE_MAX and _emoji_count(v) <= 1 and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out[:IDLE_MAX_LINES]


def idle_cache_stale(st, cfg, now=None):
    """The cached idle lines are out of date: none yet, a new local day, a changed config, or a pause
    that began / ran out since they were computed."""
    now = now if now is not None else time.time()
    c = st.get("idle_spinner")
    return (not valid_idle_cache(c) or c.get("day") != local_date(now).isoformat()
            or c.get("cfg") != config_hash(cfg) or c.get("paused") != is_paused(st, now))


def refresh_idle_spinner(st, cfg, log=None, now=None, force=True):
    """Recompute state["idle_spinner"] (forced, or only when stale). Only once `/workout setup` has run
    with the progress spinner on. The caller holds the lock; locked_state's exit writes settings.json if
    the value changed. Returns True if recomputed."""
    if not (st.get("spinner_setup") and cfg.get("spinner") == "progress"):
        return False
    now = now if now is not None else time.time()
    if not force and not idle_cache_stale(st, cfg, now):
        return False
    try:
        log = log if log is not None else read_log()
        st["idle_spinner"] = {"day": local_date(now).isoformat(), "cfg": config_hash(cfg),
                              "paused": is_paused(st, now), "verbs": idle_verbs(cfg, st, log, now)}
    except Exception as e:
        log_error("idle-spinner", e)
        return False
    return True


def _detect_indent(text):
    """The indent unit of a pretty-printed JSON object ("  ", "\t", ...), or None for compact JSON."""
    m = re.match(r'\s*\{[ \t]*\r?\n([ \t]+)\S', text)
    return m.group(1) if m else None


_CAS_TEST_HOOK = None  # tests: called between writing the temp file and the final compare


def write_spinner_verbs(value, target):
    """Add / replace (value = dict) or delete (value = None) ONLY the spinnerVerbs key of `target`, the
    settings.json the person agreed to at `/workout setup spinner --yes` (see agreed_settings_path).
    Every other key keeps its value and order; the indent unit, the trailing newline and the file mode
    are kept; the write is a temp file in the same dir + os.replace. A missing or unparsable file is
    skipped (logged), never created or truncated. Compare-and-swap: the file's (mtime, size, inode) is
    taken before reading and checked again right before the replace; if it changed, the read is redone
    once, and if it changed again the write is skipped (logged). The caller holds the workout .lock.
    Returns "written", "unchanged" or "skipped: <why>"."""
    p = target
    if not p:
        return "skipped: no settings.json was agreed to (/workout setup spinner --yes)"
    real = os.path.realpath(p)
    for attempt in (1, 2):
        try:
            sb = os.stat(real)
            with open(real, "rb") as f:
                raw = f.read()
            text = raw.decode("utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("settings.json is not a JSON object")
        except Exception as e:
            log_error("spinner-settings-read", e)
            return f"skipped: could not read {p} ({type(e).__name__})"
        if value is None:
            if "spinnerVerbs" not in data:
                return "unchanged"
            del data["spinnerVerbs"]
        else:
            if data.get("spinnerVerbs") == value:
                return "unchanged"
            data["spinnerVerbs"] = value
        indent = _detect_indent(text)
        out = json.dumps(data, indent=indent, ensure_ascii=False) + ("\n" if raw.endswith(b"\n") else "")
        tmp = f"{real}.workout-tmp.{os.getpid()}.{secrets.token_hex(3)}"
        try:
            with open(tmp, "wb") as f:
                f.write(out.encode("utf-8"))
            os.chmod(tmp, stat.S_IMODE(sb.st_mode))
            if _CAS_TEST_HOOK:
                _CAS_TEST_HOOK(real, attempt)
            now_sb = os.stat(real)
            if (now_sb.st_mtime_ns, now_sb.st_size, now_sb.st_ino) != (sb.st_mtime_ns, sb.st_size, sb.st_ino):
                os.unlink(tmp)
                if attempt == 1:
                    continue  # someone else wrote settings.json meanwhile: re-read and redo once
                log_error("spinner-settings-cas")
                return f"skipped: {p} kept changing while writing"
            os.replace(tmp, real)
        except Exception as e:
            log_error("spinner-settings-write", e)
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            return f"skipped: could not write {p} ({type(e).__name__})"
        return "written"
    return "skipped"  # not reached


SPINNER_RETRY_S = 60


def agreed_settings_path(st):
    """The settings.json the person agreed to at `/workout setup spinner --yes` (its realpath, kept in state),
    or None. The spinner is only ever written there: never to whatever settings.json the current process's
    CLAUDE_CONFIG_DIR points at, since several Claude profiles may share one data dir."""
    v = st.get("spinner_settings_path") if isinstance(st, dict) else None
    return v if isinstance(v, str) and v else None


def sync_spinner(st, force=False, spinner=None, now=None):
    """Make settings.json spinnerVerbs match the global desired value (see spinner_desired), once
    `/workout setup` has run. Called with the workout .lock held: at the end of every locked_state()
    (prompt, timer fire / exit, Stop, every cli command, stale-session pruning) and by setup (force).
    The last value written is cached in state ("spinner_last"), so the steady state costs no file read
    and no write; a failed write is retried at most once a minute. With "spinner": "off" the desired
    value is "no key": it is removed once (if present) and, cached as such, never written again."""
    if not st.get("spinner_setup"):
        return "skipped: not set up (/workout setup)"
    target = agreed_settings_path(st)
    if target is None:
        return "skipped: no settings.json was agreed to (/workout setup spinner --yes)"
    now = now if now is not None else time.time()
    if spinner is None:
        spinner = peek_config().get("spinner", "off")
    desired = spinner_desired(st, spinner, now)
    prior = st.get("spinner_prior")
    if desired is None and isinstance(prior, dict) and prior.get("value") is not None:
        desired = prior["value"]  # nothing due: the person's own spinnerVerbs (from before setup) stand
    key = json.dumps(desired, sort_keys=True, ensure_ascii=False)
    if not force:
        if st.get("spinner_last") == key:
            return "unchanged"
        if _valid_num(st.get("spinner_fail_at")) and now - st["spinner_fail_at"] < SPINNER_RETRY_S:
            return "skipped: retrying later"
    r = write_spinner_verbs(desired, target)
    if r == "written" and desired is None:
        st["spinner_removed_at"] = now  # `/workout setup` can say when (and that it was this add-on)
    if r in ("written", "unchanged"):
        st["spinner_last"] = key
        st.pop("spinner_fail_at", None)
    else:
        st["spinner_fail_at"] = now
    return r


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def tab_title_sequence(recs, streak):
    """OSC 2 window/tab title, e.g. '✅ +15 push-ups · 🔥4d' (control chars stripped)."""
    title = "✅ " + " ".join("+" + fmt_set(r["exercise"], r["reps"], r["unit"]) for r in recs)
    if streak >= 1:
        title += f" · 🔥{streak}d"
    title = _CTRL.sub("", title)[:80]
    return f"\x1b]2;{title}\x07"


# --------------------------------------------------------------------------
# spinner verbs + tips

GENERIC_VERBS = [
    "Repping", "Protein-shaking the dependency tree", "Spotting your merge conflicts",
    "Hydrating the cache (and myself)",
    "Stretching the context window", "Lifting state up", "Bench-pressing the backlog",
    "Deadlifting legacy code", "Pumping iron and APIs", "Cardio-compiling", "Sweating the null checks",
    "Bulking up the test suite", "Cutting dead code", "Burpee-driven development", "Rebasing my glutes",
    "Foam-rolling the stack trace",
    "Leg day for the linter", "Supersetting the subtasks", "Hitting a PR (personal record)",
    "Running laps around the event loop", "Breathing through the build", "Tightening the core module",
    "Benching the benchmarks", "Pre-workout, pre-commit", "No pain, no gain, no flaky tests",
    "Counting reps, not tokens",
]

EXERCISE_VERBS = {
    "push-ups": ["Doing push-ups, pushing code", "git push-up --force"],
    "jumping jacks": ["Jumping jacks-in-the-box", "Jumping to conclusions (and jacks)"],
    "crunches": ["Crunching numbers and abs", "Crunching through the diff"],
    "squats": ["Squatting while Claude squashes bugs", "Squat-merging"],
    "plank": ["Planking (don't you dare drop)", "Planking through the build"],
    "lunges": ["Lunging into the next commit", "Lunging for the deadline"],
    "calf raises": ["Raising calves, raising exceptions"],
    "wall sit": ["Wall-sitting behind the firewall"],
}

FORM_CUES = {
    "push-ups": "Push-ups: tight core, chest to fist height, elbows at 45°",
    "jumping jacks": "Jumping jacks: soft knees, land on the balls of your feet",
    "crunches": "Crunches: exhale up, chin off chest, no yanking your neck",
    "squats": "Squats: weight in heels, knees track toes, sit back like there's a chair",
    "plank": "Plank: straight line head to heels, squeeze glutes, keep breathing",
    "lunges": "Lunges: front knee over ankle, back knee kisses the floor",
    "calf raises": "Calf raises: pause at the top, lower slowly",
    "wall sit": "Wall sit: thighs parallel, back flat on the wall, breathe",
}

VERB_MAX = 45
TIP_MAX = 100


def spinner_verbs(cfg):
    out, seen = [], set()

    def add(v):
        v = v.strip().rstrip(".…").strip()
        if v and len(v) <= VERB_MAX and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)

    for e in cfg["exercises"]:
        verbs = EXERCISE_VERBS.get(e["name"].lower())
        if verbs:
            for v in verbs:
                add(v)
        else:
            add(f"Doing {e['name']} between diffs"[:VERB_MAX])
    for v in GENERIC_VERBS:
        add(v)
    return out


def tips_path():
    return path("tips.json")


def spinner_config(cfg, st=None):
    """Settings to merge by hand: the tips only. spinnerVerbs is never part of it: with "spinner": "task"
    or "progress" this add-on writes it itself, and with "off" (the default) the key stays absent so Claude
    Code shows its own verbs. (The joke verbs are still in spinner_verbs(), for anyone who wants them.)"""
    conf = {}
    conf["spinnerTipsEnabled"] = True
    conf["spinnerTipsOverride"] = {"tipsFile": tips_path(), "label": "Workout Buddy", "excludeDefault": True}
    return conf


def _tip_id(s):
    """Tip id from a key: [A-Za-z0-9._-]{1,64}. If sanitizing loses characters (e.g. a Hebrew
    exercise name), a short hash of the original keeps distinct keys distinct."""
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")
    if clean != s:
        return ("wb-" + clean)[:55].rstrip("-") + "-" + hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]
    return ("wb-" + clean)[:64]


def _clip(s, n=TIP_MAX):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def build_tips(cfg, st, log, today=None):
    """Tip objects in the spinnerTipsOverride entry format: {id, text, cooldownSessions, priority}."""
    today = today or dt.date.today()
    done = done_records(log)
    tips = []

    def tip(tid, text, priority=0, cooldown=0):
        tips.append({"id": _tip_id(tid), "text": _clip(text), "cooldownSessions": cooldown, "priority": priority})

    streak = streak_days(log, today)
    if streak >= 1:
        tip("streak", f"🔥 {streak}-day streak", 6)
    else:
        tip("streak", "No streak yet", 6)

    week_start = today - dt.timedelta(days=6)
    week = [r for r in done if week_start <= local_date(r["ts"]) <= today]
    w_reps = sum(_amount(r) for r in week if r.get("unit", "reps") != "seconds")
    w_secs = sum(_amount(r) for r in week if r.get("unit") == "seconds")
    all_reps = sum(_amount(r) for r in done if r.get("unit", "reps") != "seconds")
    wtxt = f"{w_reps:,} reps this week · {all_reps:,} all-time"
    if w_secs:
        wtxt += f" · {fmt_secs(w_secs)} held"
    tip("week", wtxt, 5)

    n_today = today_totals(log, today)[0]
    n_yday = today_totals(log, today - dt.timedelta(days=1))[0]
    tip("today", f"Today: {n_today} set{'s' if n_today != 1 else ''} · yesterday: {n_yday}", 5)

    # No "Next: <set>" tip: tips show under the spinner on every turn, and the set is shown only once due.

    stats = ach_stats(log, cfg, today)
    na = next_achievement(st, stats)
    if na:
        a, remaining = na
        tip("next-achievement", fmt_remaining(a, remaining, _ach_target(a, stats)), 5)
    got = st.get("achievements") or {}
    tip("trophies", f"🏆 {len(got)}/{len(ACHIEVEMENTS)} achievements unlocked · /workout achievements", 2)

    if cfg.get("progression", True):
        lvl, mult = level_for(streak), multiplier(streak)
        if mult < LEVEL_CAP:
            more = LEVEL_EVERY_DAYS - streak % LEVEL_EVERY_DAYS
            tip("level", f"{fmt_level(lvl, mult)} reps · {more} more streak day{'s' if more != 1 else ''} "
                         f"to Level {lvl + 1}", 3)
        else:
            tip("level", f"{fmt_level(lvl, mult)} reps · max level", 3)

    for e in cfg["exercises"]:
        cue = FORM_CUES.get(e["name"].lower()) or f"{e['name']}: slow and controlled beats fast and sloppy"
        tip("form-" + e["name"], cue, 0, 2)

    tip("share", "/workout share → a screenshot-ready weekly card", 1, 3)
    tip("pause", "In a meeting? /workout pause 60 · back with /workout resume", 1, 3)
    tip("undo", "Didn't do the set? /workout undo takes back the last one", 1, 3)
    return tips[:200]


def write_tips(cfg=None, st=None, log=None):
    """Rewrite tips.json atomically. Never raises."""
    try:
        cfg = cfg or load_config()
        st = st if st is not None else read_state()
        log = log if log is not None else read_log()
        ensure_home()
        atomic_write_json(tips_path(), {"tips": build_tips(cfg, st, log)})
    except Exception as e:
        log_error("tips", e)


# --------------------------------------------------------------------------
# turn-length predictor
#
# At hook-prompt the prompt is reduced to a few numbers and a one-word category (the text is dropped
# right there), p_long = P(the turn runs >= long_s) is estimated, and the policy picks when the set
# becomes due. At Stop the real duration is appended to turns.jsonl; model.json is retrained from it
# in a detached process every `retrain_every` finished turns, or by `/workout train`.
#
#   p_long = w * p_model + (1 - w) * p_prior,   w = n / (n + 40),  n = turns the model was trained on
#   p_long >= hi -> due at early_delay_s (15) | p_long <= lo -> late_delay_s (120) | else delay_s (60)
#   never earlier than 10 s; a turn that ends before its due time never shows a task (Stop clears the
#   turn's token under the lock before the timer can mark the set due).

MIN_DELAY_S = 10.0
PRIOR_N = 40.0
EMA_ALPHA = 0.3
TURNS_MAX = 5000
TURNS_SLACK = 250
MODEL_VERSION = 1
MIN_TRAIN_ROWS = 20
L2 = 0.01
EPOCHS = 400
LR = 0.5
MOMENTUM = 0.9
ADAPT_STEP = 0.05
ADAPT_CAP = 0.95
ADAPT_WINDOW = 30
ADAPT_MIN_RAISE = 10
ADAPT_RAISE_RATE = 0.15
ADAPT_LOWER_RATE = 0.05
FALSE_EARLY_MARGIN_S = 30
BRIEF_SHOW_S = 20  # evaluation only: a set on screen less than this before Stop
EARLY_FIRE_SLACK_S = 5
DECISIONS_KEEP = 5
MIN_SUPPORT = 30  # a yes/no feature seen fewer times than this (either way) gets no weight

FEATURE_NAMES = ("log_chars", "log_words", "log_lines", "code_block", "slash_command", "question_mark",
                 "build_verb", "build_verb_first", "has_prev_turn", "log_prev_duration", "log_ema_duration",
                 "has_prev_stop", "log_since_prev_stop", "hour_sin", "hour_cos") + tuple(
                     "cat_" + c for c in CATEGORIES)

FEATURE_LABELS = {
    "log_chars": "prompt length", "log_words": "word count", "log_lines": "line count",
    "code_block": "code block", "slash_command": "slash command", "question_mark": "question mark",
    "build_verb": "build verb", "build_verb_first": "starts with a build verb",
    "has_prev_turn": "earlier turn in session", "log_prev_duration": "previous turn's length",
    "log_ema_duration": "session's average turn length", "has_prev_stop": "earlier Stop in session",
    "log_since_prev_stop": "time since the last Stop", "hour_sin": "hour of day", "hour_cos": "hour of day",
}


def min_delay_s():
    """10 s. A test run (WORKOUT_TEST=1) may lower it via WORKOUT_MIN_DELAY_S."""
    if sandboxed():
        v = os.environ.get("WORKOUT_MIN_DELAY_S")
        if v is not None:
            try:
                return max(0.0, float(v))
            except ValueError:
                pass
    return MIN_DELAY_S


def is_slash(text):
    t = text.lstrip() if isinstance(text, str) else ""
    return len(t) > 1 and t[0] == "/" and t[1].isalpha()


def text_features(text):
    """Numbers + a one-word category from a prompt. Nothing here keeps any of the text."""
    t = text if isinstance(text, str) else ""
    words = _words(t)
    slash = is_slash(t)
    rest = words[1:] if slash else words  # a slash command's name is not a verb; its arguments may be
    return {
        "char_len": len(t),
        "word_count": len(words),
        "line_count": (t.count("\n") + 1) if t else 0,
        "has_code_block": "```" in t,
        "is_slash_command": slash,
        "question_mark": "?" in t,
        "category": classify_prompt(t),
        "build_verb": any(w in BUILD_VERBS for w in rest[:400]),
        "build_verb_first": bool(rest) and rest[0] in BUILD_VERBS,
    }


def session_features(hist, now):
    hist = hist if isinstance(hist, dict) else {}
    pd, ema, ps = hist.get("prev_duration_s"), hist.get("ema_duration_s"), hist.get("prev_stop_ts")
    return {
        "session_prev_duration_s": round(pd, 3) if _valid_num(pd) else None,
        "session_ema_duration_s": round(ema, 3) if _valid_num(ema) else None,
        "seconds_since_session_prev_stop": round(now - ps, 3) if _valid_num(ps) and now >= ps else None,
        "hour_of_day": dt.datetime.fromtimestamp(now).hour,
    }


def hist_after(hist, duration, stop_ts):
    """Session history after a finished turn (an interrupted turn leaves it unchanged)."""
    hist = hist if isinstance(hist, dict) else {}
    ema = hist.get("ema_duration_s")
    ema = duration if not _valid_num(ema) else EMA_ALPHA * duration + (1 - EMA_ALPHA) * ema
    return {"prev_duration_s": round(duration, 3), "ema_duration_s": round(ema, 3), "prev_stop_ts": stop_ts}


def _lg(v):
    return math.log1p(float(v)) if _valid_num(v) else 0.0


def vectorize(f):
    pd, ema, gap = f.get("session_prev_duration_s"), f.get("session_ema_duration_s"), \
        f.get("seconds_since_session_prev_stop")
    h = f.get("hour_of_day")
    ang = 2 * math.pi * (h % 24) / 24 if _valid_num(h) else 0.0
    cat = f.get("category")
    return [_lg(f.get("char_len")), _lg(f.get("word_count")), _lg(f.get("line_count")),
            float(bool(f.get("has_code_block"))), float(bool(f.get("is_slash_command"))),
            float(bool(f.get("question_mark"))), float(bool(f.get("build_verb"))),
            float(bool(f.get("build_verb_first"))),
            1.0 if _valid_num(pd) else 0.0, _lg(pd), _lg(ema), 1.0 if _valid_num(gap) else 0.0, _lg(gap),
            math.sin(ang) if _valid_num(h) else 0.0, math.cos(ang) if _valid_num(h) else 0.0] + \
        [1.0 if cat == c else 0.0 for c in CATEGORIES]


def prior_p(f):
    """Hand-written prior for P(long turn), used alone until there is data and blended in after."""
    if f.get("category") == "ack":
        return 0.02
    if f.get("category") == "go":
        return 0.5  # not in the v3 brief: a go-ahead starts work (see CATEGORIES)
    if f.get("is_slash_command"):
        return 0.1
    n = f.get("char_len") or 0
    verb = bool(f.get("build_verb"))
    if n < 40 and not verb:
        return 0.1
    if verb and n > 80:
        return 0.7
    return 0.35


def sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 50.0)))
    e = math.exp(max(z, -50.0))
    return e / (1.0 + e)


_model_cache = {}


def valid_model(m):
    d = len(FEATURE_NAMES)
    try:
        return (isinstance(m, dict) and m.get("version") == MODEL_VERSION and list(m.get("features")) == list(FEATURE_NAMES)
                and all(isinstance(m.get(k), list) and len(m[k]) == d and all(_finite(v) for v in m[k])
                        for k in ("mean", "std", "w"))
                and all(v > 0 for v in m["std"]) and _finite(m.get("b"))
                and isinstance(m.get("n"), int) and m["n"] >= 0 and _valid_num(m.get("long_s")))
    except Exception:
        return False


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def load_model():
    """model.json, or None. Cached per process on (mtime, size)."""
    p = path("model.json")
    try:
        sb = os.stat(p)
    except OSError:
        return None
    key = (p, sb.st_mtime_ns, sb.st_size)
    if _model_cache.get("key") == key:
        return _model_cache["model"]
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        if not valid_model(m):
            raise ValueError("model.json does not match this version's features")
    except Exception as e:
        log_error("model-load", e)
        m = None
    _model_cache.update(key=key, model=m)
    return m


def model_parts(m, f):
    """(p_model, [(contribution, feature), ...]) where contribution = w_j * standardized x_j."""
    z = m["b"]
    contrib = []
    for name, x, mu, sd, w in zip(FEATURE_NAMES, vectorize(f), m["mean"], m["std"], m["w"]):
        c = w * (x - mu) / sd
        z += c
        contrib.append((c, name))
    return sigmoid(z), contrib


def predict_long(cfg, f, model=None):
    """{p_long, p_prior, p_model, w, n, top} for features f."""
    prior = prior_p(f)
    m = model if model is not None and model.get("long_s") == cfg["long_s"] else None
    if m and m["n"] > 0:
        pm, contrib = model_parts(m, f)
        w = m["n"] / (m["n"] + PRIOR_N)
        # The blend weight is the evidence for THIS prompt: a yes/no feature it has (an ack, a slash
        # command) that the model saw on fewer than MIN_SUPPORT turns (k; its weight is masked to 0)
        # caps w at k/(k+40). One ack in 1,753 turns leaves an ack to the prior (0.02), not to a model
        # that knows nothing about acks. Features seen 30+ times keep the plain n/(n+40).
        sup = m.get("support") if isinstance(m.get("support"), dict) else {}
        for name, x in zip(FEATURE_NAMES, vectorize(f)):
            k = sup.get(name)
            if x == 1.0 and isinstance(k, int) and not isinstance(k, bool) and 0 <= k < MIN_SUPPORT:
                w = min(w, k / (k + PRIOR_N))
        p = w * pm + (1 - w) * prior
        top = [[name, round(c, 3)] for c, name in sorted(contrib, key=lambda t: -abs(t[0]))[:4]]
        return {"p_long": round(p, 4), "p_prior": prior, "p_model": round(pm, 4), "w": round(w, 4),
                "n": m["n"], "top": top}
    return {"p_long": prior, "p_prior": prior, "p_model": None, "w": 0.0, "n": 0, "top": []}


def effective_hi(cfg, st):
    bump = ((st or {}).get("predict") or {}).get("hi_bump") or 0
    hi = cfg["predict_hi"]
    return round(max(hi, min(ADAPT_CAP, hi + max(0.0, bump))), 4)


def choose_delay(cfg, p, hi):
    """(band, delay_s). Bands: early / mid / late, or flat with "predict": false. Never < 10 s."""
    if not cfg.get("predict", True):
        band, d = "flat", cfg["delay_s"]
    elif p >= hi:
        band, d = "early", cfg["early_delay_s"]
    elif p <= cfg["predict_lo"]:
        band, d = "late", cfg["late_delay_s"]
    else:
        band, d = "mid", cfg["delay_s"]
    return band, max(min_delay_s(), float(d))


def predict_state(st):
    pr = st.get("predict")
    if not isinstance(pr, dict):
        pr = st["predict"] = {}
    if not _valid_num(pr.get("hi_bump")):
        pr["hi_bump"] = 0.0
    if not isinstance(pr.get("early"), list):
        pr["early"] = []
    if not isinstance(pr.get("new_since_train"), int) or isinstance(pr.get("new_since_train"), bool):
        pr["new_since_train"] = 0
    return pr


def adapt_after_turn(st, cfg, turn, duration):
    """Self-correction of hi from early fires: a "false early" fired early but the turn ended within
    early_delay_s + 30 s. Over the last 30 early fires: > 15% false (with >= 10 fires) raises hi by 0.05
    (hi <= 0.95); < 5% (with 30 fires) lowers it 0.05 back toward the configured value. The window
    restarts after every change, so one bad stretch moves hi once, not on every following fire."""
    if turn.get("band") != "early" or not _valid_num(turn.get("fired_at_s")):
        return None
    if turn["fired_at_s"] > float(turn.get("decided_delay_s") or 0) + EARLY_FIRE_SLACK_S:
        return None  # held back by the cooldown: not an early fire
    pr = predict_state(st)
    false_early = duration < float(cfg["early_delay_s"]) + FALSE_EARLY_MARGIN_S
    win = (pr["early"] + [1 if false_early else 0])[-ADAPT_WINDOW:]
    rate = sum(win) / len(win)
    change = None
    if len(win) >= ADAPT_MIN_RAISE and rate > ADAPT_RAISE_RATE and cfg["predict_hi"] + pr["hi_bump"] < ADAPT_CAP:
        pr["hi_bump"] = round(min(ADAPT_CAP - cfg["predict_hi"], pr["hi_bump"] + ADAPT_STEP), 4)
        change, win = "up", []
    elif len(win) >= ADAPT_WINDOW and rate < ADAPT_LOWER_RATE and pr["hi_bump"] > 0:
        pr["hi_bump"] = round(max(0.0, pr["hi_bump"] - ADAPT_STEP), 4)
        change, win = "down", []
    pr["early"] = win
    return change


# ---- turns.jsonl


def sid_hash(sid):
    return hashlib.sha1(str(sid).encode("utf-8")).hexdigest()[:10]


def turn_record(turn, sid, duration, censored, source="live"):
    return {"ts": turn.get("ts"), "sid": sid_hash(sid), "source": source, "features": turn.get("features") or {},
            "duration_s": None if censored else round(duration, 3), "censored": bool(censored),
            "decided_delay_s": turn.get("decided_delay_s"), "p_long": turn.get("p_long"),
            "band": turn.get("band"), "fired": bool(turn.get("fired")), "fired_at_s": turn.get("fired_at_s")}


def append_turns(recs):
    """Record finished turns (prompt-shape numbers and the duration, never the text) for the predictor.
    Only where the predictor can use them ("predict": true, or "timing": "classic"): the public default (the
    waiting meter, predictor off) keeps no turn records at all."""
    if not recs:
        return
    cfg = peek_config()
    if not cfg.get("predict", False) and meter_mode(cfg):
        return
    ensure_home()
    p = path("turns.jsonl")
    with open(p, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    try:
        if os.path.getsize(p) > TURNS_MAX * 300:  # cheap gate before counting lines (~450 bytes a line)
            with open(p, "rb") as f:
                n = sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b""))
            if n > TURNS_MAX + TURNS_SLACK:
                write_turns(read_turns())
    except Exception as e:
        log_error("turns-trim", e)


def read_turns():
    out = []
    try:
        with open(path("turns.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict) and _valid_num(r.get("ts")) and isinstance(r.get("features"), dict):
                    out.append(r)
    except FileNotFoundError:
        pass
    return out


def write_turns(recs):
    """Rewrite turns.jsonl time-ordered, keeping the newest TURNS_MAX."""
    recs = sorted(recs, key=lambda r: r["ts"])[-TURNS_MAX:]
    ensure_home()
    atomic_write_bytes(path("turns.jsonl"),
                       "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs).encode("utf-8"))
    return len(recs)


def labelled(recs, long_s):
    """Uncensored turns as (features, label) in time order."""
    rows = [r for r in recs if not r.get("censored") and _valid_num(r.get("duration_s"))]
    rows.sort(key=lambda r: r["ts"])
    return [(r["features"], 1 if r["duration_s"] >= long_s else 0, r) for r in rows]


# ---- training: logistic regression, batch gradient descent (Nesterov momentum) + L2, zero init


def fit_logreg(feats, ys, l2=L2, epochs=EPOCHS, lr=LR):
    """Deterministic: zero weights, bias at the base rate's logit, full-batch steps, no sampling."""
    n, d = len(ys), len(FEATURE_NAMES)
    cols = [list(c) for c in zip(*[vectorize(f) for f in feats])]
    # A yes/no feature with fewer than MIN_SUPPORT examples on either side is masked (weight fixed at 0):
    # 8 slash commands or 1 true ack would otherwise get a large weight from a handful of turns.
    masked, support = [], {}
    for j, c in enumerate(cols):
        if all(v in (0.0, 1.0) for v in c):
            ones = int(sum(c))
            support[FEATURE_NAMES[j]] = ones
            if 0 < min(ones, n - ones) < MIN_SUPPORT:
                masked.append(FEATURE_NAMES[j])
                cols[j] = [0.0] * n
    mean = [sum(c) / n for c in cols]
    std = []
    for c, mu in zip(cols, mean):
        sd = math.sqrt(sum((v - mu) ** 2 for v in c) / n)
        std.append(sd if sd > 1e-9 else 1.0)
    cols = [[(v - mu) / sd for v in c] for c, mu, sd in zip(cols, mean, std)]
    mean = [0.0 if FEATURE_NAMES[j] in masked else mu for j, mu in enumerate(mean)]
    base = min(1 - 1e-3, max(1e-3, sum(ys) / n))
    b = math.log(base / (1 - base))
    w = [0.0] * d
    vw, vb = [0.0] * d, 0.0
    ran = 0
    for ep in range(epochs):
        ran = ep + 1
        lw = [wj + MOMENTUM * v for wj, v in zip(w, vw)]
        lb = b + MOMENTUM * vb
        z = [lb] * n
        for j in range(d):
            wj = lw[j]
            if wj:
                z = [zi + wj * xi for zi, xi in zip(z, cols[j])]
        err = [sigmoid(zi) - yi for zi, yi in zip(z, ys)]
        gb = sum(err) / n
        gw = [sum(map(operator.mul, err, cols[j])) / n + l2 * lw[j] for j in range(d)]
        vw = [MOMENTUM * v - lr * g for v, g in zip(vw, gw)]
        vb = MOMENTUM * vb - lr * gb
        w = [wj + v for wj, v in zip(w, vw)]
        b += vb
        if max(abs(gb), max(abs(g) for g in gw)) < 1e-5:
            break
    return {"version": MODEL_VERSION, "features": list(FEATURE_NAMES), "mean": [round(v, 6) for v in mean],
            "std": [round(v, 6) for v in std], "w": [round(v, 6) for v in w], "b": round(b, 6), "n": n,
            "base_rate": round(sum(ys) / n, 4), "l2": l2, "epochs": ran, "masked": masked, "support": support}


def train_model(cfg, recs=None):
    """Retrain model.json from turns.jsonl. Returns (model or None, message)."""
    recs = read_turns() if recs is None else recs
    rows = labelled(recs, cfg["long_s"])
    if len(rows) < MIN_TRAIN_ROWS or len({y for _, y, _ in rows}) < 2:
        return None, (f"not enough data to train: {len(rows)} finished turns"
                      f"{'' if len(rows) < MIN_TRAIN_ROWS else ', all on one side of long_s'}"
                      f" (need {MIN_TRAIN_ROWS}, both short and long); the prior is used meanwhile")
    t0 = time.perf_counter()
    m = fit_logreg([f for f, _, _ in rows], [y for _, y, _ in rows])
    m["long_s"] = cfg["long_s"]
    m["trained_at"] = round(time.time(), 3)
    m["sources"] = dict(collections.Counter(r.get("source", "live") for _, _, r in rows))
    m["train_s"] = round(time.perf_counter() - t0, 3)
    ensure_home()
    atomic_write_json(path("model.json"), m)
    return m, (f"trained on {m['n']} finished turns ({', '.join(f'{v} {k}' for k, v in sorted(m['sources'].items()))}), "
               f"long = {cfg['long_s']}s+, base rate {m['base_rate']:.0%}, {m['epochs']} epochs, {m['train_s']}s")


def cmd_train_bg():
    """Detached retrain (spawned by Stop). One at a time: a second one exits at once."""
    ensure_home()
    with open(path(".train.lock"), "a") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        try:
            train_model(load_config())
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


# ---- evaluation


def auc(scores, labels):
    """ROC AUC (Mann-Whitney, ties averaged); None without both classes."""
    pairs = sorted(zip(scores, labels), key=lambda t: t[0])
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return None
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg = (i + 1 + j) / 2  # mean of ranks i+1 .. j
        rank_sum += avg * sum(1 for k in range(i, j) if pairs[k][1])
        i = j
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ys) if ys else None


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def simulate_fires(starts, durs, delays, cooldown_s, repeat_s):
    """Replay turns in time order the way the timers run them: a turn's first set is due at its delay,
    then every repeat_s while it runs, and no two sets anywhere closer than cooldown_s. A set is shown only
    if its time comes before the turn ends. Returns, per turn, the fire times in seconds after its start."""
    n = len(starts)
    ends = [a + d for a, d in zip(starts, durs)]
    cand = [a + dl for a, dl in zip(starts, delays)]
    fires = [[] for _ in range(n)]
    active = {i for i in range(n) if cand[i] < ends[i]}
    last = -math.inf
    while active:
        gate = last + cooldown_s
        i = min(active, key=lambda k: (max(cand[k], gate), cand[k], k))
        t = max(cand[i], gate)
        if t >= ends[i]:
            active.discard(i)  # the cooldown outlasts this turn; it only gets later
            continue
        fires[i].append(t - starts[i])
        last = t
        cand[i] = t + max(repeat_s, 1e-6)
        if cand[i] >= ends[i]:
            active.discard(i)
    return fires


def evaluate(recs, cfg, frac=0.8):
    """Time-ordered holdout: fit on the first 80% of finished turns, score the last 20%."""
    rows = labelled(recs, cfg["long_s"])
    out = {"n_total": len(recs), "n_finished": len(rows),
           "n_censored": sum(1 for r in recs if r.get("censored")),
           "by_source": dict(collections.Counter(r.get("source", "live") for r in recs)),
           "base_rate": (sum(y for _, y, _ in rows) / len(rows)) if rows else None,
           "first_ts": rows[0][2]["ts"] if rows else None, "last_ts": rows[-1][2]["ts"] if rows else None}
    cut = int(len(rows) * frac)
    train, test = rows[:cut], rows[cut:]
    out.update(n_train=len(train), n_test=len(test))
    if len(train) < MIN_TRAIN_ROWS or len({y for _, y, _ in train}) < 2 or not test:
        out["error"] = "not enough finished turns for a holdout (need 20+ in the first 80%, both classes)"
        return out
    m = fit_logreg([f for f, _, _ in train], [y for _, y, _ in train])
    m["long_s"] = cfg["long_s"]
    ys = [y for _, y, _ in test]
    durs = [r["duration_s"] for _, _, r in test]
    pri = [prior_p(f) for f, _, _ in test]
    mod = [model_parts(m, f)[0] for f, _, _ in test]
    w = m["n"] / (m["n"] + PRIOR_N)
    bl = [predict_long(cfg, f, m)["p_long"] for f, _, _ in test]  # the live blend, per-prompt weight included
    base = m["base_rate"]
    out.update(test_first_ts=test[0][2]["ts"], test_last_ts=test[-1][2]["ts"], w=w,
               auc_model=auc(mod, ys), auc_blend=auc(bl, ys), auc_prior=auc(pri, ys),
               brier_model=brier(mod, ys), brier_blend=brier(bl, ys), brier_prior=brier(pri, ys),
               brier_base=brier([base] * len(ys), ys), test_base_rate=sum(ys) / len(ys))
    hi, early = cfg["predict_hi"], float(cfg["early_delay_s"])
    flat = max(min_delay_s(), float(cfg["delay_s"]))
    pol = [choose_delay(dict(cfg, predict=True), p, hi) for p in bl]
    bands = collections.Counter(b for b, _ in pol)
    short = [i for i, y in enumerate(ys) if not y]
    long_ = [i for i, y in enumerate(ys) if y]
    shown = [durs[i] >= pol[i][1] for i in range(len(ys))]
    flat_shown = [d >= flat for d in durs]
    gain = [max(0.0, durs[i] - pol[i][1]) - max(0.0, durs[i] - flat) for i in range(len(ys))]
    early_fired = [i for i in range(len(ys)) if pol[i][0] == "early" and shown[i]]
    out.update(
        bands=dict(bands),
        short_shown_pct=100.0 * sum(shown[i] for i in short) / len(short) if short else None,
        short_shown_flat_pct=100.0 * sum(flat_shown[i] for i in short) / len(short) if short else None,
        tiny_shown_pct=(100.0 * sum(1 for i in range(len(ys)) if durs[i] < early and shown[i]) /
                        max(1, sum(1 for d in durs if d < early))),
        long_early_pct=100.0 * sum(1 for i in long_ if pol[i][1] <= early + EARLY_FIRE_SLACK_S) / len(long_)
        if long_ else None,
        gain_median_long_s=_median([gain[i] for i in long_]),
        gain_mean_all_s=sum(gain) / len(gain),
        gain_total_min=sum(gain) / 60.0,
        missed_vs_flat_pct=100.0 * sum(1 for i in range(len(ys)) if flat_shown[i] and not shown[i]) / len(ys),
        extra_vs_flat_pct=100.0 * sum(1 for i in range(len(ys)) if shown[i] and not flat_shown[i]) / len(ys),
        false_early_pct=(100.0 * sum(1 for i in early_fired if durs[i] < early + FALSE_EARLY_MARGIN_S) /
                         len(early_fired)) if early_fired else None,
        n_early_fired=len(early_fired),
    )
    # With the global cooldown and repeats, as live: sets shown, short turns that showed one, and how long
    # a task is on screen (time from the first set to Stop; each set is a fixed count, not exercise time).
    starts = [r["ts"] for _, _, r in test]
    cd, rep_s = float(cfg["cooldown_s"]), float(cfg["repeat_s"])
    sim = {"policy": simulate_fires(starts, durs, [d for _, d in pol], cd, rep_s),
           "flat": simulate_fires(starts, durs, [flat] * len(durs), cd, rep_s)}
    for k, fires in sim.items():
        out[f"cd_sets_{k}"] = sum(len(f) for f in fires)
        out[f"cd_turns_shown_{k}"] = sum(1 for f in fires if f)
        out[f"cd_short_shown_{k}"] = sum(1 for i in short if fires[i])
        out[f"cd_onscreen_min_{k}"] = sum(durs[i] - f[0] for i, f in enumerate(fires) if f) / 60.0
    efc = [i for i, f in enumerate(sim["policy"]) if f and pol[i][0] == "early"
           and f[0] <= pol[i][1] + EARLY_FIRE_SLACK_S]
    out.update(n_short=len(short), cd_early_fires=len(efc),
               cd_false_early=sum(1 for i in efc if durs[i] < early + FALSE_EARLY_MARGIN_S),
               cd_brief=sum(1 for f, d in zip(sim["policy"], durs) if f and d - f[0] < BRIEF_SHOW_S),
               cd_brief_flat=sum(1 for f, d in zip(sim["flat"], durs) if f and d - f[0] < BRIEF_SHOW_S))
    return out


def _pct(v):
    return "n/a" if v is None else f"{v:.1f}%"


def _num(v, fmt="{:.3f}"):
    return "n/a" if v is None else fmt.format(v)


def _day(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if _valid_num(ts) else "?"


def format_eval(ev, cfg):
    src = ", ".join(f"{v} {k}" for k, v in sorted(ev["by_source"].items())) or "none"
    lines = [f"Turns: {ev['n_total']} ({src}); {ev['n_finished']} finished, {ev['n_censored']} interrupted "
             f"(no Stop; kept out of training)"]
    if ev["n_finished"]:
        lines.append(f"Dates: {_day(ev['first_ts'])} → {_day(ev['last_ts'])} · long = {cfg['long_s']}s+ · "
                     f"base rate {ev['base_rate']:.1%}")
    if ev.get("error"):
        lines.append(ev["error"])
        return "\n".join(lines)
    lines += [
        f"Holdout (time-ordered): train {ev['n_train']} (to {_day(ev['test_first_ts'])}), "
        f"test {ev['n_test']} ({_day(ev['test_first_ts'])} → {_day(ev['test_last_ts'])}), "
        f"test base rate {ev['test_base_rate']:.1%}",
        f"  AUC    model {_num(ev['auc_model'])} · blended {_num(ev['auc_blend'])} (w={ev['w']:.2f}) · "
        f"prior {_num(ev['auc_prior'])}",
        f"  Brier  model {_num(ev['brier_model'])} · blended {_num(ev['brier_blend'])} · prior "
        f"{_num(ev['brier_prior'])} · base rate {_num(ev['brier_base'])} (lower is better)",
        f"Policy on the holdout, each turn alone (hi {cfg['predict_hi']}, lo {cfg['predict_lo']}; due at "
        f"{cfg['early_delay_s']}/{cfg['delay_s']}/{cfg['late_delay_s']}s): "
        + ", ".join(f"{k} {v}" for k, v in sorted(ev["bands"].items())),
        "  turns that ended before their due time and showed a task: 0 (by construction)",
        f"  turns under {cfg['early_delay_s']}s that showed a task: {_pct(ev['tiny_shown_pct'])}",
        f"  short turns (<{cfg['long_s']}s) that showed a task: {_pct(ev['short_shown_pct'])} "
        f"(flat {cfg['delay_s']}s rule: {_pct(ev['short_shown_flat_pct'])})",
        f"  long turns caught early (due ≤ {float(cfg['early_delay_s']) + EARLY_FIRE_SLACK_S:g}s): "
        f"{_pct(ev['long_early_pct'])}",
        f"  task on screen vs the flat rule (screen time, not exercise: a set is a fixed count): median "
        f"{_num(ev['gain_median_long_s'], '{:+.0f}s')} per long turn",
        f"  a task the flat rule would show but this one does not: {_pct(ev['missed_vs_flat_pct'])} of turns; "
        f"the reverse: {_pct(ev['extra_vs_flat_pct'])}",
        f"  false early (fired early, turn over within {float(cfg['early_delay_s']) + FALSE_EARLY_MARGIN_S:g}s): "
        f"{_pct(ev['false_early_pct'])} of {ev['n_early_fired']} early fires",
        f"Replayed in time order with the global cooldown ({cfg['cooldown_s']}s) and repeats ({cfg['repeat_s']}s), "
        f"as live — this policy vs the flat {cfg['delay_s']}s rule:",
        f"  sets shown: {ev['cd_sets_policy']} vs {ev['cd_sets_flat']} (turns with a set: "
        f"{ev['cd_turns_shown_policy']} vs {ev['cd_turns_shown_flat']} of {ev['n_test']})",
        f"  short turns (<{cfg['long_s']}s) that showed a task: {ev['cd_short_shown_policy']} vs "
        f"{ev['cd_short_shown_flat']} of {ev['n_short']}",
        f"  sets on screen under {BRIEF_SHOW_S}s before Stop: {ev['cd_brief']} vs {ev['cd_brief_flat']}",
        f"  false early: {ev['cd_false_early']} of {ev['cd_early_fires']} early fires",
        f"  task on screen in total: {ev['cd_onscreen_min_policy']:.0f} vs {ev['cd_onscreen_min_flat']:.0f} min",
    ]
    return "\n".join(lines)


# ---- bootstrap from Claude Code transcripts (read-only)
#
# A transcript is ~/.claude/projects/<project>/<session>.jsonl, one JSON object per line. Subagent
# transcripts live in <session>/subagents/ and are not read. Rules (from the files on disk):
#   real prompt  = type "user", not isSidechain / isMeta / isCompactSummary / isVisibleInTranscriptOnly,
#                  no toolUseResult and no tool_result block, string or text-block content, origin.kind
#                  absent or "human", and not a <local-command-*>, <bash-*>, Caveat: or interrupt text.
#                  "<command-name>/x</command-name><command-args>a</command-args>" becomes "/x a".
#   boundary     = any other turn-starting user entry, checked BEFORE isMeta (peer messages carry
#                  isMeta: true): origin.kind task-notification / peer / auto-continuation (or another
#                  non-human kind), a scheduled task, injected text (see is_injected), and a "!" shell
#                  command (<bash-input>), which Claude Code runs itself, outside any model turn.
#   turn end     = a main-thread system entry with subtype "turn_duration": Claude Code writes it when
#                  the turn ends (where the Stop hook fires); the turn closes at its timestamp and later
#                  entries belong to no turn. ("stop_hook_summary" is not used: a /goal Stop hook keeps the
#                  turn going after it.) Older transcripts have no such entry: then the last activity rules.
#   local command = a slash command answered by <local-command-stdout> or a system "local_command" entry
#                  before any model activity (/model, /compact, /goal, /reload-plugins, ...): not a turn.
#   interrupt    = "[Request interrupted by user..." text: the turn so far is censored (Esc, no Stop).
#   activity     = an assistant entry or a tool_result user entry (main thread): extends the turn's end.
#   duration     = turn end (or last activity) timestamp - prompt timestamp; a prompt with no activity
#                  before the next prompt / boundary is not a turn.
#   Human messages typed mid-turn are "queued_command" attachments: skipped, the turn goes on (as live).
#   The last turn of a file touched in the last 15 minutes is censored (it may still be running).

_CMD_NAME = re.compile(r"<command-name>\s*(/?[^<\s]+)\s*</command-name>")
_CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
SKIP_PREFIXES = ("<local-command-", "<bash-stdout>", "<bash-stderr>", "Caveat:",
                 "<system-reminder>", "<user-memory-input>")


def parse_ts(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def classify_entry(e):
    """("prompt", text) | ("boundary", None) | ("interrupt", None) | ("activity", None) | ("end", None) |
    ("local", None) | ("skip", None)."""
    if not isinstance(e, dict) or e.get("isSidechain"):
        return "skip", None
    t = e.get("type")
    if t == "assistant":
        return "activity", None
    if t == "system":
        st = e.get("subtype")
        return ("end", None) if st == "turn_duration" else ("local", None) if st == "local_command" else ("skip", None)
    if t != "user":
        return "skip", None
    if "toolUseResult" in e:
        return "activity", None
    msg = e.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return "activity", None
        texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"
                 and isinstance(b.get("text"), str)]
        if not texts:
            return "skip", None
        text = "\n".join(texts)
    elif isinstance(content, str):
        text = content
    else:
        return "skip", None
    s = text.lstrip()
    if s.startswith("[Request interrupted"):
        return "interrupt", None
    origin = e.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if (kind not in (None, "human")) or e.get("scheduledTaskId") or is_injected(s) or s.startswith("<bash-input>"):
        return "boundary", None
    if s.startswith("<local-command-stdout>"):
        return "local", None
    if e.get("isMeta") or e.get("isCompactSummary") or e.get("isVisibleInTranscriptOnly"):
        return "skip", None
    if s.startswith(SKIP_PREFIXES):
        return "skip", None
    if s.startswith(("<command-message>", "<command-name>")):
        m = _CMD_NAME.search(s)
        if not m:
            return "skip", None
        name = m.group(1) if m.group(1).startswith("/") else "/" + m.group(1)
        a = _CMD_ARGS.search(s)
        args = a.group(1).strip() if a else ""
        return "prompt", f"{name} {args}".strip()
    return "prompt", text


def parse_transcript(fp, now=None, recent_s=900, since_ts=None):
    """Turn records ({ts, sid, source, features, duration_s, censored}) from one transcript file, streamed
    line by line. Malformed lines and entries without a timestamp are skipped. No text is kept."""
    now = now if now is not None else time.time()
    out = []
    sid = os.path.splitext(os.path.basename(fp))[0]
    hist = {}
    cur = None

    def close(censored):
        nonlocal cur, hist
        if cur is None:
            return
        c, cur = cur, None
        if c["last"] is None and not censored:
            return  # nothing ran: a local command, not a turn
        f = dict(c["features"])
        f.update(session_features(c["hist"], c["ts"]))
        dur = None if censored else max(0.0, c["last"] - c["ts"])
        if not censored:
            hist = hist_after(hist, dur, c["last"])
        if since_ts is None or c["ts"] >= since_ts:
            out.append({"ts": round(c["ts"], 3), "sid": sid_hash(c["sid"]), "source": "transcript", "features": f,
                        "duration_s": None if censored else round(dur, 3), "censored": censored,
                        "decided_delay_s": None, "p_long": None, "band": None, "fired": False, "fired_at_s": None})

    with open(fp, "rb") as fh:
        for raw in fh:
            try:
                e = json.loads(raw)
            except Exception:
                continue
            kind, text = classify_entry(e)
            if kind == "skip":
                continue
            ts = parse_ts(e.get("timestamp"))
            if kind == "activity":
                if cur is not None and ts is not None and ts >= cur["ts"]:
                    cur["last"] = ts if cur["last"] is None else max(cur["last"], ts)
                continue
            if kind == "interrupt":
                close(True)
                continue
            if kind == "local":
                if cur is not None and cur["last"] is None and cur["features"].get("is_slash_command"):
                    cur = None  # a local command (/model, /compact, /goal, ...): Claude Code answered, not a turn
                continue
            if kind == "end":
                if cur is not None and ts is not None and ts >= cur["ts"]:
                    cur["last"] = ts
                close(False)
                continue
            close(False)
            if kind == "prompt" and ts is not None:
                if isinstance(e.get("sessionId"), str) and e["sessionId"]:
                    sid = e["sessionId"]
                cur = {"ts": ts, "last": None, "sid": sid, "features": text_features(text), "hist": dict(hist)}
    try:
        recent = now - os.path.getmtime(fp) < recent_s
    except OSError:
        recent = False
    close(recent)
    return out


def default_transcript_roots():
    return [os.path.join(claude_config_dir(), "projects")]


def bootstrap_from_transcripts(cfg, days=60, roots=None, now=None):
    """Read transcripts (read-only), replace the transcript-sourced rows of turns.jsonl with fresh ones
    (live rows of the same turns win), retrain. Returns a report string."""
    t0 = time.perf_counter()
    now = now if now is not None else time.time()
    since = now - days * 86400 if days else None
    roots = roots or default_transcript_roots()
    files = []
    for root in roots:
        try:
            for proj in sorted(os.listdir(root)):
                pdir = os.path.join(root, proj)
                if not os.path.isdir(pdir):
                    continue
                for fn in sorted(os.listdir(pdir)):
                    fp = os.path.join(pdir, fn)
                    if fn.endswith(".jsonl") and os.path.isfile(fp):
                        try:
                            if since is None or os.path.getmtime(fp) >= since:
                                files.append(fp)
                        except OSError:
                            pass
        except OSError as e:
            log_error("transcripts-list", e)
    got, bad_files, size, seen, dupes = [], 0, 0, set(), 0
    for fp in files:
        try:
            size += os.path.getsize(fp)
            for r in parse_transcript(fp, now=now, since_ts=since):
                # a resumed or forked session repeats earlier turns in a second file: keep one copy
                key = (r["ts"], json.dumps(r["features"], sort_keys=True))
                if key in seen:
                    dupes += 1
                    continue
                seen.add(key)
                got.append(r)
        except Exception as e:
            bad_files += 1
            log_error("transcript-parse", e)
    with file_lock():
        existing = read_turns()
        live = [r for r in existing if r.get("source") != "transcript"]
        by_sid = {}
        for r in got:
            by_sid.setdefault(r["sid"], []).append(r)

        def twin(r):
            return next((t for t in by_sid.get(r.get("sid"), ()) if abs(t["ts"] - r["ts"]) < 5), None)

        # A finished live row wins over its transcript twin (it has the decision and the real Stop time).
        # An interrupted live row loses to a finished transcript twin: the live hook may have censored a
        # turn that the transcript shows ran to its end.
        drop_live = {id(r) for r in live if r.get("censored") and (twin(r) or {}).get("censored") is False}
        live = [r for r in live if id(r) not in drop_live]
        live_keys = {}
        for r in live:
            live_keys.setdefault(r.get("sid"), []).append(r["ts"])
        fresh = [r for r in got if not any(abs(r["ts"] - t) < 5 for t in live_keys.get(r["sid"], ()))]
        kept = write_turns(live + fresh)
    parse_s = time.perf_counter() - t0
    fin = [r for r in fresh if not r["censored"]]
    lines = [f"Read {len(files)} transcript files ({size / 1e6:.0f} MB) under {', '.join(roots)} in {parse_s:.1f}s"
             + (f"; {bad_files} unreadable" if bad_files else ""),
             f"  {len(fresh)} turns ({len(fin)} finished, {len(fresh) - len(fin)} interrupted)"
             + (f", {len(got) - len(fresh)} already logged live" if len(got) != len(fresh) else "")
             + (f", {dupes} repeated across resumed files" if dupes else "")
             + (f", {len(drop_live)} live rows marked interrupted replaced" if drop_live else "")
             + (f", from {_day(min(r['ts'] for r in fresh))} to {_day(max(r['ts'] for r in fresh))}" if fresh else ""),
             f"  turns.jsonl now holds {kept} turns (numbers + a category only, no prompt text)"]
    m, msg = train_model(cfg)
    lines.append(("Model " if m else "") + msg)
    return "\n".join(lines)


def cli_train(args, cfg):
    if args and args[0] in ("--from-transcripts", "transcripts"):
        days, roots, rest = 60, [], args[1:]
        i = 0
        while i < len(rest):
            if rest[i] == "--days" and i + 1 < len(rest):
                try:
                    days = float(rest[i + 1])
                except ValueError:
                    return f"Bad --days: {rest[i + 1]!r}"
                i += 2
            elif rest[i] == "--root" and i + 1 < len(rest):
                roots.append(os.path.expanduser(rest[i + 1]))
                i += 2
            else:
                return f"Unknown option: {rest[i]!r}. Usage: train [--from-transcripts [--days N] [--root DIR]]"
        return bootstrap_from_transcripts(cfg, days=days, roots=roots or None)
    if args:
        return f"Unknown option: {args[0]!r}. Usage: train [--from-transcripts [--days N] [--root DIR]]"
    m, msg = train_model(cfg)
    if m:
        with locked_state() as st:
            predict_state(st)["new_since_train"] = 0
    return ("Model " if m else "") + msg


def cli_predict_stats(cfg):
    return format_eval(evaluate(read_turns(), cfg), cfg)


def _fmt_decision(d, cfg):
    lines = [f"  p_long {d.get('p_long'):.2f} = "
             + (f"{d.get('w'):.2f} × model {d.get('p_model'):.2f} + {1 - d.get('w'):.2f} × prior "
                f"{d.get('p_prior'):.2f} (model trained on {d.get('n')} turns)"
                if d.get("p_model") is not None else f"prior {d.get('p_prior'):.2f} (no model yet)"),
             f"  → {d.get('band')}: due at {d.get('delay_s'):g}s (hi {d.get('hi')}, lo {cfg['predict_lo']})"
             f"{'' if cfg.get('predict', True) else ', prediction off: flat delay_s'}"]
    f = d.get("features") or {}
    lines.append(f"  inputs: {f.get('category')} · {f.get('char_len')} chars · {f.get('word_count')} words"
                 + (" · slash command" if f.get("is_slash_command") else "")
                 + (" · build verb" if f.get("build_verb") else "")
                 + (f" · previous turn {fmt_secs(f['session_prev_duration_s'])}"
                    if _valid_num(f.get("session_prev_duration_s")) else " · first turn in session"))
    if d.get("top"):
        lines.append("  top model features: " + ", ".join(
            f"{FEATURE_LABELS.get(n, n.replace('cat_', 'category '))} {c:+.2f}" for n, c in d["top"]))
    if d.get("outcome"):
        o = d["outcome"]
        lines.append("  outcome: " + ("interrupted (no Stop)" if o.get("censored") else
                                       f"ran {fmt_secs(o.get('duration_s') or 0)}, "
                                       + (f"task shown at {o.get('fired_at_s'):.0f}s" if o.get("fired")
                                          else "no task shown")))
    return lines


def cli_why(args, cfg):
    st = read_state()
    decs = [d for d in (st.get("decisions") or []) if isinstance(d, dict)]
    if args:
        want = sid_hash(args[0])
        decs = [d for d in decs if d.get("sid") == want]
    if not decs:
        if meter_mode(cfg):
            return ('"timing" is "meter": a set is asked from the waiting meter, not from a predicted due time, '
                    'so no prompt gets a decision (set "timing": "classic" for the predictor). '
                    + meter_line(st, cfg))
        return "No decision recorded yet: the next prompt makes one."
    out = []
    for label, d in zip(("Latest prompt", "Prompt before it"), reversed(decs[-2:])):
        when = dt.datetime.fromtimestamp(d["ts"]).strftime("%H:%M:%S") if _valid_num(d.get("ts")) else "?"
        slash = " — a slash command, likely this /workout one" if (
            label == "Latest prompt" and (d.get("features") or {}).get("is_slash_command")) else ""
        out.append(f"{label} ({when}, session {d.get('sid')}){slash}:")
        out += _fmt_decision(d, cfg)
    pr = st.get("predict") or {}
    if pr.get("hi_bump"):
        out.append(f"hi is raised by {pr['hi_bump']:.2f} after early fires that ended too soon "
                   f"(effective {effective_hi(cfg, st)})")
    if meter_mode(cfg):
        out.insert(0, '"timing" is "meter": these decisions are from "classic" timing; sets now come from the '
                      "waiting meter (" + meter_line(st, cfg) + ").")
    return "\n".join(out)


# --------------------------------------------------------------------------
# hooks

def read_stdin_json():
    try:
        data = sys.stdin.read()
        obj = json.loads(data) if data.strip() else {}
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def cmd_hook_prompt():
    payload = read_stdin_json()
    sid = str(payload.get("session_id") or "default")
    transcript = str(payload.get("transcript_path") or "")
    text = payload.get("prompt")
    injected = is_injected(text)
    own = isinstance(text, str) and re.match(r"\s*/workout\b", text) is not None
    feats = None if injected else text_features(text)  # numbers + a category; the text is dropped here
    del payload, text
    if injected:
        # A task notification / another session's message. Mid-turn it is part of the running turn (its
        # timer and due set stay as they are); with Claude idle it starts a turn nobody asked for, which
        # gets no task and no record. The one thing to settle: a turn left open by Esc (or a missed Stop)
        # is closed now, as a human prompt would close it, so the injected turn's Stop does not record it
        # as a finished turn of the wrong length. Its due sets are carried (credited at that Stop, v1 rule).
        if transcript:
            st0 = read_state()
            s0 = st0["sessions"].get(sid) or {}
            if s0.get("token") and isinstance(s0.get("turn"), dict) and _valid_num(s0["turn"].get("ts")) \
                    and transcript_turn_over(transcript, s0["turn"]["ts"]) is not None:
                cfg0 = load_config()
                with locked_state() as st:
                    prev = st["sessions"].get(sid) or {}
                    if prev.get("token") == s0.get("token"):
                        prev["carried"] = _close_open_turn(st, sid, prev)
                        prev.update(token=None, pending=[], turn=None, updated_at=time.time())
                        if meter_mode(cfg0):
                            meter_tick(st, cfg0, time.time())  # v7: that turn stops counting
        return
    cfg = load_config()  # creates config.json with defaults on first run
    meter = meter_mode(cfg)
    # /workout itself: the prior (0.1), never the model. v7 meter mode: no predictor at all
    model = None if (own or meter) else load_model()
    token = secrets.token_hex(8)
    now = time.time()
    with locked_state() as st:
        prev = st["sessions"].get(sid) or {}
        running = bool(prev.get("token")) and isinstance(prev.get("turn"), dict)
        if running and transcript and _valid_num(prev["turn"].get("ts")) \
                and now - prev["turn"]["ts"] < float(cfg["max_lifetime_s"]) \
                and transcript_turn_over(transcript, prev["turn"]["ts"]) is None:
            # A message typed while Claude is still working: Claude Code hands it to the running turn,
            # and one Stop ends both. Same turn, same timer, same due set.
            prev["updated_at"] = now
            return
        # Credit policy for a turn that never reached Stop (Esc, or a missed Stop hook), same as v1: its
        # due sets are carried and credited at the next Stop of this session. They leave the screen now
        # (the new turn shows a task only once its own set is due), and the turn is recorded as
        # interrupted (censored: kept out of training).
        carried = _close_open_turn(st, sid, prev) if running else \
            split_pending(prev.get("carried") or [])[0]
        # v5: a question still up after the last turn (ask_grace_s after Stop / Esc) closes now, unclicked:
        # declined. Replacing the session record drops its listener entry, so that timer shuts it down.
        # Except for a /workout prompt (this hook runs before the command does): `/workout done` must still
        # find the question to answer it, so the question and its listener carry over to the new record.
        keep_grace = own and grace_ask(prev, now) is not None
        gone = [] if keep_grace else settle_grace(prev, sid, now)
        if gone:
            try:
                log_declines(st, gone, cfg)
            except Exception as e:
                log_error("grace-declined-log", e)
        hist = st["hists"].get(sid) or prev.get("hist")
        hist = hist if isinstance(hist, dict) else {}
        f = dict(feats)
        f.update(session_features(hist, now))
        if meter:
            # v7: no due time. The timer's first look comes after one meter poll (or meter_min_turn_s, if
            # shorter); turns.jsonl still records the features and the duration (band "meter")
            pred = None
            delay = max(0.0, min(float(cfg["meter_min_turn_s"]), meter_poll_s()))
            turn = {"ts": round(now, 3), "features": f, "p_long": None, "band": "meter",
                    "decided_delay_s": None, "fired": False, "fired_at_s": None}
        else:
            pred = predict_long(cfg, f, model)
            hi = effective_hi(cfg, st)
            band, delay = choose_delay(cfg, pred["p_long"], hi)
            turn = {"ts": round(now, 3), "features": f, "p_long": pred["p_long"], "band": band,
                    "decided_delay_s": delay, "fired": False, "fired_at_s": None}
        st["sessions"][sid] = {"token": token, "started_at": now, "updated_at": now, "pending": [],
                               "carried": carried, "transcript_path": transcript, "theme": f["category"],
                               "hist": hist, "turn": turn}
        if keep_grace:
            st["sessions"][sid]["grace"] = prev["grace"]
            if isinstance(prev.get("listener"), dict):
                st["sessions"][sid]["listener"] = prev["listener"]
        if pred is not None:
            dec = dict(pred, ts=round(now, 3), sid=sid_hash(sid), band=band, delay_s=delay, hi=hi, features=f)
            st["decisions"] = (st["decisions"] + [dec])[-DECISIONS_KEEP:]
        ensure_next_set(st, cfg)  # chosen ahead, shown only once due
        st["cfg_hash"] = config_hash(cfg)
        if meter:
            meter_tick(st, cfg, now)  # the stretch before this prompt, then: busy from now
        proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_timer", sid, token, transcript,
                                 repr(delay)],
                                start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True)
        st["timer_pids"].append(proc.pid)
        snapshot = st
    if not os.path.exists(tips_path()):
        write_tips(cfg, snapshot)


def _close_open_turn(st, sid, prev):
    """A turn that never reached Stop: record it as interrupted (censored, kept out of training) and
    return its due sets plus earlier carried ones, to be credited at this session's next Stop."""
    append_turns([turn_record(prev["turn"], sid, None, True)])
    for d in st["decisions"]:
        if isinstance(d, dict) and d.get("sid") == sid_hash(sid) and d.get("ts") == prev["turn"].get("ts"):
            d["outcome"] = {"censored": True}
    credit, _accepted, declined = split_pending((prev.get("carried") or []) + (prev.get("pending") or []))
    if declined:  # asked, never taken, and the turn is over: declined (an accepted one is already logged)
        try:
            log_declines(st, [log_rec(p, sid, time.time(), "declined", asked_at=p.get("asked_at"),
                                      ask_id=p.get("id")) for p in declined])
        except Exception as e:
            log_error("declined-log", e)
    return credit


def _current(sid, token):
    st = read_state()
    s = st["sessions"].get(sid)
    return bool(s) and s.get("token") == token


def _sleep_while_current(sid, token, seconds, deadline):
    """Sleep up to `seconds`; False if the token went stale or lifetime ran out."""
    end = min(time.time() + max(0.0, seconds), deadline)
    while True:
        if not _current(sid, token):
            return False
        left = end - time.time()
        if left <= 0:
            return time.time() < deadline
        time.sleep(min(1.0, max(0.02, left)))


def last_activity(transcript, max_files=5000):
    """Newest mtime across the transcript and its session dir (subagent and
    workflow transcripts, tool results), or None if the transcript is missing.
    A long tool call writes nothing to the main transcript, but subagents do."""
    try:
        newest = os.path.getmtime(transcript)
    except OSError:
        return None
    sdir = transcript[:-6] if transcript.endswith(".jsonl") else None
    if sdir and os.path.isdir(sdir):
        seen = 0
        for root, _dirs, files in os.walk(sdir):
            for fn in files:
                seen += 1
                if seen > max_files:
                    return newest
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, fn)))
                except OSError:
                    pass
    return newest


def transcript_interrupted(transcript, since_ts, tail_bytes=256 * 1024):
    """True if the transcript's tail shows "[Request interrupted by user..." after since_ts with no newer
    prompt: the turn was stopped with Esc, Claude is idle, and no Stop will come. Reads only the tail;
    an entry larger than the tail is simply not seen (then the stale-transcript check still applies)."""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            chunk = f.read()
    except OSError:
        return False
    lines = chunk.split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]  # the first piece is a partial line
    for raw in reversed(lines):
        if b"interrupted" not in raw and b'"user"' not in raw:
            continue
        try:
            e = json.loads(raw)
        except Exception:
            continue
        kind, _ = classify_entry(e)
        ts = parse_ts(e.get("timestamp"))
        if ts is not None and ts < since_ts - 2:
            return False
        if kind == "interrupt":
            return True
        if kind in ("prompt", "boundary"):
            return False
    return False


def transcript_turn_over(transcript, since_ts, tail_bytes=256 * 1024):
    """How the turn that started at since_ts ended, from the transcript's tail: "interrupted" (Esc wrote
    "[Request interrupted by user..."), "finished" (Claude Code wrote its turn_duration entry, so Stop has
    run even if our hook missed it), or None (still running, or no transcript to tell). Newer prompts are
    looked past: the message being submitted may already be in the file."""
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            chunk = f.read()
    except (OSError, TypeError, ValueError):
        return None
    lines = chunk.split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]
    for raw in reversed(lines):
        if b"interrupted" not in raw and b"turn_duration" not in raw and b'"user"' not in raw:
            continue
        try:
            e = json.loads(raw)
        except Exception:
            continue
        kind, _ = classify_entry(e)
        ts = parse_ts(e.get("timestamp"))
        if ts is not None and ts < since_ts - 2:
            return None
        if kind == "interrupt":
            return "interrupted"
        if kind == "end" and ts is not None and ts > since_ts + 0.5:  # not the previous turn's own end
            return "finished"
    return None


def cmd_timer(sid, token, transcript, delay=None):
    ctl = {"listener": None, "reason": None}  # v5: the turn's button listener, and why the loop ended
    try:
        try:
            _timer_loop(sid, token, transcript, delay, ctl)
        finally:
            # However the timer ends (lifetime, pause, quiet hours, error), stop showing the task for this
            # turn (status line and spinner). Pending sets stay for Stop to credit.
            try:
                with locked_state() as st:
                    s = st["sessions"].get(sid)
                    if s and s.get("token") == token:
                        now = time.time()
                        s["nudging"] = False
                        end_badge(s, now)  # Esc / lifetime: the badge stays up badge_s more
                        cfg = load_config()
                        if ctl["reason"] == "esc":
                            # Esc is Stop for the button (v5): the question stays up ask_grace_s
                            start_grace(st, cfg, s, now, sid)
                        if meter_mode(cfg):
                            meter_tick(st, cfg, now)  # v7: Esc / pause / lifetime: the meter sees it now
            except Exception as e:
                log_error("timer-finally", e)
        # v5: after the turn, the timer lives on only to serve the button while the question is still up
        _serve_after_turn(sid, ctl)
    finally:
        if ctl["listener"] is not None:
            ctl["listener"].close()
            ctl["listener"] = None


ONSCREEN_POLL_S = 10.0
SHOW_HEARTBEAT_S = 45.0


def show_heartbeat_s():
    """How long a due set stays on screen without its timer refreshing it (45 s): a timer that died
    (SIGKILL, a closed laptop) releases the spinner at the next state change after that. A sandboxed
    data dir (WORKOUT_HOME set, as in tests) may lower it via WORKOUT_HEARTBEAT_S."""
    if sandboxed() and os.environ.get("WORKOUT_HEARTBEAT_S"):
        try:
            return max(0.1, float(os.environ["WORKOUT_HEARTBEAT_S"]))
        except ValueError:
            pass
    return SHOW_HEARTBEAT_S


# --------------------------------------------------------------------------
# the ask: "15 push-ups?" in the band above the prompt
#
# When a set comes due, the timer appends it to the session's pending list with status "asked", asked_at
# and an 8-hex id. It is logged only when someone answers it: the band's [1] done (`cli done --ask <id>`)
# or /workout done. An ask still open at Stop stays up a little longer (the grace window, see below); one
# that is never answered is recorded as "declined" in log.jsonl (counted nowhere).

ASK_POLL_S = 2.0


def ask_poll_s():
    """2 s. A test run (WORKOUT_TEST=1) may change it via WORKOUT_ASK_POLL_S."""
    if sandboxed() and os.environ.get("WORKOUT_ASK_POLL_S"):
        try:
            return max(0.05, float(os.environ["WORKOUT_ASK_POLL_S"]))
        except ValueError:
            pass
    return ASK_POLL_S


ASKED_STATUSES = ("asked", "accepted", "skipped", "superseded", "declined")  # an ask was made this turn (v4.1: one per turn)


def open_ask(s):
    """The session's current asked (not yet accepted) set, or None."""
    pend = [p for p in ((s or {}).get("pending") or []) if valid_pending(p)]
    for p in reversed(pend):
        if p.get("status") == "asked":
            return p
        if p.get("status") == "accepted":
            return None
    return None


def log_rec(p, sid, now, status, **extra):
    r = {"ts": now, "session_id": sid, "exercise": p["exercise"], "reps": int(p.get("reps") or 0),
         "unit": p.get("unit", "reps"), "status": status}
    r.update({k: v for k, v in extra.items() if v is not None})
    return r


# v4.1: one ask per turn, and a success badge once it is taken. The status line of that session shows
# "✅ 20 crunches · bravo! 🔥" for the rest of the turn AND for badge_s (120 s) after the acceptance or
# after the turn ended (Stop, Esc, the timer's end), whichever is later, or until that session's next
# prompt (hook-prompt replaces the session record, badge included). The cheer rotates, never the same one
# twice in a row (state["last_cheer"]). The badge lives in the session record (s["badge"]), so it outlives
# Stop clearing the pending list; status computes its end from timestamps and never needs the timer.
CHEERS = ("bravo! 🔥", "kudos! 💪", "beast mode 🦾", "nailed it ✨", "crushed it 🙌", "strong work 💥",
          "way to go 🚀")
CHEER_MAX = 16
BADGE_S = 120.0


def pick_cheer(st, rng=random):
    """A cheer from CHEERS, never the one shown last (recorded in st["last_cheer"])."""
    c = pick_line(CHEERS, st.get("last_cheer"), rng)
    st["last_cheer"] = c
    return c


def badge_text(p, cheer):
    return f"✅ {fmt_set(p['exercise'], int(p.get('reps') or 0), p.get('unit', 'reps'))} · {cheer}"


def end_badge(s, now):
    """The turn that holds this badge is over (Stop, Esc, the timer's end): from now on it shows for
    badge_s more (counted from the later of the acceptance and this moment)."""
    b = s.get("badge") if isinstance(s, dict) else None
    if isinstance(b, dict) and not _valid_num(b.get("ended_at")):
        b["ended_at"] = now


def accept_ask(st, cfg, sid, s, p, now, via, announce=True, turn_ts=None):
    """Log an asked set as done now (under the lock): achievements, level, the next set, the idle spinner.
    Marks it accepted so Stop neither logs it again nor declines it, and puts the success badge on the
    session. turn_ts: the asking turn's prompt time when that turn is already over (an answer in the
    grace window after Stop). Returns (rec, log, new_ach)."""
    if turn_ts is None:
        turn_ts = (s.get("turn") or {}).get("ts")
    rec = log_rec(p, sid, now, "done", via=via, ask_id=p.get("id"), turn_ts=turn_ts)
    append_log([rec])
    log = read_log()
    new_ach, level_line = _post_log_updates(st, cfg, log, now)
    p.update(status="accepted", accepted_at=now, via=via, announced=not announce,
             ach=new_ach, level_line=level_line)
    st["last_exercise"] = p["exercise"]
    note_ask(st, p.get("id"), p["exercise"], "done", now)  # v5.2: a taken ask counts in "the last 3 asks"
    cheer = pick_cheer(st)
    s["badge"] = {"text": badge_text(p, cheer), "ask_id": p.get("id"), "accepted_at": now,
                  "token": s.get("token"), "cheer": cheer,
                  "set": {"exercise": p["exercise"], "reps": int(p.get("reps") or 0), "unit": p.get("unit", "reps")}}
    ensure_next_set(st, cfg, log, force=True)
    refresh_idle_spinner(st, cfg, log, now)
    s["updated_at"] = now
    return rec, log, new_ach


# --------------------------------------------------------------------------
# the link button (v5, test suite only: "button": "link" needs WORKOUT_TEST_LEGACY_MODES, see BUTTON_MODES;
# an install never starts this listener, see make_asker): "💪 32s wall sit?  ✓ done" in a status line,
# "✓ done" an OSC 8 link
#
# With "ask_mode": "button" (the default) nothing is ever logged without an explicit action: a click on
# the link, or /workout done. When the timer asks a set it also starts a tiny stdlib HTTP server in a
# daemon thread of the timer process, bound to 127.0.0.1 on an ephemeral port, with a fresh one-time
# secret (secrets.token_urlsafe(16)). The session's state gets s["listener"] = {port, token, ask_id,
# timer, alive_until}; the token is written nowhere else (no request log, no errors.log line names it).
#   GET /done/<token>, Host 127.0.0.1:<port> or localhost:<port>  -> accept the asked set exactly once,
#       under the .lock ("via": "button"; the same bookkeeping as any accept), and answer a small
#       self-contained page "✅ 32s wall sit · bravo! 🔥 / Logged. You can close this tab.". A repeat
#       click: "Already logged", no second row. A click after the question closed: 410, nothing logged.
#   anything else (another path, a wrong token, another method, another Host: DNS rebinding) -> 404 and
#       no state read or change.
# Lifetime. The listener lives while its question is up and dies with the timer process:
#   - the live turn, while the ask is open;
#   - after Stop or Esc, ask_grace_s (180 s) more, the question still on the status line, since the set
#     may still be under way when Claude finishes (s["grace"] = {ask, until, turn_ts}); the timer
#     process stays alive only for this, polling every second;
#   - CLICK_LINGER_S (10 s) after a click, so a double click or a reload gets "Already logged".
# It closes at the first of: the grace runs out (the ask is recorded "declined", silently), the click
# (+ linger), the session's next prompt (hook-prompt declines the open ask and replaces the session
# record, so the timer finds its listener entry gone), pause / quiet hours / the lifetime limit during the
# turn (the ask stays pending; Stop declines it). The status line never probes the port (it must stay
# fast and lock-free): the timer refreshes s["listener"]["alive_until"] (now + 10 s) on every poll, and
# the link is drawn only while now < alive_until. A dead timer: no link within 10 s.

LISTENER_HOST = "127.0.0.1"
LISTENER_ALIVE_S = 10.0
CLICK_LINGER_S = 10.0
GRACE_POLL_S = 1.0


def click_linger_s():
    """10 s. A test run (WORKOUT_TEST=1) may change it via WORKOUT_CLICK_LINGER_S."""
    if sandboxed() and os.environ.get("WORKOUT_CLICK_LINGER_S"):
        try:
            return max(0.0, float(os.environ["WORKOUT_CLICK_LINGER_S"]))
        except ValueError:
            pass
    return CLICK_LINGER_S


def button_url(port, token):
    return f"http://{LISTENER_HOST}:{port}/done/{token}"


def osc8(url, text):
    """An OSC 8 hyperlink: ESC ] 8 ;; URL ESC \\ TEXT ESC ] 8 ;; ESC \\."""
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


def button_link(s, p, now):
    """'  ✓ done' (an OSC 8 link to this ask's listener) while that listener is alive, else ''."""
    L = s.get("listener") if isinstance(s, dict) else None
    if not (isinstance(L, dict) and isinstance(p, dict) and p.get("id") and L.get("ask_id") == p.get("id")):
        return ""
    port, tok = L.get("port"), L.get("token")
    if not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536):
        return ""
    if not (isinstance(tok, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,64}", tok)):
        return ""
    if not (_valid_num(L.get("alive_until")) and now < L["alive_until"]):
        return ""
    return "  " + osc8(button_url(port, tok), "✓ done")


def keeper_alive(s, p, now):
    """v6: this ask's mod keeper (s["listener"] with kind "mod") is alive: its timer refreshed it within
    LISTENER_ALIVE_S. The mod-mode twin of a live button_link."""
    L = s.get("listener") if isinstance(s, dict) else None
    return (isinstance(L, dict) and L.get("kind") == "mod" and isinstance(p, dict) and bool(p.get("id"))
            and L.get("ask_id") == p.get("id") and _valid_num(L.get("alive_until")) and now < L["alive_until"])


def grace_ask(s, now=None):
    """The session's question still up after its turn ended (ask_grace_s after Stop / Esc), or None."""
    g = s.get("grace") if isinstance(s, dict) else None
    if not (isinstance(g, dict) and valid_pending(g.get("ask")) and g["ask"].get("status") == "asked"):
        return None
    if now is not None and not (_valid_num(g.get("until")) and now <= g["until"]):
        return None
    return g["ask"]


def _pid_alive(pid):
    """False only when the process is known to be gone (a missing or odd pid counts as alive)."""
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def start_grace(st, cfg, s, now, sid=None):
    """Stop / Esc with the button's question still up: keep it up ask_grace_s more (moved out of
    pending, so the turn's end does not decline it). Only while its listener is alive (fresh heartbeat
    and, v5.1, its timer process still running); else nothing (the ask is declined as before). An older
    grace question still recorded (v5.1) is settled first: declined, never silently dropped. Returns
    True if a grace window started."""
    grace = float(cfg.get("ask_grace_s", 180))
    p = open_ask(s)
    L = s.get("listener")
    if grace <= 0 or p is None or not isinstance(L, dict) or L.get("ask_id") != p.get("id") \
            or not (_valid_num(L.get("alive_until")) and now < L["alive_until"]) \
            or not _pid_alive(L.get("pid")):
        return False
    if s.get("grace") is not None:
        old = settle_grace(s, sid if sid is not None else "default", now)
        if old:
            try:
                log_declines(st, old, cfg)
            except Exception as e:
                log_error("grace-replaced-log", e)
    until = now + grace
    if meter_mode(cfg) and meter_ask_ttl_s(cfg) and _valid_num(p.get("asked_at")):
        # v7: the question's whole life is meter_ask_ttl_s; past it, Stop / Esc declines it as usual
        until = min(until, p["asked_at"] + meter_ask_ttl_s(cfg))
        if until <= now:
            return False
    s["pending"] = [q for q in (s.get("pending") or []) if q is not p]
    s["grace"] = {"ask": p, "until": until, "turn_ts": (s.get("turn") or {}).get("ts")}
    return True


def settle_grace(s, sid, now):
    """Close a grace-window question unclicked: drop it and return its "declined" log record(s)."""
    g = s.pop("grace", None) if isinstance(s, dict) else None
    if not isinstance(g, dict) or not valid_pending(g.get("ask")) or g["ask"].get("status") != "asked":
        return []
    p = g["ask"]
    return [log_rec(p, sid, now, "declined", asked_at=p.get("asked_at"), ask_id=p.get("id"))]


def find_ask(s, ask_id):
    """(entry, grace or None) for the ask `ask_id` of this session: in the live turn's pending list, or
    in the grace window. (None, None) if it is gone."""
    for p in (s.get("pending") or []):
        if valid_pending(p) and p.get("id") == ask_id:
            return p, None
    g = s.get("grace")
    if isinstance(g, dict) and valid_pending(g.get("ask")) and g["ask"].get("id") == ask_id:
        return g["ask"], g
    return None, None


def listener_keep(s, L, now):
    """The listener's question is still up (or it was clicked within the linger window)."""
    ask_id = L.get("ask_id")
    if s.get("token") and s.get("token") == L.get("timer") and s.get("nudging") is not False:
        if any(valid_pending(p) and p.get("id") == ask_id and p.get("status") == "asked"
               for p in (s.get("pending") or [])):
            return True
    ga = grace_ask(s, now)
    if ga is not None and ga.get("id") == ask_id:
        return True
    c = L.get("clicked_at")
    return _valid_num(c) and now < c + click_linger_s()


def _logged_before(ask_id):
    """The 'done' log record of this ask, if one was written (a repeat click after Stop cleared it)."""
    for r in reversed(read_log()):
        if r.get("ask_id") == ask_id and r.get("status") == "done":
            return r
    return None


PAGE_STYLE = ("body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
              "background:#111418;color:#e8eaed;font:18px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',"
              "sans-serif}main{padding:32px 24px;text-align:center;max-width:32rem}"
              "h1{font-size:28px;font-weight:600;margin:0 0 12px}p{margin:0;color:#aab1bb}")


def button_page(title, line):
    """A tiny self-contained dark page (no external assets), and a window.close() attempt."""
    import html  # imported here: the status line and the hooks never pay for it
    t, l = html.escape(title), html.escape(line)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"color-scheme\" content=\"dark\">"
            f"<title>Workout Buddy</title><style>{PAGE_STYLE}</style></head>"
            f"<body><main><h1>{t}</h1><p>{l}</p></main>"
            "<script>setTimeout(function(){try{window.close()}catch(e){}},1200)</script></body></html>")


def button_click(owner):
    """A click on the link of listener `owner` (its token already checked). Returns (http code, page)."""
    cfg = load_config()
    accepted = None
    closed = (410, button_page("Not logged", "This question has closed. You can close this tab."))
    with locked_state() as st:
        s = st["sessions"].get(owner.sid)
        L = s.get("listener") if isinstance(s, dict) else None
        if not (isinstance(L, dict) and isinstance(L.get("token"), str)
                and secrets.compare_digest(L["token"].encode(), owner.token.encode())):
            return closed  # the next prompt replaced the session: the question is gone
        now = time.time()
        p, g = find_ask(s, owner.ask_id)
        if p is None:
            r = _logged_before(owner.ask_id)
            if r is None:
                return closed
            what = fmt_set(r.get("exercise", "?"), _amount(r), r.get("unit", "reps"))
            return 200, button_page(f"✅ {what}", "Already logged. You can close this tab.")
        if p.get("status") == "accepted":
            b = s.get("badge") if isinstance(s.get("badge"), dict) else {}
            text = b.get("text") if b.get("ask_id") == p.get("id") and isinstance(b.get("text"), str) \
                else f"✅ {task_text(dict(p, reps=int(p.get('reps') or 0)))}"
            return 200, button_page(text, "Already logged. You can close this tab.")
        if p.get("status") != "asked" or (g is not None and not (_valid_num(g.get("until")) and now <= g["until"])):
            return closed  # undone with /workout undo, or the grace window ran out
        accepted = accept_ask(st, cfg, owner.sid, s, p, now, via="button", announce=g is None,
                              turn_ts=g.get("turn_ts") if g is not None else None)
        L["clicked_at"] = now
        if g is not None:
            end_badge(s, now)  # the turn is over: the badge shows badge_s from now
        text = s["badge"]["text"]
        snapshot = st
    rec, log, new_ach = accepted
    write_tips(cfg, snapshot, log)
    notify("Workout Buddy", "Logged", f"Logged {task_text(rec)}", cfg, kind="done")
    notify_achievements(new_ach, cfg)
    return 200, button_page(text, "Logged. You can close this tab.")


_BUTTON_CLASSES = None


def _button_classes():
    """(server class, handler class), defined on first use: http.server costs ~30 ms to import, which only
    the timer that asks ever pays (the status line and the hooks never import it)."""
    global _BUTTON_CLASSES
    if _BUTTON_CLASSES is not None:
        return _BUTTON_CLASSES
    import http.server

    class _ButtonHandler(http.server.BaseHTTPRequestHandler):
        server_version = "WorkoutBuddy"
        sys_version = ""
        timeout = 10  # a browser's speculative connection that never sends a request does not pin a thread

        def log_message(self, *args):  # never: the request line carries the token
            pass

        def _send(self, code, page):
            body = page.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _not_found(self):
            self._send(404, button_page("Not found", ""))

        def do_GET(self):
            owner = self.server.owner
            host = (self.headers.get("Host") or "").strip().lower()
            if host not in (f"127.0.0.1:{owner.port}", f"localhost:{owner.port}"):
                return self._not_found()  # DNS rebinding: a page on another name resolving to 127.0.0.1
            if not secrets.compare_digest(self.path.encode("utf-8", "replace"),
                                          f"/done/{owner.token}".encode("utf-8")):
                return self._not_found()
            try:
                code, page = button_click(owner)
            except Exception as e:
                log_error("button-click", e)
                code, page = 500, button_page("Not logged", "Something went wrong. Use /workout done.")
            self._send(code, page)
            try:  # v5.2: the click is answered (and logged) first; the tab is closed a moment later
                schedule_close_tab(owner.port, peek_config())
            except Exception as e:
                log_error("close-tab", e)

        def __getattr__(self, name):
            if name.startswith("do_"):  # POST, HEAD, PUT, OPTIONS, anything: 404, no state touched
                return self._not_found
            raise AttributeError(name)


    class _ButtonServer(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = False

        def handle_error(self, request, client_address):
            log_error("button-request", sys.exc_info()[1])

    _BUTTON_CLASSES = (_ButtonServer, _ButtonHandler)
    return _BUTTON_CLASSES


# --------------------------------------------------------------------------
# close the tab (v5.2)
#
# Test suite only (it belongs to "button": "link", see BUTTON_MODES). A browser refuses
# window.close() for a tab it did not open by script (the page still tries, and says "You can close this
# tab"), so the listener does it: CLOSE_TAB_DELAY_S after answering a click (token checked), in a
# background thread of the timer, it asks each running browser over AppleScript to close its tabs whose URL
# starts with "http://127.0.0.1:<this listener's port>/", and nothing else. A browser that is not running is
# never asked (`application "X" is running` launches nothing), so none is ever started. The first time,
# macOS asks once whether the terminal (or python3) may control Safari; denied, it fails silently (one
# errors.log line per click) and the page's own close attempt stays. "close_tab": false turns it off.

CLOSE_TAB_BROWSERS = ("Safari", "Google Chrome", "Arc", "Brave Browser")  # Safari: the system default
CLOSE_TAB_DELAY_S = 1.5
CLOSE_TAB_TIMEOUT_S = 5.0
OSASCRIPT = "/usr/bin/osascript"


def close_tab_delay_s():
    """1.5 s. A test run (WORKOUT_TEST=1) may change it via WORKOUT_CLOSE_TAB_DELAY_S."""
    if sandboxed() and os.environ.get("WORKOUT_CLOSE_TAB_DELAY_S"):
        try:
            return max(0.0, float(os.environ["WORKOUT_CLOSE_TAB_DELAY_S"]))
        except ValueError:
            pass
    return CLOSE_TAB_DELAY_S


def close_tab_prefix(port):
    """'http://127.0.0.1:<port>/': the only URLs ever closed. A port that is not a plain int in 1..65535
    raises (no script is built from it)."""
    if not (isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536):
        raise ValueError(f"bad port {port!r}")
    return f"http://{LISTENER_HOST}:{port}/"


def close_tab_script(app, port):
    """The AppleScript closing `app`'s tabs whose URL starts with this listener's prefix, window by window
    (a window that is not a browser window, or has no such tab, is skipped)."""
    if app not in CLOSE_TAB_BROWSERS:
        raise ValueError(f"not a known browser: {app!r}")
    return (f"tell application {applescript_quote(app)}\n"
            "\trepeat with w in (get every window)\n"
            "\t\ttry\n"
            f"\t\t\tclose (every tab of w whose URL starts with {applescript_quote(close_tab_prefix(port))})\n"
            "\t\tend try\n"
            "\tend repeat\n"
            "end tell")


def _osascript(argv):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=CLOSE_TAB_TIMEOUT_S)


def close_tabs(port, runner=None):
    """Close this listener's tabs in every running browser. Returns the browsers asked to close. Never
    raises; the first failure is logged once (errors.log), the rest are swallowed."""
    runner = runner or _osascript
    asked, failed = [], None
    try:
        close_tab_prefix(port)
    except ValueError as e:
        log_error("close-tab", e)
        return asked
    for app in CLOSE_TAB_BROWSERS:
        try:
            r = runner([OSASCRIPT, "-e", f"application {applescript_quote(app)} is running"])
            if r.returncode != 0 or (r.stdout or "").strip() != "true":
                continue
            r = runner([OSASCRIPT, "-e", close_tab_script(app, port)])
            asked.append(app)
            if r.returncode != 0:  # e.g. -1743: not allowed to control it (macOS Automation)
                raise RuntimeError(f"{app}: osascript exit {r.returncode}: {(r.stderr or '').strip()[:200]}")
        except Exception as e:
            if failed is None:
                failed = e
    if failed is not None:
        log_error("close-tab", failed)
    return asked


def _close_tab_later(port):
    time.sleep(close_tab_delay_s())
    if sandboxed():  # a test run: never touch a real browser
        p = os.environ.get("WORKOUT_CLOSE_TAB_LOG")
        if p:
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "port": port, "prefix": close_tab_prefix(port)}) + "\n")
        return
    close_tabs(port)


def schedule_close_tab(port, cfg):
    """After a click was answered: close its tab CLOSE_TAB_DELAY_S later, in a background thread (not a
    daemon: the timer process waits for it, bounded by the osascript timeouts). None when "close_tab" is
    off."""
    if not cfg.get("close_tab", False) or not is_macos():
        return None
    import threading

    def run():
        try:
            _close_tab_later(port)
        except Exception as e:
            log_error("close-tab", e)

    t = threading.Thread(target=run, name="workout-close-tab", daemon=False)
    t.start()
    return t


class AskListener:
    """The button's loopback listener for one ask: bound at construction, served by start() in a daemon
    thread, gone with close() (or with the timer process)."""

    def __init__(self, sid, ask_id):
        self.sid, self.ask_id = sid, ask_id
        self.token = secrets.token_urlsafe(16)
        server_cls, handler_cls = _button_classes()
        self.server = server_cls((LISTENER_HOST, 0), handler_cls)
        self.server.owner = self
        self.port = self.server.server_address[1]
        self.thread = None
        self.closed = False

    def start(self):
        if self.thread is None and not self.closed:
            import threading
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1},
                                           name="workout-button", daemon=True)
            self.thread.start()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.thread is not None:
                self.server.shutdown()
        finally:
            self.server.server_close()


class AskKeeper:
    """v6, "button": "mod": the listener's stand-in with no socket. The mod answers through the CLI, so
    nothing listens; the timer still keeps s["listener"] = {kind: "mod", token, ask_id, timer, alive_until,
    pid} fresh while the question is up, exactly as for the link (the token is only this keeper's identity
    in state.json, never a secret anyone presents), so the grace window after Stop / Esc, its silent
    decline when it runs out, and a dead timer taking the question down all work unchanged."""

    port = None

    def __init__(self, sid, ask_id):
        self.sid, self.ask_id = sid, ask_id
        self.token = secrets.token_urlsafe(16)
        self.closed = False

    def start(self):
        pass

    def close(self):
        self.closed = True


# v6: "button": "mod" is one switch in the shared config.json, but the hooks also run under other Claude
# config dirs (CLAUDE_CONFIG_DIR profiles) where the mod may not be loaded. So the mod proves it is drawing
# for a session: while its band runs it touches <home>/mod-seen/<session_id> every MOD_BEAT_S. Only a
# session with a fresh beat gets the keeper; every other session keeps the v5 link, so no session is ever
# left with a bare question and nothing to press.
MOD_SEEN_DIR = "mod-seen"
MOD_SEEN_S = 120.0  # the mod beats every 30 s; a beat older than this means no band in that session
MOD_SEEN_PRUNE_S = 86400.0
_SID_FILE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def mod_seen(sid, now=None, prune=True):
    """True while the Workout Buddy mod has beaten for this session within MOD_SEEN_S. Prunes beats
    older than a day on the way (a few small files), unless prune is False (v7: the meter's look at
    other sessions)."""
    now = time.time() if now is None else now
    d = path(MOD_SEEN_DIR)
    if prune:
        try:
            for name in os.listdir(d):
                f = os.path.join(d, name)
                try:
                    if now - os.stat(f).st_mtime > MOD_SEEN_PRUNE_S:
                        os.unlink(f)
                except OSError:
                    pass
        except OSError:
            return False
    if not isinstance(sid, str) or not _SID_FILE.fullmatch(sid):
        return False
    try:
        age = now - os.stat(os.path.join(d, sid)).st_mtime
    except OSError:
        return False
    return -5.0 <= age <= MOD_SEEN_S


def make_asker(cfg, sid, ask_id):
    """(object, its s["listener"] fields) for this ask: the mod's keeper (no socket), or, in the test suite's
    legacy modes only, the loopback listener ("button": "link", or "mod" in a session where the mod has not
    beaten, see mod_seen). An install never opens a port: without the legacy modes it is always the keeper."""
    if not legacy_modes_allowed() or (cfg.get("button") == "mod" and mod_seen(sid)):
        k = AskKeeper(sid, ask_id)
        return k, {"kind": "mod", "token": k.token}
    lst = AskListener(sid, ask_id)
    return lst, {"port": lst.port, "token": lst.token}

# --------------------------------------------------------------------------
# the waiting meter (v7, "timing": "meter", the default)
#
# Every second some session is busy (between its human prompt and its Stop) fills one meter in state.json,
# across all turns and sessions: meter_s. It counts the UNION of busy time: two sessions busy for the same
# 60 s add 60 s, not 120. It is advanced by meter_tick under the .lock, from the places that already run:
# hook-prompt, hook-stop and every turn timer's poll (METER_POLL_S, 5 s), never by a new process.
#   meter_on     whether the meter was running at the last tick; the seconds from last_tick_ts to now are
#                added only if it was (so a tick counts the stretch BEFORE it, by the state then), each
#                stretch capped at METER_GAP_CAP_S (60 s: a closed laptop or a dead timer adds at most that).
#   running      some session's turn is busy (token + human prompt time, no Esc seen (turn over_at), its
#                transcript not stale (s["idle"]), within max_lifetime_s, its timer alive), and nothing pauses it: no
#                pause, no quiet hours, and no ask open anywhere (asked with a live heartbeat, or in its
#                grace window). So the meter is frozen while a set is asked, and counts again once it is
#                done, skipped or expired (meter_ask_ttl_s, 180 s after the ask, turn and grace together:
#                an ignored question in an unwatched session must not hold every other session).
# The ask: when meter_s >= meter_target_s, the next timer poll of a turn that has run meter_min_turn_s
# (20 s) and has not asked yet asks (the same ask / button / mod machinery as classic). A turn that ends
# before 20 s never asks, but its seconds count. One ask per turn, at most one open ask at a time. With
# "button": "mod", a session whose band is not drawing (no fresh mod beat) defers to another qualifying
# session whose band is (and whose timer polled within METER_TIMER_FRESH_S); with none, any qualifying
# session asks (the link). At the ask, meter_s = 0 and a new target is drawn: meter_base_s + uniform(
# -meter_jitter_s, +meter_jitter_s), floored at 0. cooldown_s, repeat_s, delay_s and the predictor are
# not used (turns.jsonl still gets each turn's features and duration, for the predictor in classic).
METER_POLL_S = 5.0
METER_GAP_CAP_S = 60.0
METER_TIMER_FRESH_S = 15.0
METER_TIMER_DEAD_S = 30.0  # a turn whose timer has not polled for this long is not counted (6 polls)


def meter_ask_ttl_s(cfg):
    """How long a question asked in meter mode stays up, live turn and grace window together (0: no
    limit). One open question holds the meter and every other session, so an unanswered one must end."""
    v = (cfg or {}).get("meter_ask_ttl_s", 180)
    return float(v) if _valid_num(v) and v > 0 else 0.0


def meter_ask_expired(p, cfg, now):
    ttl = meter_ask_ttl_s(cfg)
    return bool(ttl) and isinstance(p, dict) and _valid_num(p.get("asked_at")) and now - p["asked_at"] >= ttl


def expire_meter_ask(st, cfg, sid, s, now):
    """Meter mode, under the lock: this turn's question has been up meter_ask_ttl_s unanswered: decline
    it, by the same path as the mod's [2] skip (off the screen, a "declined" row, next_set rotated, its
    listener closed). The turn keeps its one ask (nothing more is asked this turn); the meter, held while
    it was up, counts again from where the ask left it (0). True if it expired."""
    p = open_ask(s)
    if p is None or not meter_ask_expired(p, cfg, now):
        return False
    p["status"] = "declined"
    p["declined_at"] = now
    p["expired"] = True
    L = s.get("listener")
    if isinstance(L, dict) and L.get("ask_id") == p.get("id"):
        s.pop("listener", None)
    s["updated_at"] = now
    log_declines(st, [log_rec(p, sid, now, "declined", asked_at=p.get("asked_at"), ask_id=p.get("id"),
                              expired=True)], cfg)
    return True


def meter_mode(cfg):
    return (cfg or {}).get("timing", "meter") == "meter"


def meter_poll_s():
    """5 s; a test run (WORKOUT_TEST=1) may lower it via WORKOUT_METER_POLL_S."""
    if sandboxed() and os.environ.get("WORKOUT_METER_POLL_S"):
        try:
            return max(0.05, float(os.environ["WORKOUT_METER_POLL_S"]))
        except ValueError:
            pass
    return METER_POLL_S


def meter_new_target(cfg, rng=random):
    base, jit = float(cfg["meter_base_s"]), float(cfg["meter_jitter_s"])
    return round(max(0.0, base + rng.uniform(-jit, jit)), 1)


def meter_target(st, cfg, rng=random):
    """The current target; drawn when missing or outside what the config allows now (a changed config)."""
    t = st.get("meter_target_s")
    base, jit = float(cfg["meter_base_s"]), float(cfg["meter_jitter_s"])
    if not _valid_num(t) or not (max(0.0, base - jit) - 0.5 <= t <= base + jit + 0.5):
        t = st["meter_target_s"] = meter_new_target(cfg, rng)
    return t


def meter_reset(st, cfg, rng=random):
    """An ask was made: empty the meter and draw the next target."""
    st["meter_s"] = 0.0
    st["meter_target_s"] = meter_new_target(cfg, rng)


def turn_busy(s, now, cfg):
    """This session's turn is one Claude is working on: a token, a human prompt time, no Esc seen, its
    transcript not stale, within max_lifetime_s, and its timer alive: not ended (nudging False: lifetime,
    an error, classic's pause return) and, once it has polled, polled within METER_TIMER_DEAD_S (a killed
    timer). Only the timer sees Esc and staleness, so without it the turn would count until the next
    prompt."""
    if not isinstance(s, dict) or not s.get("token") or s.get("nudging") is False:
        return False
    polled = s.get("meter_poll_at")
    if _valid_num(polled) and now - polled > METER_TIMER_DEAD_S:
        return False
    turn = s.get("turn")
    if not isinstance(turn, dict) or not _valid_num(turn.get("ts")) or turn.get("over_at") is not None:
        return False
    if s.get("idle"):
        return False
    return now - turn["ts"] < float(cfg["max_lifetime_s"])


def live_ask(s, now):
    """This session's ask still on screen: asked with a live heartbeat (show_until; a dead timer's ask
    stops counting 45 s later), or in its grace window. None otherwise."""
    p = open_ask(s)
    if p is not None and _valid_num(p.get("show_until")) and p["show_until"] >= now:
        return p
    return grace_ask(s, now)


def any_ask_open(st, now):
    return any(live_ask(s, now) is not None for s in st["sessions"].values() if isinstance(s, dict))


def meter_running(st, cfg, now):
    if is_paused(st, now) or in_quiet_hours(cfg, now) or any_ask_open(st, now):
        return False
    return any(turn_busy(s, now, cfg) for s in st["sessions"].values())


def meter_tick(st, cfg, now):
    """Add the seconds since the last tick if the meter was running then (capped), then note whether it
    runs now. Call under the lock, after the state change it should see."""
    m = st.get("meter_s") if _valid_num(st.get("meter_s")) else 0.0
    last = st.get("last_tick_ts")
    if st.get("meter_on") is True and _valid_num(last) and now > last:
        m += min(now - last, METER_GAP_CAP_S)
    st["meter_s"] = round(m, 3)
    # never backwards: a hook that read the clock before it got the lock may tick after a later timer tick
    st["last_tick_ts"] = round(max(now, last) if _valid_num(last) else now, 3)
    st["meter_on"] = meter_running(st, cfg, now)
    meter_target(st, cfg)
    return st["meter_s"]


def meter_turn_qualifies(cfg, s, now):
    """This session's turn may ask now: busy, has run meter_min_turn_s, and has asked nothing yet."""
    if not turn_busy(s, now, cfg):
        return False
    turn = s["turn"]
    if turn.get("fired") or grace_ask(s, now) is not None:
        return False
    if any(valid_pending(p) and p.get("status") in ASKED_STATUSES for p in (s.get("pending") or [])):
        return False
    return now - turn["ts"] >= float(cfg["meter_min_turn_s"])


def meter_should_ask(st, cfg, sid, s, now):
    """The ask rule (see above). meter_tick first, so meter_s is current."""
    if not meter_turn_qualifies(cfg, s, now) or any_ask_open(st, now):
        return False
    if (st.get("meter_s") or 0) < meter_target(st, cfg):
        return False
    if cfg.get("ask_mode", "button") == "button" and cfg.get("button") == "mod" and not mod_seen(sid, now):
        if not (legacy_modes_allowed() and cfg.get("statusline", False)):
            return False  # no band in this session: a question here could not be answered
        for osid, o in st["sessions"].items():
            if osid == sid or not isinstance(o, dict):
                continue
            polled = o.get("meter_poll_at")
            if _valid_num(polled) and now - polled <= METER_TIMER_FRESH_S and meter_turn_qualifies(cfg, o, now) \
                    and mod_seen(osid, now, prune=False):
                return False  # that session's band will ask
    return True


def meter_view(st, cfg, now=None):
    """(seconds, target) as of now, read-only: the stored meter plus the running stretch since the last
    tick (capped, as the next tick would add it)."""
    now = time.time() if now is None else now
    m = st.get("meter_s") if _valid_num(st.get("meter_s")) else 0.0
    last = st.get("last_tick_ts")
    if st.get("meter_on") is True and _valid_num(last) and now > last:
        m += min(now - last, METER_GAP_CAP_S)
    t = st.get("meter_target_s")
    base, jit = float(cfg["meter_base_s"]), float(cfg["meter_jitter_s"])
    if not _valid_num(t) or not (max(0.0, base - jit) - 0.5 <= t <= base + jit + 0.5):
        t = base
    return m, t


def meter_line(st, cfg, now=None):
    """'waiting meter: 3m 12s of ~5m' (the base, not the jittered target)."""
    m, _t = meter_view(st, cfg, now)
    out = f"waiting meter: {fmt_secs(m)} of ~{fmt_secs(cfg['meter_base_s'])}"
    if any_ask_open(st, now if now is not None else time.time()):
        out += " (on hold while a set is asked)"
    return out


METER_BAR_CELLS = 5


def meter_bar(st, cfg, now=None):
    """'▰▰▱▱▱': the meter against its target, for the status line (no lock, no write)."""
    m, t = meter_view(st, cfg, now)
    full = METER_BAR_CELLS if t <= 0 else min(METER_BAR_CELLS, int(METER_BAR_CELLS * m / t))
    return "▰" * full + "▱" * (METER_BAR_CELLS - full)


def _serve_after_turn(sid, ctl):
    """After the turn loop: keep the listener up while its question is (the grace window after Stop /
    Esc, or the linger after a click), then close it. An unclicked grace question is declined here when
    its window runs out."""
    lst = ctl.get("listener")
    if lst is None:
        return
    cfg = load_config()
    cap = time.time() + float(cfg.get("ask_grace_s", 180)) + click_linger_s() + 30.0
    try:
        while time.time() < cap:
            with locked_state() as st:
                s = st["sessions"].get(sid)
                L = s.get("listener") if isinstance(s, dict) else None
                if not (isinstance(L, dict) and L.get("token") == lst.token):
                    return  # the next prompt replaced the session (and declined the question)
                now = time.time()
                if grace_ask(s) is not None and grace_ask(s, now) is None:
                    gone = settle_grace(s, sid, now)  # the window ran out, unclicked: declined, silently
                    if gone:
                        log_declines(st, gone, cfg)
                if listener_keep(s, L, now):
                    L["alive_until"] = now + LISTENER_ALIVE_S
                else:
                    s.pop("listener", None)
                    return
            lst.start()  # (asked and Stopped before the loop ever started it)
            time.sleep(min(GRACE_POLL_S, ask_poll_s()))
        with locked_state() as st:  # the safety cap: never serve past it
            s = st["sessions"].get(sid)
            if isinstance(s, dict) and isinstance(s.get("listener"), dict) and s["listener"].get("token") == lst.token:
                s.pop("listener", None)
                gone = settle_grace(s, sid, time.time())
                if gone:
                    log_declines(st, gone, cfg)
    finally:
        lst.close()
        ctl["listener"] = None


def _timer_loop(sid, token, transcript, delay=None, ctl=None):
    ctl = ctl if ctl is not None else {"listener": None, "reason": None}
    cfg = load_config()
    start = time.time()
    deadline = start + float(cfg["max_lifetime_s"])
    if not _valid_num(delay):
        delay = max(min_delay_s(), float(cfg["delay_s"]))
    if not _sleep_while_current(sid, token, float(delay), deadline):
        return
    next_at = 0.0  # earliest time of the next nudge for this turn (repeat_s spacing)
    while time.time() < deadline:
        cfg = load_config()
        stale_s = float(cfg["stale_s"])
        # Long waits (repeat, cooldown) are sliced so idleness is noticed promptly.
        poll = max(0.2, min(30.0, stale_s / 2))
        meter = meter_mode(cfg)
        if meter:
            poll = min(poll, meter_poll_s())  # v7: the meter's tick, the ask rule and the Esc check
        activity = last_activity(transcript) if transcript else None
        mode = cfg.get("ask_mode", "button")
        fired = None
        wait = 0.0
        with locked_state() as st:
            s = st["sessions"].get(sid)
            if not s or s.get("token") != token:
                return  # Stop (or a newer prompt) got here first: the turn ended before its due time
            now = time.time()
            refresh_idle_spinner(st, cfg, now=now, force=False)  # a new local day, a pause that ran out
            turn_ts = (s.get("turn") or {}).get("ts") or s.get("started_at") or 0
            if meter:
                meter_tick(st, cfg, now)  # the stretch since the last tick, by the state then
                s["meter_poll_at"] = now  # this turn's timer is alive (the band preference looks at it)
                if expire_meter_ask(st, cfg, sid, s, now):
                    poll = min(poll, ask_poll_s())  # the listener closes on this poll
            pend = [p for p in (s.get("pending") or []) if is_open(p)]
            on_screen = bool(pend) and s.get("nudging") is not False
            # "✅ <set>" after an accepted ask is on screen too, until Stop, Esc or the timer's end
            ticked = [p for p in (s.get("pending") or []) if valid_pending(p) and p.get("status") == "accepted"]
            if (on_screen or ticked or meter) and transcript and transcript_interrupted(transcript, turn_ts):
                ctl["reason"] = "esc"
                if meter and isinstance(s.get("turn"), dict):
                    s["turn"]["over_at"] = now  # v7: the meter stops counting this turn
                    meter_tick(st, cfg, now)
                return  # Esc while the task is up: no Stop will come; take it down now (finally: nudging off)
            if on_screen:
                pend[-1]["show_until"] = now + show_heartbeat_s()  # alive: keep it up a little longer
                poll = min(poll, ONSCREEN_POLL_S)
            if ticked:
                ticked[-1]["show_until"] = now + show_heartbeat_s()
                poll = min(poll, ONSCREEN_POLL_S)
            if open_ask(s) is not None:
                poll = min(poll, ask_poll_s())
            # v5: the button listener lives while its question is up (plus a short linger after a click)
            close_listener = False
            lst = ctl.get("listener")
            if lst is not None:
                L = s.get("listener")
                if isinstance(L, dict) and L.get("token") == lst.token and listener_keep(s, L, now):
                    L["alive_until"] = now + LISTENER_ALIVE_S
                    poll = min(poll, ask_poll_s())
                else:
                    if isinstance(L, dict) and L.get("token") == lst.token:
                        s.pop("listener", None)
                    close_listener = True
            if activity is not None and now - activity > stale_s:
                # Looks idle: Esc'd, or a long silent tool call. Do not nudge, but
                # keep the turn and its pending sets; resume if activity comes back.
                if not s.get("idle"):
                    s["idle"] = True
                    s["updated_at"] = now
                wait = poll
            else:
                if s.get("idle"):
                    s["idle"] = False
                held = is_paused(st, now) or in_quiet_hours(cfg, now)
                if held and not meter:
                    return
                # v7, meter mode: paused / quiet hours ask nothing but keep the timer watching (Esc, staleness,
                # the meter's tick); a timer that returned here would leave the turn counted after a resume
                remaining = max((st.get("last_fired_at") or 0) + float(cfg["cooldown_s"]) - now, next_at - now)
                asking = mode == "button"
                if meter:
                    # v7: the waiting meter decides (no cooldown, no repeats, no due time)
                    if held or not meter_should_ask(st, cfg, sid, s, now):
                        wait = poll
                    elif transcript and transcript_interrupted(transcript, turn_ts):
                        ctl["reason"] = "esc"
                        if isinstance(s.get("turn"), dict):
                            s["turn"]["over_at"] = now
                            meter_tick(st, cfg, now)
                        return
                    else:
                        fired = {}
                elif asking and (not _valid_num((s.get("turn") or {}).get("ts")) or any(
                        valid_pending(p) and p.get("status") in ASKED_STATUSES for p in (s.get("pending") or []))
                        or grace_ask(s, now) is not None):
                    # no human prompt time (v4.2): nothing is asked. One ask per turn: once a set was asked
                    # this turn (taken or still open) nothing more is asked until the next turn (and the
                    # global cooldown)
                    wait = poll
                elif remaining > 0:
                    wait = min(remaining, poll)
                elif transcript and transcript_interrupted(transcript, turn_ts):
                    ctl["reason"] = "esc"
                    return  # Esc'd: the turn is over without a Stop; nothing becomes due
                else:
                    fired = {}
                if fired is not None:
                    fired = dict(ensure_next_set(st, cfg))  # the set chosen ahead, now due
                    fired["ts"] = now
                    fired["show_until"] = now + show_heartbeat_s()  # refreshed on every poll while the timer lives
                    if asking:
                        fired.update(status="asked", asked_at=now, id=secrets.token_hex(4))
                    if mode == "button" and ctl.get("listener") is None:
                        try:
                            lst, fields = make_asker(cfg, sid, fired["id"])
                            ctl["listener"] = lst
                            s["listener"] = dict(fields, ask_id=fired["id"], timer=token,
                                                 alive_until=now + LISTENER_ALIVE_S, pid=os.getpid())
                        except Exception as e:
                            log_error("button-listen", e)  # no link: the question shows without it
                    s.setdefault("pending", []).append(fired)
                    s["updated_at"] = now
                    turn = s.get("turn")
                    if isinstance(turn, dict) and not turn.get("fired"):
                        turn["fired"] = True
                        turn["fired_at_s"] = round(now - (turn.get("ts") or now), 3)
                    st["last_fired_at"] = now
                    st["last_exercise"] = fired["exercise"]
                    line = coach_pick(st, cfg, "fire", s.get("theme"))
                    if meter:
                        meter_reset(st, cfg)  # v7: the ask empties the meter; a new target
                # leaving the with-block writes the task into the spinner (sync_spinner)
            if meter:
                meter_tick(st, cfg, now)  # running or not, after this poll's changes (an ask freezes it)
        if close_listener and ctl.get("listener") is not None:
            ctl["listener"].close()
            ctl["listener"] = None
        elif ctl.get("listener") is not None:
            ctl["listener"].start()  # no-op once serving
        if fired is None:
            if not _sleep_while_current(sid, token, wait + 0.01, deadline):
                return
            continue
        what = task_text(fired)
        asking = fired.get("status") == "asked"
        # the task is the body; the coach line is at most the subtitle (banners are off by default)
        notify("Workout Buddy", render_fire(line, fired), what + ("?" if asking else ""), cfg, kind="fire",
               speech=f"Time for {what}")
        next_at = fired["ts"] + float(cfg["repeat_s"])


def _post_log_updates(st, cfg, log, now):
    """Achievements + level change after sets were logged. Returns (new_ids, level_line or None)."""
    new = unlock_achievements(st, log, cfg, now)
    streak = streak_days(log)
    enabled = cfg.get("progression", True)
    lvl, mult = level_for(streak, enabled), multiplier(streak, enabled)
    prev = st.get("level")
    st["level"] = lvl
    line = None
    if enabled and ((isinstance(prev, int) and prev != lvl) or (prev is None and lvl > 1)):
        line = f"{fmt_level(lvl, mult)} reps"
    return new, line


def stop_summary(sets, log):
    """One plain line: 'Logged 20 push-ups · today 3 sets / 55 reps / 30s · 🔥4d · /workout undo to take it back'.
    No praise, no "next" (the next set is never shown before it is due); zero parts are left out."""
    n, reps, secs = today_totals(log)
    parts = [f"Logged {sets}", f"today {fmt_totals(n, reps, secs)}"]
    streak = streak_days(log)
    if streak >= 1:
        parts.append(f"🔥{streak}d")
    parts.append("/workout undo to take it back")
    return " · ".join(parts)


def _record_stop(st, cfg, sid, s, now):
    """Turn bookkeeping at Stop: the turn record, session history, adaptive hi, retrain trigger."""
    turn = s.get("turn")
    if not (s.get("token") and isinstance(turn, dict) and _valid_num(turn.get("ts"))):
        return
    duration = max(0.0, now - turn["ts"])
    append_turns([turn_record(turn, sid, duration, False)])
    s["hist"] = st["hists"][sid] = hist_after(st["hists"].get(sid) or s.get("hist"), duration, now)
    if meter_mode(cfg):
        return  # v7: the turn is recorded (features + duration); no decisions, no adaptive hi, no retrain
    adapt_after_turn(st, cfg, turn, duration)
    for d in reversed(st.get("decisions") or []):
        if isinstance(d, dict) and d.get("sid") == sid_hash(sid) and d.get("ts") == turn["ts"]:
            d["outcome"] = {"duration_s": round(duration, 1), "fired": bool(turn.get("fired")),
                            "fired_at_s": turn.get("fired_at_s"), "censored": False}
            break
    pr = predict_state(st)
    pr["new_since_train"] += 1
    if pr["new_since_train"] >= cfg["retrain_every"]:
        pr["new_since_train"] = 0
        try:
            proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_train"], start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    close_fds=True)
            st["timer_pids"].append(proc.pid)
        except Exception as e:
            log_error("train-spawn", e)


def split_pending(entries):
    """(credit, accepted, declined) from a turn's pending + carried entries: v3.2-style due sets (no
    status, "ask_mode": "auto") are credited at Stop; accepted asks were logged when accepted; asks never
    accepted (still asked, or superseded by a later ask) are declined. Skipped (undone) ones are dropped."""
    credit, accepted, declined = [], [], []
    for p in entries:
        if not valid_pending(p):
            continue
        st_ = p.get("status")
        if st_ is None:
            credit.append(p)
        elif st_ == "accepted":
            accepted.append(p)
        elif st_ in ("asked", "superseded"):
            declined.append(p)
    return credit, accepted, declined


def cmd_hook_stop():
    payload = read_stdin_json()
    sid = str(payload.get("session_id") or "default")
    cfg = load_config()
    out = None
    snapshot = None
    try:
        with locked_state() as st:
            snapshot = st
            s = st["sessions"].get(sid)
            if not s:
                return
            now = time.time()
            try:
                _record_stop(st, cfg, sid, s, now)
            except Exception as e:
                log_error("stop-record", e)
            if s.get("token"):
                try:
                    start_grace(st, cfg, s, now, sid)  # v5: the button's question stays up ask_grace_s more
                except Exception as e:
                    log_error("stop-grace", e)
            credit, accepted, declined = split_pending((s.get("carried") or []) + (s.get("pending") or []))
            this_turn = {id(p) for p in (s.get("pending") or [])}
            announce = [p for p in accepted if id(p) in this_turn and not p.get("announced")]
            end_badge(s, now)  # the badge stays up badge_s past the later of acceptance and Stop
            s["token"] = None
            s["pending"] = []
            s["carried"] = []
            s["turn"] = None
            s["updated_at"] = now
            if meter_mode(cfg):
                try:
                    meter_tick(st, cfg, now)  # v7: the turn's last stretch counts; this session is idle now
                except Exception as e:
                    log_error("stop-meter", e)
            if declined:
                # asked, never taken: on the record, counted nowhere, and nothing is said
                log_declines(st, [log_rec(p, sid, now, "declined", asked_at=p.get("asked_at"), ask_id=p.get("id"))
                                  for p in declined], cfg)
            if not credit and not announce:
                # nothing was due (or nothing taken): nothing on screen, nothing written (unless the idle
                # lines are stale: the first Stop of a new local day, a changed config, a pause that ran out)
                refresh_idle_spinner(st, cfg, now=now, force=False)
                return
            new_ach, level_lines = [], []
            for p in announce:
                p["announced"] = True
                new_ach += [a for a in (p.get("ach") or []) if a in ACH_BY_ID and a not in new_ach]
                if p.get("level_line"):
                    level_lines = [p["level_line"]]  # the latest level
            recs = [{"exercise": p["exercise"], "reps": int(p.get("reps") or 0), "unit": p.get("unit", "reps")}
                    for p in announce]
            if credit:
                crecs = [log_rec(p, sid, now, "done") for p in credit]
                append_log(crecs)
                recs += crecs
            log = read_log()
            if credit:
                ach2, level_line = _post_log_updates(st, cfg, log, now)
                new_ach += [a for a in ach2 if a not in new_ach]
                if level_line:
                    level_lines = [level_line]
                ensure_next_set(st, cfg, log, force=True)
                refresh_idle_spinner(st, cfg, log, now)  # the new totals, streak, next set, achievements
            else:
                refresh_idle_spinner(st, cfg, now=now, force=False)
            # leaving the with-block swaps the task for the idle lines (one write), unless another
            # session has a set due
        sets = " + ".join(task_text(r) for r in recs)
        if credit:  # an accepted ask already had its banner when it was taken
            notify("Workout Buddy", "Claude's done", f"Logged {sets}", cfg, kind="done")
            notify_achievements([a for a in new_ach if a in ach2], cfg)
        lines = [stop_summary(sets, log)] + ach_lines(new_ach) + level_lines
        out = {"systemMessage": "\n".join(lines)}
        if cfg.get("tab_title", True):
            out["terminalSequence"] = tab_title_sequence(recs, streak_days(log))
    finally:
        write_tips(cfg, snapshot)
    if out:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))


# --------------------------------------------------------------------------
# status line

def peek_config():
    """Config for the status line: read-only (never creates config.json or logs)."""
    try:
        with open(path("config.json"), encoding="utf-8") as f:
            return normalize_config(json.load(f))
    except Exception:
        return normalize_config({})


def _badge_until(st, sid, now, badge_s=None):
    """(text, until, badge dict) of this session's success badge while it shows, else (None, None, None).
    See session_badge."""
    s = (st.get("sessions") or {}).get(sid) if sid else None
    if not isinstance(s, dict) or is_paused(st, now):
        return None, None, None
    b = s.get("badge")
    if not isinstance(b, dict) or not isinstance(b.get("text"), str) or not _valid_num(b.get("accepted_at")):
        return None, None, None
    badge_s = float(BADGE_S if badge_s is None else badge_s)
    acc = b["accepted_at"]
    if _valid_num(b.get("ended_at")):
        until = max(acc, b["ended_at"]) + badge_s
    elif s.get("token") and s.get("token") == b.get("token"):
        p = next((p for p in (s.get("pending") or []) if valid_pending(p) and p.get("status") == "accepted"
                  and p.get("id") == b.get("ask_id")), None)
        if p is None:
            return None, None, None  # undone with /workout undo
        hb = p.get("show_until") if _valid_num(p.get("show_until")) and s.get("nudging") is not False else 0
        until = max(acc + badge_s, hb)  # the timer's heartbeat keeps it up for the rest of the turn
    else:
        return None, None, None
    return (b["text"], until, b) if now <= until else (None, None, None)


def session_badge(st, sid, now, badge_s=None):
    """'✅ 20 crunches · bravo! 🔥' after this session's ask was taken, or None. Shown for the rest of the
    turn (while its timer keeps it up) and at least badge_s after the acceptance; once the turn ended
    (Stop, Esc, the timer's end: ended_at) until badge_s after the later of acceptance and that end. A
    new prompt in the session drops it (hook-prompt replaces the session record). From timestamps only:
    no timer needed after Stop."""
    return _badge_until(st, sid, now, badge_s)[0]


def _question(p):
    return f"💪 {fmt_set(p['exercise'], int(p.get('reps', 0)), p.get('unit', 'reps'))}?"


def session_view(st, sid, now, badge_s=None):
    """What this session shows, as data: (kind, text, entry, until). kind is "ask" (the question, text
    with its OSC 8 link while a v5 listener lives), "badge" (the success badge, entry the badge dict) or
    None. until: the time it stops showing unless something refreshes it (the timer's heartbeat, the
    grace window, the badge's end). The status line and ui.json (v6) both read this."""
    s = (st.get("sessions") or {}).get(sid) if sid else None
    if not isinstance(s, dict) or is_paused(st, now):
        return None, None, None, None
    pend = [p for p in (s.get("pending") or []) if valid_pending(p) and p.get("status") in ("asked", "accepted")]
    if s.get("token") and pend and pend[-1]["status"] == "asked":
        p = pend[-1]
        show_until = p.get("show_until")
        if s.get("nudging") is not False and _valid_num(show_until) and now <= show_until:
            return "ask", _question(p) + button_link(s, p, now), p, show_until
        if grace_ask(s, now) is None:
            return None, None, None, None
    p = grace_ask(s, now)  # v5: after Stop / Esc the question stays up ask_grace_s, with its button
    if p is not None:
        btn = button_link(s, p, now)
        # v5.1: only while its listener is alive (a dead timer: gone within 10 s, not 180); v6: or, in
        # "button": "mod", while its keeper is (the mod draws the button)
        if btn or keeper_alive(s, p, now):
            until = min(s["grace"]["until"], s["listener"]["alive_until"])
            return "ask", _question(p) + btn, p, until
    text, until, b = _badge_until(st, sid, now, badge_s)
    return ("badge", text, b, until) if text else (None, None, None, None)


def session_ask(st, sid, now, badge_s=None):
    """(text, entry) for this session: '💪 10 push-ups?' while asked (only while the turn is live and its
    timer alive: nudging not False, within show_until, which the timer refreshes; Esc or a dead timer
    takes it down), then the success badge once taken (see session_badge). (None, None) otherwise. Only
    the session whose turn asked shows either."""
    kind, text, entry, _until = session_view(st, sid, now, badge_s)
    return (text, entry) if kind else (None, None)


def session_ask_text(st, sid, now, badge_s=None):
    return session_ask(st, sid, now, badge_s)[0]


# --------------------------------------------------------------------------
# v6: ui.json, the mod's cheap feed
#
# The mod polls one small file instead of spawning Python every frame. Every state change goes through
# locked_state(), which rewrites ui.json (atomically, 0600) whenever what a session shows changed:
#   {"version": 1, "updated_at": <ts>, "sessions": {<sid>: {"mode": "ask" | "badge" | "none", "ask_id",
#    "exercise", "reps", "unit", "text": "20 squats", "cheer", "until_ts"}}}
# It mirrors session_view (the status line stays the source of truth) and never holds a token or a port.
# until_ts is when the entry stops showing unless state refreshes it: the mod hides it after that.

UI_VERSION = 1


def badge_cheer(b):
    c = b.get("cheer") if isinstance(b, dict) else None
    if isinstance(c, str) and c:
        return c
    t = b.get("text") if isinstance(b, dict) else None
    return t.split(" · ", 1)[1] if isinstance(t, str) and " · " in t else ""


def ui_entry(st, sid, now, badge_s=None):
    kind, _text, entry, until = session_view(st, sid, now, badge_s)
    if kind == "ask":
        p = entry
        return {"mode": "ask", "ask_id": p.get("id"), "exercise": p["exercise"], "reps": int(p.get("reps") or 0),
                "unit": p.get("unit", "reps"),
                "text": fmt_set(p["exercise"], int(p.get("reps") or 0), p.get("unit", "reps")),
                "cheer": "", "until_ts": round(until, 3)}
    if kind == "badge":
        b = entry
        s = st["sessions"][sid]
        p = next((q for q in ((s.get("pending") or []) + ([s["grace"]["ask"]] if isinstance(s.get("grace"), dict)
                                                         else []))
                  if isinstance(q, dict) and q.get("id") == b.get("ask_id")), None) or b.get("set") or {}
        ex, reps, unit = p.get("exercise"), p.get("reps"), p.get("unit", "reps")
        text = fmt_set(ex, int(reps or 0), unit) if isinstance(ex, str) and _valid_num(reps) else \
            b["text"].removeprefix("✅ ").split(" · ", 1)[0]
        return {"mode": "badge", "ask_id": b.get("ask_id"), "exercise": ex if isinstance(ex, str) else None,
                "reps": int(reps) if _valid_num(reps) else None, "unit": unit, "text": text,
                "cheer": badge_cheer(b), "until_ts": round(until, 3)}
    return {"mode": "none", "ask_id": None, "exercise": None, "reps": None, "unit": None, "text": "",
            "cheer": "", "until_ts": None}


def ui_snapshot(st, now=None, badge_s=None):
    now = now if now is not None else time.time()
    sessions = st.get("sessions") if isinstance(st.get("sessions"), dict) else {}
    return {sid: ui_entry(st, sid, now, badge_s) for sid in sessions if isinstance(sessions[sid], dict)}


def write_ui(st, now=None):
    """Rewrite ui.json when what any session shows changed (never raises)."""
    try:
        cfg = peek_config()
        sessions = ui_snapshot(st, now, cfg.get("badge_s", BADGE_S))
        p = path("ui.json")
        try:
            with open(p, encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and old.get("version") == UI_VERSION and old.get("sessions") == sessions:
                return False
        except Exception:
            pass
        atomic_write_bytes(p, (json.dumps({"version": UI_VERSION, "updated_at": round(time.time(), 3),
                                           "sessions": sessions}, ensure_ascii=False) + "\n").encode("utf-8"))
        return True
    except Exception as e:
        log_error("ui-json", e)
        return False


def band_draws(cfg, sid, now=None):
    """v6.1: True when this session's ask and badge belong to the mod's band, not the status line:
    "button": "mod" and the mod has beaten for this session (mod_seen, the same test that drops the OSC 8
    link). Any other session, and "button": "link", keeps them in the status line."""
    return cfg.get("button") == "mod" and mod_seen(sid, now)


def status_text(prefer_sid=None, now=None):
    """This session's ask ('💪 10 push-ups?', then the badge '✅ 10 push-ups · bravo! 🔥'); else, for "ask_mode": "auto" sets,
    '🏋 NOW: 41s plank' while one is due; else '💤 paused'; else '💪 3 sets today 🔥4d' once a set is
    logged today; else empty. Never the next set before it is due. Empty with "statusline": false.
    v6.1: no ask and no badge where the mod's band shows them (band_draws); the rest is unchanged."""
    cfg = peek_config()
    if not cfg.get("statusline", True):
        return ""
    now = now if now is not None else time.time()
    try:
        with open(path("state.json"), encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            st = empty_state()
    except Exception:
        st = empty_state()
    try:
        ask, _entry = session_ask(st, prefer_sid, now, cfg.get("badge_s", BADGE_S))
    except Exception:
        ask = None
    if ask and band_draws(cfg, prefer_sid, now):
        ask = None  # the band above the prompt shows the question and the badge
    if ask:
        return ask
    p = due_set(st, now, prefer_sid=prefer_sid, skip_idle=True, asked=False)
    if p:
        return f"🏋 NOW: {fmt_set(p['exercise'], int(p.get('reps', 0)), p.get('unit', 'reps'))}"
    pu = st.get("paused_until") or 0
    if _valid_num(pu) and pu > now:
        return "💤 paused"
    log = read_log()
    n, _, _ = today_totals(log, local_date(now))
    if n:
        streak = streak_days(log, local_date(now))
        out = f"💪 {n} set{'s' if n != 1 else ''} today"
        if streak >= 2:
            out += f" 🔥{streak}d"
        if meter_mode(cfg):
            try:
                out += " " + meter_bar(st, cfg, now)  # v7: the waiting meter, from state.json as read
            except Exception:
                pass
        return out
    return ""


# --------------------------------------------------------------------------
# share card

CARD_INNER = 48  # "║ " + 48 + " ║" = 52 columns


def char_width(ch):
    """Terminal column width: wide/fullwidth (emoji) = 2, joiners/selectors/combining = 0, else 1."""
    if ch in "‍︎️" or unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(s):
    return sum(char_width(c) for c in s)


def fit(s, width):
    """Truncate (with …) and right-pad s to exactly `width` display columns."""
    if display_width(s) > width:
        out, w = "", 0
        for c in s:
            cw = char_width(c)
            if w + cw > width - 1:
                break
            out += c
            w += cw
        s = out + "…"
    return s + " " * (width - display_width(s))


def week_summary(log, cfg, st, today=None):
    today = today or dt.date.today()
    start = today - dt.timedelta(days=6)
    done = [r for r in done_records(log) if start <= local_date(r["ts"]) <= today]
    by_ex = _by_exercise(done)
    by_day = {}
    for r in done:
        d = local_date(r["ts"])
        by_day[d] = by_day.get(d, 0) + 1
    reps = sum(_amount(r) for r in done if r.get("unit", "reps") != "seconds")
    secs = sum(_amount(r) for r in done if r.get("unit") == "seconds")
    best = max(by_day.items(), key=lambda kv: (kv[1], kv[0])) if by_day else None
    got = st.get("achievements") or {}
    latest = None
    known = [k for k in got if k in ACH_BY_ID and isinstance(got[k], dict)]
    if known:
        order = [a[0] for a in ACHIEVEMENTS]  # same-moment unlocks: the bigger one wins
        latest = ACH_BY_ID[max(known, key=lambda k: (got[k].get("at") or 0, order.index(k)))][1]
    streak = streak_days(log, today)
    enabled = cfg.get("progression", True)
    return {"start": start, "end": today, "sets": len(done), "reps": reps, "secs": secs, "by_ex": by_ex,
            "best": best, "latest": latest, "streak": streak,
            "level": level_for(streak, enabled), "mult": multiplier(streak, enabled)}


def _range(a, b):
    if a.year == b.year:
        return f"{a:%b} {a.day} – {b:%b} {b.day}, {b.year}"
    return f"{a:%b} {a.day}, {a.year} – {b:%b} {b.day}, {b.year}"


def share_card(log, cfg, st, today=None):
    w = week_summary(log, cfg, st, today)
    W = CARD_INNER
    top = "╔" + "═" * (W + 2) + "╗"
    sep = "╟" + "─" * (W + 2) + "╢"
    bot = "╚" + "═" * (W + 2) + "╝"

    def row(s=""):
        return "║ " + fit(s, W) + " ║"

    lines = [top, row("WORKOUT BUDDY · for Claude Code"), row(f"📅 {_range(w['start'], w['end'])}"), sep]
    vol = f"{w['reps']:,} reps"
    if w["secs"]:
        vol += f" · {fmt_secs(w['secs'])} held"
    lines.append(row(f"💪 {vol} · {w['sets']} set{'s' if w['sets'] != 1 else ''}"))
    lines.append(row(f"🔥 {w['streak']}-day streak · {fmt_level(w['level'], w['mult'])}"))
    lines.append(sep)
    if w["by_ex"]:
        top_sets = max(c for c, _, _ in w["by_ex"].values())
        for ex, (c, tot, unit) in sorted(w["by_ex"].items(), key=lambda kv: (-kv[1][0], kv[0]))[:8]:
            n = max(1, round(18 * c / top_sets))
            bar = "█" * n + "░" * (18 - n)
            val = fmt_secs(tot) if unit == "seconds" else f"{tot:,}"
            val = f"{c}× · {val}"  # the bar is set count; the label says so, then the volume
            lines.append(row(f"{fit(ex, 13)} {bar} {val:>14}"))
    else:
        lines.append(row("No sets yet this week. /workout done to start."))
    lines.append(sep)
    if w["best"]:
        d, c = w["best"]
        lines.append(row(f"Best day: {d:%a} {d:%b} {d.day} · {c} set{'s' if c != 1 else ''}"))
    lines.append(row(f"🏆 Latest: {w['latest']}" if w["latest"] else "🏆 Latest: none yet. First Blood awaits."))
    lines.append(sep)
    lines.append(row("while Claude coded, I trained 💪"))
    lines.append(row("#ClaudeCode"))
    lines.append(bot)
    return "\n".join(lines)


def share_text(log, cfg, st, today=None):
    w = week_summary(log, cfg, st, today)
    parts = []
    if w["reps"]:
        parts.append(f"{w['reps']:,} reps")
    if w["secs"]:
        parts.append(f"{fmt_secs(w['secs'])} of holds")
    vol = " + ".join(parts) or "0 reps (so far)"
    l1 = f"This week, while Claude coded, I trained: {vol} across {w['sets']} set{'s' if w['sets'] != 1 else ''} 💪"
    if w["streak"] >= 2:
        l1 += f" {w['streak']}-day streak 🔥"
    l2 = "My Claude Code hooks nudge me to move every time the model is working. Compile-time cardio."
    return f"{l1}\n{l2}\n#ClaudeCode #WorkoutBuddy"


# --------------------------------------------------------------------------
# cli

HELP = """Workout Buddy: /workout <command>
  stats                 today, last 7 days, streak, level, totals, waiting meter (the default)
  achievements          unlocked and locked achievements, with progress
  share                 a weekly card to screenshot
  share --text          two or three lines to post
  pause [minutes|day]   no asks for a while (default 60 min; 'day' = until midnight)
  resume                asks are back on
  skip                  skip the set being asked (as 2 in the band); the next one will be different
  done [exercise] [n]   log a set by hand (e.g. done push-ups 20); bare: the set being asked
  undo                  take back the most recently logged set
  setup                 optional extras (spinner words, banners); changes nothing without --yes
  config                where the data lives and the settings in effect
  help                  this text
In the band above the prompt: 1 = done, 2 = skip."""


def cli_stats(cfg):
    log = read_log()
    done = done_records(log)
    today = dt.date.today()
    lines = []
    n, reps, secs = today_totals(log, today)
    lines.append(f"Today: {n} set{'s' if n != 1 else ''}, {reps} reps" + (f", {secs}s held" if secs else ""))
    for ex, (c, tot, unit) in sorted(_by_exercise([r for r in done if local_date(r["ts"]) == today]).items()):
        lines.append(f"  {ex}: {c} × = {tot}{'s' if unit == 'seconds' else ''}")
    week_start = today - dt.timedelta(days=6)
    week = [r for r in done if local_date(r["ts"]) >= week_start]
    lines.append(f"Last 7 days: {len(week)} set{'s' if len(week) != 1 else ''}")
    for ex, (c, tot, unit) in sorted(_by_exercise(week).items(), key=lambda kv: -kv[1][0]):
        lines.append(f"  {ex}: {c} set{'s' if c != 1 else ''}, {tot}{'s' if unit == 'seconds' else ' reps'}")
    s = streak_days(log, today)
    lines.append(f"Streak: {s} day{'s' if s != 1 else ''}" + (" 🔥" if s >= 2 else ""))
    if cfg.get("progression", True):
        lines.append(f"{fmt_level(level_for(s), multiplier(s))} reps (+0.1 per 3 streak days, max ×1.5)")
    else:
        lines.append("Level: progression off (×1.0)")
    tot_reps = sum(_amount(r) for r in done if r.get("unit") != "seconds")
    tot_secs = sum(_amount(r) for r in done if r.get("unit") == "seconds")
    skipped = sum(1 for r in log if r.get("status") == "skipped")
    lines.append(f"All time: {len(done)} sets, {tot_reps} reps, {tot_secs}s held, {skipped} skipped")
    st = read_state()
    lines.append(f"Achievements: {len(st.get('achievements') or {})}/{len(ACHIEVEMENTS)} 🏆")
    if meter_mode(cfg):
        ml = meter_line(st, cfg)
        lines.append(ml[:1].upper() + ml[1:])
    if is_paused(st):
        until = dt.datetime.fromtimestamp(st["paused_until"]).strftime("%H:%M")
        lines.append(f"Paused until {until}")
    return "\n".join(lines)


def cli_achievements(cfg):
    log = read_log()
    with locked_state() as st:
        if unlock_achievements(st, log, cfg):  # catch up on anything earned since the last Stop
            refresh_idle_spinner(st, cfg, log)
        got = dict(st.get("achievements") or {})
    stats = ach_stats(log, cfg)
    lines = [f"🏆 Achievements — {len(got)}/{len(ACHIEVEMENTS)} unlocked"]
    for a in ACHIEVEMENTS:
        if a[0] in got:
            at = got[a[0]].get("at") if isinstance(got[a[0]], dict) else None
            when = f" · {dt.datetime.fromtimestamp(at):%b %d}" if _valid_num(at) else ""
            lines.append(f"  ✅ {a[1]} — {a[2]}{when}")
    for a in ACHIEVEMENTS:
        if a[0] in got:
            continue
        target = _ach_target(a, stats)
        if a[3] in ("night", "early"):
            prog = "not yet"
        elif a[3] == "hold_day":
            prog = f"best day {fmt_secs(stats['hold_day'])} of 5m"
        else:
            cur = min(stats[a[3]], target)
            prog = f"{cur:,}/{target:,} {a[5]}".rstrip()
        lines.append(f"  🔒 {a[1]} — {a[2]} · {prog}")
    return "\n".join(lines)


def _by_exercise(recs):
    out = {}
    for r in recs:
        c, tot, unit = out.get(r.get("exercise", "?"), (0, 0, r.get("unit", "reps")))
        out[r.get("exercise", "?")] = (c + 1, tot + _amount(r), unit)
    return out


def cli_pause(args, cfg=None):
    cfg = cfg or load_config()
    now = time.time()
    if args and args[0].lower() in ("day", "today"):
        tomorrow = dt.datetime.combine(dt.date.today() + dt.timedelta(days=1), dt.time())
        until = tomorrow.timestamp()
    else:
        minutes = 60.0
        if args:
            try:
                minutes = float(args[0])
                if minutes <= 0:
                    raise ValueError
            except ValueError:
                return f"Bad minutes: {args[0]!r}. Usage: pause [minutes|day]"
        until = now + minutes * 60
    with locked_state() as st:
        st["paused_until"] = until
        refresh_idle_spinner(st, cfg)
    return f"💤 Paused until {dt.datetime.fromtimestamp(until).strftime('%a %H:%M')}."


def cli_resume(cfg=None):
    cfg = cfg or load_config()
    with locked_state() as st:
        was = is_paused(st)
        st["paused_until"] = 0
        refresh_idle_spinner(st, cfg)
    return "▶️ Resumed — nudges are back on." if was else "Not paused — nudges are on."


def cli_undo(cfg):
    """`/workout undo`: mark the most recently logged set "skipped" (taken back) and drop its badge."""
    with file_lock():
        log = read_log()
        for i in range(len(log) - 1, -1, -1):
            if log[i].get("status") == "done":
                log[i]["status"] = "skipped"
                log[i]["skipped_at"] = time.time()
                data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in log)
                atomic_write_bytes(path("log.jsonl"), data.encode("utf-8"))
                r = log[i]
                undone = fmt_set(r.get('exercise', '?'), r.get('reps', 0), r.get('unit', 'reps'))
                break
        else:
            return "Nothing to undo — no logged sets."
    with locked_state() as st:
        ensure_next_set(st, cfg, force=True)
        if r.get("ask_id"):
            for s in st["sessions"].values():
                g = s.get("grace") if isinstance(s, dict) else None
                gp = [g["ask"]] if isinstance(g, dict) and isinstance(g.get("ask"), dict) else []
                for p in ((s.get("pending") or []) + gp) if isinstance(s, dict) else []:
                    if isinstance(p, dict) and p.get("id") == r["ask_id"] and p.get("status") == "accepted":
                        p["status"] = "skipped"  # off the status line, not announced at Stop
                b = s.get("badge") if isinstance(s, dict) else None
                if isinstance(b, dict) and b.get("ask_id") == r["ask_id"]:
                    s.pop("badge", None)  # the badge goes too, also after Stop
        refresh_idle_spinner(st, cfg)
    return f"↩️ Undone: {undone}."


def _norm(s):
    return s.lower().replace("-", " ").replace("_", " ").strip()


def _done_asked(cfg, reps=None, exercise=None):
    """`/workout done` while a set is asked (any live session, the newest ask; with `exercise`, only an ask
    of that exercise): log that set now ("via": "manual") and close the question. None when nothing
    (matching) is asked."""
    now = time.time()
    with locked_state() as st:
        best = None
        if not is_paused(st, now):
            for sid, s in st["sessions"].items():
                if not isinstance(s, dict):
                    continue
                # the live turn's open ask, or (v5) a question still up after its turn (ask_grace_s)
                for p, g in ((open_ask(s) if s.get("token") else None, None),
                             (grace_ask(s, now), s.get("grace"))):
                    if p is None or (exercise is not None and p.get("exercise") != exercise):
                        continue
                    if best is None or (p.get("asked_at") or 0) > (best[2].get("asked_at") or 0):
                        best = (sid, s, p, g)
        if best is None:
            return None
        sid, s, p, g = best
        if reps is not None:
            p["reps"] = reps
        rec, log, new_ach = accept_ask(st, cfg, sid, s, p, now, via="manual", announce=False,
                                       turn_ts=g.get("turn_ts") if g is not None else None)
        if g is not None:
            end_badge(s, now)  # its turn is over: the badge shows badge_s from now
        L = s.get("listener")
        if isinstance(L, dict) and L.get("ask_id") == p.get("id"):
            L["clicked_at"] = now  # as a click: the button lingers a moment, then says "Already logged"
        level_line = p.get("level_line")
    notify_achievements(new_ach, cfg)
    n, r, secs = today_totals(log)
    lines = [f"💪 Logged {task_text(rec)} · today: {fmt_totals(n, r, secs)}"] + ach_lines(new_ach)
    if level_line:
        lines.append(level_line)
    return "\n".join(lines)


ALREADY_LOGGED_S = 120


def _recent_accept(now=None):
    """A bare `/workout done` right after the band's [1] already logged the set: say so instead of logging
    the next set. None otherwise."""
    now = now if now is not None else time.time()
    for r in reversed(read_log()):
        if r.get("status") == "skipped":
            continue
        if r.get("status") == "done" and r.get("via") in ("button", "mod") and _valid_num(r.get("ts")) \
                and 0 <= now - r["ts"] < ALREADY_LOGGED_S:
            what = fmt_set(r.get("exercise", "?"), _amount(r), r.get("unit", "reps"))
            return (f"Already logged: {what}, {int(now - r['ts'])}s ago. "
                    f"/workout done <exercise> [n] logs another set; /workout undo takes it back.")
        if r.get("status") == "done":
            return None
    return None


# v6: the mod's two buttons answer one exact ask by its id ("ask_id" in ui.json: 8 hex digits)
_ASK_ID = re.compile(r"[0-9a-f]{8}")
_cli_exit = 0  # the exit status of `cli` (v6: done / decline <ask_id> exit 1 when nothing was answered)


def _known_ask_id(st, ask_id):
    if any(r.get("id") == ask_id for r in _ask_hist(st)):
        return True
    for s in (st.get("sessions") or {}).values():
        if not isinstance(s, dict):
            continue
        g = s.get("grace")
        for p in (s.get("pending") or []) + ([g["ask"]] if isinstance(g, dict) and isinstance(g.get("ask"), dict) else []):
            if isinstance(p, dict) and p.get("id") == ask_id:
                return True
        b = s.get("badge")
        if isinstance(b, dict) and b.get("ask_id") == ask_id:
            return True
    return False


def ask_id_arg(args):
    """The ask id in `cli done|decline <ask_id>` (also `--ask <id>`), or None. A bare 8-hex word counts
    when it has a letter, or when it names an ask on record (an all-digit one would otherwise be reps)."""
    words = list(args)
    if len(words) == 2 and words[0] in ("--ask", "--ask-id") and _ASK_ID.fullmatch(words[1]):
        return words[1]
    if len(words) == 1 and _ASK_ID.fullmatch(words[0]):
        w = words[0]
        if not w.isdigit() or _known_ask_id(read_state(), w):
            return w
    return None


def _find_open_ask(st, now, ask_id=None):
    """(sid, s, p, grace) of the ask `ask_id` while it is still open (the live turn's open ask, or the
    question still up after its turn), or with no id the newest open ask; None when there is none."""
    best = None
    for sid, s in (st.get("sessions") or {}).items():
        if not isinstance(s, dict):
            continue
        for p, g in ((open_ask(s) if s.get("token") else None, None), (grace_ask(s, now), s.get("grace"))):
            if p is None or (ask_id is not None and p.get("id") != ask_id):
                continue
            if best is None or (p.get("asked_at") or 0) > (best[2].get("asked_at") or 0):
                best = (sid, s, p, g)
    return best


def _closed_line(st, ask_id):
    """Why `ask_id` cannot be answered: already logged, or closed."""
    for s in (st.get("sessions") or {}).values():
        b = s.get("badge") if isinstance(s, dict) else None
        if isinstance(b, dict) and b.get("ask_id") == ask_id and isinstance(b.get("text"), str):
            return "Already logged: " + b["text"].removeprefix("✅ ").split(" · ", 1)[0] + "."
    r = _logged_before(ask_id)
    if r is not None and r.get("status") == "done":
        return f"Already logged: {fmt_set(r.get('exercise', '?'), _amount(r), r.get('unit', 'reps'))}."
    return "That question has closed — nothing logged."


def cli_done_ask(ask_id, cfg):
    """`cli done <ask_id>` (the mod's [1] done): accept exactly that ask, as a click would ("via": "mod");
    a no-op with one line when it is no longer open."""
    global _cli_exit
    now = time.time()
    with locked_state() as st:
        if is_paused(st, now):
            _cli_exit = 1
            return "Workout Buddy is paused — nothing logged."
        hit = _find_open_ask(st, now, ask_id)
        if hit is None:
            line = _closed_line(st, ask_id)
            _cli_exit = 0 if line.startswith("Already logged") else 1
            return line
        sid, s, p, g = hit
        rec, log, new_ach = accept_ask(st, cfg, sid, s, p, now, via="mod", announce=g is None,
                                       turn_ts=g.get("turn_ts") if g is not None else None)
        if g is not None:
            end_badge(s, now)  # its turn is over: the badge shows badge_s from now
        L = s.get("listener")
        if isinstance(L, dict) and L.get("ask_id") == p.get("id"):
            L["clicked_at"] = now
        snapshot = st
    write_tips(cfg, snapshot, log)
    notify("Workout Buddy", "Logged", f"Logged {task_text(rec)}", cfg, kind="done")
    notify_achievements(new_ach, cfg)
    n, r, secs = today_totals(log)
    parts = [f"💪 Logged {task_text(rec)}", f"today: {fmt_totals(n, r, secs)}"]
    parts += [f"🏆 {ACH_BY_ID[a][1]}" for a in new_ach if a in ACH_BY_ID]
    return " · ".join(parts)


def cli_decline(args, cfg):
    """`cli skip` / `cli decline [ask_id]` (the band's [2] skip): decline the open ask now, by the same path as every
    other decline (a "declined" log row, next_set rotated away from it, its listener closed). Silent:
    no banner, one line."""
    global _cli_exit
    ask_id = ask_id_arg(args) if args else None
    if args and ask_id is None:
        _cli_exit = 1
        return "Usage: /workout skip"
    now = time.time()
    with locked_state() as st:
        hit = _find_open_ask(st, now, ask_id)
        if hit is None:
            _cli_exit = 1
            return "No open question to skip." if ask_id is None else \
                "That question has closed — nothing to skip."
        sid, s, p, g = hit
        if g is not None:
            recs = settle_grace(s, sid, now)  # the question after its turn: closed, declined
        else:
            p["status"] = "declined"  # off the screen; Stop neither logs nor declines it again
            p["declined_at"] = now
            recs = [log_rec(p, sid, now, "declined", asked_at=p.get("asked_at"), ask_id=p.get("id"))]
        L = s.get("listener")
        if isinstance(L, dict) and L.get("ask_id") == p.get("id"):
            s.pop("listener", None)  # its timer finds the entry gone and closes the listener / keeper
        s["updated_at"] = now
        log_declines(st, recs, cfg)
        refresh_idle_spinner(st, cfg, now=now, force=False)
    return f"Skipped {task_text(dict(p, reps=int(p.get('reps') or 0)))} — next one will be different."


def _bad_ask_flag(args):
    """`--ask <x>` that ask_id_arg refused (not an 8-hex id): one line, never read as reps or a name."""
    global _cli_exit
    if args and args[0] in ("--ask", "--ask-id"):
        _cli_exit = 1
        return "Not an ask id — nothing done."
    return None


def cli_done(args, cfg):
    ask_id = ask_id_arg(args)
    if ask_id is not None:
        return cli_done_ask(ask_id, cfg)
    bad = _bad_ask_flag(list(args))
    if bad is not None:
        return bad
    reps = None
    words = list(args)
    if words:
        try:
            reps = int(words[-1])
            words = words[:-1]
        except ValueError:
            pass
    if reps is None and words:
        try:
            reps = int(words[0])
            words = words[1:]
        except ValueError:
            pass
    if reps is not None and reps <= 0:
        return "Reps must be positive."
    name = " ".join(words).strip()
    if not name:
        r = _done_asked(cfg, reps)
        if r is not None:
            return r
        r = _recent_accept()
        if r is not None:
            return r
    st = read_state()
    ex = None
    if name:
        n = _norm(name)
        for e in cfg["exercises"]:
            if _norm(e["name"]) == n:
                ex = e
                break
        if ex is None:
            for e in cfg["exercises"]:
                if _norm(e["name"]).startswith(n) or n.startswith(_norm(e["name"]).rstrip("s")):
                    ex = e
                    break
        if ex is None:
            ex = {"name": name, "min": 10, "max": 10, "unit": "reps"}
        r = _done_asked(cfg, reps, exercise=ex["name"])  # the set being asked, by name: close the ask
        if r is not None:
            return r
    else:
        ns = st.get("next_set") if set_fits_config(st.get("next_set"), cfg) else None
        if ns:  # no exercise given: the set that was on screen
            ex = next(e for e in cfg["exercises"] if e["name"] == ns["exercise"])
            if reps is None:
                reps = ns["reps"]
        else:
            last = st.get("last_exercise")
            ex = next((e for e in cfg["exercises"] if e["name"] == last), cfg["exercises"][0])
    if reps is None:
        reps = (ex["min"] + ex["max"]) // 2
    now = time.time()
    rec = {"ts": now, "session_id": "manual", "exercise": ex["name"], "reps": reps,
           "unit": ex["unit"], "status": "done"}
    with file_lock():
        append_log([rec])
    log = read_log()
    with locked_state() as st:
        new_ach, level_line = _post_log_updates(st, cfg, log, now)
        st["last_exercise"] = ex["name"]
        ensure_next_set(st, cfg, log, force=True)
        refresh_idle_spinner(st, cfg, log, now)
        # a set logged by hand does not clear a set due in a live turn: that one is credited at its Stop
    notify_achievements(new_ach, cfg)
    n, r, secs = today_totals(log)
    lines = [f"💪 Logged {fmt_set(ex['name'], reps, ex['unit'])} · today: {fmt_totals(n, r, secs)}"]
    lines += ach_lines(new_ach)
    if level_line:
        lines.append(level_line)
    return "\n".join(lines)


def cli_test(cfg):
    with locked_state() as st:
        p = dict(ensure_next_set(st, cfg))
        line = coach_pick(st, cfg, "fire")
    what = task_text(p)
    subtitle = render_fire(line, p)
    if not cfg.get("notification", True):
        return (f"Banners are off (\"notification\": false), so nothing was sent. It would show:\n"
                f"  Workout Buddy · {subtitle}\n  {what}\n"
                f"/workout config shows how to turn them on.")
    notify("Workout Buddy", subtitle, what, cfg, kind="test", speech=f"Time for {what}")
    return f"Sent a test notification: {what} ({subtitle})"


def cli_config(cfg):
    lines = [f"Config: {path('config.json')}  (edit it by hand; changes apply on the next prompt)",
             f"Data:   {home()}",
             f"Claude Code settings: {settings_path()}  (changed only by /workout setup ... --yes)"]
    if _config_error:
        lines.append(f"⚠️ config.json is invalid ({_config_error}); using defaults.")
    if not legacy_modes_allowed():
        # the README's keys only (an install: the waiting meter and the band's button, nothing else)
        for k in PUBLIC_CONFIG_KEYS:
            if k != "exercises":
                lines.append(f"  {k} = {json.dumps(cfg[k])}")
        lines.append(f"  ({meter_line(read_state(), cfg)})")
    else:
        meter = meter_mode(cfg)
        lines.append(f"  timing = {json.dumps(cfg['timing'])}")
        if meter:
            for k in ("meter_base_s", "meter_jitter_s", "meter_min_turn_s", "meter_ask_ttl_s"):
                lines.append(f"  {k} = {json.dumps(cfg[k])}")
            lines.append(f"  ({meter_line(read_state(), cfg)}; delay_s, repeat_s, cooldown_s and the predictor "
                         'keys apply only with "timing": "classic")')
        for k in ("delay_s", "repeat_s", "cooldown_s", "stale_s", "max_lifetime_s", "notification", "sound",
                  "sound_name", "say", "quiet_hours", "coach", "progression", "tab_title", "statusline",
                  "spinner", "button", "badge_s", "predict", "long_s", "early_delay_s", "late_delay_s",
                  "predict_hi", "predict_lo", "retrain_every"):
            lines.append(f"  {k} = {json.dumps(cfg[k])}")
    if not cfg["notification"]:
        lines.append('  (banners are off: /workout setup banners --yes turns them on, macOS only)')
    lines.append("  exercises:")
    for e in cfg["exercises"]:
        lines.append(f"    - {e['name']}: {e['min']}-{e['max']} {e['unit']}")
    return "\n".join(lines)


SETTINGS_BACKUPS_KEEP = 5


def read_settings_spinner(p):
    """(ok, current spinnerVerbs or None) from the settings.json at p, read-only."""
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False, None
        return True, data.get("spinnerVerbs")
    except Exception:
        return False, None


def backup_settings(src):
    """Copy the settings.json at src into <data>/backups/ (owner-only) before /workout setup changes it; keep
    the newest SETTINGS_BACKUPS_KEEP. Returns the backup's path, or None when there was nothing to copy."""
    try:
        with open(src, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    d = path("backups")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, f"settings.json.{dt.datetime.now():%Y%m%d-%H%M%S}.{secrets.token_hex(2)}")
    atomic_write_bytes(dst, raw)
    try:
        olds = sorted(n for n in os.listdir(d) if n.startswith("settings.json."))
        for n in olds[:-SETTINGS_BACKUPS_KEEP]:
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(d, n))
    except OSError:
        pass
    return dst


def set_config_keys(updates):
    """Change keys of the add-on's own config.json (never Claude Code's settings), keeping every other key.
    Under the workout .lock; a temp file and os.replace, never a truncation."""
    p = path("config.json")
    with file_lock():
        try:
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("config.json is not a JSON object")
        except FileNotFoundError:
            raw = public_config(DEFAULT_CONFIG)
        raw.update(updates)
        atomic_write_json(p, raw)
    return normalize_config(raw)


def _apply_spinner(cfg):
    """Apply the "spinner" setting to settings.json spinnerVerbs (only ever called after an explicit yes).
    Turning it on the first time remembers what spinnerVerbs held before (and saves a backup copy of
    settings.json); while it is on, that earlier value stands whenever no set is due; "off" puts it back
    (or removes the key if there was none) and stops managing the key. A key Workout Buddy never managed
    is never touched by "off"."""
    mode = cfg.get("spinner", "off")
    here = os.path.realpath(settings_path())
    backup = None
    with locked_state() as st:
        ensure_next_set(st, cfg)
        st["cfg_hash"] = config_hash(cfg)
        managed = isinstance(st.get("spinner_prior"), dict)
        agreed = agreed_settings_path(st) or here
        sp = agreed if managed else here
        if mode != "off" and managed and agreed != here:
            # set up for another profile's settings.json (a shared data dir): never move it silently
            return (f"Spinner words are already set up for {agreed}, not {here}. Nothing changed.\n"
                    "To move them here: run /workout setup spinner off --yes (it puts back the earlier value "
                    "in that file), then /workout setup spinner --yes again.")
        if mode == "off":
            if not managed:
                st["spinner_setup"] = False
                result = "unchanged"
                head = (f"Spinner off → {sp}: Workout Buddy never set spinnerVerbs there, so nothing was written. "
                        "Claude Code shows its own spinner words.")
            else:
                prior = st["spinner_prior"].get("value")
                backup = backup_settings(sp)
                result = write_spinner_verbs(prior, sp)
                if result in ("written", "unchanged"):
                    st.pop("spinner_prior", None)
                    st.pop("spinner_settings_path", None)
                    st["spinner_setup"] = False
                    st["spinner_last"] = None
                    if result == "written":
                        st["spinner_removed_at"] = time.time()
                what = (("removed spinnerVerbs" if prior is None else "put your earlier spinnerVerbs back")
                        if result == "written" else
                        "spinnerVerbs already held its earlier value, nothing written" if result == "unchanged"
                        else result)
                head = (f"Spinner off → {sp}: {what}. Workout Buddy no longer writes spinnerVerbs "
                        "(/workout setup spinner --yes turns it on again).")
        else:
            if not managed:
                ok, cur = read_settings_spinner(sp)
                if not ok:
                    log_error("spinner-settings-read")
                    st["spinner_setup"] = False
                    return (f"Spinner \"{mode}\" → {sp}: skipped: could not read {sp} (missing, or not a JSON "
                            "object). Nothing was written; Workout Buddy never creates that file.")
                st["spinner_prior"] = {"value": cur}
                st["spinner_settings_path"] = sp  # the one file this agreement covers
                backup = backup_settings(sp)
            st["spinner_setup"] = True
            if mode == "progress":
                refresh_idle_spinner(st, cfg)
            result = sync_spinner(st, force=True, spinner=mode)
            due = due_set(st)
            now_txt = (f"due now: {fmt_set(due['exercise'], int(due['reps']), due.get('unit', 'reps'))}" if due
                       else ("nothing due now, so it shows your progress lines" if mode == "progress"
                             else "nothing due now"))
            head = (f"Spinner \"{mode}\" → spinnerVerbs in {sp} ({now_txt}): {result}. "
                    f"It shows \"NOW: <set>\" only while one is due; "
                    + ("otherwise a few progress lines." if mode == "progress"
                       else "otherwise your own spinner words (or Claude Code's).")
                    + "\nBefore you uninstall Workout Buddy, run /workout setup spinner off --yes: uninstalling "
                    "alone leaves spinnerVerbs in that file.")
        conf = spinner_config(cfg, st)
        snapshot = st
    write_tips(cfg, snapshot)
    if backup:
        head += f"\nBackup of settings.json before the change: {backup}"
    return (head + "\nSpinner settings (tips.json refreshes after every set; Claude Code rereads it when it "
            "starts):\n" + json.dumps(conf, indent=2, ensure_ascii=False))


YES = ("--yes", "-y", "yes")


def setup_overview(cfg):
    st = read_state()
    spin_on = isinstance(st.get("spinner_prior"), dict) and st.get("spinner_setup")
    spin_path = (agreed_settings_path(st) if spin_on else None) or settings_path()
    lines = [
        "Workout Buddy setup. Nothing has been changed.",
        "",
        "Installing Workout Buddy changed nothing in your Claude Code settings. It works as is: the band",
        "above the prompt asks for a set, 1 = done, 2 = skip. Two extras are optional and change something",
        "only when you confirm with --yes:",
        "",
        f"1. Spinner words (now {'on' if spin_on else 'off'}). While a set is due, Claude's spinner says",
        "   \"NOW: 15 push-ups\". This changes one key, \"spinnerVerbs\", in",
        f"   {spin_path}",
        "   and nothing else in that file. A backup copy of the file is saved first, in",
        f"   {path('backups')}. Turning it off puts your earlier value back; do that before you uninstall.",
        "     turn on:   /workout setup spinner --yes",
        "     turn off:  /workout setup spinner off --yes",
        "",
        f"2. Desktop banners (now {'on' if cfg.get('notification') else 'off'}). A notification when a set is "
        "asked (macOS only).",
        f"   This changes \"notification\" in Workout Buddy's own {path('config.json')};",
        "   your Claude Code settings are not touched.",
        "     turn on:   /workout setup banners --yes",
        "     turn off:  /workout setup banners off --yes",
    ]
    if not is_macos():
        lines.append("   (this machine is not macOS, so banners would stay silent)")
    return "\n".join(lines)


def cli_setup(args, cfg):
    """`/workout setup`: explain the optional changes; apply one only with --yes. Subcommands:
    spinner [task|progress|off] --yes, banners [on|off] --yes, apply --yes (apply the "spinner" value
    already in config.json)."""
    words = [w.lower() for w in args]
    yes = any(w in YES for w in words)
    words = [w for w in words if w not in YES]
    what = words[0] if words else ""
    arg = words[1] if len(words) > 1 else None
    if what in ("", "help", "info"):
        return setup_overview(cfg)
    if what == "spinner":
        mode = arg or ("task" if cfg.get("spinner", "off") == "off" else cfg["spinner"])
        if mode == "on":
            mode = "task"
        if mode not in SPINNER_MODES:
            return "Usage: /workout setup spinner [task|progress|off] --yes"
        if not yes:
            if mode == "off":
                st = read_state()
                target = (agreed_settings_path(st) if isinstance(st.get("spinner_prior"), dict) else None) \
                    or settings_path()
                return ("This would put back the spinnerVerbs value you had before (or remove the key if there "
                        f"was none) in {target}, after saving a backup copy.\n"
                        "Nothing changed yet. To do it: /workout setup spinner off --yes")
            return (f"This would set one key, \"spinnerVerbs\", in {settings_path()} while a set is due "
                    "(\"NOW: 15 push-ups\"), and keep every other key as it is. A backup copy is saved first.\n"
                    f"Nothing changed yet. To do it: /workout setup spinner{'' if mode == 'task' else ' ' + mode} --yes")
        cfg = set_config_keys({"spinner": mode})
        return _apply_spinner(cfg)
    if what in ("banners", "banner", "notifications", "notification"):
        on = (arg or "on") in ("on", "true", "yes")
        if arg not in (None, "on", "off", "true", "false"):
            return "Usage: /workout setup banners [on|off] --yes"
        if not yes:
            return (f"This would set \"notification\": {json.dumps(on)} in {path('config.json')} (Workout Buddy's "
                    f"own file). Nothing changed yet. To do it: /workout setup banners {'on' if on else 'off'} --yes")
        set_config_keys({"notification": on})
        note = "" if is_macos() or not on else " (this machine is not macOS: banners stay silent)"
        return f"Banners {'on' if on else 'off'}: \"notification\": {json.dumps(on)} in {path('config.json')}.{note}"
    if what == "apply":
        if not yes:
            return (f"This would apply \"spinner\": {json.dumps(cfg.get('spinner', 'off'))} from config.json to "
                    f"{settings_path()}. Nothing changed yet. To do it: /workout setup apply --yes")
        return _apply_spinner(cfg)
    return "Unknown setup option.\n\n" + setup_overview(cfg)


def cmd_cli(args):
    cfg = load_config()
    cmd = (args[0].lower() if args else "stats")
    rest = args[1:]
    if cmd in ("stats", "stat", ""):
        return cli_stats(cfg)
    if cmd in ("achievements", "achievement", "trophies", "ach"):
        return cli_achievements(cfg)
    if cmd == "share":
        log, st = read_log(), read_state()
        if rest and rest[0].lower() in ("--text", "text", "-t"):
            return share_text(log, cfg, st)
        return share_card(log, cfg, st)
    if cmd == "setup":
        return cli_setup(rest, cfg)
    if cmd == "test":
        return cli_test(cfg)
    if cmd == "config":
        with locked_state() as st:
            refresh_idle_spinner(st, cfg, force=False)  # pick up a changed config.json now
        return cli_config(cfg)
    if cmd == "why":
        return cli_why(rest, cfg)
    if cmd in ("predict-stats", "predict", "pstats"):
        return cli_predict_stats(cfg)
    if cmd == "train":
        return cli_train(rest, cfg)
    if cmd in ("help", "-h", "--help"):
        return HELP
    if cmd == "pause":
        out = cli_pause(rest, cfg)
    elif cmd in ("resume", "unpause"):
        out = cli_resume(cfg)
    elif cmd == "undo":
        out = cli_undo(cfg)
    elif cmd in ("done", "log"):
        out = cli_done(rest, cfg)
    elif cmd in ("skip", "decline", "no"):
        out = cli_decline(rest, cfg)
    else:
        return f"Unknown command: {cmd}\n\n{HELP}"
    if meter_mode(cfg):
        try:
            with locked_state() as st:
                meter_tick(st, cfg, time.time())  # v7: an answered question (or a pause) moves the meter now
        except Exception as e:
            log_error("cli-meter", e)
    write_tips(cfg)  # state or log changed: refresh the spinner tips
    return out


# --------------------------------------------------------------------------

def main(argv):
    sub = argv[0] if argv else ""
    try:
        if sub == "hook-prompt":
            cmd_hook_prompt()
        elif sub == "hook-stop":
            cmd_hook_stop()
        elif sub == "_timer":
            if len(argv) >= 3:
                delay = None
                if len(argv) > 4:
                    try:
                        delay = float(argv[4])
                    except ValueError:
                        delay = None
                cmd_timer(argv[1], argv[2], argv[3] if len(argv) > 3 else "", delay)
        elif sub == "_train":
            cmd_train_bg()
        elif sub == "where":
            print(home())
        elif sub == "status":
            sys.stdout.write(status_text(argv[1] if len(argv) > 1 and argv[1] else None))
        elif sub == "spinner-config":
            cfg = load_config()
            with locked_state() as st:
                ensure_next_set(st, cfg)
                snapshot = st
            write_tips(cfg, snapshot)
            print(json.dumps(spinner_config(cfg, snapshot), indent=2, ensure_ascii=False))
        elif sub == "cli":
            # $ARGUMENTS may arrive as one quoted string
            args = [w for a in argv[1:] for w in a.split()]
            print(cmd_cli(args))
            return _cli_exit
        else:
            print(HELP)
    except Exception as e:
        log_error(sub or "main", e)
        if sub == "cli":
            print(f"Workout Buddy hit an error; details in {path('errors.log')}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
