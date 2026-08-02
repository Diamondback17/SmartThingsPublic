# Halo — a Pebble watchface

A sleek, fully-digital watchface built primarily for **Gabbro** (Pebble
Round 2, the 260x260 round display), designed around a dial-like bezel so
it reads naturally on a circular screen instead of forcing a rectangular
layout into a circle. No clock hands, nothing on screen moves on its own.

- **Numeric time, centered** — big light-weight digits, with date, a
  drawn weather icon, current weather, and a sunrise/sunset countdown
  beneath, all vertically centered in the circle.
- **Weather** — temperature and a short condition ("CLOUDY", "RAIN", ...)
  fetched via the phone's companion JS and pushed to the watch over
  AppMessage (the watch itself has no network access). Refreshes on
  launch and every 30 minutes. A small drawn icon (sun, cloud, rain,
  snow, storm, or fog) accompanies it.
- **Tap for wind** — tap/shake the watch to briefly swap the weather line
  for wind speed and direction (e.g. "12 MPH NW") for 4 seconds, then it
  reverts on its own. A one-shot reveal, not a live-updating readout.
- **Sunrise/sunset countdown** — a themed line below the weather shows
  "SUNSET 2H14M" or "SUNRISE 6H32M", counting down to whichever comes
  next (correctly rolling over past midnight using tomorrow's sunrise).
- **Day/night bezel** — the 96-tick edge ring doubles as a 24-hour
  daylight gauge: ticks between today's real sunrise and sunset glow with
  the theme accent, the rest use a dimmed version of the same color.
  Still fully static — it's a gauge, not a hand.
- **Moon phase** — a small phase-accurate moon icon, computed locally
  from the date (no network needed), appears near the bottom — only at
  night, since that's the only time it'd matter.
- **Cohesive mood theming** — a single accent color runs through the
  weather icon, the moon, the day-side bezel ticks, the chapter ring, the
  battery ring's track, and the grain speckle, so the whole face reads as
  one themed unit rather than separately-colored parts. It shifts through
  dawn (orange) → day (blue) → dusk (violet) → night (indigo) based on
  real sunrise/sunset, and dramatic weather (storm, snow, fog, rain)
  overrides it with its own mood color regardless of time of day. Status
  colors (battery charge level, the bluetooth alert) stay fixed and
  outside the theme, since they need to stay recognizable.
- **Settings page** — accessible from the watch app's entry in the phone's
  Pebble app ("Settings"). Lets you override the accent to a fixed color
  (or leave it on Auto) and pick Fahrenheit or Celsius. Persists on the
  watch (survives disconnects/reboots) and in the phone app.
- Full color on Gabbro/Basalt/Chalk/Emery, clean white-on-black fallback
  on the 1-bit Aplite/Diorite displays (theming and bezel shading
  simplify to white/gray there, since there's no color to theme). Layout
  scales with screen size. Updates once a minute.

## Project layout

```
pebble-watchface/
  package.json          # Pebble project manifest (uuid, targets, appKeys)
  src/c/main.c           # watchface implementation
  src/js/pebble-js-app.js # companion JS: weather/sun/wind fetch, AppMessage, settings page
```

## Weather setup

Weather, wind, and sun times use [Open-Meteo](https://open-meteo.com)
(free, no API key required) and the phone's geolocation. When you install
the watch app, the phone's Pebble app will prompt for location permission
the first time the companion JS runs — grant it, or the watchface will
show "NO LOCATION" and fall back to placeholder sunrise/sunset times for
the bezel and countdown.

## Settings

Open the watch app's page in the phone's Pebble app and tap **Settings**
to change the accent color or temperature unit. Saving triggers an
immediate weather refresh (needed since the unit affects the API request
itself) and pushes both settings to the watch, which persists them to
flash storage so they survive a watch reboot even before the phone
reconnects.

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
- `theme_accent` is the single place that maps accent override + weather
  category + time-of-day period to a color; `dim_color` derives the
  darker shade used for tracks/night ticks — edit the color tables there
  to retheme.
- `draw_weather_icon`/`draw_cloud` are simple vector glyphs per weather
  category; tweak their shapes there.
- `compute_moon_phase`/`draw_moon_phase` compute and render the moon —
  it only draws when `current_period() == PERIOD_NIGHT`.
- `update_countdown_text` in `main.c` controls the sunrise/sunset
  countdown format.
- `WIND_REVEAL_MS` controls how long the tap-triggered wind readout stays
  up before reverting.
- `GRAIN_COUNT` controls how much speckle texture is drawn.
- `draw_battery_ring`'s `inset`/`thickness` locals control how thick the
  ring is and how far it sits from the chapter ring.
- `weatherCodeInfo` in `pebble-js-app.js` controls the condition labels
  and categories; `tick_handler`'s `tm_min % 30` in `main.c` controls the
  refresh interval; `buildConfigHtml` is the settings page markup.
