# Cyber Matrix — a Pebble watchface

A "go crazy" digital watchface for Pebble (Aplite, Basalt, Chalk, Diorite,
Emery):

- **Live digital-rain background** — falling character columns, à la The
  Matrix, running behind everything and continuously animating every second.
- **Glitch HUD clock** — big monospace time readout in a terminal-style
  panel, with seconds and date underneath (plus today's step count on
  watches with the Health service).
- **Chromatic-aberration glitch bursts** — the clock randomly "glitches"
  for a few frames (red/cyan mis-registered copies + a burst of rain
  scrambling), with a small chance every second, or on demand.
- **Shake to glitch** — tap/shake the watch to trigger the glitch burst
  and a vibration on demand.
- **Radial battery ring** — a colored arc traced around the very edge of
  the screen showing charge level (green/yellow/red, cyan while charging).
- **Bluetooth status dot** — solid dot when connected, an "X" plus a
  double vibration pulse when the phone disconnects.
- Full color on Basalt/Chalk/Emery, sensible white-on-black fallback on
  the 1-bit Aplite/Diorite displays. Layout adapts for the round Chalk
  display.

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
pebble install --phone <phone-ip>      # or --emulator basalt
```

You can also just drag `src/c/main.c` and `package.json` into a new
[CloudPebble](https://cloudpebble.rebble.io) project if you'd rather build
in the browser.

## Customizing

- `s_charset` in `main.c` controls which characters fall in the rain —
  swap in different symbols for a different vibe.
- `CELL_W`/`CELL_H` control the rain grid density.
- `GLITCH_FRAME_COUNT` / `GLITCH_FRAME_MS` control how long/fast glitch
  bursts play.
- The `rand() % 45` check in `tick_handler` controls how often random
  glitches fire on their own — raise it to calm the watchface down, lower
  it to make it glitchier.
