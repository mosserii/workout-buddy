"""The public build's own promises: no hardcoded paths, where the data lives, consent before touching
Claude Code's settings, a clean Linux path, a silent prompt hook, and the plugin's shape on disk."""
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_workout import load_module, wait_for

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins" / "workout-buddy"
SCRIPT = PLUGIN / "scripts" / "workout.py"
WB = PLUGIN / "scripts" / "wb"
PY = sys.executable

# Each pattern is written so it does not match itself (a [x] class), so this file passes the same grep.
FORBIDDEN = [r"/U[s]ers/", r"/opt/h[o]mebrew", r"/home/z[o]har", r"\.claude/w[o]rkout\b", r"\.claude-z[o]harm",
             r"\.claude-r[i]\b", r"(?i)robotic[i]maging", r"z[o]har@", r"<[o]wner>"]
SHIPPED_SKIP_DIRS = {".git", ".tmp", "__pycache__", ".pytest_cache", "node_modules", "types"}


def shipped_files():
    """Everything a user gets from the repo, but the Python tests (which name the patterns they forbid)."""
    for p in REPO.rglob("*"):
        rel = p.relative_to(REPO)
        if any(part in SHIPPED_SKIP_DIRS for part in rel.parts) or not p.is_file():
            continue
        if rel.parts[0] == "tests":
            continue
        yield p


def kill_timers(home):
    try:
        st = json.loads((Path(home) / "state.json").read_text())
    except Exception:
        return
    for pid in st.get("timer_pids", []):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@pytest.fixture
def world(tmp_path):
    """A fresh machine as a plugin install sees it: HOME and CLAUDE_CONFIG_DIR are temp dirs with a
    settings.json, CLAUDE_PLUGIN_DATA is the plugin's data dir, and no WORKOUT_HOME (so no test knob is
    honoured: this is the real code path)."""
    home = tmp_path / "home"
    cfgdir = home / ".claude"
    cfgdir.mkdir(parents=True)
    settings = cfgdir / "settings.json"
    settings.write_text(json.dumps({"theme": "dark", "spinnerVerbs": {"mode": "append", "verbs": ["Mine"]}},
                                   indent=2) + "\n")
    data = cfgdir / "plugins" / "data" / "workout-buddy-workout-buddy"
    data.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("WORKOUT_", "CLAUDE_"))}
    env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(cfgdir), CLAUDE_PLUGIN_DATA=str(data),
               CLAUDE_PLUGIN_ROOT=str(PLUGIN))
    for k in ("SSH_CONNECTION", "SSH_TTY"):
        env.pop(k, None)
    w = {"home": home, "cfgdir": cfgdir, "settings": settings, "data": data, "env": env, "tmp": tmp_path}
    yield w
    kill_timers(data)


def sh(w, *args, stdin="", env=None):
    return subprocess.run(["sh", str(WB), *args], input=stdin, capture_output=True, text=True,
                          env=env or w["env"], timeout=20)


def tree(p):
    return sorted(str(x.relative_to(p)) for x in Path(p).rglob("*"))


def fingerprint(p):
    sb = p.stat()
    return sb.st_mtime_ns, sb.st_size, sb.st_ino, p.read_bytes()


# ---------------------------------------------------------------- no hardcoded paths

def test_no_hardcoded_paths_in_shipped_files():
    hits = []
    for p in shipped_files():
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pat in FORBIDDEN:
            for m in re.finditer(pat, text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{p.relative_to(REPO)}:{line}: {pat}")
    assert hits == []
    names = {p.name for p in shipped_files()}
    assert "mascot.js" in names


def test_the_launcher_and_the_hooks_use_the_plugin_root_only():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    cmds = [h["command"] for ev in ("UserPromptSubmit", "Stop") for g in hooks["hooks"][ev] for h in g["hooks"]]
    assert cmds == ['sh "${CLAUDE_PLUGIN_ROOT}/scripts/wb" hook-prompt', 'sh "${CLAUDE_PLUGIN_ROOT}/scripts/wb" hook-stop']
    assert hooks["modules"] == ["./register.js"]
    assert set(hooks["hooks"]) == {"UserPromptSubmit", "Stop"}
    assert WB.stat().st_mode & stat.S_IXUSR and SCRIPT.stat().st_mode & stat.S_IXUSR
    assert WB.read_text().startswith("#!/bin/sh\n")


def test_marketplace_lists_the_one_plugin():
    mk = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    plug = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert mk["name"] == "workout-buddy" and plug["name"] == "workout-buddy"
    assert [(p["name"], p["source"]) for p in mk["plugins"]] == [("workout-buddy", "./plugins/workout-buddy")]


# ---------------------------------------------------------------- the data dir

def test_data_dir_resolution_order(world, monkeypatch):
    e = dict(world["env"])
    assert sh(world, "where", env=e).stdout.strip() == str(world["data"])  # CLAUDE_PLUGIN_DATA
    assert sh(world, "where", env=dict(e, WORKOUT_HOME=str(world["tmp"] / "wh"))).stdout.strip() == \
        str(world["tmp"] / "wh")  # WORKOUT_HOME wins
    e.pop("CLAUDE_PLUGIN_DATA")
    assert sh(world, "where", env=e).stdout.strip() == str(world["home"] / ".workout-buddy")  # the fallback
    assert sh(world, "where", env=dict(e, CLAUDE_PLUGIN_DATA="  ")).stdout.strip() == \
        str(world["home"] / ".workout-buddy")  # blank is unset
    # the band's --home flag is WORKOUT_HOME by another name
    assert sh(world, "--home", str(world["tmp"] / "x"), "where").stdout.strip() == str(world["tmp"] / "x")
    # and in-process
    monkeypatch.setenv("HOME", str(world["home"]))
    monkeypatch.delenv("WORKOUT_HOME")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(world["data"]))
    mod = load_module()
    assert mod.home() == str(world["data"])
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    assert mod.home() == str(world["home"] / ".workout-buddy")


def test_claude_config_dir_is_respected(world, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(world["tmp"] / "cfg2"))
    mod = load_module()
    assert mod.settings_path() == str(world["tmp"] / "cfg2" / "settings.json")
    assert mod.default_transcript_roots() == [str(world["tmp"] / "cfg2" / "projects")]
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(world["home"]))
    assert mod.settings_path() == str(world["home"] / ".claude" / "settings.json")
    monkeypatch.setenv("WORKOUT_SETTINGS_PATH", str(world["tmp"] / "s.json"))
    assert mod.settings_path() == str(world["tmp"] / "s.json")
    out = sh(world, "cli", "config").stdout
    assert f"Claude Code settings: {world['settings']}" in out and f"Data:   {world['data']}" in out


# ---------------------------------------------------------------- consent

def test_install_and_use_write_nothing_to_settings_json(world):
    before = fingerprint(world["settings"])
    cfg_tree = [x for x in tree(world["cfgdir"]) if not x.startswith("plugins/data")]
    home_tree = tree(world["home"])
    sid = "sess-a"
    r = sh(world, "hook-prompt", stdin=json.dumps({"session_id": sid, "transcript_path": "", "prompt": "fix the bug"}))
    assert r.returncode == 0 and r.stdout == ""
    for args in (["cli"], ["cli", "stats"], ["cli", "setup"], ["cli", "setup", "spinner"], ["cli", "setup", "banners"],
                 ["cli", "config"], ["cli", "share"], ["cli", "help"], ["cli", "pause", "5"], ["cli", "resume"],
                 ["cli", "done", "squats", "20"], ["cli", "undo"], ["status", sid], ["spinner-config"], ["where"]):
        assert sh(world, *args).returncode == 0, args
    sh(world, "hook-stop", stdin=json.dumps({"session_id": sid}))
    assert fingerprint(world["settings"]) == before
    assert [x for x in tree(world["cfgdir"]) if not x.startswith("plugins/data")] == cfg_tree
    # nothing outside the plugin data dir either (no ~/.workout-buddy when Claude Code gives a data dir)
    assert [x for x in tree(world["home"]) if not x.startswith(".claude/plugins/data")] == \
        [x for x in home_tree if not x.startswith(".claude/plugins/data")]
    assert (world["data"] / "config.json").exists() and (world["data"] / "log.jsonl").exists()
    assert not (world["data"] / "turns.jsonl").exists()  # the predictor is off: no turn records
    assert not (world["data"] / "backups").exists()


def test_setup_explains_then_changes_only_on_yes_with_a_backup(world):
    s0 = fingerprint(world["settings"])
    out = sh(world, "cli", "setup").stdout
    assert out.startswith("Workout Buddy setup. Nothing has been changed.")
    assert "spinnerVerbs" in out and str(world["settings"]) in out and "--yes" in out
    assert "Desktop banners" in out
    assert "Nothing changed yet" in sh(world, "cli", "setup", "spinner").stdout
    assert "Nothing changed yet" in sh(world, "cli", "setup", "banners", "on").stdout
    assert fingerprint(world["settings"]) == s0
    cfg0 = json.loads((world["data"] / "config.json").read_text())
    assert cfg0["spinner"] == "off" and cfg0["notification"] is False

    out = sh(world, "cli", "setup", "spinner", "--yes").stdout
    assert "Backup of settings.json" in out
    backups = list((world["data"] / "backups").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == s0[3]
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert json.loads((world["data"] / "config.json").read_text())["spinner"] == "task"
    # nothing due: the person's own spinnerVerbs stand, every other key as it was
    assert json.loads(world["settings"].read_text()) == json.loads(s0[3])
    st = json.loads((world["data"] / "state.json").read_text())
    assert st["spinner_setup"] is True and st["spinner_prior"] == {"value": {"mode": "append", "verbs": ["Mine"]}}

    out = sh(world, "cli", "setup", "spinner", "off", "--yes").stdout
    assert out.startswith("Spinner off")
    assert json.loads(world["settings"].read_text()) == json.loads(s0[3])
    st = json.loads((world["data"] / "state.json").read_text())
    assert st["spinner_setup"] is False and "spinner_prior" not in st
    assert not list(world["settings"].parent.glob("settings.json.workout-tmp*"))

    out = sh(world, "cli", "setup", "banners", "on", "--yes").stdout
    assert out.startswith("Banners on")
    assert json.loads((world["data"] / "config.json").read_text())["notification"] is True
    assert json.loads(world["settings"].read_text()) == json.loads(s0[3])


def test_spinner_off_puts_back_what_was_there_and_removes_only_its_own_key(world):
    world["settings"].write_text(json.dumps({"theme": "dark"}, indent=2) + "\n")
    st_path = world["data"] / "state.json"
    sh(world, "cli", "setup", "spinner", "--yes")
    st = json.loads(st_path.read_text())
    assert st["spinner_prior"] == {"value": None}
    # a set is due: the key shows it (written by the same guarded writer, under the lock)
    st["sessions"]["s1"] = {"token": "t", "updated_at": time.time(),
                            "pending": [{"exercise": "squats", "reps": 20, "unit": "reps", "ts": time.time()}]}
    st_path.write_text(json.dumps(st))
    sh(world, "cli", "resume")
    assert json.loads(world["settings"].read_text())["spinnerVerbs"] == {"mode": "replace", "verbs": ["NOW: 20 squats"]}
    out = sh(world, "cli", "setup", "spinner", "off", "--yes").stdout
    assert "removed spinnerVerbs" in out
    assert json.loads(world["settings"].read_text()) == {"theme": "dark"}


# ---------------------------------------------------------------- the prompt hook is silent

@pytest.mark.parametrize("payload", [
    {"session_id": "s1", "transcript_path": "", "prompt": "refactor the parser"},
    {"session_id": "s1", "transcript_path": "", "prompt": "/workout stats"},
    {"session_id": "s1", "transcript_path": "", "prompt": "<task-notification>done</task-notification>"},
    {"session_id": "s2", "prompt": "x" * 20000},
    {},
    "not json at all",
])
def test_hook_prompt_prints_nothing(world, payload):
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    for _ in range(2):  # a first run (creates config.json) and a later one
        r = sh(world, "hook-prompt", stdin=stdin)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    r = subprocess.run([PY, str(SCRIPT), "hook-prompt"], input=stdin, capture_output=True, text=True,
                       env=world["env"], timeout=20)
    assert r.stdout == ""
    r = sh(world, "hook-stop", stdin=stdin)
    assert r.returncode == 0 and (r.stdout == "" or isinstance(json.loads(r.stdout), dict))


def test_missing_python_never_blocks_a_prompt(world, tmp_path):
    env = dict(world["env"], PATH=str(tmp_path / "empty"))
    r = subprocess.run(["/bin/sh", str(WB), "hook-prompt"], input="{}", capture_output=True, text=True, env=env,
                       timeout=20)
    assert r.returncode == 0 and r.stdout == ""
    assert r.stderr.strip() == "Workout Buddy: python3 was not found on PATH; install Python 3.9+ to use it."


# ---------------------------------------------------------------- Linux: no macOS-only call

STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "{log}"
"""


def stub_bin(tmp_path):
    d = tmp_path / "stubbin"
    d.mkdir()
    log = tmp_path / "stub-calls.log"
    for name in ("osascript", "say", "ioreg", "afplay"):
        p = d / name
        p.write_text(STUB.format(log=log))
        p.chmod(0o755)
    return d, log


def _sandbox(world, platform, stubs):
    home = world["tmp"] / f"wh-{platform}"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"notification": True, "sound": True, "say": True, "close_tab": True,
                                                  "tab_title": True}))
    env = dict(world["env"], WORKOUT_HOME=str(home), WORKOUT_TEST="1", WORKOUT_PLATFORM=platform,
               PATH=str(stubs) + os.pathsep + world["env"].get("PATH", ""))
    return home, env


def test_linux_path_runs_cleanly_with_every_macos_call_disabled(world, tmp_path):
    stubs, calls = stub_bin(tmp_path)
    home, env = _sandbox(world, "linux", stubs)
    for args in (["cli", "test"], ["cli", "done", "squats", "20"], ["cli", "achievements"], ["cli", "stats"]):
        r = sh(world, *args, env=env)
        assert r.returncode == 0 and "error" not in r.stdout.lower(), (args, r.stdout)
    r = sh(world, "hook-prompt", env=env, stdin=json.dumps({"session_id": "s1", "transcript_path": "", "prompt": "go"}))
    assert r.stdout == ""
    sh(world, "hook-stop", env=env, stdin=json.dumps({"session_id": "s1"}))
    kill_timers(home)
    assert not calls.exists() or calls.read_text() == ""
    assert not (home / "errors.log").exists() or "osascript" not in (home / "errors.log").read_text()
    # in-process: banners and the tab close stand down
    os.environ["WORKOUT_PLATFORM"] = "linux"
    try:
        mod = load_module()
        assert mod.is_macos() is False
        assert mod.schedule_close_tab(52345, {"close_tab": True}) is None
    finally:
        os.environ["WORKOUT_PLATFORM"] = "darwin"


def test_the_stubs_would_have_seen_a_macos_call(world, tmp_path):
    """The control for the test above: the same run on the macOS path reaches the stubbed osascript."""
    stubs, calls = stub_bin(tmp_path)
    home, env = _sandbox(world, "darwin", stubs)
    sh(world, "cli", "test", env=env)
    assert calls.exists() and "osascript" in calls.read_text()


# ---------------------------------------------------------------- public defaults

def test_public_defaults(monkeypatch):
    mod = load_module()
    d = mod.DEFAULT_CONFIG
    want = {"timing": "meter", "cooldown_s": 300, "meter_base_s": 300, "meter_min_turn_s": 20, "meter_ask_ttl_s": 180,
            "button": "mod", "ask_mode": "button", "notification": False, "sound": False, "say": False,
            "tab_title": False, "spinner": "off", "statusline": False, "predict": False, "close_tab": False}
    assert {k: d[k] for k in want} == want
    assert mod.normalize_config({}) == mod.normalize_config(json.loads(json.dumps(d)))
    # the older modes are not reachable from config.json on a real install
    monkeypatch.delenv("WORKOUT_TEST_LEGACY_MODES", raising=False)
    old = {"ask_mode": "auto", "button": "link", "timing": "classic"}
    assert {k: mod.normalize_config(old)[k] for k in old} == {"ask_mode": "button", "button": "mod", "timing": "meter"}
    assert mod.normalize_config({"ask_mode": "idle"})["ask_mode"] == "button"
    monkeypatch.setenv("WORKOUT_TEST_LEGACY_MODES", "1")
    monkeypatch.delenv("WORKOUT_TEST")  # the flag alone is not enough: only a test run (WORKOUT_TEST=1) honours it
    assert {k: mod.normalize_config(old)[k] for k in old} == {"ask_mode": "button", "button": "mod", "timing": "meter"}


def test_user_facing_text_names_only_the_button(world):
    """Help, setup and config talk about the band's 1 / 2 and /workout done, and nothing else that logs a set."""
    texts = [sh(world, "cli", "help").stdout, sh(world, "cli", "setup").stdout, sh(world, "cli", "config").stdout,
             (REPO / "README.md").read_text()]
    for t in texts:
        for word in ("idle", "keyboard", "mouse", "hid", "ioreg", "step away", "stepping away", "ask_mode"):
            assert not re.search(r"\b" + word + r"\b", t, re.I), word


# ---------------------------------------------------------------- the meter asks only where a band shows

def test_meter_asks_only_in_a_session_whose_band_is_drawing(world):
    home = world["tmp"] / "wh-meter"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"meter_base_s": 0.3, "meter_jitter_s": 0, "meter_min_turn_s": 0.3,
                                                  "max_lifetime_s": 20}))
    (home / "mod-seen").mkdir()
    (home / "mod-seen" / "band").write_text("1")
    env = dict(world["env"], WORKOUT_HOME=str(home), WORKOUT_TEST="1", WORKOUT_METER_POLL_S="0.1",
               WORKOUT_ASK_POLL_S="0.1")
    try:
        for sid in ("plain", "band"):
            sh(world, "hook-prompt", env=env, stdin=json.dumps({"session_id": sid, "transcript_path": "", "prompt": "go"}))

        def feed():
            try:
                return json.loads((home / "ui.json").read_text())["sessions"]
            except Exception:
                return {}

        assert wait_for(lambda: feed().get("band", {}).get("mode") == "ask", 8)
        time.sleep(0.5)
        assert feed().get("plain", {}).get("mode") in (None, "none")
        ask_id = feed()["band"]["ask_id"]
        r = sh(world, "cli", "done", "--ask", ask_id, env=env)
        assert r.returncode == 0 and r.stdout.startswith("💪 Logged ")
        assert wait_for(lambda: feed().get("band", {}).get("mode") == "badge", 3)
        recs = [json.loads(x) for x in (home / "log.jsonl").read_text().splitlines()]
        assert [(x["status"], x.get("via")) for x in recs] == [("done", "mod")]
    finally:
        kill_timers(home)


def test_readme_and_license():
    r = (REPO / "README.md").read_text()
    install = r.split("## Install", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"^```\n(.*?)```$", install, re.M | re.S)
    assert blocks == ["/plugin marketplace add mosserii/workout-buddy\n/plugin install workout-buddy@workout-buddy\n"]
    assert "Claude Code 2.1.287 or later" in install and "`python3` 3.9 or later on your `PATH`" in install
    for s in ("**Claude thinks. You rep.**", "## Update", "/plugin marketplace update workout-buddy",
              "## How it works", "## /workout", "## Settings", "## Privacy", "## Uninstall",
              "/plugin uninstall workout-buddy@workout-buddy", "Clawd is Claude Code's mascot, drawn here as fan art.",
              "Not affiliated with or endorsed by Anthropic."):
        assert s in r, s
    lic = (REPO / "LICENSE").read_text()
    assert lic.startswith("MIT License\n\nCopyright (c) 2026 Zohar Mosseri\n")


# ---------------------------------------------------------------- review round 2 (one regression test each)

def _due(data, sid="s1", exercise="squats", reps=20):
    """Put a due set in state.json, as a turn's timer would."""
    st_path = Path(data) / "state.json"
    st = json.loads(st_path.read_text())
    st["sessions"][sid] = {"token": "t", "updated_at": time.time(),
                           "pending": [{"exercise": exercise, "reps": reps, "unit": "reps", "ts": time.time()}]}
    st_path.write_text(json.dumps(st))


def _not_due(data, sid="s1"):
    st_path = Path(data) / "state.json"
    st = json.loads(st_path.read_text())
    st["sessions"].pop(sid, None)
    st_path.write_text(json.dumps(st))


def test_spinner_writes_only_the_settings_json_agreed_to(world, tmp_path):
    """Finding 1: two Claude profiles share one data dir (WORKOUT_HOME). Setup in profile A must never let
    profile B's settings.json be written, set due or not, and "off" from B puts back A's own value."""
    shared = tmp_path / "shared"
    a_settings = world["settings"]
    a0 = a_settings.read_bytes()
    b_dir = tmp_path / "profile-b"
    b_dir.mkdir()
    b_settings = b_dir / "settings.json"
    b_settings.write_text(json.dumps({"theme": "light", "spinnerVerbs": {"mode": "append", "verbs": ["B's own"]}}) + "\n")
    env_a = dict(world["env"], WORKOUT_HOME=str(shared), CLAUDE_CONFIG_DIR=str(world["cfgdir"]))
    env_b = dict(world["env"], WORKOUT_HOME=str(shared), CLAUDE_CONFIG_DIR=str(b_dir))
    assert "Backup of settings.json" in sh(world, "cli", "setup", "spinner", "--yes", env=env_a).stdout
    b0 = fingerprint(b_settings)

    _due(shared)
    sh(world, "cli", "resume", env=env_b)  # profile B's process: the set shows in A's spinner, never in B's
    assert fingerprint(b_settings) == b0
    st = json.loads((shared / "state.json").read_text())
    assert st["spinner_settings_path"] == os.path.realpath(a_settings)
    assert json.loads(a_settings.read_text())["spinnerVerbs"] == {"mode": "replace", "verbs": ["NOW: 20 squats"]}
    _not_due(shared)
    sh(world, "hook-stop", stdin=json.dumps({"session_id": "zz"}), env=env_b)
    sh(world, "cli", "pause", "5", env=env_b)
    sh(world, "cli", "resume", env=env_b)
    assert fingerprint(b_settings) == b0  # A's earlier words never land in B
    assert a_settings.read_bytes() == a0  # A has its own earlier value back

    out = sh(world, "cli", "setup", "spinner", "--yes", env=env_b).stdout
    assert out.startswith("Spinner words are already set up for " + os.path.realpath(a_settings))
    assert fingerprint(b_settings) == b0
    assert os.path.realpath(a_settings) in sh(world, "cli", "setup", env=env_b).stdout
    assert os.path.realpath(a_settings) in sh(world, "cli", "setup", "spinner", "off", env=env_b).stdout

    _due(shared)
    sh(world, "cli", "resume", env=env_a)
    out = sh(world, "cli", "setup", "spinner", "off", "--yes", env=env_b).stdout
    assert out.startswith("Spinner off → " + os.path.realpath(a_settings)) and "earlier spinnerVerbs back" in out
    assert a_settings.read_bytes() == a0 and fingerprint(b_settings) == b0
    backups = list((shared / "backups").iterdir())
    assert backups and all(p.read_bytes() != b_settings.read_bytes() for p in backups)  # B never even copied
    st = json.loads((shared / "state.json").read_text())
    assert st["spinner_setup"] is False and "spinner_settings_path" not in st
    sh(world, "cli", "resume", env=env_a)
    assert a_settings.read_bytes() == a0  # no longer managed: nothing writes it


def test_spinner_setup_with_no_readable_settings_json_agrees_to_nothing(world):
    world["settings"].unlink()
    out = sh(world, "cli", "setup", "spinner", "--yes").stdout
    assert "skipped: could not read" in out and "Nothing was written" in out
    st = json.loads((world["data"] / "state.json").read_text())
    assert st["spinner_setup"] is False and "spinner_settings_path" not in st
    world["settings"].write_text("{}\n")  # the file shows up later: still nothing until setup runs again
    _due(world["data"])
    sh(world, "cli", "resume")
    assert world["settings"].read_text() == "{}\n"


def _band_world(world, **cfg):
    """A test run (WORKOUT_TEST=1: fast polls only, no older modes) with one band session, "band"."""
    home = world["tmp"] / "wh-band"
    home.mkdir()
    base = {"meter_base_s": 0.3, "meter_jitter_s": 0, "meter_min_turn_s": 0.3, "max_lifetime_s": 20}
    base.update(cfg)
    (home / "config.json").write_text(json.dumps(base))
    (home / "mod-seen").mkdir()
    (home / "mod-seen" / "band").write_text("1")
    env = dict(world["env"], WORKOUT_HOME=str(home), WORKOUT_TEST="1", WORKOUT_METER_POLL_S="0.1",
               WORKOUT_ASK_POLL_S="0.1")
    return home, env


def _feed(home):
    try:
        return json.loads((home / "ui.json").read_text())["sessions"]
    except Exception:
        return {}


def _log(home):
    p = home / "log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_an_unanswered_question_expires_noted_as_skipped_as_the_readme_says(world):
    """Finding 2: the README said an expired question logs nothing; it is logged as declined (a skip)."""
    home, env = _band_world(world, meter_ask_ttl_s=0.8)
    try:
        sh(world, "hook-prompt", env=env, stdin=json.dumps({"session_id": "band", "transcript_path": "", "prompt": "go"}))
        assert wait_for(lambda: _feed(home).get("band", {}).get("mode") == "ask", 8)
        ask_id = _feed(home)["band"]["ask_id"]
        assert wait_for(lambda: _log(home), 8)
        assert wait_for(lambda: _feed(home).get("band", {}).get("mode") == "none", 5)
        recs = _log(home)
        assert [(r["status"], r.get("expired"), r.get("ask_id")) for r in recs] == [("declined", True, ask_id)]
        assert "Today: 0 sets" in sh(world, "cli", "stats", env=env).stdout  # counted nowhere
    finally:
        kill_timers(home)
    readme = (REPO / "README.md").read_text()
    how = readme.split("## How it works", 1)[1].split("\n## ", 1)[0]
    assert "goes away after 3 minutes and is noted as skipped" in how
    assert "nothing is logged" not in readme


def test_an_install_never_opens_a_port(world, monkeypatch):
    """Finding 3: the status-line link and its 127.0.0.1 listener belong to the test suite's older modes.
    On an install, "statusline": true, "button": "link" or "timing": "classic" in config.json changes none
    of that: the meter, the band's button, and no question where no band draws."""
    home, env = _band_world(world, statusline=True, button="link", timing="classic")
    (home / "mod-seen" / "band").unlink()  # no band anywhere
    try:
        for sid in ("plain", "other"):
            sh(world, "hook-prompt", env=env, stdin=json.dumps({"session_id": sid, "transcript_path": "", "prompt": "go"}))
        time.sleep(2.0)  # many meter polls past the target
        st = json.loads((home / "state.json").read_text())
        for s in st["sessions"].values():
            assert "listener" not in s and not [p for p in s.get("pending") or [] if p.get("status") == "asked"]
        assert st.get("meter_s", 0) >= 0.3  # the meter ran past its target, and still nothing asked
        for sid in ("plain", "other"):
            out = sh(world, "status", sid, env=env).stdout
            assert "127.0.0.1" not in out and "\x1b]8;;" not in out
    finally:
        kill_timers(home)
    mod = load_module()
    monkeypatch.delenv("WORKOUT_TEST_LEGACY_MODES", raising=False)
    for cfg in ({"button": "link"}, {"button": "mod"}):
        asker, fields = mod.make_asker(cfg, "no-band-here", "abcd1234")
        assert isinstance(asker, mod.AskKeeper) and fields["kind"] == "mod" and "port" not in fields
    n = mod.normalize_config({"statusline": True, "button": "link", "timing": "classic"})
    assert (n["button"], n["timing"]) == ("mod", "meter")
    priv = (REPO / "README.md").read_text().split("## Privacy", 1)[1]
    assert "no network connections, opens\nno ports" in priv


def test_banners_are_recorded_but_never_shown_off_macos(monkeypatch, tmp_path):
    """Finding 4: on the Linux path notify() still records each banner in the test log (marked suppressed),
    so the suite sees the same events on either platform, and nothing macOS-only runs."""
    log_p = tmp_path / "notify.log"
    monkeypatch.setenv("WORKOUT_NOTIFY_LOG", str(log_p))
    monkeypatch.setenv("WORKOUT_PLATFORM", "linux")
    mod = load_module()
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: calls.append(a))
    mod.notify("Workout Buddy", "sub", "20 squats", {"notification": True, "say": True, "sound": True}, kind="fire",
               speech="Time for 20 squats")
    mod.notify("Workout Buddy", "sub", "off", {"notification": False}, kind="fire")
    recs = [json.loads(x) for x in log_p.read_text().splitlines()]
    assert [(r["body"], r["suppressed"]) for r in recs] == [("20 squats", True)] and calls == []
    monkeypatch.delenv("WORKOUT_NOTIFY_LOG")
    mod.notify("Workout Buddy", "sub", "20 squats", {"notification": True, "say": True}, kind="fire", speech="x")
    assert calls == []  # without the test log: Linux shows and says nothing
    monkeypatch.setenv("WORKOUT_NOTIFY_LOG", str(log_p))
    monkeypatch.setenv("WORKOUT_PLATFORM", "darwin")
    mod.notify("Workout Buddy", "sub", "mac", {"notification": True}, kind="fire")
    assert json.loads(log_p.read_text().splitlines()[-1])["suppressed"] is False and calls == []


def test_the_suite_defaults_to_the_public_build():
    """Finding 5: a module without `pytestmark = pytest.mark.legacy_modes` runs exactly what an install
    reads (WORKOUT_TEST=1 only turns on the test knobs), and only the modules written against the older
    modes opt in to them."""
    assert os.environ.get("WORKOUT_TEST") == "1" and "WORKOUT_TEST_LEGACY_MODES" not in os.environ
    mod = load_module()
    assert mod.sandboxed() is True and mod.legacy_modes_allowed() is False
    old = {"ask_mode": "auto", "button": "link", "timing": "classic"}
    assert {k: mod.normalize_config(old)[k] for k in old} == {"ask_mode": "button", "button": "mod", "timing": "meter"}
    marked = {p.name for p in (REPO / "tests").glob("test_*.py")
              if re.search(r"^pytestmark = pytest\.mark\.legacy_modes", p.read_text(), re.M)}
    assert "test_public.py" not in marked
    assert marked <= {"test_workout.py", "test_v2.py", "test_v3.py", "test_v32.py", "test_v4.py", "test_v41.py",
                      "test_v5.py", "test_v52.py", "test_v6.py", "test_v7.py"}


IDLE_WORDS = [r"ioreg", r"HIDIdleTime", r"IOHIDSystem", r"idle_accept", r"FAKE_IDLE", r"hid_idle", r"(?i)keyboard",
              r"(?i)\bmouse\b", r"(?i)step(ping|ped)? away", r"seen\.json", r"remote_session", r'"via": "idle"']


def test_the_public_source_has_no_idle_detection(world, monkeypatch):
    """Finding 7: no idle / HID acceptance in the shipped code, its comments or the config it writes."""
    hits = []
    for p in shipped_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        hits += [f"{p.relative_to(REPO)}: {w}" for w in IDLE_WORDS if re.search(w, text)]
    for p in (REPO / "tests").glob("test_*.py"):
        if p.name != "test_public.py":
            text = p.read_text()
            hits += [f"{p.name}: {w}" for w in (r"ioreg", r"HIDIdleTime", r"IOHIDSystem", r"FAKE_IDLE", r"hid_idle",
                                                  r"seen\.json", r'"via"\] == "idle"') if re.search(w, text)]
    assert hits == []
    mod = load_module()
    monkeypatch.setenv("WORKOUT_TEST_LEGACY_MODES", "1")  # not even the test suite's older modes have it
    assert "idle" not in mod.ASK_MODES and mod.normalize_config({"ask_mode": "idle"})["ask_mode"] == "button"
    assert "idle_accept_s" not in mod.DEFAULT_CONFIG
    sh(world, "hook-prompt", stdin=json.dumps({"session_id": "s1", "prompt": "hi"}))
    cfg = json.loads((world["data"] / "config.json").read_text())
    assert list(cfg) == list(mod.PUBLIC_CONFIG_KEYS)
    settings_table = (REPO / "README.md").read_text().split("## Settings", 1)[1].split("\n## ", 1)[0]
    assert re.findall(r"^\| `(\w+)` \|", settings_table, re.M) == list(mod.PUBLIC_CONFIG_KEYS)
    out = sh(world, "cli", "config").stdout
    for k in ("ask_mode", "ask_grace_s", "button", "timing", "delay_s", "predict", "statusline"):
        assert f"  {k} = " not in out, k
    (world["data"] / "config.json").unlink()
    sh(world, "cli", "setup", "banners", "on", "--yes")  # set_config_keys on a fresh file: the same keys
    cfg = json.loads((world["data"] / "config.json").read_text())
    assert list(cfg) == list(mod.PUBLIC_CONFIG_KEYS) and cfg["notification"] is True


def test_no_personal_notes_in_any_file_of_the_repo():
    """Finding 8: the author's name only as the author (LICENSE, the manifests), never a quote or a note."""
    hits = []
    for p in list(shipped_files()) + list((REPO / "tests").glob("*.py")):
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer("Zo" + r"har(?! Mosseri)|\b[Hh]e looks\b|\b20\d\d-\d\d-\d\d: \"", text):
            hits.append(f"{p.relative_to(REPO)}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)}")
    assert hits == []


def test_skip_skips_the_question_and_undo_takes_back_a_set(world):
    """Finding 9: /workout skip is the band's 2 (the question goes, nothing logged is touched); /workout
    undo takes back the last logged set."""
    home, env = _band_world(world)
    try:
        assert sh(world, "cli", "done", "squats", "20", env=env).stdout.startswith("💪 Logged 20 squats")
        sh(world, "hook-prompt", env=env, stdin=json.dumps({"session_id": "band", "transcript_path": "", "prompt": "go"}))
        assert wait_for(lambda: _feed(home).get("band", {}).get("mode") == "ask", 8)
        ask = _feed(home)["band"]
        r = sh(world, "cli", "skip", env=env)
        assert r.returncode == 0 and r.stdout.startswith(f"Skipped {ask['text']}")
        assert wait_for(lambda: _feed(home).get("band", {}).get("mode") == "none", 3)
        assert [(x["status"], x.get("ask_id")) for x in _log(home)] == [("done", None), ("declined", ask["ask_id"])]
        r = sh(world, "cli", "skip", env=env)
        assert r.returncode == 1 and r.stdout.startswith("No open question to skip")
        assert [x["status"] for x in _log(home)] == ["done", "declined"]  # still nothing taken back
        r = sh(world, "cli", "undo", env=env)
        assert r.stdout.startswith("↩️ Undone: 20 squats") and [x["status"] for x in _log(home)] == ["skipped", "declined"]
        assert sh(world, "cli", "undo", env=env).stdout.startswith("Nothing to undo")
    finally:
        kill_timers(home)
    mod = load_module()
    assert "skip the set being asked" in mod.HELP and "undo                  take back" in mod.HELP
    assert mod.stop_summary("20 squats", []).endswith("/workout undo to take it back")
    readme = (REPO / "README.md").read_text()
    assert "| `/workout skip` | Skip the set being asked" in readme and "| `/workout undo` | Take back" in readme
    register = (PLUGIN / "hooks" / "register.js").read_text()
    assert "skip|undo" in register


def test_test_knobs_need_WORKOUT_TEST_not_just_a_data_dir(world, monkeypatch, tmp_path):
    """Finding 11: WORKOUT_HOME is a user setting (and the band passes --home on every press); it never
    turns on a test knob or the older modes. Only WORKOUT_TEST=1 does."""
    home = tmp_path / "user-home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"button": "link", "timing": "classic", "ask_mode": "auto"}))
    env = dict(world["env"], WORKOUT_TEST_LEGACY_MODES="1", WORKOUT_PLATFORM="linux", WORKOUT_METER_POLL_S="0.1")
    out = sh(world, "--home", str(home), "cli", "config", env=env).stdout
    assert f"Data:   {home}" in out and "  timing = " not in out and "  button = " not in out
    monkeypatch.setenv("WORKOUT_HOME", str(home))
    monkeypatch.delenv("WORKOUT_TEST")
    for k, v in (("WORKOUT_TEST_LEGACY_MODES", "1"), ("WORKOUT_METER_POLL_S", "0.1"), ("WORKOUT_ASK_POLL_S", "0.1"),
                 ("WORKOUT_HEARTBEAT_S", "0.1"), ("WORKOUT_MIN_DELAY_S", "0"), ("WORKOUT_PLATFORM", "plan9")):
        monkeypatch.setenv(k, v)
    mod = load_module()
    assert mod.sandboxed() is False and mod.legacy_modes_allowed() is False
    assert (mod.meter_poll_s(), mod.ask_poll_s(), mod.show_heartbeat_s(), mod.min_delay_s()) == (5.0, 2.0, 45.0, 10.0)
    assert mod.is_macos() == (sys.platform == "darwin")
    src = SCRIPT.read_text()
    assert "WORKOUT_HOME is set without WORKOUT_SETTINGS_PATH" not in src
    assert 'os.environ.get("WORKOUT_HOME") and' not in src


def test_python_older_than_3_9_says_so_and_never_blocks(tmp_path):
    """Finding 12: the file parses on 3.8; on 3.8 it prints one line to stderr, nothing to stdout (the
    prompt hook's stdout reaches Claude), and exits 0."""
    import ast
    ast.parse(SCRIPT.read_text(), feature_version=(3, 8))
    code = ("import sys, runpy; sys.version_info = (3, 8, 10, 'final', 0); sys.argv = ['workout.py', 'hook-prompt'];"
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')")
    r = subprocess.run([PY, "-c", code], input="{}", capture_output=True, text=True, timeout=20,
                       env=dict(os.environ, WORKOUT_HOME=str(tmp_path / "wh")))
    assert r.returncode == 0 and r.stdout == ""
    assert r.stderr == "Workout Buddy: needs Python 3.9 or later (found 3.8); it does nothing until then.\n"
    assert not (tmp_path / "wh").exists()  # nothing ran


PY39 = "/usr/bin/python3"


@pytest.mark.skipif(not (os.path.exists(PY39) and subprocess.run([PY39, "-c", "import sys; sys.exit(sys.version_info[:2] != (3, 9))"]).returncode == 0),
                    reason="needs a Python 3.9 at /usr/bin/python3")
def test_the_floor_python_3_9_runs_every_path(world):
    """The README's floor, for real: the hooks, a set, the band's answer path and the share card on 3.9."""
    env = dict(world["env"], PATH=os.path.dirname(PY39) + os.pathsep + world["env"]["PATH"])
    r = subprocess.run([PY39, str(SCRIPT), "hook-prompt"], input=json.dumps({"session_id": "s", "prompt": "fix it"}),
                       capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    for args in (["cli", "done", "squats", "20"], ["cli", "share"], ["cli", "stats"], ["status", "s"],
                 ["cli", "done", "--ask", "abcd1234"]):
        r = subprocess.run([PY39, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=20)
        assert r.stderr == "" and "error" not in r.stdout.lower(), (args, r.stdout, r.stderr)
    r = subprocess.run([PY39, str(SCRIPT), "hook-stop"], input=json.dumps({"session_id": "s"}), capture_output=True,
                       text=True, env=env, timeout=20)
    assert r.returncode == 0 and r.stderr == ""
    assert not (world["data"] / "errors.log").exists()


PRIVACY = {  # every number kept about a prompt -> the words the README's Privacy section uses for it
    "char_len": "length in characters", "word_count": "words", "line_count": "and lines",
    "has_code_block": "code block", "question_mark": "question mark", "is_slash_command": "slash command",
    "build_verb": '"build" verb', "build_verb_first": "started with one", "category": "one-word topic",
    "hour_of_day": "hour of the day", "session_prev_duration_s": "how long its previous turn took",
    "session_ema_duration_s": "its average turn length",
    "seconds_since_session_prev_stop": "the time since its\n    last turn ended",
}
HISTS = {"prev_duration_s": "the last turn's length", "ema_duration_s": "the average", "prev_stop_ts": "when it ended"}


def test_the_privacy_section_names_everything_kept_about_a_prompt(world):
    """Finding 13: whatever hook-prompt and Stop keep is named in the README (a new field fails here)."""
    priv = (REPO / "README.md").read_text().split("## Privacy", 1)[1].split("\n## ", 1)[0]
    sh(world, "hook-prompt", stdin=json.dumps({"session_id": "s1", "transcript_path": "", "prompt": "/fix the tests?"}))
    st = json.loads((world["data"] / "state.json").read_text())
    feats = st["sessions"]["s1"]["turn"]["features"]
    assert set(feats) == set(PRIVACY)
    for k, words in PRIVACY.items():
        assert words in priv, k
    assert "fix the tests" not in (world["data"] / "state.json").read_text()
    sh(world, "hook-stop", stdin=json.dumps({"session_id": "s1"}))
    st = json.loads((world["data"] / "state.json").read_text())
    assert set(st["hists"]["s1"]) == set(HISTS)
    for k, words in HISTS.items():
        assert words in priv, k
    assert "for 30 days, for at most 200 sessions" in priv
    mod = load_module()
    assert (mod.HIST_TTL_S, mod.HIST_KEEP) == (30 * 86400, 200)


def test_setup_warns_to_turn_the_spinner_off_before_uninstalling(world):
    """Finding 14: uninstalling alone leaves spinnerVerbs behind; setup says so when it turns it on."""
    out = sh(world, "cli", "setup", "spinner", "--yes").stdout
    assert ("Before you uninstall Workout Buddy, run /workout setup spinner off --yes: uninstalling alone leaves "
            "spinnerVerbs in that file.") in out
    assert "do that before you uninstall" in sh(world, "cli", "setup").stdout
    readme = (REPO / "README.md").read_text()
    assert "run it before you\n  uninstall" in readme and "first run `/workout setup spinner off --yes`" in readme


def test_the_share_card_claims_no_partnership(world):
    """Finding 15: "WORKOUT BUDDY × CLAUDE CODE" read as a collaboration; it is "for Claude Code"."""
    sh(world, "cli", "done", "squats", "20")
    card = sh(world, "cli", "share").stdout
    assert card.split("\n")[1].startswith("║ WORKOUT BUDDY · for Claude Code ")
    for text in (card, sh(world, "cli", "share", "--text").stdout, (REPO / "README.md").read_text()):
        assert not re.search(r"(?i)×\s*claude|claude\s*code\s*×", text)
