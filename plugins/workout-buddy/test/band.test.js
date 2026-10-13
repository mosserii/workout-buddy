import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  parseFeed, pickEntry, entryKey, layout, fit, argvFor, commandArgv, launcher, createGate, countdown, uiPath,
  firstLine, badgeText, MASCOT_COLS, hotkeysFor, beatPath, beatDue, BEAT_MS, resolveDataDir, installedLayout,
  pluginDataId, pluginsDirFor,
} from '../lib/band.js'

const ROOT = '/x/plugins/cache/workout-buddy/workout-buddy/1.0.0'
const HOME = '/x/data/wb'

const NOW = 1_800_000_000_000 // ms
const T = NOW / 1000

function feedText(sessions, extra = {}) {
  return JSON.stringify({ version: 1, updated_at: T, sessions, ...extra })
}

const ASK = { mode: 'ask', ask_id: 'ab12cd34', exercise: 'squats', reps: 20, unit: 'reps', text: '20 squats', cheer: '', until_ts: T + 30 }
const BADGE = { mode: 'badge', ask_id: 'ab12cd34', exercise: 'squats', reps: 20, unit: 'reps', text: '20 squats', cheer: 'kudos! 💪', until_ts: T + 100 }
const NONE = { mode: 'none', ask_id: null, exercise: null, reps: null, unit: null, text: '', cheer: '', until_ts: null }

test('parseFeed: missing, broken, foreign and valid files', () => {
  assert.equal(parseFeed(null), null)
  assert.equal(parseFeed(''), null)
  assert.equal(parseFeed('{not json'), null)
  assert.equal(parseFeed(JSON.stringify({ version: 2, sessions: {} })), null)
  assert.equal(parseFeed(JSON.stringify([1, 2])), null)
  const f = parseFeed(feedText({ s1: ASK, s2: NONE, s3: 'junk' }))
  assert.equal(f.version, 1)
  assert.equal(f.sessions.s1.mode, 'ask')
  assert.equal(f.sessions.s1.askId, 'ab12cd34')
  assert.equal(f.sessions.s2.mode, 'none')
  assert.equal(f.sessions.s3, undefined)
})

test('parseFeed: an ask without a well-formed id cannot be answered, so it is not shown', () => {
  const f = parseFeed(feedText({ a: { ...ASK, ask_id: 'x; rm -rf /' }, b: { ...ASK, ask_id: null } }))
  assert.equal(f.sessions.a.mode, 'none')
  assert.equal(f.sessions.b.mode, 'none')
})

test('pickEntry: only this session, only while fresh', () => {
  const f = parseFeed(feedText({ me: ASK, other: { ...ASK, ask_id: 'ffffffff' } }))
  assert.equal(pickEntry(f, 'me', NOW).askId, 'ab12cd34')
  assert.equal(pickEntry(f, 'nobody', NOW), null)
  assert.equal(pickEntry(f, 'me', NOW + 31_000), null) // until_ts passed
  const g = parseFeed(feedText({ me: BADGE }))
  assert.equal(pickEntry(g, 'me', NOW).mode, 'badge')
  assert.equal(pickEntry(g, 'me', NOW + 101_000), null)
  assert.equal(pickEntry(parseFeed(feedText({ me: NONE })), 'me', NOW), null)
  assert.equal(pickEntry(null, 'me', NOW), null)
})

test('pickEntry without a session id: nothing, never another session\'s ask', () => {
  const f = parseFeed(feedText({ a: { ...ASK, until_ts: T + 10 }, b: { ...ASK, ask_id: 'ffffffff', until_ts: T + 40 }, c: BADGE }))
  assert.equal(pickEntry(f, null, NOW), null)
  assert.equal(pickEntry(f, '', NOW), null)
  assert.equal(pickEntry(f, undefined, NOW), null)
  assert.equal(pickEntry(f, '__proto__', NOW), null)
  assert.equal(pickEntry(f, 'constructor', NOW), null)
})

test('entryKey ignores the heartbeat (until_ts) but not what is drawn', () => {
  const a = parseFeed(feedText({ me: ASK })).sessions.me
  const b = parseFeed(feedText({ me: { ...ASK, until_ts: T + 99 } })).sessions.me
  const c = parseFeed(feedText({ me: BADGE })).sessions.me
  assert.equal(entryKey(a), entryKey(b))
  assert.notEqual(entryKey(a), entryKey(c))
  assert.equal(entryKey(null), '')
})

test('layout: full with the mascot when wide, then compact, then tiny', () => {
  const q = '20 squats?'
  assert.equal(layout(120, 10, q).kind, 'full')
  assert.equal(layout(120, 10, q).mascot, true)
  assert.equal(layout(120, 10, q).textCols, 120 - MASCOT_COLS - 2)
  assert.equal(layout(38, 10, q).kind, 'full') // 14 + 2 + 22
  assert.equal(layout(37, 10, q).kind, 'compact')
  assert.equal(layout(37, 10, q).mascot, false)
  assert.equal(layout(29, 10, q).kind, 'compact') // q 10 + 1 + 2 + 16
  assert.equal(layout(28, 10, q).kind, 'tiny')
  assert.equal(layout(120, 3, q).kind, 'compact') // too short for the mascot's 4 rows
  assert.equal(layout(120, 1, q).rows, 1)
  assert.equal(layout(120, 1, q).hint, false)
  assert.equal(layout(undefined, undefined, q).kind, 'full') // missing props: assume 80 x 4
})

test('layout never asks for more rows than the band has', () => {
  for (const cols of [10, 20, 30, 40, 60, 200]) {
    for (const rows of [1, 2, 3, 4, 8]) {
      assert.ok(layout(cols, rows, '45s plank?').rows <= rows, `${cols}x${rows}`)
    }
  }
})

test('fit shortens with an ellipsis and counts code points', () => {
  assert.equal(fit('20 squats?', 20), '20 squats?')
  assert.equal(fit('20 squats?', 5), '20 s…')
  assert.equal(fit('✅ 20 squats · kudos! 💪', 100), '✅ 20 squats · kudos! 💪')
  assert.equal(fit('abc', 0), '')
})

test('argv: sh, the launcher under the plugin root, the data dir, cli, the action, the exact ask id', () => {
  const pre = ['sh', ROOT + '/scripts/wb', '--home', HOME]
  assert.deepEqual(argvFor('done', 'ab12cd34', ROOT, HOME), [...pre, 'cli', 'done', '--ask', 'ab12cd34'])
  assert.deepEqual(argvFor('decline', 'ab12cd34', ROOT + '/', HOME + '/'), [...pre, 'cli', 'decline', '--ask', 'ab12cd34'])
  // an all-digit id still goes behind --ask, never as a bare word workout.py could read as reps
  assert.deepEqual(argvFor('done', '12345670', ROOT, HOME), [...pre, 'cli', 'done', '--ask', '12345670'])
  assert.deepEqual(launcher(ROOT, null), ['sh', ROOT + '/scripts/wb']) // no data dir known: wb resolves its own
  assert.throws(() => launcher(null, HOME))
  assert.throws(() => argvFor('skip', 'ab12cd34', ROOT, HOME))
  assert.throws(() => argvFor('done', '', ROOT, HOME))
  assert.throws(() => argvFor('done', 'AB12CD34', ROOT, HOME))
  assert.throws(() => argvFor('done', 'ab12cd34; ls', ROOT, HOME))
  assert.throws(() => argvFor('done', undefined, ROOT, HOME))
})

test('/workout argv: plain words only, each its own argv entry; empty means stats', () => {
  const pre = ['sh', ROOT + '/scripts/wb', '--home', HOME, 'cli']
  assert.deepEqual(commandArgv('', ROOT, HOME), [...pre, 'stats'])
  assert.deepEqual(commandArgv('  pause   30 ', ROOT, HOME), [...pre, 'pause', '30'])
  assert.deepEqual(commandArgv('setup spinner --yes', ROOT, HOME), [...pre, 'setup', 'spinner', '--yes'])
  assert.deepEqual(commandArgv('done push-ups 20', ROOT, HOME), [...pre, 'done', 'push-ups', '20'])
  for (const bad of ['pause; rm -rf ~', 'share $(id)', 'a`b`', 'x > y', 'one two three four five six seven eight nine']) {
    assert.equal(commandArgv(bad, ROOT, HOME), null, bad)
  }
})

test('data dir: WORKOUT_HOME, then CLAUDE_PLUGIN_DATA, then the installed plugin data dir, then a found one, then ~/.workout-buddy', () => {
  const inst = '/u/.claude/plugins/cache/workout-buddy/workout-buddy/1.0.0'
  const dev = '/src/workout-buddy/plugins/workout-buddy'
  const base = { home: '/u' }
  assert.deepEqual(resolveDataDir({ ...base, workoutHome: '/w/', pluginData: '/p' }, inst), { dir: '/w', final: true, why: 'WORKOUT_HOME' })
  assert.deepEqual(resolveDataDir({ ...base, pluginData: '/p' }, inst), { dir: '/p', final: true, why: 'CLAUDE_PLUGIN_DATA' })
  assert.deepEqual(resolveDataDir(base, inst), { dir: '/u/.claude/plugins/data/workout-buddy-workout-buddy', final: true, why: 'installed' })
  assert.deepEqual(resolveDataDir(base, inst + '/'), resolveDataDir(base, inst))
  // a marketplace with other characters: everything outside a-z A-Z 0-9 _ - becomes '-'
  assert.equal(pluginDataId('workout-buddy', 'my.market place'), 'workout-buddy-my-market-place')
  assert.deepEqual(installedLayout('/c/plugins/cache/mk/workout-buddy/abc123'), { pluginsDir: '/c/plugins', marketplace: 'mk', name: 'workout-buddy' })
  assert.equal(installedLayout(dev), null)
  // a --plugin-dir session: the newest data dir that holds a ui.json, else the fallback (not final)
  const found = [{ dir: '/u/.claude/plugins/data/workout-buddy-a', mtimeMs: 5 }, { dir: '/u/.claude/plugins/data/workout-buddy-b', mtimeMs: 9 }]
  assert.deepEqual(resolveDataDir(base, dev, found), { dir: '/u/.claude/plugins/data/workout-buddy-b', final: true, why: 'plugin-dir' })
  assert.deepEqual(resolveDataDir(base, dev, []), { dir: '/u/.workout-buddy', final: false, why: 'fallback' })
  assert.deepEqual(resolveDataDir({}, dev, []), { dir: null, final: false, why: 'fallback' })
  assert.equal(resolveDataDir({ ...base, workoutHome: '  ' }, dev).why, 'fallback') // blank is unset
  // where those dirs are looked for: CLAUDE_CODE_PLUGIN_CACHE_DIR, else CLAUDE_CONFIG_DIR/plugins, else ~/.claude/plugins
  assert.equal(pluginsDirFor({ home: '/u' }), '/u/.claude/plugins')
  assert.equal(pluginsDirFor({ home: '/u', configDir: '/cfg/' }), '/cfg/plugins')
  assert.equal(pluginsDirFor({ home: '/u', configDir: '/cfg', pluginCacheDir: '/pc' }), '/pc')
  assert.equal(pluginsDirFor({}), null)
})

test('gate: one run at a time, repeat presses ignored until it leaves', () => {
  const g = createGate()
  assert.equal(g.busy, false)
  assert.equal(g.enter(), true)
  assert.equal(g.busy, true)
  assert.equal(g.enter(), false)
  assert.equal(g.enter(), false)
  g.leave()
  assert.equal(g.enter(), true)
})

test('gate debounces concurrent async presses', async () => {
  const g = createGate()
  let runs = 0
  const press = async () => {
    if (!g.enter()) return
    try {
      runs += 1
      await new Promise((r) => setTimeout(r, 20))
    } finally {
      g.leave()
    }
  }
  await Promise.all([press(), press(), press()])
  assert.equal(runs, 1)
  await press()
  assert.equal(runs, 2)
})

test('countdown of a timed set from when it first showed', () => {
  const e = parseFeed(feedText({ me: { ...ASK, exercise: 'plank', reps: 45, unit: 'seconds', text: '45s plank' } })).sessions.me
  assert.equal(countdown(e, NOW, NOW), 45)
  assert.equal(countdown(e, NOW, NOW + 10_500), 35)
  assert.equal(countdown(e, NOW, NOW + 99_000), 0)
  assert.equal(countdown(parseFeed(feedText({ me: ASK })).sessions.me, NOW, NOW), null)
})

test('paths, first lines and the badge text', () => {
  assert.equal(uiPath(null), null)
  assert.equal(uiPath('/tmp/wh/'), '/tmp/wh/ui.json')
  assert.equal(firstLine('\n  That question has closed — nothing logged.\nmore'), 'That question has closed — nothing logged.')
  assert.equal(badgeText('20 squats', 'kudos! 💪'), '✅ 20 squats · kudos! 💪')
  assert.equal(badgeText('20 squats', ''), '✅ 20 squats')
})

test('hotkeys: always the digits, while Claude works and after it stops', () => {
  for (const w of [true, false, undefined]) {
    const k = hotkeysFor(w)
    assert.deepEqual([k.done, k.skip], ['1', '2'])
    assert.equal(k.hint, 'press 1 when done')
    for (const h of [k.done, k.skip]) assert.match(h, /^[0-9]$/) // a digit fires from an empty prompt, no focus
  }
})

test('heartbeat path and timing', () => {
  assert.equal(beatPath(null, 'abc-123_D'), null) // no data dir known: no beat
  assert.equal(beatPath('/tmp/wh/', 's1'), '/tmp/wh/mod-seen/s1')
  for (const bad of [null, '', '../x', 'a/b', 'a b', 'x'.repeat(129), 7]) assert.equal(beatPath('/tmp/wh', bad), null)
  assert.equal(beatDue(null, 0, null, NOW), false) // no id, no beat
  assert.equal(beatDue(null, 0, 's1', NOW), true) // first
  assert.equal(beatDue('s1', NOW, 's1', NOW + 1000), false)
  assert.equal(beatDue('s1', NOW, 's1', NOW + BEAT_MS), true)
  assert.equal(beatDue('s1', NOW, 's2', NOW + 1), true) // /clear or /resume: at once
  assert.ok(BEAT_MS * 3 < 120000) // well inside workout.py's MOD_SEEN_S
})
