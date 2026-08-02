#include <pebble.h>

// ---------------------------------------------------------------------------
// "Cyber Matrix" watchface
//
// Digital-rain background, a glitching HUD clock readout, a radial battery
// ring, a bluetooth status blip and a shake-to-glitch easter egg.
// ---------------------------------------------------------------------------

#define MAX_COLS 24
#define MAX_ROWS 24
#define GLITCH_FRAME_MS 70
#define GLITCH_FRAME_COUNT 5
// Reference width the base layout (Aplite/Basalt/Diorite) was designed
// against; every other platform's layout is derived from bounds relative
// to this, so the same code scales up cleanly on the big 260x260 Gabbro
// (Pebble Round 2) display without needing a per-platform layout table.
#define BASE_WIDTH 144.0

static Window *s_window;
static Layer *s_canvas_layer;

static char s_time_buffer[8];
static char s_seconds_buffer[4];
static char s_date_buffer[24];
static char s_steps_buffer[24];

static int s_cols, s_rows;
static int s_cell_w, s_cell_h;
static char s_grid[MAX_ROWS][MAX_COLS];
static int8_t s_drop_head[MAX_COLS];
static int8_t s_drop_len[MAX_COLS];
static uint8_t s_drop_speed[MAX_COLS];
static uint8_t s_drop_counter[MAX_COLS];

static int s_battery_percent = 100;
static bool s_battery_charging = false;
static bool s_bt_connected = true;
static bool s_health_available = false;

static GRect s_box;
static GFont s_time_font;
static GFont s_detail_font;
static GFont s_rain_font;
static int s_time_row_h;
static int s_line_h;

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
  // Scale the rain grid's cell size with the display so every platform
  // (144px-wide Aplite up to the 260px Gabbro) shows roughly the same
  // number of columns/rows instead of Gabbro getting either a sparse
  // few columns or capping out at MAX_COLS.
  double scale = bounds.size.w / BASE_WIDTH;
  s_cell_w = (int)(12 * scale + 0.5);
  s_cell_h = (int)(14 * scale + 0.5);
  if (s_cell_w < 10) s_cell_w = 10;
  if (s_cell_h < 12) s_cell_h = 12;

  s_cols = bounds.size.w / s_cell_w;
  s_rows = bounds.size.h / s_cell_h;
  if (s_cols > MAX_COLS) s_cols = MAX_COLS;
  if (s_rows > MAX_ROWS) s_rows = MAX_ROWS;

  if (s_cell_w >= 20) {
    s_rain_font = fonts_get_system_font(FONT_KEY_GOTHIC_24_BOLD);
  } else if (s_cell_w >= 15) {
    s_rain_font = fonts_get_system_font(FONT_KEY_GOTHIC_18_BOLD);
  } else {
    s_rain_font = fonts_get_system_font(FONT_KEY_GOTHIC_14_BOLD);
  }

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
      GRect cell = GRect(c * s_cell_w, r * s_cell_h, s_cell_w + 4, s_cell_h + 2);
      graphics_draw_text(ctx, buf, s_rain_font, cell, GTextOverflowModeFill,
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
  int top_offset = PBL_IF_ROUND_ELSE((int)(bounds.size.h * 0.19), 20);
  GPoint center = GPoint(bounds.size.w / 2, top_offset);
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

// Lays out the terminal HUD box once (on window load / size change) rather
// than every redraw. Box width is derived from the display width so it
// fills a sensible fraction of the big square Gabbro screen instead of
// staying pinned at the pixel size that fit 144px-wide Aplite; round
// platforms get a narrower fraction so the corners don't get clipped by
// the circular display mask.
static void compute_layout(GRect bounds) {
  int box_w = PBL_IF_ROUND_ELSE((int)(bounds.size.w * 0.70),
                                 bounds.size.w - 20);
  bool big = box_w >= 170;

  s_time_font = fonts_get_system_font(
      big ? FONT_KEY_LECO_42_NUMBERS : FONT_KEY_LECO_38_BOLD_NUMBERS);
  s_detail_font = fonts_get_system_font(
      big ? FONT_KEY_GOTHIC_18_BOLD : FONT_KEY_GOTHIC_14_BOLD);
  s_time_row_h = big ? 54 : 44;
  s_line_h = big ? 20 : 16;

  int lines = s_health_available ? 3 : 2;  // seconds, date, (steps)
  int box_h = 2 + s_time_row_h + lines * s_line_h;

  s_box = GRect(bounds.size.w / 2 - box_w / 2,
                bounds.size.h / 2 - box_h / 2, box_w, box_h);
}

static void draw_hud_box(GContext *ctx) {
  graphics_context_set_fill_color(ctx, GColorBlack);
  graphics_fill_rect(ctx, s_box, 4, GCornersAll);

  GColor accent = PBL_IF_COLOR_ELSE(GColorBrightGreen, GColorWhite);
  graphics_context_set_stroke_color(ctx, accent);
  graphics_draw_round_rect(ctx, s_box, 4);
}

static void draw_time(GContext *ctx) {
  GColor color = PBL_IF_COLOR_ELSE(GColorBrightGreen, GColorWhite);
  GRect time_rect =
      GRect(s_box.origin.x, s_box.origin.y + 2, s_box.size.w, s_time_row_h);

  if (s_glitch_frames_left > 0) {
    // chromatic-aberration style glitch: two mis-registered copies
    graphics_context_set_text_color(ctx, PBL_IF_COLOR_ELSE(GColorRed, GColorWhite));
    GRect off1 = time_rect;
    off1.origin.x -= 2;
    graphics_draw_text(ctx, s_time_buffer, s_time_font, off1,
                        GTextOverflowModeFill, GTextAlignmentCenter, NULL);

    graphics_context_set_text_color(ctx, PBL_IF_COLOR_ELSE(GColorCyan, GColorWhite));
    GRect off2 = time_rect;
    off2.origin.x += 2;
    graphics_draw_text(ctx, s_time_buffer, s_time_font, off2,
                        GTextOverflowModeFill, GTextAlignmentCenter, NULL);
  }

  graphics_context_set_text_color(ctx, color);
  graphics_draw_text(ctx, s_time_buffer, s_time_font, time_rect,
                      GTextOverflowModeFill, GTextAlignmentCenter, NULL);
}

static void draw_details(GContext *ctx) {
  GColor dim = PBL_IF_COLOR_ELSE(GColorIslamicGreen, GColorLightGray);
  graphics_context_set_text_color(ctx, dim);

  int y = s_box.origin.y + 2 + s_time_row_h;

  char line[28];
  snprintf(line, sizeof(line), ":%s SEC", s_seconds_buffer);
  GRect sec_rect = GRect(s_box.origin.x, y, s_box.size.w, s_line_h);
  graphics_draw_text(ctx, line, s_detail_font, sec_rect, GTextOverflowModeFill,
                      GTextAlignmentCenter, NULL);
  y += s_line_h;

  GRect date_rect = GRect(s_box.origin.x, y, s_box.size.w, s_line_h);
  graphics_draw_text(ctx, s_date_buffer, s_detail_font, date_rect,
                      GTextOverflowModeFill, GTextAlignmentCenter, NULL);
  y += s_line_h;

  if (s_health_available) {
    GRect steps_rect = GRect(s_box.origin.x, y, s_box.size.w, s_line_h);
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

  matrix_draw(ctx, bounds);

  draw_hud_box(ctx);
  draw_time(ctx);
  draw_details(ctx);

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
