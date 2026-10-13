// The mascot: Clawd, Claude Code's mascot, drawn here as fan art in pixels, and the frames of each
// exercise.
//
// This is the one swappable module for the band's character. Another mascot is a file with the same
// exports: NAME, GRID_W, GRID_H, COLS, ROWS, TICK_MS, PALETTE, PIXELS, DEFAULT_COLOR, KEYFRAMES, TIMING,
// FRAMES, durationOf, animationFor, frameAt, frameAtTime, cellOf, frameCells, rasterCells, textRows, plain,
// and the same animation names (squats, pushups, jacks, crunches, lunges, calves, plank, wallsit, generic,
// victory, badge, idle). hooks/register.js imports it as '../lib/mascot.js'.
//
// A frame is GRID_H strings of GRID_W pixels each. One pixel per character:
//   '.' empty   'O' body (Claude orange)   'E' eye   '*' sparkle   '~' sweat   'W' wall   '+' twinkle
// Two pixel rows share one terminal row (half blocks: '▀' top, '▄' bottom, '█' both), so a frame
// draws as GRID_W columns by GRID_H / 2 rows, and every pixel is square on screen.
//
// The grid stays 14 x 8 (14 columns x 4 terminal rows) on purpose: lib/band.js reserves exactly
// MASCOT_COLS = 14 columns and a 4-row band for the art, and the tests pin the two together.
//
// Clawd, front view (the idle pose): a flat orange body twice as wide as tall, two tall white eyes in
// the upper part set in from the sides, a stubby nub straight out on each side at mid-height, and four
// thin legs in two pairs with a one-pixel gap inside each pair:
//
//   ..............
//   ..OOOOOOOOOO..
//   ..OOEOOOOEOO..
//   OOOOEOOOOEOOOO
//   ..OOOOOOOOOO..
//   ..OOOOOOOOOO..
//   ...O.O..O.O...
//   ...O.O..O.O...
//
// Timing: register.js advances one frame every TICK_MS (its FRAME_MS). Each animation is written as
// keyframes with a duration in ms; FRAMES[name] is the keyframes expanded to one entry per tick, so a
// consumer that just steps frameAt(name, i) at TICK_MS plays the real timing. KEYFRAMES / TIMING carry
// the same thing unexpanded, and frameAtTime(name, ms) picks by elapsed time.
// Pure: no mods API in this file.

export const NAME = 'Clawd'
export const GRID_W = 14
export const GRID_H = 8
export const COLS = GRID_W
export const ROWS = GRID_H / 2
export const TICK_MS = 140 // must equal FRAME_MS in hooks/register.js (a test checks)

// 24-bit colors (0xRRGGBB) for the Raster, and the same as hex for Text.
export const PALETTE = {
  O: 0xd77757, // Claude orange
  E: 0xffffff, // eyes
  '*': 0xffd166, // sparkle
  '~': 0x7ec8e3, // sweat
  W: 0x6b6b6b, // wall
  '+': 0xfff4d6, // small twinkle (victory, badge)
}
export const PIXELS = '.' + Object.keys(PALETTE).join('')
export const DEFAULT_COLOR = 0x01000000 // the terminal's own color, in a Raster

export function hex(c) {
  return '#' + c.toString(16).padStart(6, '0')
}

// ---------------------------------------------------------------- building frames

function blank() {
  return Array.from({ length: GRID_H }, () => Array(GRID_W).fill('.'))
}

function put(g, x, y, ch) {
  if (x >= 0 && x < GRID_W && y >= 0 && y < GRID_H) g[y][x] = ch
}

function rows(g) {
  return g.map((r) => r.join(''))
}

function rect(g, x, y, w, h, ch = 'O') {
  for (let r = y; r < y + h; r++) for (let c = x; c < x + w; c++) put(g, c, r, ch)
}

// A frame drawn by hand: GRID_H lines of GRID_W pixels (checked, so a typo fails at import).
export function art(lines) {
  if (lines.length !== GRID_H) throw new Error('art: ' + lines.length + ' rows')
  for (const l of lines) {
    if (l.length !== GRID_W) throw new Error('art: row width ' + l.length + ': ' + l)
    for (const ch of l) if (!PIXELS.includes(ch)) throw new Error('art: pixel ' + ch)
  }
  return lines.slice()
}

// Front view. x, y: the body's top-left pixel; w, h: its size. Eyes, nubs and legs hang off the body.
//   eyes    open | blink (half shut) | happy (^ ^) | effort (> <) | left | right (looking)
//   arms    side (straight out, mid-height) | up (raised high) | down (angled down) | out (raised a bit) | none
//   legs    stand | tuck | apart | wide | knees | lungeL | lungeR | none
//   legLen  how long the legs are; default: down to the floor (the grid's last row)
export function compose({
  x = 2, y = 1, w = 10, h = 5,
  eyes = 'open', arms = 'side', legs = 'stand', legLen,
  extras = [], erase = [],
} = {}) {
  const g = blank()
  rect(g, x, y, w, h)
  const ex = [x + 2, x + w - 3]
  const ey = y + 1
  if (eyes === 'open') for (const c of ex) { put(g, c, ey, 'E'); put(g, c, ey + 1, 'E') }
  if (eyes === 'left' || eyes === 'right') {
    const d = eyes === 'left' ? -1 : 1
    for (const c of ex) { put(g, c + d, ey, 'E'); put(g, c + d, ey + 1, 'E') }
  }
  if (eyes === 'blink') for (const c of ex) put(g, c, ey + 1, 'E')
  if (eyes === 'happy') for (const c of ex) { put(g, c - 1, ey + 1, 'E'); put(g, c, ey, 'E'); put(g, c + 1, ey + 1, 'E') }
  if (eyes === 'effort') {
    put(g, ex[0] - 1, ey, 'E'); put(g, ex[0], ey + 1, 'E') // >
    put(g, ex[1] + 1, ey, 'E'); put(g, ex[1], ey + 1, 'E') // <
  }
  const L = x - 1
  const R = x + w
  const my = y + Math.floor(h / 2) // mid-height
  if (arms === 'side') for (const c of [L - 1, L, R, R + 1]) put(g, c, my, 'O')
  if (arms === 'down') for (const [c, d] of [[L, -1], [R, 1]]) { put(g, c, my, 'O'); put(g, c + d, my + 1, 'O') }
  if (arms === 'up') for (const [c, d] of [[L, -1], [R, 1]]) { put(g, c, y + 1, 'O'); put(g, c + d, y, 'O'); put(g, c + d, y - 1, 'O') }
  if (arms === 'out') for (const [c, d] of [[L, -1], [R, 1]]) { put(g, c, my - 1, 'O'); put(g, c + d, my - 2, 'O') }
  const ly = y + h // first row under the body
  const len = legLen ?? GRID_H - ly
  const feet = [x + 1, x + 3, x + w - 4, x + w - 2]
  const outward = [-1, -1, 1, 1]
  if (legs === 'stand') for (const c of feet) for (let k = 0; k < len; k++) put(g, c, ly + k, 'O')
  if (legs === 'tuck') for (const c of feet) put(g, c, ly, 'O')
  if (legs === 'apart' || legs === 'wide') {
    // each pair steps out a pixel per row ('wide' one more)
    const s = legs === 'wide' ? 1 : 0
    feet.forEach((c, i) => { for (let k = 0; k < len; k++) put(g, c + outward[i] * (k + s), ly + k, 'O') })
  }
  if (legs === 'knees') {
    // squat: knees pushed out, feet planted
    feet.forEach((c, i) => { put(g, c + outward[i], ly, 'O'); if (len > 1) put(g, c, ly + 1, 'O') })
  }
  if (legs === 'lungeL' || legs === 'lungeR') {
    // the stepping pair strides out and down to the floor; the other pair bends, knee down
    const stepping = legs === 'lungeL' ? [0, 1] : [2, 3]
    feet.forEach((c, i) => {
      if (stepping.includes(i)) for (let k = 0; k < len; k++) put(g, c + outward[i] * (k + 1), ly + k, 'O')
      else { put(g, c, ly, 'O'); if (len > 1) put(g, c - outward[i], ly + 1, 'O') }
    })
  }
  for (const [cx, cy] of erase) put(g, cx, cy, '.')
  for (const [cx, cy, ch] of extras) put(g, cx, cy, ch)
  return rows(g)
}

// Move a frame by whole pixels; what leaves the grid is dropped.
export function shift(frame, dx = 0, dy = 0) {
  const g = blank()
  frame.forEach((row, y) => [...row].forEach((ch, x) => { if (ch !== '.') put(g, x + dx, y + dy, ch) }))
  return rows(g)
}

export function overlay(frame, extras) {
  const g = frame.map((r) => [...r])
  for (const [x, y, ch] of extras) put(g, x, y, ch)
  return rows(g)
}

// ---------------------------------------------------------------- the poses

const STAND = compose({})
const BLINK = compose({ eyes: 'blink' })

// Squats, front view: drop a pixel, then sit deep with the knees out, then pop up tall.
const SQ_HALF = compose({ y: 2 })
const SQ_DEEP = compose({ y: 3, h: 4, eyes: 'effort', legs: 'knees' })
const SQ_TOP = compose({ y: 0, eyes: 'happy' })

// Push-ups, side view facing right: one eye, the near nub is the pushing arm, toes at the back.
// Up: arm straight, the body one straight slope from toes to head. Down: flat, a pixel off the floor.
const PU_UP = art([
  '..............',
  '..............',
  '.........OOOO.',
  '.....OOOOOOEO.',
  '.OOOOOOOOOOOO.',
  '.OOOOOOOO.O...',
  '.OOOO.....O...',
  'O.........O...',
])
const PU_UP_HAPPY = art([
  '..............',
  '..............',
  '.........OOEO.',
  '.....OOOOOOOO.',
  '.OOOOOOOOOOOO.',
  '.OOOOOOOO.O...',
  '.OOOO.....O...',
  'O.........O...',
])
const PU_MID = art([
  '..............',
  '..............',
  '..............',
  '.......OOOOOO.',
  '.OOOOOOOOOOEO.',
  '.OOOOOOOOOOOO.',
  '.OOOOOO...O...',
  'O.........O...',
])
const PU_DOWN = art([
  '..............',
  '..............',
  '..............',
  '..............',
  '.OOOOOOOOOOOO.',
  '.OOOOOOOOOOOO.',
  '.OOOOOOOOOOEO.',
  'O........OO...',
])

// Plank, side view: forearms flat on the floor, body one straight line, holding.
const PLANK = art([
  '..............',
  '..............',
  '..............',
  '..............',
  '.OOOOOOOOOOOO.',
  '.OOOOOOOOOOEO.',
  '.OOOOOOOOOOOO.',
  'O........OOO..',
])
const PLANK_SHUT = art([
  '..............',
  '..............',
  '..............',
  '..............',
  '.OOOOOOOOOOOO.',
  '.OOOOOOOOOOOO.',
  '.OOOOOOOOOOEO.',
  'O........OOO..',
])
// the hips quiver up a pixel
const PLANK_SHAKE = art([
  '..............',
  '..............',
  '..............',
  '....OOOOOO....',
  '.OOOOOOOOOOOO.',
  '.OOOOOOOOOOEO.',
  '.OOO......OOO.',
  'O........OOO..',
])

// Wall sit, side view facing right: back flat on the wall, thighs level, shins straight down,
// the nub held out in front.
const WALL_SIT = art([
  'W.............',
  'WOOOOOO.......',
  'WOOOOEO.......',
  'WOOOOEOOO.....',
  'WOOOOOO.......',
  'WOOOOOOOOOOO..',
  'W........O.O..',
  'W........O.O..',
])
const WALL_SHUT = art([
  'W.............',
  'WOOOOOO.......',
  'WOOOOOO.......',
  'WOOOOEOOO.....',
  'WOOOOOO.......',
  'WOOOOOOOOOOO..',
  'W........O.O..',
  'W........O.O..',
])
// slides a pixel down the wall, then pushes back up
const WALL_SLIP = art([
  'W.............',
  'W.............',
  'WOOOOOO.......',
  'WOOOOOO.......',
  'WOOOOEOOO.....',
  'WOOOOOO.......',
  'WOOOOOOOOOOO..',
  'W........O.O..',
])

// Crunches, side view: on the back with the knees up, the top curls up toward the knees and back.
// Head on the left; the nub reaches for the knees as the top comes up.
const CR_FLAT = art([
  '..............',
  '..............',
  '..............',
  '..............',
  '...........O..',
  '.OEOOOOOO.O.O.',
  'OOOOOOOOOO...O',
  '.OOOOOOOO....O',
])
const CR_FLAT_BLINK = art([
  '..............',
  '..............',
  '..............',
  '..............',
  '...........O..',
  '.OOOOOOOO.O.O.',
  'OOEOOOOOOO...O',
  '.OOOOOOOO....O',
])
const CR_MID = art([
  '..............',
  '..............',
  '..............',
  '..OOOO........',
  '.OOOOEOO...O..',
  '..OOOOOOO.O.O.',
  '....OOOOOO...O',
  '.....OOOO....O',
])
const CR_UP = art([
  '..............',
  '..OOO.........',
  '..OOOE........',
  '..OOOOOOOO....',
  '...OOOOO...O..',
  '....OOOOO.O.O.',
  '.....OOOOO...O',
  '......OOOO...O',
])
const CR_UP_HAPPY = art([
  '..............',
  '..OOOE........',
  '..OOOO........',
  '..OOOOOOOO....',
  '...OOOOO...O..',
  '....OOOOO.O.O.',
  '.....OOOOO...O',
  '......OOOO...O',
])

// Victory sparkles: two sets that twinkle in turn.
const SPARKS_A = [[0, 3, '*'], [13, 3, '+'], [1, 7, '+'], [12, 6, '*']]
const SPARKS_B = [[0, 5, '+'], [13, 4, '*'], [0, 1, '*'], [13, 7, '+']]

// ---------------------------------------------------------------- the animations

// Each animation: keyframes as [frame, ms]. Durations are whole ticks, so the expanded FRAMES play
// them exactly at register.js's fixed tick.
const T = TICK_MS
export const KEYFRAMES = {
  idle: [
    [STAND, 18 * T],
    [BLINK, T],
    [STAND, 6 * T],
    [compose({ eyes: 'left' }), 5 * T],
    [compose({ eyes: 'right' }), 5 * T],
    [STAND, 8 * T],
    [BLINK, T],
    [STAND, 2 * T],
    [BLINK, T],
  ],
  squats: [
    [STAND, 2 * T],
    [SQ_HALF, T],
    [SQ_DEEP, 3 * T],
    [SQ_HALF, T],
    [SQ_TOP, 2 * T],
    [STAND, 2 * T],
    [BLINK, T],
  ],
  pushups: [
    [PU_UP, 2 * T],
    [PU_MID, T],
    [PU_DOWN, 3 * T],
    [PU_MID, T],
    [PU_UP, T],
    [PU_UP_HAPPY, 2 * T],
  ],
  jacks: [
    [compose({ arms: 'down' }), 2 * T],
    [compose({ y: 0, arms: 'out', legs: 'apart', legLen: 2 }), T],
    [compose({ arms: 'up', legs: 'wide', eyes: 'happy' }), 2 * T],
    [compose({ y: 0, arms: 'out', legs: 'apart', legLen: 2 }), T],
  ],
  crunches: [
    [CR_FLAT, 2 * T],
    [CR_MID, T],
    [CR_UP, T],
    [CR_UP_HAPPY, 2 * T],
    [CR_MID, T],
    [CR_FLAT, T],
    [CR_FLAT_BLINK, T],
  ],
  lunges: [
    [STAND, 2 * T],
    [compose({ x: 1, y: 2, h: 4, legs: 'lungeL', eyes: 'left' }), T],
    [compose({ x: 1, y: 2, h: 4, legs: 'lungeL', eyes: 'effort' }), 3 * T],
    [STAND, 2 * T],
    [compose({ x: 3, y: 2, h: 4, legs: 'lungeR', eyes: 'right' }), T],
    [compose({ x: 3, y: 2, h: 4, legs: 'lungeR', eyes: 'effort' }), 3 * T],
  ],
  calves: [
    [STAND, 3 * T],
    [compose({ y: 0, legLen: 3 }), T],
    [compose({ y: 0, legLen: 3, eyes: 'happy' }), 3 * T],
    [compose({ y: 0, legLen: 3 }), T],
    [STAND, 2 * T],
    [BLINK, T],
  ],
  plank: [
    [PLANK, 6 * T],
    [PLANK_SHAKE, T],
    [PLANK, T],
    [PLANK_SHAKE, T],
    [PLANK, 3 * T],
    [overlay(PLANK, [[12, 2, '~']]), 2 * T],
    [overlay(PLANK_SHUT, [[13, 3, '~']]), T],
    [overlay(PLANK_SHUT, [[13, 5, '~']]), T],
    [PLANK_SHUT, T],
    [PLANK, 3 * T],
  ],
  wallsit: [
    [WALL_SIT, 6 * T],
    [overlay(WALL_SIT, [[7, 0, '~']]), 2 * T],
    [overlay(WALL_SHUT, [[8, 1, '~']]), T],
    [overlay(WALL_SHUT, [[8, 3, '~']]), T],
    [WALL_SHUT, T],
    [WALL_SLIP, 2 * T],
    [WALL_SHUT, T],
    [WALL_SIT, 3 * T],
  ],
  generic: [
    [STAND, 3 * T],
    [compose({ y: 0, legLen: 2, arms: 'out', eyes: 'happy' }), 2 * T],
    [STAND, 3 * T],
    [BLINK, T],
  ],
  victory: [
    [compose({ y: 0, arms: 'out', legs: 'tuck', eyes: 'happy', extras: SPARKS_A }), 2 * T],
    [compose({ y: 0, arms: 'out', legs: 'tuck', eyes: 'happy', extras: SPARKS_B }), T],
    [compose({ y: 1, arms: 'up', eyes: 'happy', extras: SPARKS_B }), 2 * T],
    [compose({ y: 1, arms: 'up', eyes: 'happy', extras: SPARKS_A }), T],
  ],
  badge: [[compose({ eyes: 'happy', extras: [[0, 0, '*'], [13, 1, '+']] }), T]],
}

// ms per keyframe, per animation.
export const TIMING = Object.fromEntries(Object.entries(KEYFRAMES).map(([k, v]) => [k, v.map(([, ms]) => ms)]))

// One frame per tick, for a fixed-rate consumer (register.js).
export const FRAMES = Object.fromEntries(
  Object.entries(KEYFRAMES).map(([k, v]) => [k, v.flatMap(([f, ms]) => Array(Math.max(1, Math.round(ms / TICK_MS))).fill(f))]),
)

// Total loop length in ms.
export function durationOf(name) {
  return (TIMING[name] || TIMING.generic).reduce((a, b) => a + b, 0)
}

// Which animation an exercise gets: by name, tolerant of spelling ("Push ups", "push-ups").
export function animationFor(exercise) {
  const n = String(exercise || '').toLowerCase().replace(/[^a-z]/g, '')
  if (n.includes('squat')) return 'squats'
  if (n.includes('push')) return 'pushups'
  if (n.includes('jack')) return 'jacks'
  if (n.includes('crunch') || n.includes('situp')) return 'crunches'
  if (n.includes('lunge')) return 'lunges'
  if (n.includes('calf') || n.includes('calves') || n.includes('tiptoe')) return 'calves'
  if (n.includes('plank')) return 'plank'
  if (n.includes('wallsit') || n.includes('wall')) return 'wallsit'
  return 'generic'
}

// The i-th tick of an animation (wraps; negative counts from the end).
export function frameAt(name, i) {
  const f = FRAMES[name] || FRAMES.generic
  return f[((i % f.length) + f.length) % f.length]
}

// The frame showing ms into an animation (wraps).
export function frameAtTime(name, ms) {
  const key = name in KEYFRAMES ? name : 'generic'
  const total = durationOf(key)
  let t = ((ms % total) + total) % total
  for (const [f, d] of KEYFRAMES[key]) {
    if (t < d) return f
    t -= d
  }
  return KEYFRAMES[key][KEYFRAMES[key].length - 1][0]
}

// ---------------------------------------------------------------- rendering

// One terminal cell from its two pixels: [codePoint, foreground, background].
export function cellOf(top, bottom) {
  const t = top === '.' ? null : PALETTE[top]
  const b = bottom === '.' ? null : PALETTE[bottom]
  if (t == null && b == null) return [0x20, DEFAULT_COLOR, DEFAULT_COLOR]
  if (t != null && b == null) return [0x2580, t, DEFAULT_COLOR] // ▀
  if (t == null && b != null) return [0x2584, b, DEFAULT_COLOR] // ▄
  if (t === b) return [0x2588, t, DEFAULT_COLOR] // █
  return [0x2580, t, b]
}

export function frameCells(frame) {
  const out = []
  for (let r = 0; r < ROWS; r++) {
    for (let c = 0; c < COLS; c++) out.push(cellOf(frame[2 * r][c], frame[2 * r + 1][c]))
  }
  return out
}

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

// Standard padded base64 of bytes (no dependence on Uint8Array.prototype.toBase64).
export function base64(bytes) {
  let s = ''
  let i = 0
  for (; i + 2 < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8) | bytes[i + 2]
    s += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + B64[(n >> 6) & 63] + B64[n & 63]
  }
  const rest = bytes.length - i
  if (rest === 1) {
    const n = bytes[i] << 16
    s += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + '=='
  } else if (rest === 2) {
    const n = (bytes[i] << 16) | (bytes[i + 1] << 8)
    s += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] + B64[(n >> 6) & 63] + '='
  }
  return s
}

// A Raster's `cells`: columns * rows little-endian u32 triplets, base64.
export function rasterCells(frame) {
  const words = new Uint32Array(frameCells(frame).flat())
  const bytes = new Uint8Array(words.length * 4)
  words.forEach((w, k) => {
    bytes[4 * k] = w & 255
    bytes[4 * k + 1] = (w >>> 8) & 255
    bytes[4 * k + 2] = (w >>> 16) & 255
    bytes[4 * k + 3] = (w >>> 24) & 255
  })
  return base64(bytes)
}

// The same frame as Text runs, for a surface without Raster: per row, [{ text, color, backgroundColor }].
export function textRows(frame) {
  const out = []
  for (let r = 0; r < ROWS; r++) {
    const runs = []
    for (let c = 0; c < COLS; c++) {
      const [cp, fg, bg] = cellOf(frame[2 * r][c], frame[2 * r + 1][c])
      const seg = {
        text: String.fromCodePoint(cp),
        color: fg === DEFAULT_COLOR ? undefined : hex(fg),
        backgroundColor: bg === DEFAULT_COLOR ? undefined : hex(bg),
      }
      const last = runs[runs.length - 1]
      if (last && last.color === seg.color && last.backgroundColor === seg.backgroundColor) last.text += seg.text
      else runs.push(seg)
    }
    out.push(runs)
  }
  return out
}

// Plain text (no color), for tests and the README.
export function plain(frame) {
  return textRows(frame).map((runs) => runs.map((s) => s.text).join(''))
}
