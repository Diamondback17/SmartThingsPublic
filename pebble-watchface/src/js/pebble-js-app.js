// Companion JS for Halo: gets the phone's location, fetches current
// weather from Open-Meteo (free, no API key needed), and pushes
// temperature + a short condition string to the watch over AppMessage.
// The watch (main.c) pings this with an empty AppMessage every 30
// minutes as its cue to refetch.

var LOCATION_OPTIONS = {
  enableHighAccuracy: false,
  timeout: 15000,
  maximumAge: 60000
};

function weatherCodeToText(code) {
  // WMO weather codes, as returned by Open-Meteo's current_weather field.
  if (code === 0) return "CLEAR";
  if (code === 1 || code === 2) return "P.CLOUDY";
  if (code === 3) return "CLOUDY";
  if (code === 45 || code === 48) return "FOG";
  if (code >= 51 && code <= 57) return "DRIZZLE";
  if (code >= 61 && code <= 67) return "RAIN";
  if (code >= 71 && code <= 77) return "SNOW";
  if (code >= 80 && code <= 82) return "SHOWERS";
  if (code >= 85 && code <= 86) return "SNOW SHWR";
  if (code >= 95) return "STORM";
  return "N/A";
}

function sendWeather(temperature, condition) {
  Pebble.sendAppMessage(
    { 'TEMPERATURE': Math.round(temperature), 'CONDITION': condition },
    function() { console.log("Weather sent to watch"); },
    function(e) { console.log("Failed to send weather: " + JSON.stringify(e)); }
  );
}

function fetchWeather(latitude, longitude) {
  var url = "https://api.open-meteo.com/v1/forecast?latitude=" + latitude +
      "&longitude=" + longitude +
      "&current_weather=true&temperature_unit=fahrenheit";

  var xhr = new XMLHttpRequest();
  xhr.timeout = 15000;
  xhr.onload = function() {
    if (xhr.status !== 200) {
      console.log("Weather request failed: status " + xhr.status);
      return;
    }
    var json = JSON.parse(xhr.responseText);
    var current = json.current_weather;
    sendWeather(current.temperature, weatherCodeToText(current.weathercode));
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
  sendWeather(0, "NO LOCATION");
}

function refreshWeather() {
  navigator.geolocation.getCurrentPosition(locationSuccess, locationError, LOCATION_OPTIONS);
}

Pebble.addEventListener('ready', refreshWeather);
Pebble.addEventListener('appmessage', refreshWeather);
