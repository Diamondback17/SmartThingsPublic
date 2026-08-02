#include <pebble.h>

// ---------------------------------------------------------------------------
// "Cyber Matrix" watchface
//
// Digital-rain background, a glitching HUD clock readout, a radial battery
// ring, a bluetooth status blip and a shake-to-glitch easter egg.
// ---------------------------------------------------------------------------

#define CELL_W 12
#define CELL_H 14
#define MAX_COLS 20
#define MAX_ROWS 20
#define GLITCH_FRAME_MS 70
#define GLITCH_FRAME_COUNT 5

static Window *s_window;
static Layer *s_canvas_layer;

static char s_time_buffer[8];
static char s_seconds_buffer[4];
static char s_date_buffer[24];
static char s_steps_buffer[24];

static int s_cols, s_rows;
static char s_grid[MAX_ROWS][MAX_COLS];
static int8_t s_drop_head[MAX_COLS];
static int8_t s_drop_len[MAX_COLS];
static uint8_t s_drop_speed[MAX_COLS];
static uint8_t s_drop_counter[MAX_COLS];

static int s_battery_percent = 100;
static bool s_battery_charging = false;
static bool s_bt_connected = true;
static bool s_health_available = false;

static AppTimer *s_glitch_timer;
static int s_glitch_frames_left = 0;

static const char s_charset[] =
    "01$#%&@*+=-<>/\\|{}[]?^~ABCDEFGHIJKLMNOPQRSTUVWXYZ";
static const int s_charset_len = sizeof(s_charset) - 1;

// ---------------------------------------------------------------------------
// Matrix rain
// ---------------------------------------------------------------------------

static char random_char(void) {
  return s_charset[rand() % s_charset_len];
}

static void seed_column(int col) {
  s_drop_head[col] = -(rand() % MAX_ROWS);
  s_drop_len[col] = 4 + (rand() % 6);
  s_drop_speed[col] = 1 + (rand() % 2);
  s_drop_counter[col] = 0;
}

static void matrix_init(GRect bounds) {
  s_cols = bounds.size.w / CELL_W;
  s_rows = bounds.size.h / CELL_H;
  if (s_cols > MAX_COLS) s_cols = MAX_COLS;
  if (s_rows > MAX_ROWS) s_rows = MAX_ROWS;

  memset(s_grid, ' ', sizeof(s_grid));
  for (int c = 0; c < s_cols; c++) {
    seed_column(c);
  }
}

static void matrix_tick(void) {
  for (int c = 0; c < s_cols; c++) {
    s_drop_counter[c]++;
    if (s_drop_counter[c] < s_drop_speed[c]) {
      continue;
    }
    s_drop_counter[c] = 0;
    s_drop_head[c]++;

    if (s_drop_head[c] >= 0 && s_drop_head[c] < s_rows) {
      s_grid[s_drop_head[c]][c] = random_char();
    }
    // occasional flicker of an already-lit cell in the trail
    if ((rand() % 4) == 0) {
      int r = s_drop_head[c] - (rand() % (s_drop_len[c] + 1));
      if (r >= 0 && r < s_rows) {
        s_grid[r][c] = random_char();
      }
    }
    if (s_drop_head[c] - s_drop_len[c] > s_rows) {
      seed_column(c);
    }
  }
}

static void matrix_draw(GContext *ctx, GRect bounds) {
  graphics_context_set_text_color(
      ctx, PBL_IF_COLOR_ELSE(GColorDarkGreen, GColorWhite));
  GFont font = fonts_get_system_font(FONT_KEY_GOTHIC_14_BOLD);

  for (int c = 0; c < s_cols; c++) {
    for (int dr = 0; dr <= s_drop_len[c]; dr++) {
      int r = s_drop_head[c] - dr;
      if (r < 0 || r >= s_rows) continue;

      GColor color;
      if (dr == 0) {
        color = GColorWhite;
      } else if (dr <= 2) {
        color = PBL_IF_COLOR_ELSE(GColorBrightGreen, GColorWhite);
      } else if (dr <= 4) {
        color = PBL_IF_COLOR_ELSE(GColorMediumSpringGreen, GColorWhite);
      } else {
        color = PBL_IF_COLOR_ELSE(GColorIslamicGreen, GColorDarkGray);
      }
      graphics_context_set_text_color(ctx, color);

      char buf[2] = {s_grid[r][c], '\0'};
      GRect cell = GRect(c * CELL_W, r * CELL_H, CELL_W + 4, CELL_H + 2);
      graphics_draw_text(ctx, buf, font, cell, GTextOverflowModeFill,
                          GTextAlignmentLeft, NULL);
    }
  }
}

// ---------------------------------------------------------------------------
// HUD overlays
// ---------------------------------------------------------------------------

static void draw_battery_ring(GContext *ctx, GRect bounds) {
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
  graphics_context_set_fill_color(ctx, color);

  int32_t angle_end = DEG_TO_TRIGANGLE(360 * s_battery_percent / 100);
  graphics_fill_radial(ctx, bounds, GOvalScaleModeFillCircle, 3,
                        DEG_TO_TRIGANGLE(0), angle_end);
}

static void draw_bluetooth_status(GContext *ctx, GRect bounds) {
  GPoint center = GPoint(bounds.size.w / 2, PBL_IF_ROUND_ELSE(34, 20));
  GColor color = s_bt_connected ? PBL_IF_COLOR_ELSE(GColorBlueMoon, GColorWhite)
                                 : PBL_IF_COLOR_ELSE(GColorRed, GColorWhite);
  graphics_context_set_fill_color(ctx, color);
  graphics_fill_circle(ctx, center, 4);

  if (!s_bt_connected) {
    graphics_context_set_stroke_color(ctx, color);
    graphics_draw_line(ctx, GPoint(center.x - 6, center.y - 6),
                        GPoint(center.x + 6, center.y + 6));
    graphics_draw_line(ctx, GPoint(center.x - 6, center.y + 6),
                        GPoint(center.x + 6, center.y - 6));
  }
}

static void draw_hud_box(GContext *ctx, GRect bounds, GRect *out_box) {
  int box_h = s_health_available ? 92 : 76;
  GRect box = GRect(bounds.size.w / 2 - 62, bounds.size.h / 2 - box_h / 2, 124,
                     box_h);
  *out_box = box;

  graphics_context_set_fill_color(ctx, GColorBlack);
  graphics_fill_rect(ctx, box, 4, GCornersAll);

  GColor accent = PBL_IF_COLOR_ELSE(GColorBrightGreen, GColorWhite);
  graphics_context_set_stroke_color(ctx, accent);
  graphics_draw_round_rect_by_value(ctx, box, 4, GCornersAll);
}

static void draw_time(GContext *ctx, GRect box) {
  GFont font = fonts_get_system_font(FONT_KEY_LECO_38_BOLD_NUMBERS);
  GColor color = PBL_IF_COLOR_ELSE(GColorBrightGreen, GColorWhite);

  GRect time_rect = GRect(box.origin.x, box.origin.y + 2, box.size.w, 44);

  if (s_glitch_frames_left > 0) {
    // chromatic-aberration style glitch: two mis-registered copies
    graphics_context_set_text_color(ctx, PBL_IF_COLOR_ELSE(GColorRed, GColorWhite));
    GRect off1 = time_rect;
    off1.origin.x -= 2;
    graphics_draw_text(ctx, s_time_buffer, font, off1, GTextOverflowModeFill,
                        GTextAlignmentCenter, NULL);

    graphics_context_set_text_color(ctx, PBL_IF_COLOR_ELSE(GColorCyan, GColorWhite));
    GRect off2 = time_rect;
    off2.origin.x += 2;
    graphics_draw_text(ctx, s_time_buffer, font, off2, GTextOverflowModeFill,
                        GTextAlignmentCenter, NULL);
  }

  graphics_context_set_text_color(ctx, color);
  graphics_draw_text(ctx, s_time_buffer, font, time_rect, GTextOverflowModeFill,
                      GTextAlignmentCenter, NULL);
}

static void draw_details(GContext *ctx, GRect box) {
  GColor dim = PBL_IF_COLOR_ELSE(GColorIslamicGreen, GColorLightGray);
  GFont small_font = fonts_get_system_font(FONT_KEY_GOTHIC_14_BOLD);

  graphics_context_set_text_color(ctx, dim);
  GRect sec_rect = GRect(box.origin.x, box.origin.y + 44, box.size.w, 16);
  char line[28];
  snprintf(line, sizeof(line), ":%s SEC", s_seconds_buffer);
  graphics_draw_text(ctx, line, small_font, sec_rect, GTextOverflowModeFill,
                      GTextAlignmentCenter, NULL);

  GRect date_rect = GRect(box.origin.x, box.origin.y + 60, box.size.w, 16);
  graphics_draw_text(ctx, s_date_buffer, small_font, date_rect,
                      GTextOverflowModeFill, GTextAlignmentCenter, NULL);

  if (s_health_available) {
    GRect steps_rect = GRect(box.origin.x, box.origin.y + 76, box.size.w, 16);
    graphics_draw_text(ctx, s_steps_buffer, small_font, steps_rect,
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

  matrix_draw(ctx, bounds);

  GRect box;
  draw_hud_box(ctx, bounds, &box);
  draw_time(ctx, box);
  draw_details(ctx, box);

  draw_bluetooth_status(ctx, bounds);
  draw_battery_ring(ctx, bounds);
}

// ---------------------------------------------------------------------------
// Glitch burst (shake-to-glitch)
// ---------------------------------------------------------------------------

static void glitch_timer_callback(void *data) {
  s_glitch_frames_left--;
  if (s_glitch_frames_left > 0) {
    for (int c = 0; c < s_cols; c++) {
      int r = rand() % s_rows;
      s_grid[r][c] = random_char();
    }
    layer_mark_dirty(s_canvas_layer);
    s_glitch_timer =
        app_timer_register(GLITCH_FRAME_MS, glitch_timer_callback, NULL);
  } else {
    layer_mark_dirty(s_canvas_layer);
  }
}

static void trigger_glitch(void) {
  if (s_glitch_frames_left > 0) return;
  s_glitch_frames_left = GLITCH_FRAME_COUNT;
  vibes_short_pulse();
  s_glitch_timer = app_timer_register(GLITCH_FRAME_MS, glitch_timer_callback, NULL);
}

static void accel_tap_handler(AccelAxisType axis, int32_t direction) {
  trigger_glitch();
}

// ---------------------------------------------------------------------------
// Services
// ---------------------------------------------------------------------------

static void update_time_buffers(struct tm *tick_time) {
  strftime(s_time_buffer, sizeof(s_time_buffer),
           clock_is_24h_style() ? "%H:%M" : "%I:%M", tick_time);
  strftime(s_seconds_buffer, sizeof(s_seconds_buffer), "%S", tick_time);
  strftime(s_date_buffer, sizeof(s_date_buffer), "%a %d %b", tick_time);
}

static void update_steps(void) {
  if (!s_health_available) return;
  HealthValue steps =
      health_service_sum_today(HealthMetricStepCount);
  snprintf(s_steps_buffer, sizeof(s_steps_buffer), "%d STEPS", (int)steps);
}

static void tick_handler(struct tm *tick_time, TimeUnits units_changed) {
  update_time_buffers(tick_time);
  matrix_tick();
  update_steps();

  // rare spontaneous glitch, purely cosmetic
  if ((rand() % 45) == 0) {
    trigger_glitch();
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

  matrix_init(bounds);

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
  accel_tap_service_subscribe(accel_tap_handler);
}

static void deinit(void) {
  tick_timer_service_unsubscribe();
  battery_state_service_unsubscribe();
  connection_service_unsubscribe();
  accel_tap_service_unsubscribe();
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
