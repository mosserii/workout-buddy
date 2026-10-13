"""Every test runs in a throwaway world: HOME, CLAUDE_CONFIG_DIR and the data dir are temp dirs, and
CLAUDE_PLUGIN_DATA is unset, so nothing here can read or write a real ~/.claude or a real data dir.
The `env` fixture in test_workout.py then points WORKOUT_HOME at its own temp dir.

The default is the public build: WORKOUT_TEST=1 turns on the test-only knobs (fast polls, a forced
platform, the banner log) and nothing else, so config.json is read exactly as an install reads it (the
button only, the band's "mod" button, the waiting meter). A module written against the older modes
("ask_mode": "auto", "button": "link", "timing": "classic") says so with
`pytestmark = pytest.mark.legacy_modes`, and only there is WORKOUT_TEST_LEGACY_MODES=1 set. test_public.py
also runs the installed code path with no WORKOUT_* variable at all.

WORKOUT_PLATFORM: the suite takes the macOS path by default; WB_TEST_PLATFORM=linux runs it all on the
Linux path (no banner, no speech: notify() still records each banner in WORKOUT_NOTIFY_LOG, marked
"suppressed")."""
import os

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "legacy_modes: the module runs on the older modes (test suite only)")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch, request):
    home = tmp_path / "_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("WORKOUT_HOME", str(tmp_path / "_wh"))
    monkeypatch.setenv("WORKOUT_TEST", "1")
    if request.node.get_closest_marker("legacy_modes"):
        monkeypatch.setenv("WORKOUT_TEST_LEGACY_MODES", "1")
    else:
        monkeypatch.delenv("WORKOUT_TEST_LEGACY_MODES", raising=False)
    monkeypatch.setenv("WORKOUT_PLATFORM", os.environ.get("WB_TEST_PLATFORM") or "darwin")
    for k in ("CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "WORKOUT_SETTINGS_PATH", "WORKOUT_NOTIFY_LOG"):
        monkeypatch.delenv(k, raising=False)
    yield
