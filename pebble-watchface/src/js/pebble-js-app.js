// Companion JS for Halo: gets the phone's location, fetches current
// weather + today's sunrise/sunset from Open-Meteo (free, no API key
// needed), and pushes it all to the watch over AppMessage. The watch
// (main.c) pings this with an empty AppMessage every 30 minutes as its
// cue to refetch, and uses the sunrise/sunset times to light its bezel
// and theme its accent color.

var LOCATION_OPTIONS = {
  enableHighAccuracy: false,
  timeout: 15000,
  maximumAge: 60000
};

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

// Open-Meteo returns local times like "2026-08-02T06:12" when
// timezone=auto is set - pull out minutes-since-midnight directly rather
// than fighting JS Date's own timezone handling.
function minutesFromIsoTime(iso) {
  var timePart = iso.split("T")[1];
  var parts = timePart.split(":");
  return parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10);
}

function sendWeather(temperature, condition, category, sunriseMin, sunsetMin) {
  var payload = {
    'TEMPERATURE': Math.round(temperature),
    'CONDITION': condition,
    'CATEGORY': category
  };
  if (sunriseMin !== undefined) payload['SUNRISE'] = sunriseMin;
  if (sunsetMin !== undefined) payload['SUNSET'] = sunsetMin;

  Pebble.sendAppMessage(
    payload,
    function() { console.log("Weather sent to watch"); },
    function(e) { console.log("Failed to send weather: " + JSON.stringify(e)); }
  );
}

function fetchWeather(latitude, longitude) {
  var url = "https://api.open-meteo.com/v1/forecast?latitude=" + latitude +
      "&longitude=" + longitude +
      "&current_weather=true&daily=sunrise,sunset&timezone=auto" +
      "&temperature_unit=fahrenheit";

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
    var sunriseMin = json.daily && json.daily.sunrise ?
        minutesFromIsoTime(json.daily.sunrise[0]) : undefined;
    var sunsetMin = json.daily && json.daily.sunset ?
        minutesFromIsoTime(json.daily.sunset[0]) : undefined;

    sendWeather(current.temperature, info.text, info.category, sunriseMin, sunsetMin);
  };
  xhr.onerror = function() {
    console.log("Weather request error");
  };
  xhr.open("GET", url, true);
  xhr.send();
}

function locationSuccess(pos) {
  fetchWeather(pos.coords.latitude, pos.coords.longitude);
}

function locationError(err) {
  console.log("Location error (" + err.code + "): " + err.message);
  sendWeather(0, "NO LOCATION", 1);
}

function refreshWeather() {
  navigator.geolocation.getCurrentPosition(locationSuccess, locationError, LOCATION_OPTIONS);
}

Pebble.addEventListener('ready', refreshWeather);
Pebble.addEventListener('appmessage', refreshWeather);
