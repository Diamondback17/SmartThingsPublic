# Halo — a Pebble watchface

A sleek, fully-digital watchface built primarily for **Gabbro** (Pebble
Round 2, the 260x260 round display), designed around a dial-like bezel so
it reads naturally on a circular screen instead of forcing a rectangular
layout into a circle. No clock hands, nothing on screen moves on its own.

- **Numeric time, centered** — big light-weight digits, date and (on
  Health-capable watches) today's step count beneath, all vertically
  centered in the circle.
- **Tick-mark bezel** — a static 60-tick ring around the very edge (5
  minor ticks per major hour tick), like a dial's engraved scale. Purely
  decorative and fixed — it never moves, so it isn't a hand.
- **Chapter ring** — a thin circle just inside the bezel, separating the
  tick texture from the center readout.
- **Grain speckle** — sparse static pixels scattered in the ring between
  the chapter ring and the center text, for a bit of texture on the flat
  black face.
- **Battery ring** — a ring inside the chapter ring shows charge level
  (green/yellow/red, cyan while charging).
- **Bluetooth alert** — a small dot appears near the top only when the
  phone disconnects (with a vibration), and disappears once reconnected —
  visible only when it's actionable.
- Full color on Gabbro/Basalt/Chalk/Emery, clean white-on-black fallback
  on the 1-bit Aplite/Diorite displays. Layout scales with screen size.
  Updates once a minute (no seconds display, so no need to redraw more
  often).

## Project layout

```
pebble-watchface/
  package.json      # Pebble project manifest (uuid, targets, capabilities)
  src/c/main.c       # entire watchface implementation
```

## Building & installing

This is a standard Pebble C project. With the [Pebble
SDK](https://developer.rebble.io/developer.pebble.com/sdk/index.html) /
`pebble-tool` (via the Rebble webservices, since Pebble's own build cloud is
retired) installed:

```sh
cd pebble-watchface
pebble build
pebble install --phone <phone-ip>      # or --emulator gabbro
```

You can also just drag `src/c/main.c` and `package.json` into a new
[CloudPebble](https://cloudpebble.rebble.io) project if you'd rather build
in the browser.

## Customizing

- `compute_layout` in `main.c` picks fonts/line heights based on screen
  width, and also scatters the grain speckle positions once per screen
  size.
- `draw_tick_bezel`'s `minor_len`/`major_len` control tick length; the
  loop's `i % 5` controls major-tick spacing.
- `GRAIN_COUNT` controls how much speckle texture is drawn.
- `draw_battery_ring`'s `inset`/`thickness` locals control how thick the
  ring is and how far it sits from the chapter ring.
