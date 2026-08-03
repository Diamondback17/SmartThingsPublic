#include <pebble.h>
#include <math.h>

// ---------------------------------------------------------------------------
// "Halo" watchface
//
// A sleek, fully-digital face built for round displays (primarily Gabbro's
// 260x260 Pebble Round 2 screen). The bezel doubles as a 24-hour day/night
// gauge lit from real sunrise/sunset times, a drawn weather icon and a
// computed moon phase (visible at night) add character, and one accent
// color threads through the icon, moon, bezel, chapter ring, battery
// track, and grain for a cohesive weather/time-driven mood - overridable
// from the watch's Settings page. A countdown to the next sunrise/sunset
// sits below the weather line, and tapping the watch briefly swaps that
// line for wind speed/direction. Nothing on screen moves on its own - no
// seconds hand, no sweeping indicator. Weather comes from the phone's
// companion JS (src/js/pebble-js-app.js) over AppMessage, since the watch
// has no network access of its own.
// ---------------------------------------------------------------------------

#define GRAIN_COUNT 46
#define DAY_TICKS 96  // 15-minute resolution across 24 hours
#define WIND_REVEAL_MS 4000

// Must match the "appKeys" order in package.json/appinfo.json.
#define KEY_TEMPERATURE 0
#define KEY_CONDITION 1
#define KEY_REQUEST 2
#define KEY_CATEGORY 3
#define KEY_SUNRISE 4
#define KEY_SUNSET 5
#define KEY_SUNRISE_TOMORROW 6
#define KEY_WIND_SPEED 7
#define KEY_WIND_DIR 8
#define KEY_UNIT 9
#define KEY_ACCENT_MODE 10

#define PERSIST_KEY_UNIT 100
#define PERSIST_KEY_ACCENT 101

typedef enum {
  WX_CLEAR = 0,
  WX_CLOUDY = 1,
  WX_FOG = 2,
  WX_RAIN = 3,
  WX_SNOW = 4,
  WX_STORM = 5,
} WeatherCategory;

typedef enum {
  PERIOD_NIGHT,
  PERIOD_DAWN,
  PERIOD_DAY,
  PERIOD_DUSK,
} ThemePeriod;

typedef enum {
  ACCENT_AUTO = 0,
  ACCENT_BLUE = 1,
  ACCENT_ORANGE = 2,
  ACCENT_VIOLET = 3,
  ACCENT_GREEN = 4,
  ACCENT_RED = 5,
  ACCENT_GOLD = 6,
  ACCENT_MONO = 7,
} AccentMode;

static Window *s_window;
static Layer *s_canvas_layer;

static char s_time_buffer[8];
static char s_date_buffer[24];
static char s_weather_buffer[32];
static char s_wind_buffer[32];
static char s_countdown_buffer[24];
static WeatherCategory s_weather_category = WX_CLOUDY;
static bool s_unit_celsius = false;
static AccentMode s_accent_mode = ACCENT_AUTO;

static int s_battery_percent = 100;
static bool s_battery_charging = false;
static bool s_bt_connected = true;

static int s_now_min = 8 * 60;               // minutes since local midnight
static int s_sunrise_min = 6 * 60;           // placeholders until first weather push
static int s_sunset_min = 20 * 60;
static int s_sunrise_tomorrow_min = 6 * 60;

static bool s_show_wind = false;
static AppTimer *s_wind_timer;

static GFont s_time_font;
static GFont s_detail_font;
static int s_time_h;
static int s_line_h;
static int s_icon_row_h;
static int s_battery_inset;
static int s_chapter_inset;

static GPoint s_grain[GRAIN_COUNT];

// ---------------------------------------------------------------------------
// Theming: time-of-day period + weather mood -> accent color
// ---------------------------------------------------------------------------

static ThemePeriod current_period(void) {
  if (s_now_min >= s_sunrise_min - 30 && s_now_min <= s_sunrise_min + 30) {
    return PERIOD_DAWN;
  }
  if (s_now_min >= s_sunset_min - 30 && s_now_min <= s_sunset_min + 30) {
    return PERIOD_DUSK;
  }
  if (s_now_min > s_sunrise_min + 30 && s_now_min < s_sunset_min - 30) {
    return PERIOD_DAY;
  }
  return PERIOD_NIGHT;
}

// A darker shade of the same hue, for background/track elements that
// should carry the theme without competing with the bright accent uses.
// Uses ceiling division so a channel that's already nonzero never gets
// dimmed all the way to 0 - if this ever gets applied twice to the same
// color by mistake, the result is "very dim" rather than invisible black
// against the black background.
static GColor dim_color(GColor c) {
#if !defined(PBL_COLOR)
  return GColorDarkGray;
#else
  GColor out = c;
  out.r = (c.r + 1) / 2;
  out.g = (c.g + 1) / 2;
  out.b = (c.b + 1) / 2;
  return out;
#endif
}

// A manual accent override wins outright. Otherwise weather is the
// primary driver: dramatic conditions (storm/snow/fog/rain) get their
// own fixed mood color outright, and clear/cloudy skies get the
// time-of-day color. This is the *undimmed* base - callers that want
// their own "dim" variant (night ticks, chapter ring, battery track,
// grain) should dim this, not theme_accent() below, or cloudy weather
// would get dimmed twice and collapse to invisible black.
static GColor theme_base_color(void) {
#if !defined(PBL_COLOR)
  return GColorWhite;
#else
  switch (s_accent_mode) {
    case ACCENT_BLUE: return GColorVividCerulean;
    case ACCENT_ORANGE: return GColorSunsetOrange;
    case ACCENT_VIOLET: return GColorVividViolet;
    case ACCENT_GREEN: return GColorGreen;
    case ACCENT_RED: return GColorRed;
    case ACCENT_GOLD: return GColorIcterine;
    case ACCENT_MONO: return GColorWhite;
    case ACCENT_AUTO:
    default: break;
  }

  switch (s_weather_category) {
    case WX_STORM: return GColorJazzberryJam;
    case WX_SNOW: return GColorPictonBlue;
    case WX_FOG: return GColorLightGray;
    case WX_RAIN: return GColorTiffanyBlue;
    default: break;
  }

  switch (current_period()) {
    case PERIOD_DAWN: return GColorSunsetOrange;
    case PERIOD_DAY: return GColorVividCerulean;
    case PERIOD_DUSK: return GColorVividViolet;
    default: return GColorDukeBlue;
  }
#endif
}

// The color actually used for bright/direct display (weather icon, moon,
// lit bezel ticks, countdown text): the base color, dimmed once more if
// it's cloudy so cloudy days read as duller than clear ones.
static GColor theme_accent(void) {
  GColor base = theme_base_color();
#if !defined(PBL_COLOR)
  return base;
#else
  return s_weather_category == WX_CLOUDY ? dim_color(base) : base;
#endif
}

// A near-black wash tinted by the current weather/time theme, twice as
// dim as the ring/chapter-ring tier so it reads as "black with a mood"
// rather than a visible color - a storm should feel darker and bluer
// than a clear night without the background ever competing with
// anything drawn on top of it.
static GColor background_color(void) {
  return dim_color(dim_color(theme_base_color()));
}

// ---------------------------------------------------------------------------
// Static dial texture: day/night bezel, chapter ring, grain
// ---------------------------------------------------------------------------

static GPoint point_on_circle(GPoint center, int32_t angle, int radius) {
  return GPoint(center.x + (int16_t)(sin_lookup(angle) * radius / TRIG_MAX_RATIO),
                center.y - (int16_t)(cos_lookup(angle) * radius / TRIG_MAX_RATIO));
}

// A 96-tick bezel around the very edge, one revolution per 24 hours
// (12 o'clock = midnight, 6 o'clock = noon). Ticks falling within today's
// daylight window use the bright theme accent; night ticks use a dimmed
// version of the same color, tying the day/night gauge into the overall
// mood theme. It's still fixed once drawn for the current minute - it
// doesn't sweep like a hand.
static void draw_day_night_bezel(GContext *ctx, GRect bounds) {
  GPoint center = GPoint(bounds.size.w / 2, bounds.size.h / 2);
  int radius = (bounds.size.w < bounds.size.h ? bounds.size.w : bounds.size.h) / 2 - 3;
  int minor_len = bounds.size.w / 40;
  int major_len = bounds.size.w / 18;
  if (minor_len < 3) minor_len = 3;
  if (major_len < 7) major_len = 7;

  GColor day_color = theme_accent();
  // Dim the undimmed base, not theme_accent() - it may already be dimmed
  // once for cloudy weather, and dimming that again risks collapsing to
  // black (invisible against the background).
  GColor night_color = dim_color(theme_base_color());

  for (int i = 0; i < DAY_TICKS; i++) {
    bool major = (i % 4 == 0);  // on the hour
    int32_t angle = TRIG_MAX_ANGLE * i / DAY_TICKS;
    int len = major ? major_len : minor_len;
    int tick_minutes = i * (24 * 60 / DAY_TICKS);
    bool daylight = tick_minutes >= s_sunrise_min && tick_minutes <= s_sunset_min;

    graphics_context_set_stroke_color(ctx, daylight ? day_color : night_color);
    graphics_context_set_stroke_width(ctx, major ? 3 : 1);
    graphics_draw_line(ctx, point_on_circle(center, angle, radius),
                        point_on_circle(center, angle, radius - len));
  }
}

// A thin decorative circle just inside the bezel - separates the tick
// texture from the battery ring/center readout, like a chapter ring on a
// dial.
static void draw_chapter_ring(GContext *ctx, GRect bounds) {
  GRect ring_rect = GRect(bounds.origin.x + s_chapter_inset, bounds.origin.y + s_chapter_inset,
                           bounds.size.w - 2 * s_chapter_inset, bounds.size.h - 2 * s_chapter_inset);
  graphics_context_set_fill_color(ctx, dim_color(theme_base_color()));
  graphics_fill_radial(ctx, ring_rect, GOvalScaleModeFillCircle, 1,
                        DEG_TO_TRIGANGLE(0), DEG_TO_TRIGANGLE(360));
}

// Sparse static speckle inside the chapter ring, styled per current
// weather category so the background itself reads as the condition
// rather than just a color: rain/storm get short streaks, snow gets
// small round flakes, fog gets sparser haze, clear/cloudy get a plain
// fine grain. Positions are fixed per screen size (computed once in
// compute_layout) and the style is picked fresh each redraw from the
// live category, but nothing here animates - it's still a static texture,
// just one that matches whatever the sky is doing right now.
static void draw_grain(GContext *ctx) {
  GColor color = dim_color(theme_base_color());

  switch (s_weather_category) {
    case WX_RAIN:
    case WX_STORM:
      graphics_context_set_stroke_color(ctx, color);
      graphics_context_set_stroke_width(ctx, 1);
      for (int i = 0; i < GRAIN_COUNT; i++) {
        GPoint p = s_grain[i];
        graphics_draw_line(ctx, p, GPoint(p.x - 2, p.y + 5));
      }
      break;

    case WX_SNOW:
      graphics_context_set_fill_color(ctx, color);
      for (int i = 0; i < GRAIN_COUNT; i++) {
        graphics_fill_circle(ctx, s_grain[i], 1);
      }
      break;

    case WX_FOG:
      graphics_context_set_stroke_color(ctx, color);
      for (int i = 0; i < GRAIN_COUNT; i += 2) {  // sparser - hazy rather than grainy
        graphics_draw_pixel(ctx, s_grain[i]);
      }
      break;

    case WX_CLEAR:
    case WX_CLOUDY:
    default:
      graphics_context_set_stroke_color(ctx, color);
      for (int i = 0; i < GRAIN_COUNT; i++) {
        graphics_draw_pixel(ctx, s_grain[i]);
      }
      break;
  }
}

static void draw_battery_ring(GContext *ctx, GRect bounds) {
  GColor track = dim_color(theme_base_color());
  GColor color;
  if (s_battery_charging) {
    color = PBL_IF_COLOR_ELSE(GColorCyan, GColorWhite);
  } else if (s_battery_percent > 50) {
    color = PBL_IF_COLOR_ELSE(GColorGreen, GColorWhite);
  } else if (s_battery_percent > 20) {
    color = PBL_IF_COLOR_ELSE(GColorYellow, GColorWhite);
  } else {
    color = PBL_IF_COLOR_ELSE(GColorRed, GColorWhite);
  }

  int thickness = 3;
  GRect ring_rect = GRect(bounds.origin.x + s_battery_inset, bounds.origin.y + s_battery_inset,
                           bounds.size.w - 2 * s_battery_inset, bounds.size.h - 2 * s_battery_inset);

  graphics_context_set_fill_color(ctx, track);
  graphics_fill_radial(ctx, ring_rect, GOvalScaleModeFillCircle, thickness,
                        DEG_TO_TRIGANGLE(0), DEG_TO_TRIGANGLE(360));

  graphics_context_set_fill_color(ctx, color);
  int32_t angle_end = (int32_t)(TRIG_MAX_ANGLE * s_battery_percent / 100);
  graphics_fill_radial(ctx, ring_rect, GOvalScaleModeFillCircle, thickness,
                        DEG_TO_TRIGANGLE(0), angle_end);
}

// A small dot at 12 o'clock, visible only when the phone is disconnected -
// present only when it's actionable, invisible otherwise. Always red:
// alerts stay a fixed, recognizable color regardless of theme.
static void draw_bluetooth_alert(GContext *ctx, GRect bounds) {
  if (s_bt_connected) return;
  GPoint center = GPoint(bounds.size.w / 2, bounds.origin.y + 22);
  GColor color = PBL_IF_COLOR_ELSE(GColorRed, GColorWhite);
  graphics_context_set_fill_color(ctx, color);
  graphics_fill_circle(ctx, center, 3);
}

// ---------------------------------------------------------------------------
// Moon phase (computed locally, no network needed)
// ---------------------------------------------------------------------------

// Synodic month reckoned from a known new moon (2000-01-06 18:14 UTC).
// Returns phase in [0, 1): 0/1 = new moon, 0.5 = full moon.
static double compute_moon_phase(void) {
  const double reference_new_moon = 947182440.0;  // unix time
  const double synodic_days = 29.530588853;
  double days_since_new = ((double)time(NULL) - reference_new_moon) / 86400.0;
  double cycles = days_since_new / synodic_days;
  double phase = cycles - floor(cycles);
  return phase;
}

// Renders the moon as a lit disc with a second, offset disc painted in the
// background color to carve out the shadow - a standard small-icon trick
// that cycles correctly through new/crescent/quarter/gibbous/full/back to
// new as phase goes 0 -> 1.
static void draw_moon_phase(GContext *ctx, GPoint center, int r) {
  double phase = compute_moon_phase();
  int32_t angle = (int32_t)(phase * TRIG_MAX_ANGLE);
  int32_t cos_val = cos_lookup(angle);
  int offset = (int)(((int32_t)(TRIG_MAX_RATIO - cos_val)) * r / TRIG_MAX_RATIO);

  graphics_context_set_fill_color(ctx, theme_accent());
  graphics_fill_circle(ctx, center, r);

  graphics_context_set_fill_color(ctx, background_color());
  graphics_fill_circle(ctx, GPoint(center.x - offset, center.y), r);

  GColor outline = PBL_IF_COLOR_ELSE(GColorDarkGray, GColorLightGray);
  graphics_context_set_stroke_color(ctx, outline);
  graphics_draw_circle(ctx, center, r);
}

// ---------------------------------------------------------------------------
// Weather icon
// ---------------------------------------------------------------------------

static void draw_cloud(GContext *ctx, GPoint center, int r, GColor color) {
  graphics_context_set_fill_color(ctx, color);
  graphics_fill_circle(ctx, GPoint(center.x - r / 2, center.y + r / 4), r * 6 / 10);
  graphics_fill_circle(ctx, GPoint(center.x + r / 2, center.y + r / 4), r * 6 / 10);
  graphics_fill_circle(ctx, GPoint(center.x, center.y - r / 5), r * 7 / 10);
  graphics_fill_rect(ctx, GRect(center.x - r, center.y, 2 * r, r / 2), 0, GCornerNone);
}

static void draw_weather_icon(GContext *ctx, GPoint center, int r) {
  GColor accent = theme_accent();

  switch (s_weather_category) {
    case WX_CLEAR:
      graphics_context_set_fill_color(ctx, accent);
      graphics_fill_circle(ctx, center, r * 6 / 10);
      graphics_context_set_stroke_color(ctx, accent);
      graphics_context_set_stroke_width(ctx, 1);
      for (int i = 0; i < 8; i++) {
        int32_t angle = TRIG_MAX_ANGLE * i / 8;
        graphics_draw_line(ctx, point_on_circle(center, angle, r * 8 / 10),
                            point_on_circle(center, angle, r + 2));
      }
      break;

    case WX_FOG:
      graphics_context_set_stroke_color(ctx, accent);
      graphics_context_set_stroke_width(ctx, 2);
      for (int i = -1; i <= 1; i++) {
        graphics_draw_line(ctx, GPoint(center.x - r, center.y + i * r / 2),
                            GPoint(center.x + r, center.y + i * r / 2));
      }
      break;

    case WX_RAIN:
      draw_cloud(ctx, center, r * 7 / 10, accent);
      graphics_context_set_stroke_color(ctx, accent);
      graphics_context_set_stroke_width(ctx, 2);
      for (int i = -1; i <= 1; i++) {
        GPoint top = GPoint(center.x + i * r / 2, center.y + r / 2);
        graphics_draw_line(ctx, top, GPoint(top.x - 2, top.y + r / 2));
      }
      break;

    case WX_SNOW:
      draw_cloud(ctx, center, r * 7 / 10, accent);
      graphics_context_set_fill_color(ctx, accent);
      for (int i = -1; i <= 1; i++) {
        graphics_fill_circle(ctx, GPoint(center.x + i * r / 2, center.y + r / 2), 1);
      }
      break;

    case WX_STORM:
      draw_cloud(ctx, center, r * 7 / 10, accent);
      graphics_context_set_stroke_color(ctx, accent);
      graphics_context_set_stroke_width(ctx, 2);
      graphics_draw_line(ctx, GPoint(center.x, center.y + r / 3),
                          GPoint(center.x - r / 4, center.y + r * 7 / 10));
      graphics_draw_line(ctx, GPoint(center.x - r / 4, center.y + r * 7 / 10),
                          GPoint(center.x + r / 6, center.y + r * 7 / 10));
      graphics_draw_line(ctx, GPoint(center.x + r / 6, center.y + r * 7 / 10),
                          GPoint(center.x - r / 8, center.y + r * 11 / 10));
      break;

    case WX_CLOUDY:
    default:
      draw_cloud(ctx, center, r, accent);
      break;
  }
}

// ---------------------------------------------------------------------------
// Center readout
// ---------------------------------------------------------------------------

static void compute_layout(GRect bounds) {
  bool big = bounds.size.w >= 200;
  s_time_font = fonts_get_system_font(FONT_KEY_BITHAM_42_LIGHT);
  s_detail_font =
      fonts_get_system_font(big ? FONT_KEY_GOTHIC_18 : FONT_KEY_GOTHIC_14);
  s_time_h = big ? 50 : 42;
  s_line_h = big ? 20 : 12;
  s_icon_row_h = big ? 26 : 18;

  // time, date, icon, weather/wind, countdown - keep in sync with draw_center.
  int content_h = s_time_h + 3 * s_line_h + s_icon_row_h;
  int content_half = content_h / 2 + 6;  // safety margin

  // Derive the battery ring's position from how tall the center text
  // block actually is, with a fixed clearance gap, instead of guessing a
  // screen-fraction inset independently - that's what let the countdown
  // row (added later) end up overlapping the ring despite an "adequate"
  // fixed margin against the chapter ring, which isn't the boundary that
  // actually matters here. The chapter ring then wraps just outside
  // whatever the battery ring ends up being, so the two always stay
  // correctly nested regardless of how content_h changes.
  const int battery_thickness = 3;
  const int battery_clearance = 6;
  int min_inset_floor = bounds.size.w / 8;
  s_battery_inset = bounds.size.h / 2 - content_half - battery_thickness - battery_clearance;
  if (s_battery_inset < min_inset_floor) s_battery_inset = min_inset_floor;
  s_chapter_inset = s_battery_inset - 4;
  if (s_chapter_inset < 4) s_chapter_inset = 4;

  // Scatter the grain speckle once per screen size, in the annulus between
  // the center text block and the chapter ring, so it never overlaps
  // either and never needs recomputing on every redraw.
  GPoint center = GPoint(bounds.size.w / 2, bounds.size.h / 2);
  int r_max = bounds.size.w / 2 - s_chapter_inset - 6;
  int r_min = content_half;
  if (r_min > r_max - 10) r_min = r_max / 3;
  int span = r_max - r_min;
  if (span < 1) span = 1;

  for (int i = 0; i < GRAIN_COUNT; i++) {
    int32_t angle = rand() % TRIG_MAX_ANGLE;
    int r = r_min + rand() % span;
    s_grain[i] = point_on_circle(center, angle, r);
  }
}

static void draw_center(GContext *ctx, GRect bounds) {
  // time, date, icon, weather/wind, countdown
  int total_h = s_time_h + s_line_h + s_icon_row_h + s_line_h + s_line_h;
  int top = bounds.size.h / 2 - total_h / 2;

  graphics_context_set_text_color(ctx, GColorWhite);
  GRect time_rect = GRect(0, top, bounds.size.w, s_time_h);
  graphics_draw_text(ctx, s_time_buffer, s_time_font, time_rect,
                      GTextOverflowModeFill, GTextAlignmentCenter, NULL);

  GColor dim = PBL_IF_COLOR_ELSE(GColorLightGray, GColorWhite);
  graphics_context_set_text_color(ctx, dim);
  int y = top + s_time_h;

  GRect date_rect = GRect(0, y, bounds.size.w, s_line_h);
  graphics_draw_text(ctx, s_date_buffer, s_detail_font, date_rect,
                      GTextOverflowModeFill, GTextAlignmentCenter, NULL);
  y += s_line_h;

  GPoint icon_center = GPoint(bounds.size.w / 2, y + s_icon_row_h / 2);
  draw_weather_icon(ctx, icon_center, s_icon_row_h / 2 - 2);
  y += s_icon_row_h;

  graphics_context_set_text_color(ctx, dim);
  GRect weather_rect = GRect(0, y, bounds.size.w, s_line_h);
  graphics_draw_text(ctx, s_show_wind ? s_wind_buffer : s_weather_buffer, s_detail_font,
                      weather_rect, GTextOverflowModeFill, GTextAlignmentCenter, NULL);
  y += s_line_h;

  graphics_context_set_text_color(ctx, theme_accent());
  GRect countdown_rect = GRect(0, y, bounds.size.w, s_line_h);
  graphics_draw_text(ctx, s_countdown_buffer, s_detail_font, countdown_rect,
                      GTextOverflowModeFill, GTextAlignmentCenter, NULL);
}

// ---------------------------------------------------------------------------
// Canvas
// ---------------------------------------------------------------------------

static void canvas_update_proc(Layer *layer, GContext *ctx) {
  GRect bounds = layer_get_bounds(layer);

  graphics_context_set_fill_color(ctx, background_color());
  graphics_fill_rect(ctx, bounds, 0, GCornerNone);

  draw_day_night_bezel(ctx, bounds);
  draw_chapter_ring(ctx, bounds);
  draw_grain(ctx);
  draw_battery_ring(ctx, bounds);
  draw_center(ctx, bounds);
  draw_bluetooth_alert(ctx, bounds);

  // Only worth showing when it'd actually be visible in the sky.
  if (current_period() == PERIOD_NIGHT) {
    int bottom_offset = PBL_IF_ROUND_ELSE((int)(bounds.size.h * 0.19), 20);
    GPoint moon_center = GPoint(bounds.size.w / 2, bounds.size.h - bottom_offset);
    draw_moon_phase(ctx, moon_center, bounds.size.w / 22 < 6 ? 6 : bounds.size.w / 22);
  }
}

// ---------------------------------------------------------------------------
// Services
// ---------------------------------------------------------------------------

static void update_countdown_text(void) {
  int diff;
  const char *label;
  if (s_now_min < s_sunrise_min) {
    diff = s_sunrise_min - s_now_min;
    label = "SUNRISE";
  } else if (s_now_min < s_sunset_min) {
    diff = s_sunset_min - s_now_min;
    label = "SUNSET";
  } else {
    diff = (s_sunrise_tomorrow_min + 24 * 60) - s_now_min;
    label = "SUNRISE";
  }
  if (diff < 0) diff = 0;
  snprintf(s_countdown_buffer, sizeof(s_countdown_buffer), "%s %dH%02dM", label,
           diff / 60, diff % 60);
}

static void update_time_buffers(struct tm *tick_time) {
  strftime(s_time_buffer, sizeof(s_time_buffer),
           clock_is_24h_style() ? "%H:%M" : "%I:%M", tick_time);
  strftime(s_date_buffer, sizeof(s_date_buffer), "%a %d %b", tick_time);
  s_now_min = tick_time->tm_hour * 60 + tick_time->tm_min;
  update_countdown_text();
}

// Nudges the phone's companion JS to refetch and push a fresh weather
// reading. The message content doesn't matter - the JS side just listens
// for any inbound AppMessage as its cue to refetch.
static void request_weather_update(void) {
  DictionaryIterator *iter;
  if (app_message_outbox_begin(&iter) != APP_MSG_OK) return;
  dict_write_uint8(iter, KEY_REQUEST, 1);
  app_message_outbox_send();
}

static void tick_handler(struct tm *tick_time, TimeUnits units_changed) {
  update_time_buffers(tick_time);
  if (tick_time->tm_min % 30 == 0) {
    request_weather_update();
  }
  layer_mark_dirty(s_canvas_layer);
}

static void wind_timer_callback(void *data) {
  s_show_wind = false;
  layer_mark_dirty(s_canvas_layer);
}

// Briefly swaps the weather line for wind speed/direction - an
// interactive detail rather than a continuously-updating hand, so it's a
// one-shot reveal that reverts itself.
static void accel_tap_handler(AccelAxisType axis, int32_t direction) {
  s_show_wind = true;
  layer_mark_dirty(s_canvas_layer);
  if (s_wind_timer) app_timer_cancel(s_wind_timer);
  s_wind_timer = app_timer_register(WIND_REVEAL_MS, wind_timer_callback, NULL);
}

static void inbox_received_handler(DictionaryIterator *iterator, void *context) {
  Tuple *temp_tuple = dict_find(iterator, KEY_TEMPERATURE);
  Tuple *cond_tuple = dict_find(iterator, KEY_CONDITION);
  Tuple *category_tuple = dict_find(iterator, KEY_CATEGORY);
  Tuple *sunrise_tuple = dict_find(iterator, KEY_SUNRISE);
  Tuple *sunset_tuple = dict_find(iterator, KEY_SUNSET);
  Tuple *sunrise_tomorrow_tuple = dict_find(iterator, KEY_SUNRISE_TOMORROW);
  Tuple *wind_speed_tuple = dict_find(iterator, KEY_WIND_SPEED);
  Tuple *wind_dir_tuple = dict_find(iterator, KEY_WIND_DIR);
  Tuple *unit_tuple = dict_find(iterator, KEY_UNIT);
  Tuple *accent_tuple = dict_find(iterator, KEY_ACCENT_MODE);

  if (unit_tuple) {
    s_unit_celsius = unit_tuple->value->int32 != 0;
    persist_write_bool(PERSIST_KEY_UNIT, s_unit_celsius);
  }
  if (accent_tuple) {
    s_accent_mode = (AccentMode)accent_tuple->value->int32;
    persist_write_int(PERSIST_KEY_ACCENT, s_accent_mode);
  }
  if (temp_tuple && cond_tuple) {
    snprintf(s_weather_buffer, sizeof(s_weather_buffer), "%d°%s %s",
             (int)temp_tuple->value->int32, s_unit_celsius ? "C" : "F",
             cond_tuple->value->cstring);
  }
  if (category_tuple) {
    s_weather_category = (WeatherCategory)category_tuple->value->int32;
  }
  if (wind_speed_tuple && wind_dir_tuple) {
    snprintf(s_wind_buffer, sizeof(s_wind_buffer), "%d %s %s",
             (int)wind_speed_tuple->value->int32, s_unit_celsius ? "KMH" : "MPH",
             wind_dir_tuple->value->cstring);
  }
  if (sunrise_tuple) {
    s_sunrise_min = (int)sunrise_tuple->value->int32;
  }
  if (sunset_tuple) {
    s_sunset_min = (int)sunset_tuple->value->int32;
  }
  if (sunrise_tomorrow_tuple) {
    s_sunrise_tomorrow_min = (int)sunrise_tomorrow_tuple->value->int32;
  }
  if (sunrise_tuple || sunset_tuple || sunrise_tomorrow_tuple) {
    update_countdown_text();
  }

  layer_mark_dirty(s_canvas_layer);
}

static void battery_handler(BatteryChargeState state) {
  s_battery_percent = state.charge_percent;
  s_battery_charging = state.is_charging;
  layer_mark_dirty(s_canvas_layer);
}

static void bt_handler(bool connected) {
  if (s_bt_connected && !connected) {
    vibes_double_pulse();
  }
  s_bt_connected = connected;
  layer_mark_dirty(s_canvas_layer);
}

// ---------------------------------------------------------------------------
// Window lifecycle
// ---------------------------------------------------------------------------

static void window_load(Window *window) {
  Layer *root = window_get_root_layer(window);
  GRect bounds = layer_get_bounds(root);

  compute_layout(bounds);

  s_canvas_layer = layer_create(bounds);
  layer_set_update_proc(s_canvas_layer, canvas_update_proc);
  layer_add_child(root, s_canvas_layer);

  time_t now = time(NULL);
  struct tm *tick_time = localtime(&now);
  update_time_buffers(tick_time);

  BatteryChargeState state = battery_state_service_peek();
  battery_handler(state);

  s_bt_connected = connection_service_peek_pebble_app_connection();
}

static void window_unload(Window *window) {
  layer_destroy(s_canvas_layer);
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

static void init(void) {
  srand((unsigned int)time(NULL));
  strcpy(s_weather_buffer, "-- WEATHER");
  strcpy(s_wind_buffer, "-- WIND");

  if (persist_exists(PERSIST_KEY_UNIT)) {
    s_unit_celsius = persist_read_bool(PERSIST_KEY_UNIT);
  }
  if (persist_exists(PERSIST_KEY_ACCENT)) {
    s_accent_mode = (AccentMode)persist_read_int(PERSIST_KEY_ACCENT);
  }

  s_window = window_create();
  window_set_window_handlers(s_window, (WindowHandlers){
                                            .load = window_load,
                                            .unload = window_unload,
                                        });
  window_set_background_color(s_window, GColorBlack);
  window_stack_push(s_window, true);

  tick_timer_service_subscribe(MINUTE_UNIT, tick_handler);
  battery_state_service_subscribe(battery_handler);
  connection_service_subscribe((ConnectionHandlers){
      .pebble_app_connection_handler = bt_handler,
  });
  accel_tap_service_subscribe(accel_tap_handler);

  app_message_register_inbox_received(inbox_received_handler);
  app_message_open(app_message_inbox_size_maximum(),
                    app_message_outbox_size_maximum());
  request_weather_update();
}

static void deinit(void) {
  tick_timer_service_unsubscribe();
  battery_state_service_unsubscribe();
  connection_service_unsubscribe();
  accel_tap_service_unsubscribe();
  window_destroy(s_window);
}

int main(void) {
  init();
  app_event_loop();
  deinit();
  return 0;
}
