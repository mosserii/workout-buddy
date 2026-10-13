// Pure logic for the band: where the data dir is, reading ui.json, picking this session's entry, the layout
// for a width, the debounce gate, and the argv for the buttons and /workout. No mods API in this file.

const ASK_ID = /^[0-9a-f]{8}$/

// ---------------------------------------------------------------- the data dir
//
// The Python hooks get CLAUDE_PLUGIN_DATA from Claude Code; a mod's own environment does not. So the band
// works out the same folder, first match wins:
//   1. WORKOUT_HOME (an explicit override; the hooks honour it first too)
//   2. CLAUDE_PLUGIN_DATA, should the mod's environment ever carry it
//   3. an installed plugin (its root is <plugins>/cache/<marketplace>/<plugin>/<version>): the plugin data
//      dir Claude Code documents, <plugins>/data/<id>, <id> being "<plugin>@<marketplace>" with every
//      character outside a-z A-Z 0-9 _ - replaced by "-"
//   4. a plugin loaded from a folder (--plugin-dir): the newest <plugins>/data/<plugin>-* holding a ui.json
//   5. ~/.workout-buddy, the hooks' own fallback
// Step 4 may find nothing before the first prompt; the band then looks again a little later.

export const DEFAULT_DIR_NAME = '.workout-buddy'

export function trimSlash(p) {
  return String(p).replace(/\/+$/, '')
}

export function pluginDataId(name, marketplace) {
  return (String(name) + '@' + String(marketplace)).replace(/[^a-zA-Z0-9_-]/g, '-')
}

// { pluginsDir, marketplace, name } when root is an installed copy under <plugins>/cache/, else null.
export function installedLayout(root) {
  const m = /^(.*)\/cache\/([^/]+)\/([^/]+)\/[^/]+$/.exec(trimSlash(root || ''))
  return m ? { pluginsDir: m[1], marketplace: m[2], name: m[3] } : null
}

// The plugins dir Claude Code uses: CLAUDE_CODE_PLUGIN_CACHE_DIR, else <config dir>/plugins.
export function pluginsDirFor(env) {
  if (env.pluginCacheDir) return trimSlash(env.pluginCacheDir)
  const config = env.configDir ? trimSlash(env.configDir) : (env.home ? trimSlash(env.home) + '/.claude' : null)
  return config ? config + '/plugins' : null
}

// env: { workoutHome, pluginData, configDir, home, pluginCacheDir }; root: $.plugin.root; found:
// [{ dir, mtimeMs }] for step 4 (the caller lists them). Returns { dir, final, why }: final is false while
// only the fallback answered, so the caller looks again later.
export function resolveDataDir(env, root, found = []) {
  if (env.workoutHome && String(env.workoutHome).trim()) return { dir: trimSlash(env.workoutHome), final: true, why: 'WORKOUT_HOME' }
  if (env.pluginData && String(env.pluginData).trim()) return { dir: trimSlash(env.pluginData), final: true, why: 'CLAUDE_PLUGIN_DATA' }
  const inst = installedLayout(root)
  if (inst) {
    return { dir: inst.pluginsDir + '/data/' + pluginDataId(inst.name, inst.marketplace), final: true, why: 'installed' }
  }
  const best = [...found].filter((f) => f && f.dir).sort((a, b) => (b.mtimeMs || 0) - (a.mtimeMs || 0))[0]
  if (best) return { dir: trimSlash(best.dir), final: true, why: 'plugin-dir' }
  const fallback = env.home ? trimSlash(env.home) + '/' + DEFAULT_DIR_NAME : null
  return { dir: fallback, final: false, why: 'fallback' }
}

export function uiPath(home) {
  return home ? trimSlash(home) + '/ui.json' : null
}

// ---------------------------------------------------------------- ui.json

// ui.json text -> { version, updatedAt, sessions } or null when it is missing or not ours.
export function parseFeed(text) {
  if (typeof text !== 'string' || !text.trim()) return null
  let raw
  try {
    raw = JSON.parse(text)
  } catch {
    return null
  }
  if (!raw || typeof raw !== 'object' || raw.version !== 1 || !raw.sessions || typeof raw.sessions !== 'object') {
    return null
  }
  const sessions = {}
  for (const [sid, e] of Object.entries(raw.sessions)) {
    const v = cleanEntry(e)
    if (v) sessions[sid] = v
  }
  return { version: 1, updatedAt: Number(raw.updated_at) || 0, sessions }
}

function cleanEntry(e) {
  if (!e || typeof e !== 'object') return null
  const mode = e.mode === 'ask' || e.mode === 'badge' ? e.mode : 'none'
  const until = typeof e.until_ts === 'number' && Number.isFinite(e.until_ts) ? e.until_ts : null
  const askId = typeof e.ask_id === 'string' && ASK_ID.test(e.ask_id) ? e.ask_id : null
  const out = {
    mode,
    askId,
    exercise: typeof e.exercise === 'string' ? e.exercise : '',
    reps: typeof e.reps === 'number' && Number.isFinite(e.reps) ? e.reps : 0,
    unit: e.unit === 'seconds' ? 'seconds' : 'reps',
    text: typeof e.text === 'string' ? e.text : '',
    cheer: typeof e.cheer === 'string' ? e.cheer : '',
    until,
  }
  if (out.mode === 'ask' && (!out.askId || !out.text)) out.mode = 'none' // nothing a button could answer
  if (out.mode === 'badge' && !out.text) out.mode = 'none'
  return out
}

// What this session shows now: its entry while mode is ask or badge and until_ts has not passed.
// sid null (the session id could not be read): nothing. Another session's ask is never shown here (its
// [1] would log that session's set from this one).
export function pickEntry(feed, sid, nowMs) {
  if (!feed || !sid) return null
  const now = nowMs / 1000
  const live = (e) => e && (e.mode === 'ask' || e.mode === 'badge') && e.until != null && now <= e.until
  const e = Object.prototype.hasOwnProperty.call(feed.sessions, sid) ? feed.sessions[sid] : null
  return live(e) ? e : null
}

// The key that decides a redraw: what the band would show (not until_ts, which the heartbeat moves).
export function entryKey(e) {
  return e ? [e.mode, e.askId, e.text, e.cheer].join('|') : ''
}

export function questionText(e) {
  return e.text + '?'
}

export function badgeText(text, cheer) {
  return '✅ ' + text + (cheer ? ' · ' + cheer : '')
}

// Seconds left of a timed set ("45s plank"), counted from when the band first showed it.
export function countdown(e, shownAtMs, nowMs) {
  if (!e || e.unit !== 'seconds' || !(e.reps > 0)) return null
  const left = Math.max(0, Math.ceil(e.reps - (nowMs - shownAtMs) / 1000))
  return left
}

// ---------------------------------------------------------------- layout

// The layout for the band's width and height:
//   full     the mascot (MASCOT_COLS) + gap + the question, buttons and hint in a column (4 rows)
//   compact  no mascot: the question and buttons on one row, the hint under it when there is room
//   tiny     the buttons only, with the question shortened to fit
export const MASCOT_COLS = 14
export const GAP = 2
export const BUTTONS_COLS = 16 // "1: done  2: skip"

export function layout(columns, maxRows, text) {
  const cols = Number.isFinite(columns) && columns > 0 ? columns : 80
  const rows = Number.isFinite(maxRows) && maxRows > 0 ? maxRows : 4
  const q = String(text || '')
  const right = Math.max(q.length + 1, BUTTONS_COLS, 22) // the hint "press 1 when done" is 17
  if (rows >= 4 && cols >= MASCOT_COLS + GAP + right) {
    return { kind: 'full', mascot: true, hint: true, rows: 4, textCols: cols - MASCOT_COLS - GAP }
  }
  if (cols >= q.length + 1 + 2 + BUTTONS_COLS) {
    return { kind: 'compact', mascot: false, hint: rows >= 2 && cols >= 20, rows: rows >= 2 ? 2 : 1, textCols: cols }
  }
  return { kind: 'tiny', mascot: false, hint: false, rows: 1, textCols: cols }
}

// Shorten to n columns with an ellipsis (the band's texts are single-width but the emoji in a cheer).
export function fit(s, n) {
  const chars = [...String(s)]
  if (n <= 0) return ''
  if (chars.length <= n) return chars.join('')
  return chars.slice(0, Math.max(0, n - 1)).join('') + '…'
}

// ---------------------------------------------------------------- running workout.py

// The launcher, relative to the plugin's own root: `sh <root>/scripts/wb [--home <dir>] ...`. sh is looked
// up on PATH and wb finds python3 on PATH. Always an argv, never a shell string.
export function launcher(root, home) {
  if (!root) throw new Error('no plugin root')
  const argv = ['sh', trimSlash(root) + '/scripts/wb']
  if (home) argv.push('--home', trimSlash(home))
  return argv
}

// The argv for a button: the ask answered by its id only, always behind `--ask`. A bare all-digit word
// that names no ask on record is read by workout.py as a rep count (a human's `/workout done 20`), so an
// all-digit id of a closed ask would log a different set; `--ask` never is.
export function argvFor(action, askId, root, home) {
  if (action !== 'done' && action !== 'decline') throw new Error('unknown action ' + action)
  if (!ASK_ID.test(String(askId || ''))) throw new Error('bad ask id')
  return [...launcher(root, home), 'cli', action, '--ask', askId]
}

// `/workout <args>`: the words after the command, each its own argv entry. Only plain words pass:
// letters, digits and - _ . : (a subcommand, an exercise name, a number, a flag such as --yes).
export const WORKOUT_WORD = /^[A-Za-z0-9_.:-]{1,40}$/

export function commandArgv(args, root, home) {
  const words = String(args || '').trim().split(/\s+/).filter(Boolean)
  if (words.length > 8 || words.some((w) => !WORKOUT_WORD.test(w))) return null
  return [...launcher(root, home), 'cli', ...(words.length ? words : ['stats'])]
}

// The band's hotkeys: always the digits. A bare digit typed alone into an empty prompt (and a pause)
// presses a band button with no focus needed, while Claude works and after it stops. The cost: a lone
// "1" or "2" typed as a reply to Claude while the band shows presses the button instead.
export function hotkeysFor(isWorking) {
  return { done: '1', skip: '2', hint: 'press 1 when done' }
}

// The band's heartbeat for workout.py: while the band runs for a session it touches
// <home>/mod-seen/<sid>, and only a session with a fresh beat is asked (a session without the band has
// nowhere to show the question). null for an id that is not a plain file name.
export const BEAT_MS = 30000
const SID = /^[A-Za-z0-9_-]{1,128}$/

export function beatPath(home, sid) {
  if (!home) return null
  return typeof sid === 'string' && SID.test(sid) ? trimSlash(home) + '/mod-seen/' + sid : null
}

// Beat now? On a new session id (start, /clear, /resume) at once, then every BEAT_MS.
export function beatDue(lastSid, lastBeatMs, sid, nowMs) {
  if (!sid) return false
  return sid !== lastSid || !(nowMs - lastBeatMs < BEAT_MS)
}

// One press at a time: enter() is false while a run is in flight.
export function createGate() {
  let busy = false
  return {
    enter() {
      if (busy) return false
      busy = true
      return true
    },
    leave() {
      busy = false
    },
    get busy() {
      return busy
    },
  }
}

// The first line of a command's output, for a toast.
export function firstLine(s, max = 120) {
  const line = String(s || '').split('\n').map((l) => l.trim()).find(Boolean) || ''
  return fit(line, max)
}
