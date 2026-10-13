// Run with `claude plugin test` from the plugin directory: the real hooks module against Claude Code's test
// kit (stubbed env / fs / process / clock). It checks that the band's trees validate for the terminal
// (Raster, plain buttons, digit hotkeys 1 / 2 whether Claude works or not), the desktop (no art), the beat,
// the done / skip round trips, the /workout command, and that every argv runs the plugin's own launcher.
import { expect, mock, test } from 'claude-code/testing'

const SID = 'sess-1'
const T = 1_800_000_000
const DATA = '/data/workout-buddy' // WORKOUT_HOME in these tests

function feed(entry: object) {
  return JSON.stringify({ version: 1, updated_at: T, sessions: { [SID]: entry } })
}

const ASK = { mode: 'ask', ask_id: 'ab12cd34', exercise: 'squats', reps: 20, unit: 'reps', text: '20 squats', cheer: '', until_ts: T + 60 }
const BADGE = { mode: 'badge', ask_id: 'ab12cd34', exercise: 'squats', reps: 20, unit: 'reps', text: '20 squats', cheer: 'kudos! 💪', until_ts: T + 120 }

const BAND = {
  plugin: 'workout-buddy',
  component: 'AbovePrompt',
  requestId: 'above-prompt',
  viewport: { columns: 100, rows: 30 },
  props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100, scroll: { offset: 0, bodyRows: 10 }, view: {} },
} as const

function stubs(on: any, state: { text: string; runs: string[][]; exit?: number; out?: string; toasts?: string[]; env?: Record<string, string> }) {
  on('session.start', () => ({ cwd: '/work' }))
  on('command.register', ($: any, e: any) => {
    ;(state as any).commands?.push(e.name)
    return { value: undefined }
  })
  on('env.get', ($: any, e: any) => ({ value: (state.env ?? { WORKOUT_HOME: DATA })[e.name] }))
  on('session.id', () => ({ value: SID }))
  on('fs.exists', () => ({ value: (state as any).exists ?? true }))
  on('fs.list', () => ({ value: [] }))
  on('fs.read', () => ({ value: state.text }))
  on('fs.write', ($: any, e: any) => {
    ;(state as any).writes?.push(e.path)
    return { value: undefined }
  })
  on('ui.toast', ($: any, e: any) => {
    state.toasts?.push(e.text)
    return { value: undefined }
  })
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['drawn by Claude Code'] }))
  on('process.run', ($: any, e: any) => {
    state.runs.push([...e.argv])
    const ok = (state.exit ?? 0) === 0
    const verb = e.argv[e.argv.indexOf('cli') + 1]
    if (ok && verb === 'done') state.text = feed(BADGE)
    if (ok && verb === 'decline') state.text = feed({ mode: 'none' })
    return { value: { exitCode: state.exit ?? 0, stdout: state.out ?? 'ok', stderr: '' } }
  })
}

// Every argv the band runs: sh, the plugin's own launcher (an absolute path ending in /scripts/wb, so
// nothing hardcoded), the data dir, then the CLI words. Returns the words after the launcher and its --home.
function after(argv: string[]) {
  expect(argv[0]).toBe('sh')
  expect(argv[1]).toMatch(/^\/.+\/scripts\/wb$/)
  expect(argv.slice(2, 4)).toEqual(['--home', DATA])
  return argv.slice(4)
}

test('the ask draws the mascot and two buttons, and [1] done runs cli done <ask_id>', async ($, on) => {
  const clock = mock.clock(on, { now: T * 1000 })
  const state = { text: feed(ASK), runs: [] as string[][] }
  stubs(on, state)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ type: 'Raster' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: '20 squats?' })).toBeDefined()
  expect(await ui.find({ key: 'wb-done' })).toBeDefined()
  expect(await ui.find({ key: 'wb-skip' })).toBeDefined()
  await ui.press({ key: 'wb-done' })
  expect(state.runs.length).toBe(1)
  expect(after(state.runs[0])).toEqual(['cli', 'done', '--ask', 'ab12cd34'])
  expect(await ui.find({ type: 'Text', text: '✅ 20 squats · kudos! 💪' })).toBeDefined()
  await clock.advance(2500)
  expect(await ui.find({ key: 'wb-done' })).toBeUndefined()
  await ui.unmount()
})

test('[2] skip runs cli decline <ask_id> and the band says so, then goes', async ($, on) => {
  const clock = mock.clock(on, { now: T * 1000 })
  const state = { text: feed(ASK), runs: [] as string[][] }
  stubs(on, state)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'wb-skip' })
  expect(after(state.runs[0])).toEqual(['cli', 'decline', '--ask', 'ab12cd34'])
  expect(await ui.find({ type: 'Text', text: 'skipped, the next one will be different' })).toBeDefined()
  await clock.advance(2000)
  expect(await ui.find({ type: 'Text', text: 'drawn by Claude Code' })).toBeDefined()
  await ui.unmount()
})

test('another session sees nothing', async ($, on) => {
  mock.clock(on, { now: T * 1000 })
  const state = { text: JSON.stringify({ version: 1, updated_at: T, sessions: { other: ASK } }), runs: [] as string[][] }
  stubs(on, state)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ key: 'wb-done' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: 'drawn by Claude Code' })).toBeDefined()
  await ui.unmount()
})

test('narrow band: no mascot, buttons still there', async ($, on) => {
  mock.clock(on, { now: T * 1000 })
  const state = { text: feed({ ...ASK, exercise: 'plank', reps: 45, unit: 'seconds', text: '45s plank' }), runs: [] as string[][] }
  stubs(on, state)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal', props: { ...BAND.props, bodyColumns: 30 } })
  expect(await ui.find({ type: 'Raster' })).toBeUndefined()
  expect(await ui.find({ key: 'wb-done' })).toBeDefined()
  await ui.unmount()
})

test('a failed done shows a toast and keeps the question', async ($, on) => {
  mock.clock(on, { now: T * 1000 })
  const toasts: string[] = []
  const state = { text: feed(ASK), runs: [] as string[][], exit: 1, out: 'That question has closed — nothing logged.\n', toasts }
  stubs(on, state)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  await ui.press({ key: 'wb-done' })
  expect(toasts).toEqual(['That question has closed — nothing logged.'])
  expect(await ui.find({ key: 'wb-done' })).toBeDefined()
  await ui.unmount()
})

test('/workout runs the CLI with its words and shows the output; odd input never reaches a process', async ($, on) => {
  mock.clock(on, { now: T * 1000 })
  const state = { text: feed(ASK), runs: [] as string[][], out: 'Today: 2 sets, 35 reps\nStreak: 3 days 🔥', commands: [] as string[] } as any
  stubs(on, state)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  expect(state.commands).toEqual(['workout'])
  const stats = await $.command.run({ command: 'workout', args: '' })
  expect(stats).toEqual({ text: 'Today: 2 sets, 35 reps\nStreak: 3 days 🔥' })
  expect(after(state.runs.at(-1))).toEqual(['cli', 'stats'])
  await $.command.run({ command: 'workout', args: 'setup spinner --yes' })
  expect(after(state.runs.at(-1))).toEqual(['cli', 'setup', 'spinner', '--yes'])
  const n = state.runs.length
  const bad = await $.command.run({ command: 'workout', args: 'pause; rm -rf ~' })
  expect(bad.text).toMatch(/plain words/)
  expect(state.runs.length).toBe(n)
})

test('no data dir found yet: the band does not create the fallback ~/.workout-buddy by beating', async ($, on) => {
  mock.clock(on, { now: T * 1000 })
  const state = { text: feed(ASK), runs: [] as string[][], env: { HOME: '/home/me' }, writes: [] as string[], exists: false } as any
  stubs(on, state)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  expect(state.writes.filter((w: string) => w.includes('/mod-seen/'))).toEqual([])
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ key: 'wb-done' })).toBeUndefined()
  await ui.unmount()
})

test('the band beats for this session, and shows no art and no other session on the desktop', async ($, on) => {
  mock.clock(on, { now: T * 1000 })
  const writes: string[] = []
  const state = { text: feed(ASK), runs: [] as string[][], writes } as any
  stubs(on, state)
  await $.session.start({ surface: 'desktop', isInteractive: true, cwd: '/work' })
  expect(writes).toContain(DATA + '/mod-seen/' + SID)
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await ui.find({ type: 'Raster' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /█/ })).toBeUndefined()
  expect(await ui.find({ key: 'wb-done' })).toBeDefined()
  await ui.unmount()
})

test('digit hotkeys whether Claude works or not', async ($, on) => {
  mock.clock(on, { now: T * 1000 })
  const state = { text: feed(ASK), runs: [] as string[][] }
  stubs(on, state)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const idle = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await idle.find({ key: 'wb-done' }))?.props?.hotkey).toBe('1')
  expect((await idle.find({ key: 'wb-skip' }))?.props?.hotkey).toBe('2')
  expect(await idle.find({ type: 'Text', text: /press 1 when done/ })).toBeDefined()
  await idle.unmount()
  const busy = await $.ui.mount({ ...BAND, surface: 'terminal', props: { ...BAND.props, isWorking: true } })
  expect((await busy.find({ key: 'wb-done' }))?.props?.hotkey).toBe('1')
  expect((await busy.find({ key: 'wb-skip' }))?.props?.hotkey).toBe('2')
  expect(await busy.find({ type: 'Text', text: /press 1 when done/ })).toBeDefined()
  await busy.unmount()
})
