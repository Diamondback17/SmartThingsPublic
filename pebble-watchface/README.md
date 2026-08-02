# Halo — a Pebble watchface

A sleek, fully-digital watchface built primarily for **Gabbro** (Pebble
Round 2, the 260x260 round display), designed around concentric rings so
it reads naturally on a circular screen instead of forcing a rectangular
layout into a circle. No clock hands, no clutter.

- **Numeric time, centered** — big light-weight digits, date and (on
  Health-capable watches) today's step count beneath, all vertically
  centered in the circle.
- **Seconds ring** — a thin accent-colored ring traces around the very
  edge of the screen, sweeping a full revolution once a minute.
- **Battery ring** — a second, slightly inset ring shows charge level
  (green/yellow/red, cyan while charging) — two clean concentric rings
  instead of one loud one.
- **Bluetooth alert** — a small dot appears near the top only when the
  phone disconnects (with a vibration), and disappears once reconnected —
  visible only when it's actionable.
- Full color on Gabbro/Basalt/Chalk/Emery, clean white-on-black fallback
  on the 1-bit Aplite/Diorite displays. Layout scales with screen size.

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
  width — tweak the `big` threshold or the font keys to change sizing.
- `draw_ring`'s `inset`/`thickness` arguments (called from
  `draw_seconds_ring`/`draw_battery_ring`) control how thick the rings are
  and how far apart they sit.
- The accent color for the seconds ring is set in `draw_seconds_ring`
  (`GColorVividCerulean` by default) — swap it for a different vibe.
