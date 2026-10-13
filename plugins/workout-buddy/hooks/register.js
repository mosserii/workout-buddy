// Workout Buddy, the band: the mascot does the set above the prompt, with [1] done and [2] skip, and the
// /workout command.
//
// How it talks to workout.py (run by this same plugin's UserPromptSubmit and Stop hooks):
//   - works out the data dir the hooks use (lib/band.js resolveDataDir) and reads its ui.json every
//     POLL_MS (workout.py rewrites it on every state change); only this session's entry is shown, picked
//     by $.session.id(), the same id the hooks get (no id, no band: another session's ask is never shown);
//   - touches <data>/mod-seen/<session id> every 30 s, so workout.py asks only in sessions that show a band;
//   - [1] done runs `sh <plugin>/scripts/wb --home <data> cli done --ask <ask_id>`, [2] skip runs
//     `... cli decline --ask <ask_id>` ($.process.run: an argv, no shell string). One run at a time.
//   - /workout <words> runs `... cli <words>` and shows its output.
//
// Rules this file keeps: no fs / process call inside ui.render (it draws from cached state only);
// every handler catches and falls back to next(e); timers run only while something animates.
// The host reads on(...) and $.ns.method(...) from source, so each $ call is spelled in full, and
// helpers that take $ are top-level functions of this file.

import { animationFor, frameAt, rasterCells, COLS, ROWS } from '../lib/mascot.js'
import {
  parseFeed, pickEntry, entryKey, questionText, badgeText, countdown, layout, fit, argvFor, commandArgv,
  uiPath, createGate, firstLine, hotkeysFor, beatPath, beatDue, resolveDataDir, installedLayout, pluginsDirFor,
} from '../lib/band.js'

const POLL_MS = 500
const FRAME_MS = 140 // ~7 fps
const VICTORY_MS = 2000
const SKIPPED_MS = 1500
const RESOLVE_MS = 10000 // while only the fallback data dir answered, look again this often

let root = null
let dataDir = null
let dataFinal = false
let resolvedAt = -Infinity
let sid = null
let beatSid = null
let beatAt = 0
let lastText = null
let feed = null
let entry = null // this session's entry while it shows
let lastKey = ''
let shownAt = 0 // when the current ask first showed (the countdown of a timed set starts here)
let flash = null // { kind: 'victory' | 'skipped', until, text, cheer }
let frame = 0
let nowMs = 0
let poller = null
let animator = null
let lastCount = null
const gate = createGate()

export function register(on) {
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    try {
      await start($)
    } catch {
      // the band stays empty; /workout may still answer
    }
    return result
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    try {
      const tree = drawBand($, e)
      return tree || next(e)
    } catch {
      return next(e)
    }
  })

  on('command.run', { command: 'workout' }, async ($, e) => {
    try {
      return await runCommand($, e.args)
    } catch {
      return { text: 'Workout Buddy: could not run that command.' }
    }
  })
}

// ---------------------------------------------------------------- lifecycle

async function start($) {
  try {
    root = $.plugin.root
  } catch {
    root = null
  }
  try {
    await $.command.register({
      name: 'workout',
      description: 'Workout Buddy: stats, pause, resume, skip, undo, setup, config, share',
      argumentHint: '[stats|pause|resume|skip|undo|setup|config|share|help]',
      immediate: true,
    })
  } catch {
    // a taken name: the band still works
  }
  await resolveHome($, 0)
  await poll($)
  if (poller) poller.cancel()
  poller = $.clock.every(POLL_MS, () => poll($))
}

// The data dir the hooks use (see resolveDataDir in lib/band.js). Final answers are kept; the fallback is
// looked at again every RESOLVE_MS until something better shows up (a --plugin-dir session before its
// first prompt).
async function resolveHome($, now) {
  if (dataFinal || now - resolvedAt < RESOLVE_MS) return
  resolvedAt = now
  const env = {}
  try {
    env.workoutHome = await $.env.get('WORKOUT_HOME')
  } catch {
    env.workoutHome = undefined
  }
  try {
    env.pluginData = await $.env.get('CLAUDE_PLUGIN_DATA')
  } catch {
    env.pluginData = undefined
  }
  try {
    env.configDir = await $.env.get('CLAUDE_CONFIG_DIR')
  } catch {
    env.configDir = undefined
  }
  try {
    env.home = await $.env.get('HOME')
  } catch {
    env.home = undefined
  }
  try {
    env.pluginCacheDir = await $.env.get('CLAUDE_CODE_PLUGIN_CACHE_DIR')
  } catch {
    env.pluginCacheDir = undefined
  }
  const found = []
  if (!env.workoutHome && !env.pluginData && !installedLayout(root)) {
    const plugins = pluginsDirFor(env)
    if (plugins) {
      let entries = []
      try {
        entries = await $.fs.list(plugins + '/data')
      } catch {
        entries = []
      }
      let name = 'workout-buddy'
      try {
        name = $.plugin.name || name
      } catch {
        name = 'workout-buddy'
      }
      for (const ent of entries) {
        if (!ent || ent.kind !== 'dir' || !String(ent.name).startsWith(name + '-')) continue
        const dir = plugins + '/data/' + ent.name
        try {
          const st = await $.fs.stat(dir + '/ui.json')
          if (st && st.kind === 'file') found.push({ dir, mtimeMs: st.mtimeMs })
        } catch {
          // no ui.json there yet
        }
      }
    }
  }
  const r = resolveDataDir(env, root, found)
  if (r.dir !== dataDir) {
    dataDir = r.dir
    lastText = null
    beatSid = null // beat the new place at once
  }
  dataFinal = r.final
}

async function poll($) {
  try {
    nowMs = await $.clock.now()
    await resolveHome($, nowMs)
    try {
      sid = await $.session.id()
    } catch {
      sid = null // no id, no band (see pickEntry) and no beat, so workout.py asks nothing in this session
    }
    if (beatDue(beatSid, beatAt, sid, nowMs)) {
      beatSid = sid
      beatAt = nowMs
      const bp = beatPath(dataDir, sid)
      let ok = !!bp
      if (ok && !dataFinal) {
        // only the fallback answered: beat there only if it is already in use, never create it
        try {
          ok = await $.fs.exists(dataDir)
        } catch {
          ok = false
        }
      }
      if (ok) {
        try {
          await $.fs.write(bp, String(Math.floor(nowMs / 1000)))
        } catch {
          // no beat: workout.py asks nothing in this session, the band stays empty
        }
      }
    }
    let text = null
    const feedPath = uiPath(dataDir)
    try {
      if (feedPath && (await $.fs.exists(feedPath))) {
        const r = await $.fs.read(feedPath)
        text = typeof r === 'string' ? r : null
      }
    } catch {
      text = null
    }
    if (text !== lastText) {
      lastText = text
      feed = parseFeed(text)
    }
    let changed = false
    const picked = pickEntry(feed, sid, nowMs)
    const key = entryKey(picked)
    if (key !== lastKey) {
      if (picked && picked.mode === 'ask' && (!entry || entry.askId !== picked.askId)) shownAt = nowMs
      lastKey = key
      changed = true
    }
    entry = picked
    if (flash && nowMs >= flash.until) {
      flash = null
      changed = true
    }
    const count = entry && entry.mode === 'ask' ? countdown(entry, shownAt, nowMs) : null
    if (count !== lastCount) {
      lastCount = count
      changed = true
    }
    syncAnimation($)
    if (changed) $.ui.invalidate('ui.render')
  } catch {
    // next poll
  }
}

function animating() {
  return (flash && flash.kind === 'victory') || (!flash && entry && entry.mode === 'ask')
}

function syncAnimation($) {
  if (animating()) {
    if (!animator) animator = $.clock.every(FRAME_MS, () => tick($))
  } else if (animator) {
    animator.cancel()
    animator = null
  }
}

async function tick($) {
  try {
    nowMs = await $.clock.now()
    frame += 1
    if (flash && nowMs >= flash.until) {
      flash = null
      syncAnimation($)
    }
    $.ui.invalidate('ui.render')
  } catch {
    // next tick
  }
}

// ---------------------------------------------------------------- the buttons

async function press($, action, askId, text) {
  if (!gate.enter()) return // a run is in flight: ignore repeat presses
  $.ui.invalidate('ui.render')
  try {
    let r = null
    try {
      r = await $.process.run(argvFor(action, askId, root, dataDir), { timeoutMs: 20000 })
    } catch (err) {
      $.ui.toast('Workout Buddy: ' + firstLine(err && err.message ? err.message : 'could not run workout.py'))
      return
    }
    nowMs = await $.clock.now()
    if (action === 'done') {
      if (r.exitCode === 0) {
        lastText = null // read the badge (and its cheer) workout.py just wrote
        await poll($)
        const b = entry && entry.mode === 'badge' && entry.askId === askId ? entry : null
        flash = { kind: 'victory', until: nowMs + VICTORY_MS, text: b ? b.text : text, cheer: b ? b.cheer : '' }
        frame = 0
        const trophies = String(r.stdout || '').split(' · ').filter((p) => p.startsWith('🏆'))
        if (trophies.length) $.ui.toast(trophies.join(' · '))
      } else {
        $.ui.toast(firstLine(r.stdout || r.stderr) || 'Workout Buddy: nothing logged')
      }
    } else if (r.exitCode === 0) {
      flash = { kind: 'skipped', until: nowMs + SKIPPED_MS, text, cheer: '' }
      lastText = null
      await poll($)
    } else {
      lastText = null
      await poll($)
      if (entry && entry.mode === 'ask') $.ui.toast(firstLine(r.stdout || r.stderr) || 'Workout Buddy: not skipped')
    }
  } finally {
    gate.leave()
    syncAnimation($)
    $.ui.invalidate('ui.render')
  }
}

// ---------------------------------------------------------------- /workout

async function runCommand($, args) {
  try {
    await resolveHome($, await $.clock.now())
  } catch {
    // keep what is known
  }
  const argv = commandArgv(args, root, dataFinal ? dataDir : null)
  if (!argv) return { text: 'Workout Buddy: use plain words, for example /workout pause 30 or /workout setup.' }
  let r
  try {
    r = await $.process.run(argv, { timeoutMs: 20000 })
  } catch (err) {
    return { text: 'Workout Buddy: could not run workout.py (' + firstLine(err && err.message) + ').' }
  }
  lastText = null // the command may have changed what the band shows
  const out = String(r.stdout || '').trimEnd() || String(r.stderr || '').trim() || 'Workout Buddy: no output.'
  return { text: out }
}

// ---------------------------------------------------------------- drawing (cached state only)

// The mascot is drawn only where it is known to render: the terminal's Raster (24-bit cells). Elsewhere
// (the Desktop app, whose Text is only promised theme keys and named colors, and where an invalid prop
// makes Claude Code drop the whole band) the band shows the question and buttons without the art.
function canDrawMascot($, e) {
  return e.surface === 'terminal' && !!$.ui.resolve(e).Raster
}

function mascotNode($, e, name, i) {
  const { Raster } = $.ui.resolve(e)
  return Raster({ key: 'mascot', columns: COLS, rows: ROWS, cells: rasterCells(frameAt(name, i)) })
}

function drawBand($, e) {
  const props = e.props || {}
  if (props.hasSurvey) return null
  const { Box, Text, Button } = $.ui.resolve(e)
  const cols = props.bodyColumns || 80
  const maxRows = props.maxRows || 4

  if (flash && flash.kind === 'skipped') {
    return Text({ dimColor: true, wrap: 'truncate-end', children: fit('skipped, the next one will be different', cols) })
  }

  if (flash && flash.kind === 'victory') {
    const line = badgeText(flash.text, flash.cheer)
    return celebrate($, e, Box, Text, cols, maxRows, line, 'victory', frame)
  }

  if (!entry) return null

  if (entry.mode === 'badge') {
    return celebrate($, e, Box, Text, cols, maxRows, badgeText(entry.text, entry.cheer), 'badge', 0)
  }

  // the question
  const q = questionText(entry)
  let lay = layout(cols, maxRows, q)
  if (lay.kind === 'full' && !canDrawMascot($, e)) {
    // no art on this surface: the compact row (full already implies it fits)
    lay = { kind: 'compact', mascot: false, hint: maxRows >= 2, rows: maxRows >= 2 ? 2 : 1, textCols: cols }
  }
  const busy = gate.busy
  const askId = entry.askId
  const what = entry.text
  const keys = hotkeysFor(!!props.isWorking)
  const done = Button({
    key: 'wb-done', label: busy ? 'done…' : 'done', hotkey: keys.done, plain: true,
    onPress: () => press($, 'done', askId, what),
  })
  const skip = Button({
    key: 'wb-skip', label: 'skip', hotkey: keys.skip, plain: true, dimColor: true,
    onPress: () => press($, 'decline', askId, what),
  })
  const left = lastCount
  const timer = left == null ? null : Text({ color: left > 0 ? 'cyan' : 'green', children: left > 0 ? '  ⏱ ' + left + 's' : '  ⏱ time!' })
  const hintText = left != null && left > 0 ? 'hold it… ' + keys.hint : keys.hint

  if (lay.kind === 'full') {
    return Box({
      flexDirection: 'row',
      columnGap: 2,
      children: [
        mascotNode($, e, animationFor(entry.exercise), frame),
        Box({
          flexDirection: 'column',
          width: lay.textCols,
          children: [
            Box({ flexDirection: 'row', children: [Text({ bold: true, wrap: 'truncate-end', children: fit(q, lay.textCols - (timer ? 9 : 0)) }), timer] }),
            Box({ flexDirection: 'row', columnGap: 3, children: [done, skip] }),
            Text({ dimColor: true, wrap: 'truncate-end', children: fit(hintText, lay.textCols) }),
            Text({ children: ' ' }),
          ],
        }),
      ],
    })
  }
  if (lay.kind === 'compact') {
    const row = Box({
      flexDirection: 'row',
      columnGap: 2,
      children: [Text({ bold: true, children: q }), timer, done, skip],
    })
    if (!lay.hint) return row
    return Box({ flexDirection: 'column', children: [row, Text({ dimColor: true, wrap: 'truncate-end', children: fit(hintText, cols) })] })
  }
  const room = Math.max(0, cols - 18)
  return Box({
    flexDirection: 'row',
    columnGap: 1,
    children: [room >= 4 ? Text({ bold: true, children: fit(q, room) }) : null, done, skip],
  })
}

function celebrate($, e, Box, Text, cols, maxRows, line, anim, i) {
  const lay = layout(cols, maxRows, line)
  const art = lay.kind === 'full' && canDrawMascot($, e)
  const label = Text({ bold: true, color: 'green', wrap: 'truncate-end', children: fit(line, art ? lay.textCols : cols) })
  if (!art) return label
  return Box({
    flexDirection: 'row',
    columnGap: 2,
    children: [
      mascotNode($, e, anim, i),
      Box({ flexDirection: 'column', justifyContent: 'center', width: lay.textCols, children: [label] }),
    ],
  })
}
