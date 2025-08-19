import sys, json, requests
from typing import Optional, Tuple, Dict
from datetime import datetime, timezone
from keys import WEATHERKEY, MAPSKEY  # keys.py must define WEATHERKEY and MAPSKEY

# ====== CONFIG ======
GMAPS_KEY = MAPSKEY
OWM_KEY   = WEATHERKEY
UNITS     = "imperial"   # "metric" (°C) or "imperial" (°F)
TIMEOUT_S = 12
USE_PLACES = False       # True -> Places Text Search; False -> Geocoding API
COUNTRY_PREF = ["US", "CA", "GB", "AU", "FR", "DE"]

ALIASES: Dict[str, Tuple[str, Optional[str]]] = {
    "la": ("Los Angeles", "US"),
    "l.a.": ("Los Angeles", "US"),
    "nyc": ("New York", "US"),
    "sf": ("San Francisco", "US"),
    "bay area": ("San Francisco", "US"),
    "dc": ("Washington", "US"),
    "d.c.": ("Washington", "US"),
}

if not GMAPS_KEY:
    sys.exit("ERROR: MAPSKEY (Google) is empty in keys.py")
if not OWM_KEY:
    sys.exit("ERROR: WEATHERKEY (OpenWeatherMap) is empty in keys.py")

SESSION = requests.Session()

# ---------- Google helpers ----------
def _gmaps_get(url: str, params: dict) -> dict:
    """Call Google API and normalize errors/status."""
    try:
        r = SESSION.get(url, params={**params, "key": GMAPS_KEY}, timeout=TIMEOUT_S)
    except requests.exceptions.Timeout:
        raise TimeoutError("Google Maps request timed out.")
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Network error calling Google Maps: {e}")
    if r.status_code == 429:
        raise RuntimeError("Google Maps: rate limited (HTTP 429). Try again later.")
    if r.status_code >= 400:
        raise RuntimeError(f"Google Maps error {r.status_code}: {r.text[:200]}")

    j = r.json()
    status = j.get("status", "OK")
    if status not in ("OK", "ZERO_RESULTS"):
        em = j.get("error_message", "")
        raise RuntimeError(f"Google Maps API status: {status}. {em}")
    return j

def _extract_city_country_from_components(components: list) -> Tuple[Optional[str], Optional[str]]:
    city = None
    country = None
    admin2 = None
    for c in components:
        types = c.get("types", [])
        long_name = c.get("long_name")
        short_name = c.get("short_name")
        if "locality" in types:
            city = long_name
        elif "administrative_area_level_2" in types:
            admin2 = long_name
        elif "country" in types:
            country = (short_name or long_name)
    if not city and admin2:
        city = admin2  # fallback if no locality level
    return city, country

def gmaps_geocode_text(query: str) -> Optional[dict]:
    """Use Geocoding API to resolve free text into a place with lat/lng + components."""
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    j = _gmaps_get(url, {"address": query})
    results = j.get("results", [])
    if not results:
        return None

    def score(res):
        comps = res.get("address_components", [])
        city, cc = _extract_city_country_from_components(comps)
        cc_rank = COUNTRY_PREF.index(cc) if cc in COUNTRY_PREF else len(COUNTRY_PREF)
        city_bonus = 0 if city else 1
        return (city_bonus, cc_rank)

    results.sort(key=score)
    pick = results[0]
    loc = pick["geometry"]["location"]
    city, cc = _extract_city_country_from_components(pick.get("address_components", []))
    return {
        "lat": loc["lat"],
        "lon": loc["lng"],
        "city": city,
        "country": cc,
        "formatted": pick.get("formatted_address"),
        "types": pick.get("types", []),
    }

def gmaps_places_text_search(query: str) -> Optional[dict]:
    """Use Places Text Search -> Details to resolve free text."""
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    j = _gmaps_get(url, {"query": query})
    results = j.get("results", [])
    if not results:
        return None

    def score(res):
        types = res.get("types", [])
        is_locality = 0 if ("locality" in types or "political" in types) else 1
        return (is_locality,)

    results.sort(key=score)
    first = results[0]
    place_id = first["place_id"]
    loc = first["geometry"]["location"]

    det_url = "https://maps.googleapis.com/maps/api/place/details/json"
    dj = _gmaps_get(det_url, {"place_id": place_id, "fields": "address_component,formatted_address,geometry"})
    detail = dj.get("result") or {}
    comps = detail.get("address_components", [])
    city, cc = _extract_city_country_from_components(comps)

    return {
        "lat": loc["lat"],
        "lon": loc["lng"],
        "city": city,
        "country": cc,
        "formatted": detail.get("formatted_address") or first.get("formatted_address"),
        "types": first.get("types", []),
    }

# ---------- OpenWeatherMap ----------
def owm_current_by_latlon(lat: float, lon: float, units: str = UNITS) -> dict:
    try:
        r = SESSION.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": lat, "lon": lon, "appid": OWM_KEY, "units": units},
            timeout=TIMEOUT_S,
        )
    except requests.exceptions.Timeout:
        raise TimeoutError("OpenWeatherMap request timed out.")
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Network error calling OpenWeatherMap: {e}")

    if r.status_code == 401:
        raise PermissionError("OpenWeatherMap Unauthorized — check your key / plan.")
    if r.status_code >= 400:
        raise RuntimeError(f"OpenWeatherMap error {r.status_code}: {r.text[:200]}")
    j = r.json()
    try:
        return {
            "source": "current",
            "time_utc": datetime.fromtimestamp(j["dt"], tz=timezone.utc).isoformat(),
            "location": {
                "name": j.get("name"),
                "country": (j.get("sys") or {}).get("country"),
                "coord": j.get("coord"),
            },
            "temp": j["main"]["temp"],
            "feels_like": j["main"]["feels_like"],
            "humidity": j["main"]["humidity"],
            "wind_mps": (j.get("wind") or {}).get("speed"),
            "weather": j["weather"][0]["description"] if j.get("weather") else None,
            "clouds_pct": (j.get("clouds") or {}).get("all"),
            "rain_mm_1h": (j.get("rain") or {}).get("1h"),
            "snow_mm_1h": (j.get("snow") or {}).get("1h"),
        }
    except KeyError as e:
        raise RuntimeError(f"Unexpected OpenWeatherMap payload (missing {e}). Raw: {j}")

# ---------- High-level entry ----------
def get_current_weather_via_gmaps(free_text: str, units: str = UNITS) -> dict:
    key = free_text.strip().lower()

    # Alias first (fast path for 'la', 'nyc', etc.)
    if key in ALIASES:
        city, cc = ALIASES[key]
        geo = gmaps_geocode_text(f"{city},{cc}")
        if not geo:
            raise LookupError(f"Alias '{city},{cc}' failed to geocode.")
        wx = owm_current_by_latlon(geo["lat"], geo["lon"], units=units)
        wx["resolved"] = {"input": free_text, "alias": f"{city},{cc}", "formatted": geo.get("formatted")}
        return wx

    # Use Google to resolve text → lat/lon
    geo = gmaps_places_text_search(free_text) if USE_PLACES else gmaps_geocode_text(free_text)
    if not geo:
        # Try biasing to US if nothing came back
        geo = gmaps_geocode_text(f"{free_text}, US")
        if not geo:
            raise LookupError(f"Couldn’t resolve location: '{free_text}'")

    wx = owm_current_by_latlon(geo["lat"], geo["lon"], units=units)
    wx["resolved"] = {
        "input": free_text,
        "city": geo.get("city"),
        "country": geo.get("country"),
        "formatted": geo.get("formatted"),
        "types": geo.get("types"),
    }
    return wx

# ---------- One-method natural description ----------
def describe_weather_owm_current(j: dict, units: str = UNITS) -> str:
    """Turn the OWM 'current weather' JSON (our normalized dict) into a natural sentence."""
    loc = j.get("location", {})
    city = loc.get("name") or "Unknown location"
    country = loc.get("country") or ""
    weather = (j.get("weather") or "unknown conditions")

    # Units/values
    temp_unit = "°F" if units == "imperial" else "°C"
    wind_unit = "mph" if units == "imperial" else "m/s"

    temp = j.get("temp")
    feels = j.get("feels_like")
    hum = j.get("humidity")
    wind_mps = j.get("wind_mps")
    clouds = j.get("clouds_pct")
    rain = j.get("rain_mm_1h")
    snow = j.get("snow_mm_1h")

    # Convert wind if needed
    wind = None
    if wind_mps is not None:
        wind = round(wind_mps * 2.23694, 1) if units == "imperial" else round(wind_mps, 1)

    # Build narrative
    s = f"In {city}, {country}, it’s {weather}"
    if temp is not None:
        s += f" with a temperature of {round(temp,1)}{temp_unit}"
        if feels is not None:
            s += f" (feels like {round(feels,1)}{temp_unit})"
    if hum is not None:
        s += f". Humidity is around {hum}%"
    if wind is not None:
        s += f", with winds near {wind} {wind_unit}"
    if clouds is not None:
        if clouds == 0:
            s += ". Skies are clear"
        elif clouds < 40:
            s += ". A few clouds overhead"
        elif clouds < 70:
            s += ". Partly cloudy"
        else:
            s += ". Mostly cloudy"
    if rain:
        s += f", and {round(rain,1)} mm of rain in the last hour"
    if snow:
        s += f", with {round(snow,1)} mm of snow in the last hour"
    return s + "."

# ---- Example CLI ----
if __name__ == "__main__":
    q = input("Where? (e.g., 'la', 'sf bay area', 'my dorm near UCLA'): ").strip()
    try:
        res = get_current_weather_via_gmaps(q, units=UNITS)
        # Narrative:
        print(describe_weather_owm_current(res, units=UNITS))
        # # Raw (optional): comment this out if you don't want JSON
        # print(json.dumps(res, indent=2))
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
