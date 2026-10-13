import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  FRAMES, KEYFRAMES, TIMING, TICK_MS, GRID_W, GRID_H, COLS, ROWS, PIXELS, PALETTE, DEFAULT_COLOR, animationFor,
  frameAt, frameAtTime, durationOf, frameCells, rasterCells, textRows, plain, base64, cellOf, compose, shift, art,
} from '../lib/mascot.js'
import * as mascot from '../lib/mascot.js'
import { MASCOT_COLS, layout } from '../lib/band.js'

const NAMES = ['squats', 'pushups', 'jacks', 'crunches', 'lunges', 'calves', 'plank', 'wallsit', 'generic', 'victory', 'badge', 'idle']
const EXERCISES = ['squats', 'pushups', 'jacks', 'crunches', 'lunges', 'calves', 'plank', 'wallsit', 'generic', 'victory']

test('every animation key is present, and nothing else', () => {
  assert.deepEqual(Object.keys(FRAMES).sort(), [...NAMES].sort())
  assert.deepEqual(Object.keys(KEYFRAMES).sort(), [...NAMES].sort())
  assert.deepEqual(Object.keys(TIMING).sort(), [...NAMES].sort())
})

test('every frame is GRID_H rows of GRID_W known pixels', () => {
  for (const [name, frames] of Object.entries(FRAMES)) {
    assert.ok(frames.length >= 1, name)
    frames.forEach((f, i) => {
      assert.equal(f.length, GRID_H, `${name}[${i}] rows`)
      for (const row of f) {
        assert.equal(row.length, GRID_W, `${name}[${i}] width: ${row}`)
        for (const ch of row) assert.ok(PIXELS.includes(ch), `${name}[${i}] pixel ${ch}`)
      }
    })
  }
})

test('every pixel char has a palette color, and the palette is 24-bit', () => {
  for (const ch of PIXELS.slice(1)) assert.ok(Number.isInteger(PALETTE[ch]) && PALETTE[ch] <= 0xffffff, ch)
  assert.equal(PALETTE.O, 0xd77757)
})

test('the mascot is Clawd, in Claude orange', () => {
  assert.equal(mascot.NAME, 'Clawd')
  assert.equal(PALETTE.O, 0xd77757)
})

test('the swappable interface: everything register.js imports, and every animation name', () => {
  const src = readFileSync(new URL('../hooks/register.js', import.meta.url), 'utf8')
  const m = src.match(/import \{([^}]*)\} from '\.\.\/lib\/mascot\.js'/)
  assert.ok(m, 'register.js imports lib/mascot.js')
  for (const name of m[1].split(',').map((x) => x.trim()).filter(Boolean)) assert.ok(name in mascot, name)
  for (const fn of ['durationOf', 'animationFor', 'frameAt', 'frameAtTime', 'cellOf', 'frameCells', 'rasterCells', 'textRows', 'plain']) {
    assert.equal(typeof mascot[fn], 'function', fn)
  }
})

test('the grid fits the band: COLS is what band.js reserves, ROWS the full layout rows', () => {
  assert.equal(COLS, MASCOT_COLS)
  assert.equal(ROWS, layout(120, 10, '20 squats?').rows)
  assert.ok(COLS <= 18 && ROWS <= 6)
})

test('the tick matches register.js FRAME_MS, so FRAMES play at their real timing', () => {
  const src = readFileSync(new URL('../hooks/register.js', import.meta.url), 'utf8')
  const m = src.match(/const FRAME_MS = (\d+)/)
  assert.ok(m, 'FRAME_MS in register.js')
  assert.equal(Number(m[1]), TICK_MS)
})

test('keyframes: whole ticks, and FRAMES is them expanded one entry per tick', () => {
  for (const [name, kfs] of Object.entries(KEYFRAMES)) {
    let n = 0
    for (const [f, ms] of kfs) {
      assert.ok(ms > 0 && ms % TICK_MS === 0, `${name}: ${ms} ms`)
      for (let k = 0; k < ms / TICK_MS; k++) assert.deepEqual(FRAMES[name][n++], f, `${name} tick ${n}`)
    }
    assert.equal(n, FRAMES[name].length, name)
    assert.deepEqual(TIMING[name], kfs.map(([, ms]) => ms))
    assert.equal(durationOf(name), n * TICK_MS)
  }
})

test('exercise loops are short: 4 to 10 keyframes, 2 or more distinct poses, under 3 s', () => {
  for (const name of EXERCISES) {
    const kfs = KEYFRAMES[name]
    assert.ok(kfs.length >= 4 && kfs.length <= 10, `${name}: ${kfs.length} keyframes`)
    assert.ok(new Set(kfs.map(([f]) => f.join('\n'))).size >= 2, name)
    assert.ok(durationOf(name) <= 3000, `${name}: ${durationOf(name)} ms`)
  }
})

test('every frame has Clawd in it: a body and an eye', () => {
  for (const [name, frames] of Object.entries(FRAMES)) {
    frames.forEach((f, i) => {
      const all = f.join('')
      assert.ok((all.match(/O/g) || []).length >= 25, `${name}[${i}] body`)
      assert.ok(all.includes('E'), `${name}[${i}] eye`)
    })
  }
})

test('idle is the mascot: 10x5 body, nubs out at mid-height, tall eyes, four legs in two pairs', () => {
  const f = FRAMES.idle[0]
  assert.deepEqual(f, [
    '..............',
    '..OOOOOOOOOO..',
    '..OOEOOOOEOO..',
    'OOOOEOOOOEOOOO',
    '..OOOOOOOOOO..',
    '..OOOOOOOOOO..',
    '...O.O..O.O...',
    '...O.O..O.O...',
  ])
})

test('personality: blinks, a happy squint at the top of a rep, sweat on the holds, sparkles on a win', () => {
  const has = (name, re) => KEYFRAMES[name].some(([f]) => re.test(f.join('\n')))
  const flat = (name) => KEYFRAMES[name].map(([f]) => f)
  assert.ok(flat('idle').some((f) => f.join('').split('E').length - 1 === 2), 'idle blinks')
  for (const name of ['squats', 'calves', 'jacks', 'victory', 'badge']) {
    assert.ok(flat(name).some((f) => f.join('').split('E').length - 1 === 6), `${name}: happy eyes`)
  }
  assert.ok(has('plank', /~/), 'plank sweats')
  assert.ok(has('wallsit', /~/), 'wall sit sweats')
  assert.ok(KEYFRAMES.wallsit.every(([f]) => f.every((r) => r[0] === 'W')), 'the wall stays')
  assert.ok(KEYFRAMES.victory.every(([f]) => /[*+]/.test(f.join(''))), 'victory sparkles')
})

test('motion: squats and calf raises move the body vertically, jacks open the legs', () => {
  const top = (f) => f.findIndex((r) => r.slice(2, 12).includes('OOOOOOOO'))
  const tops = (name) => new Set(KEYFRAMES[name].map(([f]) => top(f)))
  assert.ok(tops('squats').size >= 3, 'squats: three heights')
  assert.ok(tops('calves').size >= 2, 'calves rise')
  const spread = (f) => { const r = f[GRID_H - 1]; return r.lastIndexOf('O') - r.indexOf('O') }
  const s = KEYFRAMES.jacks.map(([f]) => spread(f)).filter((n) => n > 0)
  assert.ok(Math.max(...s) > Math.min(...s), 'jacks: feet apart and together')
})

test('rendered rows have a constant width: COLS cells, ROWS rows, for every frame', () => {
  for (const [name, frames] of Object.entries(FRAMES)) {
    for (const f of frames) {
      const rows = plain(f)
      assert.equal(rows.length, ROWS, name)
      for (const r of rows) assert.equal([...r].length, COLS, `${name}: ${r}`)
      assert.equal(frameCells(f).length, COLS * ROWS)
      const tr = textRows(f)
      for (const runs of tr) assert.equal(runs.reduce((n, s) => n + [...s.text].length, 0), COLS)
    }
  }
})

test('half blocks: top, bottom, both, two colors, none', () => {
  assert.deepEqual(cellOf('.', '.'), [0x20, DEFAULT_COLOR, DEFAULT_COLOR])
  assert.deepEqual(cellOf('O', '.'), [0x2580, PALETTE.O, DEFAULT_COLOR])
  assert.deepEqual(cellOf('.', 'O'), [0x2584, PALETTE.O, DEFAULT_COLOR])
  assert.deepEqual(cellOf('O', 'O'), [0x2588, PALETTE.O, DEFAULT_COLOR])
  assert.deepEqual(cellOf('E', 'O'), [0x2580, PALETTE.E, PALETTE.O])
})

test('raster cells: base64 of little-endian u32 triplets, matching Buffer', () => {
  for (const name of NAMES) {
    const f = FRAMES[name][0]
    const cells = rasterCells(f)
    const words = Uint32Array.from(frameCells(f).flat())
    assert.equal(cells, Buffer.from(words.buffer).toString('base64'))
    assert.equal(Buffer.from(cells, 'base64').length, COLS * ROWS * 12)
  }
  for (const n of [0, 1, 2, 3, 4, 5, 255]) {
    const b = Uint8Array.from({ length: n }, (_, i) => (i * 37) & 255)
    assert.equal(base64(b), Buffer.from(b).toString('base64'))
  }
})

test('text runs use hex colors only where a pixel is drawn', () => {
  for (const name of NAMES) {
    for (const runs of textRows(FRAMES[name][0])) {
      for (const s of runs) {
        if (s.color) assert.match(s.color, /^#[0-9a-f]{6}$/)
        if (s.backgroundColor) assert.match(s.backgroundColor, /^#[0-9a-f]{6}$/)
        if (/^ +$/.test(s.text)) assert.equal(s.color, undefined)
      }
    }
  }
})

test('exercise names map to their animation', () => {
  assert.equal(animationFor('squats'), 'squats')
  assert.equal(animationFor('Air Squats'), 'squats')
  assert.equal(animationFor('Push-ups'), 'pushups')
  assert.equal(animationFor('push ups'), 'pushups')
  assert.equal(animationFor('jumping jacks'), 'jacks')
  assert.equal(animationFor('crunches'), 'crunches')
  assert.equal(animationFor('sit-ups'), 'crunches')
  assert.equal(animationFor('lunges'), 'lunges')
  assert.equal(animationFor('calf raises'), 'calves')
  assert.equal(animationFor('tiptoe raises'), 'calves')
  assert.equal(animationFor('plank'), 'plank')
  assert.equal(animationFor('wall sit'), 'wallsit')
  assert.equal(animationFor('burpees'), 'generic')
  assert.equal(animationFor(undefined), 'generic')
  for (const n of ['squats', 'pushups', 'jacks', 'crunches', 'lunges', 'calves', 'plank', 'wallsit', 'generic']) {
    assert.ok(FRAMES[animationFor(n === 'wallsit' ? 'wall sit' : n)], n)
  }
})

test('frameAt wraps around, and an unknown animation falls back to generic', () => {
  const n = FRAMES.squats.length
  assert.deepEqual(frameAt('squats', n + 1), FRAMES.squats[1])
  assert.deepEqual(frameAt('squats', -1), FRAMES.squats[n - 1])
  assert.deepEqual(frameAt('nope', 0), FRAMES.generic[0])
})

test('frameAtTime agrees with frameAt at every tick', () => {
  for (const name of NAMES) {
    for (let i = 0; i < FRAMES[name].length * 2; i++) {
      assert.deepEqual(frameAtTime(name, i * TICK_MS + 1), frameAt(name, i), `${name} @${i}`)
    }
  }
  assert.deepEqual(frameAtTime('nope', 0), FRAMES.generic[0])
})

test('compose and shift clip at the edges instead of growing the grid', () => {
  const f = shift(compose({ arms: 'up', legs: 'wide' }), 5, 5)
  assert.equal(f.length, GRID_H)
  for (const r of f) assert.equal(r.length, GRID_W)
})

test('art rejects a frame of the wrong size or with an unknown pixel', () => {
  assert.throws(() => art(['..']))
  assert.throws(() => art(Array(GRID_H).fill('.'.repeat(GRID_W - 1))))
  assert.throws(() => art(Array(GRID_H).fill('X'.repeat(GRID_W))))
})
