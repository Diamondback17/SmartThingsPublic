#include <pebble.h>

// ---------------------------------------------------------------------------
// "Halo" watchface
//
// A sleek, fully-digital face built for round displays (primarily Gabbro's
// 260x260 Pebble Round 2 screen): a static tick-mark bezel and a thin
// chapter ring give it dial-like texture, a battery ring sits just inside
// them, and numeric time/date/steps sit centered. Nothing on screen moves
// on its own — no seconds hand, no sweeping indicator.
// ---------------------------------------------------------------------------

#define GRAIN_COUNT 46

static Window *s_window;
static Layer *s_canvas_layer;

static char s_time_buffer[8];
static char s_date_buffer[24];
static char s_steps_buffer[24];

static int s_battery_percent = 100;
static bool s_battery_charging = false;
static bool s_bt_connected = true;
static bool s_health_available = false;

static GFont s_time_font;
static GFont s_detail_font;
static int s_time_h;
static int s_line_h;

static GPoint s_grain[GRAIN_COUNT];

// ---------------------------------------------------------------------------
// Static dial texture: tick bezel, chapter ring, grain
// ---------------------------------------------------------------------------

static GPoint point_on_circle(GPoint center, int32_t angle, int radius) {
  return GPoint(center.x + (int16_t)(sin_lookup(angle) * radius / TRIG_MAX_RATIO),
                center.y - (int16_t)(cos_lookup(angle) * radius / TRIG_MAX_RATIO));
}

// A fixed 60-tick bezel (5 major ticks per hour mark) around the very edge.
// Purely decorative and static - it doesn't move, so it reads as dial
// texture rather than a hand.
static void draw_tick_bezel(GContext *ctx, GRect bounds) {
  GPoint center = GPoint(bounds.size.w / 2, bounds.size.h / 2);
  int radius = (bounds.size.w < bounds.size.h ? bounds.size.w : bounds.size.h) / 2 - 3;
  int minor_len = bounds.size.w / 40;
  int major_len = bounds.size.w / 18;
  if (minor_len < 3) minor_len = 3;
  if (major_len < 7) major_len = 7;

  for (int i = 0; i < 60; i++) {
    bool major = (i % 5 == 0);
    int32_t angle = TRIG_MAX_ANGLE * i / 60;
    int len = major ? major_len : minor_len;

    GColor color = major ? PBL_IF_COLOR_ELSE(GColorLightGray, GColorWhite)
                          : PBL_IF_COLOR_ELSE(GColorDarkGray, GColorLightGray);
    graphics_context_set_stroke_color(ctx, color);
    graphics_context_set_stroke_width(ctx, major ? 3 : 1);
    graphics_draw_line(ctx, point_on_circle(center, angle, radius),
                        point_on_circle(center, angle, radius - len));
  }
}

// A thin decorative circle just inside the bezel - separates the tick
// texture from the battery ring/center readout, like a chapter ring on a
// dial.
static void draw_chapter_ring(GContext *ctx, GRect bounds) {
  int inset = bounds.size.w / 6;
  GRect ring_rect = GRect(bounds.origin.x + inset, bounds.origin.y + inset,
                           bounds.size.w - 2 * inset, bounds.size.h - 2 * inset);
  graphics_context_set_fill_color(ctx, PBL_IF_COLOR_ELSE(GColorDarkGray, GColorLightGray));
  graphics_fill_radial(ctx, ring_rect, GOvalScaleModeFillCircle, 1,
                        DEG_TO_TRIGANGLE(0), DEG_TO_TRIGANGLE(360));
}

// Sparse static speckle inside the chapter ring for a bit of grain/texture
// on the otherwise flat black face. Positions are fixed per screen size
// (computed once in compute_layout), so this never animates.
static void draw_grain(GContext *ctx) {
  graphics_context_set_stroke_color(ctx, PBL_IF_COLOR_ELSE(GColorDarkGray, GColorLightGray));
  for (int i = 0; i < GRAIN_COUNT; i++) {
    graphics_draw_pixel(ctx, s_grain[i]);
  }
}

static void draw_battery_ring(GContext *ctx, GRect bounds) {
  GColor track = PBL_IF_COLOR_ELSE(GColorDarkGray, GColorLightGray);
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

  int inset = bounds.size.w / 6 + 8;
  int thickness = 3;
  GRect ring_rect = GRect(bounds.origin.x + inset, bounds.origin.y + inset,
                           bounds.size.w - 2 * inset, bounds.size.h - 2 * inset);

  graphics_context_set_fill_color(ctx, track);
  graphics_fill_radial(ctx, ring_rect, GOvalScaleModeFillCircle, thickness,
                        DEG_TO_TRIGANGLE(0), DEG_TO_TRIGANGLE(360));

  graphics_context_set_fill_color(ctx, color);
  int32_t angle_end = (int32_t)(TRIG_MAX_ANGLE * s_battery_percent / 100);
  graphics_fill_radial(ctx, ring_rect, GOvalScaleModeFillCircle, thickness,
                        DEG_TO_TRIGANGLE(0), angle_end);
}

// A small dot at 12 o'clock, visible only when the phone is disconnected -
// present only when it's actionable, invisible otherwise.
static void draw_bluetooth_alert(GContext *ctx, GRect bounds) {
  if (s_bt_connected) return;
  GPoint center = GPoint(bounds.size.w / 2, bounds.origin.y + 22);
  GColor color = PBL_IF_COLOR_ELSE(GColorRed, GColorWhite);
  graphics_context_set_fill_color(ctx, color);
  graphics_fill_circle(ctx, center, 3);
}

// ---------------------------------------------------------------------------
// Center readout
// ---------------------------------------------------------------------------

static void compute_layout(GRect bounds) {
  bool big = bounds.size.w >= 200;
  s_time_font = fonts_get_system_font(FONT_KEY_BITHAM_42_LIGHT);
  s_detail_font =
      fonts_get_system_font(big ? FONT_KEY_GOTHIC_18 : FONT_KEY_GOTHIC_14);
  s_time_h = big ? 54 : 46;
  s_line_h = big ? 22 : 16;

  // Scatter the grain speckle once per screen size, in the annulus between
  // the center text block and the chapter ring, so it never overlaps
  // either and never needs recomputing on every redraw.
  GPoint center = GPoint(bounds.size.w / 2, bounds.size.h / 2);
  int r_max = bounds.size.w / 2 - bounds.size.w / 6 - 6;
  int r_min = s_time_h;
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
  int lines_below = s_health_available ? 2 : 1;  // date, (steps)
  int total_h = s_time_h + lines_below * s_line_h;
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

  if (s_health_available) {
    GRect steps_rect = GRect(0, y, bounds.size.w, s_line_h);
    graphics_draw_text(ctx, s_steps_buffer, s_detail_font, steps_rect,
                        GTextOverflowModeFill, GTextAlignmentCenter, NULL);
  }
}

// ---------------------------------------------------------------------------
// Canvas
// ---------------------------------------------------------------------------

static void canvas_update_proc(Layer *layer, GContext *ctx) {
  GRect bounds = layer_get_bounds(layer);

  graphics_context_set_fill_color(ctx, GColorBlack);
  graphics_fill_rect(ctx, bounds, 0, GCornerNone);

  draw_tick_bezel(ctx, bounds);
  draw_chapter_ring(ctx, bounds);
  draw_grain(ctx);
  draw_battery_ring(ctx, bounds);
  draw_center(ctx, bounds);
  draw_bluetooth_alert(ctx, bounds);
}

// ---------------------------------------------------------------------------
// Services
// ---------------------------------------------------------------------------

static void update_time_buffers(struct tm *tick_time) {
  strftime(s_time_buffer, sizeof(s_time_buffer),
           clock_is_24h_style() ? "%H:%M" : "%I:%M", tick_time);
  strftime(s_date_buffer, sizeof(s_date_buffer), "%a %d %b", tick_time);
}

static void update_steps(void) {
  if (!s_health_available) return;
  HealthValue steps = health_service_sum_today(HealthMetricStepCount);
  snprintf(s_steps_buffer, sizeof(s_steps_buffer), "%d STEPS", (int)steps);
}

static void tick_handler(struct tm *tick_time, TimeUnits units_changed) {
  update_time_buffers(tick_time);
  update_steps();
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

static void health_event_handler(HealthEventType event, void *context) {
  if (event == HealthEventMovementUpdate || event == HealthEventSignificantUpdate) {
    update_steps();
    layer_mark_dirty(s_canvas_layer);
  }
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
  update_steps();

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
  strcpy(s_steps_buffer, "-- STEPS");

#if defined(PBL_HEALTH)
  s_health_available = true;
  health_service_events_subscribe(health_event_handler, NULL);
#else
  s_health_available = false;
#endif

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
}

static void deinit(void) {
  tick_timer_service_unsubscribe();
  battery_state_service_unsubscribe();
  connection_service_unsubscribe();
#if defined(PBL_HEALTH)
  health_service_events_unsubscribe();
#endif
  window_destroy(s_window);
}

int main(void) {
  init();
  app_event_loop();
  deinit();
  return 0;
}
