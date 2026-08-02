# Halo — a Pebble watchface

A sleek, fully-digital watchface built primarily for **Gabbro** (Pebble
Round 2, the 260x260 round display), designed around a dial-like bezel so
it reads naturally on a circular screen instead of forcing a rectangular
layout into a circle. No clock hands, nothing on screen moves on its own.

- **Numeric time, centered** — big light-weight digits, with date, a
  drawn weather icon, and current weather beneath, all vertically
  centered in the circle.
- **Weather** — temperature and a short condition ("CLOUDY", "RAIN", ...)
  fetched via the phone's companion JS and pushed to the watch over
  AppMessage (the watch itself has no network access). Refreshes on
  launch and every 30 minutes. A small drawn icon (sun, cloud, rain,
  snow, storm, or fog) accompanies it.
- **Day/night bezel** — the 96-tick edge ring doubles as a 24-hour
  daylight gauge: ticks between today's real sunrise and sunset (from the
  same weather fetch) glow warm, the rest stay dim indigo. Still fully
  static — it's a gauge, not a hand.
- **Moon phase** — a small phase-accurate moon icon, computed locally
  from the date (no network needed), appears near the bottom — only at
  night, since that's the only time it'd matter.
- **Mood theming** — one accent color runs through the weather icon, the
  moon, and the day-side bezel ticks: it shifts through dawn (orange) →
  day (blue) → dusk (violet) → night (indigo) based on real sunrise/
  sunset, and dramatic weather (storm, snow, fog, rain) overrides it with
  its own mood color regardless of time of day.
- **Chapter ring** — a thin circle just inside the bezel, separating the
  tick texture from the center readout.
- **Grain speckle** — sparse static pixels scattered in the ring between
  the chapter ring and the center text, for a bit of texture on the flat
  black face.
- **Battery ring** — a ring inside the chapter ring shows charge level
  (green/yellow/red, cyan while charging) — kept separate from the mood
  theme since it's status information, not decoration.
- **Bluetooth alert** — a small dot appears near the top only when the
  phone disconnects (with a vibration), and disappears once reconnected —
  visible only when it's actionable.
- Full color on Gabbro/Basalt/Chalk/Emery, clean white-on-black fallback
  on the 1-bit Aplite/Diorite displays (theming and bezel shading
  simplify to white/gray there, since there's no color to theme). Layout
  scales with screen size. Updates once a minute.

## Project layout

```
pebble-watchface/
  package.json          # Pebble project manifest (uuid, targets, appKeys)
  src/c/main.c           # watchface implementation
  src/js/pebble-js-app.js # companion JS: fetches weather + sun times, sends over AppMessage
```

## Weather setup

Weather and sun times use [Open-Meteo](https://open-meteo.com) (free, no
API key required) and the phone's geolocation. When you install the
watch app, the phone's Pebble app will prompt for location permission
the first time the companion JS runs — grant it, or the watchface will
show "NO LOCATION" and fall back to placeholder sunrise/sunset times for
the bezel.

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

You can also just drag `src/c/main.c`, `src/js/pebble-js-app.js`, and
`package.json` into a new [CloudPebble](https://cloudpebble.rebble.io)
project if you'd rather build in the browser — CloudPebble has a
separate "PebbleKit JS" tab for the JS file.

## Customizing

- `compute_layout` in `main.c` picks fonts/line heights based on screen
  width, and also scatters the grain speckle positions once per screen
  size.
- `draw_day_night_bezel`'s `minor_len`/`major_len` control tick length;
  `DAY_TICKS` controls its resolution (currently 15-minute steps).
- `theme_accent` is the single place that maps time-of-day period +
  weather category to a color — edit the color table there to retheme.
- `draw_weather_icon`/`draw_cloud` are simple vector glyphs per weather
  category; tweak their shapes there.
- `compute_moon_phase`/`draw_moon_phase` compute and render the moon —
  it only draws when `current_period() == PERIOD_NIGHT`.
- `GRAIN_COUNT` controls how much speckle texture is drawn.
- `draw_battery_ring`'s `inset`/`thickness` locals control how thick the
  ring is and how far it sits from the chapter ring.
- `weatherCodeInfo` in `pebble-js-app.js` controls the condition labels
  and categories; `tick_handler`'s `tm_min % 30` in `main.c` controls the
  refresh interval.
