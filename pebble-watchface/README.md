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
- **Cohesive, weather-led theming** — weather is the primary driver of the
  single accent color that runs through the weather icon, the moon, the
  day-side bezel ticks, the chapter ring, and the battery ring's track:
  storm/snow/fog/rain each get their own fixed mood color outright, clear
  skies get the full-bright time-of-day color (dawn orange → day blue →
  dusk violet → night indigo, from real sunrise/sunset), and cloudy skies
  dim that same color rather than looking identical to clear. Status
  colors (battery charge level, the bluetooth alert) stay fixed and
  outside the theme, since they need to stay recognizable.
- **Weather-shaped background texture** — the grain speckle isn't just
  dots: it renders as short streaks for rain/storm, small round flakes
  for snow, sparser haze for fog, and a plain fine grain for clear/cloudy
  — so the whole face's texture reflects the current sky, not just its
  color. Still fully static (positions are fixed, only the style per dot
  changes with the category), so nothing here animates either.
- **Weather-tinted backdrop** — the "black" background isn't flat: it's
  tinted a near-invisible shade of the current theme color (twice as dim
  as the rings, so it never competes with anything drawn on top). A storm
  reads as a darker, cooler black than a clear night, without ever
  looking like a colored background.
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
- `theme_base_color` maps accent override + weather category + time-of-day
  period to the undimmed base color; `theme_accent` adds one dim step on
  top for cloudy weather, for direct/bright display uses. Anything that
  wants a "dim" tone (night ticks, chapter ring, battery track, grain,
  `background_color`) should dim `theme_base_color`, not `theme_accent` -
  dimming an already-dimmed cloudy color risks collapsing toward black.
  `dim_color` itself uses ceiling division so it can't fully zero out a
  channel even if that happens anyway.
- `background_color` (twice-dimmed `theme_base_color`) is the "black"
  background fill — kept deliberately dim so it never becomes a visible
  colored background, just a mood shift in how black reads.
- `draw_weather_icon`/`draw_cloud` are simple vector glyphs per weather
  category; tweak their shapes there.
- `compute_moon_phase`/`draw_moon_phase` compute and render the moon —
  it only draws when `current_period() == PERIOD_NIGHT`.
- `update_countdown_text` in `main.c` controls the sunrise/sunset
  countdown format.
- `WIND_REVEAL_MS` controls how long the tap-triggered wind readout stays
  up before reverting.
- `GRAIN_COUNT` controls how much speckle texture is drawn; `draw_grain`'s
  switch on `s_weather_category` controls what shape each dot renders as
  per condition (streaks, flakes, haze, plain grain).
- `draw_battery_ring`'s `thickness` local controls how thick the ring is;
  its position now derives from `s_battery_inset` (see `compute_layout`)
  rather than a fixed value.
- `weatherCodeInfo` in `pebble-js-app.js` controls the condition labels
  and categories; `tick_handler`'s `tm_min % 30` in `main.c` controls the
  refresh interval; `buildConfigHtml` is the settings page markup.
