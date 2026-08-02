#include <pebble.h>

// ---------------------------------------------------------------------------
// "Halo" watchface
//
// A sleek, fully-digital face built around concentric rings so it reads
// naturally on round displays (primarily Gabbro's 260x260 Pebble Round 2
// screen): a thin sweeping ring traces the current minute's seconds, a
// second inner ring shows battery level, and the numeric time sits
// centered with date and steps beneath it. No hands, no clutter.
// ---------------------------------------------------------------------------

static Window *s_window;
static Layer *s_canvas_layer;

static char s_time_buffer[8];
static char s_date_buffer[24];
static char s_steps_buffer[24];

static int s_seconds = 0;
static int s_battery_percent = 100;
static bool s_battery_charging = false;
static bool s_bt_connected = true;
static bool s_health_available = false;

static GFont s_time_font;
static GFont s_detail_font;
static int s_time_h;
static int s_line_h;

// ---------------------------------------------------------------------------
// Rings
// ---------------------------------------------------------------------------

static void draw_ring(GContext *ctx, GRect bounds, int inset, int thickness,
                       GColor track_color, GColor progress_color,
                       float fraction) {
  GRect ring_rect = GRect(bounds.origin.x + inset, bounds.origin.y + inset,
                           bounds.size.w - 2 * inset,
                           bounds.size.h - 2 * inset);

  graphics_context_set_fill_color(ctx, track_color);
  graphics_fill_radial(ctx, ring_rect, GOvalScaleModeFillCircle, thickness,
                        DEG_TO_TRIGANGLE(0), DEG_TO_TRIGANGLE(360));

  if (fraction <= 0) return;
  graphics_context_set_fill_color(ctx, progress_color);
  int32_t angle_end = (int32_t)(TRIG_MAX_ANGLE * fraction);
  graphics_fill_radial(ctx, ring_rect, GOvalScaleModeFillCircle, thickness,
                        DEG_TO_TRIGANGLE(0), angle_end);
}

static void draw_seconds_ring(GContext *ctx, GRect bounds) {
  GColor track = PBL_IF_COLOR_ELSE(GColorDarkGray, GColorLightGray);
  GColor accent = PBL_IF_COLOR_ELSE(GColorVividCerulean, GColorWhite);
  draw_ring(ctx, bounds, 4, 4, track, accent, s_seconds / 60.0f);
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
  draw_ring(ctx, bounds, 12, 3, track, color, s_battery_percent / 100.0f);
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

  draw_seconds_ring(ctx, bounds);
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
  s_seconds = tick_time->tm_sec;
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

  tick_timer_service_subscribe(SECOND_UNIT, tick_handler);
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
