// Companion JS for Halo: gets the phone's location, fetches current
// weather + wind + today/tomorrow's sunrise/sunset from Open-Meteo (free,
// no API key needed), and pushes it all to the watch over AppMessage.
// The watch (main.c) pings this with an empty AppMessage every 30
// minutes as its cue to refetch, and drives its bezel/theming/countdown
// from the sunrise/sunset and category fields sent here. Also provides
// the watch's Settings page (accent color override, temperature unit).

var LOCATION_OPTIONS = {
  enableHighAccuracy: false,
  timeout: 15000,
  maximumAge: 60000
};

var SETTINGS_KEY = 'halo_settings';
var DEFAULT_SETTINGS = { accent: 0, unit: 0 };  // accent: 0=auto, unit: 0=F,1=C

// ---------------------------------------------------------------------------
// Settings persistence
// ---------------------------------------------------------------------------

function loadSettings() {
  try {
    var raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return DEFAULT_SETTINGS;
    var parsed = JSON.parse(raw);
    return {
      accent: typeof parsed.accent === 'number' ? parsed.accent : 0,
      unit: typeof parsed.unit === 'number' ? parsed.unit : 0
    };
  } catch (e) {
    return DEFAULT_SETTINGS;
  }
}

function saveSettings(settings) {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
}

function sendSettings(settings) {
  Pebble.sendAppMessage(
    { 'UNIT': settings.unit, 'ACCENT_MODE': settings.accent },
    function() { console.log("Settings sent to watch"); },
    function(e) { console.log("Failed to send settings: " + JSON.stringify(e)); }
  );
}

// ---------------------------------------------------------------------------
// Configuration page (self-contained data: URI, no hosting needed)
// ---------------------------------------------------------------------------

function buildConfigHtml(settings) {
  function option(value, label, current) {
    return '<option value="' + value + '"' +
        (value === current ? ' selected' : '') + '>' + label + '</option>';
  }
  return '<!DOCTYPE html><html><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>Halo Settings</title><style>' +
    'body{font-family:sans-serif;background:#111;color:#eee;padding:16px;margin:0}' +
    'h2{margin-top:0}label{display:block;margin:16px 0 4px;font-size:14px;color:#aaa}' +
    'select{width:100%;padding:10px;font-size:16px;box-sizing:border-box}' +
    'button{margin-top:24px;width:100%;padding:14px;font-size:16px;background:#2b8fd6;' +
    'color:#fff;border:none;border-radius:4px}' +
    '</style></head><body>' +
    '<h2>Halo Settings</h2>' +
    '<label>Accent color</label><select id="accent">' +
      option(0, 'Auto (weather + time of day)', settings.accent) +
      option(1, 'Blue', settings.accent) +
      option(2, 'Orange', settings.accent) +
      option(3, 'Violet', settings.accent) +
      option(4, 'Green', settings.accent) +
      option(5, 'Red', settings.accent) +
      option(6, 'Gold', settings.accent) +
      option(7, 'Monochrome', settings.accent) +
    '</select>' +
    '<label>Temperature unit</label><select id="unit">' +
      option(0, 'Fahrenheit', settings.unit) +
      option(1, 'Celsius', settings.unit) +
    '</select>' +
    '<button onclick="save()">Save</button>' +
    '<script>function save(){' +
      'var s={accent:parseInt(document.getElementById("accent").value,10),' +
      'unit:parseInt(document.getElementById("unit").value,10)};' +
      'document.location="pebblejs://close#"+encodeURIComponent(JSON.stringify(s));' +
    '}</' + 'script></body></html>';
}

Pebble.addEventListener('showConfiguration', function() {
  var html = buildConfigHtml(loadSettings());
  Pebble.openURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
});

Pebble.addEventListener('webviewclosed', function(e) {
  if (!e.response) return;
  try {
    var settings = JSON.parse(decodeURIComponent(e.response));
    saveSettings(settings);
    sendSettings(settings);
    refreshWeather();
  } catch (ex) {
    console.log("Failed to parse config response: " + ex);
  }
});

// ---------------------------------------------------------------------------
// Weather
// ---------------------------------------------------------------------------

// Category numbers must match the WeatherCategory enum in main.c:
// 0=clear, 1=cloudy, 2=fog, 3=rain, 4=snow, 5=storm.
function weatherCodeInfo(code) {
  // WMO weather codes, as returned by Open-Meteo's current_weather field.
  if (code === 0) return { text: "CLEAR", category: 0 };
  if (code === 1 || code === 2) return { text: "P.CLOUDY", category: 1 };
  if (code === 3) return { text: "CLOUDY", category: 1 };
  if (code === 45 || code === 48) return { text: "FOG", category: 2 };
  if (code >= 51 && code <= 57) return { text: "DRIZZLE", category: 3 };
  if (code >= 61 && code <= 67) return { text: "RAIN", category: 3 };
  if (code >= 71 && code <= 77) return { text: "SNOW", category: 4 };
  if (code >= 80 && code <= 82) return { text: "SHOWERS", category: 3 };
  if (code >= 85 && code <= 86) return { text: "SNOW SHWR", category: 4 };
  if (code >= 95) return { text: "STORM", category: 5 };
  return { text: "N/A", category: 1 };
}

var COMPASS_POINTS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
function degreesToCompass(deg) {
  return COMPASS_POINTS[Math.round(deg / 45) % 8];
}

// Open-Meteo returns local times like "2026-08-02T06:12" when
// timezone=auto is set - pull out minutes-since-midnight directly rather
// than fighting JS Date's own timezone handling.
function minutesFromIsoTime(iso) {
  var timePart = iso.split("T")[1];
  var parts = timePart.split(":");
  return parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
}

function sendWeather(w) {
  var payload = {
    'TEMPERATURE': Math.round(w.temperature),
    'CONDITION': w.condition,
    'CATEGORY': w.category,
    'UNIT': w.unit,
    'ACCENT_MODE': w.accent
  };
  if (w.sunrise !== undefined) payload['SUNRISE'] = w.sunrise;
  if (w.sunset !== undefined) payload['SUNSET'] = w.sunset;
  if (w.sunriseTomorrow !== undefined) payload['SUNRISE_TOMORROW'] = w.sunriseTomorrow;
  if (w.windSpeed !== undefined) payload['WIND_SPEED'] = w.windSpeed;
  if (w.windDir !== undefined) payload['WIND_DIR'] = w.windDir;

  Pebble.sendAppMessage(
    payload,
    function() { console.log("Weather sent to watch"); },
    function(e) { console.log("Failed to send weather: " + JSON.stringify(e)); }
  );
}

function fetchWeather(latitude, longitude, settings) {
  var tempUnit = settings.unit === 1 ? 'celsius' : 'fahrenheit';
  var windUnit = settings.unit === 1 ? 'kmh' : 'mph';
  var url = "https://api.open-meteo.com/v1/forecast?latitude=" + latitude +
      "&longitude=" + longitude +
      "&current_weather=true&daily=sunrise,sunset&timezone=auto" +
      "&temperature_unit=" + tempUnit + "&windspeed_unit=" + windUnit;

  var xhr = new XMLHttpRequest();
  xhr.timeout = 15000;
  xhr.onload = function() {
    if (xhr.status !== 200) {
      console.log("Weather request failed: status " + xhr.status);
      return;
    }
    var json = JSON.parse(xhr.responseText);
    var current = json.current_weather;
    var info = weatherCodeInfo(current.weathercode);
    var sunriseArr = json.daily && json.daily.sunrise;
    var sunsetArr = json.daily && json.daily.sunset;

    sendWeather({
      temperature: current.temperature,
      condition: info.text,
      category: info.category,
      sunrise: sunriseArr ? minutesFromIsoTime(sunriseArr[0]) : undefined,
      sunset: sunsetArr ? minutesFromIsoTime(sunsetArr[0]) : undefined,
      sunriseTomorrow: sunriseArr && sunriseArr[1] ? minutesFromIsoTime(sunriseArr[1]) : undefined,
      windSpeed: Math.round(current.windspeed),
      windDir: degreesToCompass(current.winddirection),
      unit: settings.unit,
      accent: settings.accent
    });
  };
  xhr.onerror = function() {
    console.log("Weather request error");
  };
  xhr.open("GET", url, true);
  xhr.send();
}

function locationError(err, settings) {
  console.log("Location error (" + err.code + "): " + err.message);
  sendWeather({
    temperature: 0,
    condition: "NO LOCATION",
    category: 1,
    unit: settings.unit,
    accent: settings.accent
  });
}

function refreshWeather() {
  var settings = loadSettings();
  navigator.geolocation.getCurrentPosition(
    function(pos) { fetchWeather(pos.coords.latitude, pos.coords.longitude, settings); },
    function(err) { locationError(err, settings); },
    LOCATION_OPTIONS
  );
}

Pebble.addEventListener('ready', refreshWeather);
Pebble.addEventListener('appmessage', refreshWeather);
