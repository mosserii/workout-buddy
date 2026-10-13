# Workout Buddy

**Claude thinks. You rep.**

A Claude Code plugin that turns the wait for a long turn into a short set. While Claude works, a band
above the prompt asks for something small, like `15 push-ups?`, with Clawd doing the exercise next to
it. Press `1` when you are done or `2` to skip.

```
      ▄▄▄▄▄▄▄▄▄▄      15 push-ups?
    ▄▄██ ████ ██▄▄    1: done   2: skip
      ██████████      press 1 when done
       █ █  █ █
```

Everything runs on your machine. Nothing is sent anywhere.

## Install

You need:

- Claude Code 2.1.287 or later: the band is a plugin mod, and the mods API needs 2.1.287 (tested on
  2.1.296). `claude --version` shows yours.
- `python3` 3.9 or later on your `PATH`.
- macOS or Linux.

At the prompt of a terminal session:

```
/plugin marketplace add mosserii/workout-buddy
/plugin install workout-buddy@workout-buddy
```

Pick the user scope when asked. That is all the setup there is. Installing changes nothing in your
Claude Code settings; see [Optional extras](#optional-extras) for the two things you can turn on.

## Update

```
/plugin marketplace update workout-buddy
```

That refreshes the catalog. To move the installed plugin to the latest version, run
`claude plugin update workout-buddy@workout-buddy` in a shell, then restart Claude Code.

## How it works

- **The band.** When a set is due, the band above the prompt shows it: the exercise, Clawd doing it, and
  two buttons. Type `1` alone into the empty prompt (or click `done`) when you have done it, or `2` to skip.
  The digits work while Claude is still working and after it stops. Timed sets (plank, wall sit) show a
  countdown.
- **About every 5 minutes of waiting.** A waiting meter fills while Claude is busy on a turn, in any of your
  sessions (two sessions busy at the same time count once). Once it holds about 5 minutes (300 s, give or
  take a minute), the next turn that has been running for at least 20 seconds asks. Short turns never ask.
- **One at a time.** One ask per turn, one open question across all your sessions, and the meter pauses
  while a question is up. A question you leave alone goes away after 3 minutes and is noted as skipped
  (a skip counts toward nothing; it only means the next set will be a different exercise).
- **Done.** Clawd cheers, the band shows something like `✅ 15 push-ups · nailed it ✨`, and the set is
  logged. A skip picks a different exercise next time.
- **Streaks and levels.** Sets add up to a daily streak; every 3 streak days the reps grow by 10% (up to
  1.5×). Achievements unlock along the way.

While a question is showing, a `1` or `2` typed on its own goes to the band. To answer Claude with a bare
digit, type a little more (for example `1.`) or wait for the band to go.

## /workout

| Command | What it does |
| :- | :- |
| `/workout` or `/workout stats` | Today, the last 7 days, your streak, level, totals and the waiting meter |
| `/workout pause [minutes\|day]` | No asks for a while (60 minutes by default; `day` means until midnight) |
| `/workout resume` | Asks are back on |
| `/workout skip` | Skip the set being asked, the same as `2` in the band |
| `/workout done [exercise] [n]` | Log a set by hand, for example `/workout done push-ups 20` |
| `/workout undo` | Take back the most recently logged set |
| `/workout setup` | Explains the optional extras; changes nothing without `--yes` |
| `/workout config` | Where your data lives and the settings in effect |
| `/workout share` | A weekly card to screenshot (`/workout share --text` for a short post) |
| `/workout achievements` | Unlocked and locked achievements, with progress |
| `/workout help` | The command list |

## Optional extras

`/workout setup` explains each one in plain words and changes nothing. To turn one on, run the line it
shows, which ends in `--yes`.

- **Spinner words.** While a set is due, Claude's spinner says `NOW: 15 push-ups`.
  `/workout setup spinner --yes` changes one key, `spinnerVerbs`, in your Claude Code `settings.json`
  (`$CLAUDE_CONFIG_DIR/settings.json`, normally `~/.claude/settings.json`) and nothing else in that file.
  A backup copy of the file is saved first, in the data folder's `backups/`. The file is written safely:
  under a lock, to a temporary file that replaces the original in one step, and only if nothing else
  changed it in the meantime. While no set is due, your own spinner words (if you had any) stay.
  Only the `settings.json` you turned it on for is ever written, even if you use several Claude Code
  config folders. `/workout setup spinner off --yes` puts back what was there before; run it before you
  uninstall, since uninstalling alone leaves the key in place.
- **Desktop banners (macOS).** A notification when a set is asked. `/workout setup banners --yes` changes
  `notification` in Workout Buddy's own `config.json`; your Claude Code settings are not touched.

## Settings

Your settings live in `config.json` in the data folder (`/workout config` prints its path). Edit it by
hand; changes apply on the next prompt. A fresh `config.json` holds exactly these keys:

| Key | Default | Meaning |
| :- | :- | :- |
| `exercises` | 8 exercises | A list of `{"name", "min", "max", "unit"}`; `unit` is `"reps"` or `"seconds"` |
| `meter_base_s` | `300` | Seconds of waiting between asks |
| `meter_jitter_s` | `60` | Random spread around that, so asks do not feel like a clock |
| `meter_min_turn_s` | `20` | A turn must have run this long before it may ask |
| `meter_ask_ttl_s` | `180` | An unanswered question goes away after this long |
| `quiet_hours` | `null` | For example `["22:00", "07:00"]`: no asks in that window |
| `progression` | `true` | Reps grow with your streak |
| `coach` | `"hype"` | The tone of the banner lines: `"hype"`, `"chill"` or `"drill-sergeant"` |
| `badge_s` | `120` | How long the `✅` badge stays after a set |
| `notification` | `false` | Desktop banners (macOS); `/workout setup banners --yes` sets it |
| `sound` | `false` | A sound with each banner (macOS) |
| `sound_name` | `"Glass"` | Which macOS sound |
| `spinner` | `"off"` | `"task"` or `"progress"` once spinner words are set up |

`WORKOUT_HOME` in your environment moves the data folder somewhere else.

## Privacy

Everything stays on your machine, in one data folder. Workout Buddy makes no network connections, opens
no ports, and has no accounts, telemetry or analytics.

- **Where.** The plugin's own data folder, which Claude Code provides:
  `~/.claude/plugins/data/workout-buddy-workout-buddy/` (under `$CLAUDE_CONFIG_DIR` if you set it).
  If that is not available it uses `~/.workout-buddy/`. `WORKOUT_HOME` overrides both.
  `/workout config` prints the folder in use.
- **What is kept.**
  - `log.jsonl`: one line per set: the time, the exercise, the reps or seconds, whether it was done,
    skipped (by you, or because the question went unanswered) or taken back with `/workout undo`, how it
    was answered (the band or `/workout done`), and the Claude Code session id and turn start time it was
    asked in.
  - `state.json`: working state: the current sessions (id, turn start time, the path of the session's
    transcript file), the waiting meter, the next set, the outcome of the last 20 questions,
    achievements and your level. For the turn in progress it also keeps a few facts about your prompt:
    its length in characters, words and lines; whether it had a code block or a question mark, was a
    slash command, or used a "build" verb such as fix, add or refactor (and whether it started with one);
    a one-word topic such as `tests` or `bug` that picks the coach's line; the hour of the day; and, from
    the same session, how long its previous turn took, its average turn length and the time since its
    last turn ended. For each session it keeps that turn history (the last turn's length, the average,
    when it ended) for 30 days, for at most 200 sessions. After `/workout setup spinner --yes` it also
    keeps that `settings.json`'s path and a copy of the `spinnerVerbs` value you had before, to put back.
    The prompt text itself is never stored.
  - `config.json` (your settings), `ui.json` (what the band shows), `tips.json` (spinner tips),
    `mod-seen/` (one tiny file per session that shows the band), `errors.log` (only if something fails),
    `backups/` (only after `/workout setup ... --yes`).
- **What is read.** Each prompt, once, to work out the numbers and topic above. While a turn runs, the end
  of the current session's transcript file, to notice when the turn finished or was interrupted; nothing
  from it is stored. Your Claude Code `settings.json` only when you run `/workout setup ... --yes`.
- **Delete it.** Remove the data folder (`/workout config` shows where). Claude Code may also remove the
  plugin's data folder when you uninstall the plugin.

## Uninstall

If you turned on spinner words, first run `/workout setup spinner off --yes` so your earlier spinner words
come back. Then:

```
/plugin uninstall workout-buddy@workout-buddy
/plugin marketplace remove workout-buddy
```

Delete the data folder too if you no longer want your history.

## Development

```bash
# the Python suite (stdlib-only at runtime; pytest only for the tests)
uv run --with pytest --with pytest-xdist python -m pytest tests -n 4 -q -rf

# the band's pure logic and the mascot frames
node --test plugins/workout-buddy/test/*.test.js

# the plugin and the hooks module, the way Claude Code reads them
claude plugin validate --strict .
claude plugin validate --strict plugins/workout-buddy
(cd plugins/workout-buddy && claude plugin test .)

# try it in one session without installing
claude --plugin-dir plugins/workout-buddy
```

Layout: `plugins/workout-buddy/` is the plugin. Its `hooks/hooks.json` holds both the two turn hooks
(`UserPromptSubmit` and `Stop`, which run `scripts/wb`, a small `sh` launcher that finds `python3`) and the
band's hooks module (`hooks/register.js`). `lib/band.js` is the band's pure logic and `lib/mascot.js` is
Clawd; another mascot is a drop-in file with the same exports.

## License

MIT. See [LICENSE](LICENSE).

Clawd is Claude Code's mascot, drawn here as fan art.

Not affiliated with or endorsed by Anthropic.
