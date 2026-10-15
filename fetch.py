"""Weather blender for Colorado Springs and Falcon, CO.

What it does, in order:
  1. Pulls the same forecast from several independent weather models (via Open-Meteo).
  2. Pulls the official National Weather Service forecast, alerts, and the latest
     real observation from the nearest weather station.
  3. Blends the models: the median is the forecast, and the spread between models
     is how confident we should be.
  4. Writes the page's data to data/<place>.json, and appends one line per run to
     data/log/<place>.jsonl so we can later score which model is most accurate here.

Run:  python3 fetch.py            (standard library only, no installs needed)
GitHub Actions runs it every hour (.github/workflows/refresh.yml).
"""

import json
import os
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Denver")

PLACES = {
    "colorado-springs": {"name": "Colorado Springs", "lat": 38.8339, "lon": -104.8214},
    "falcon": {"name": "Falcon", "lat": 38.9331, "lon": -104.6086},
}

# Independent models. Most weather apps repackage one of these, so going to the
# source avoids counting the same model twice.
MODELS = {
    "ecmwf_ifs025": "European (ECMWF)",
    "gfs_seamless": "American (GFS)",
    "icon_seamless": "German (ICON)",
    "gem_seamless": "Canadian (GEM)",
    "jma_seamless": "Japanese (JMA)",
    "ncep_hrrr_conus": "HRRR (short-range, US)",  # best for the next ~18-48 hours
}

HOURLY_VARS = ["temperature_2m", "precipitation", "wind_speed_10m", "wind_gusts_10m", "weather_code"]
NWS_HEADERS = {"User-Agent": "personal-weather-blend/1.0", "Accept": "application/geo+json"}
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LOG_DIR = os.path.join(DATA_DIR, "log")


def get_json(url, headers=None, attempts=5):
    """Fetch JSON, retrying a few times because free APIs occasionally time out."""
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.load(resp)
        except Exception as e:
            if attempt == attempts - 1:
                raise
            rate_limited = getattr(e, "code", None) == 429
            time.sleep(30 if rate_limited else 2 ** (attempt + 1))


# ---------- 1. Model forecasts ----------

def fetch_models(places):
    """One request for every place (kinder to the free API's rate limit)."""
    params = {
        "latitude": ",".join(str(p["lat"]) for p in places),
        "longitude": ",".join(str(p["lon"]) for p in places),
        "hourly": ",".join(HOURLY_VARS),
        "models": ",".join(MODELS),
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
        "timezone": "America/Denver", "forecast_days": 7,
    }
    data = get_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params))
    if isinstance(data, dict):  # a single place comes back as one object, several as a list
        data = [data]
    results = []
    for place_data in data:
        hourly = place_data["hourly"]
        # With several models, Open-Meteo names each column <variable>_<model>.
        per_model = {}
        for m in MODELS:
            cols = {v: hourly.get(f"{v}_{m}") for v in HOURLY_VARS}
            if cols["temperature_2m"] and any(t is not None for t in cols["temperature_2m"]):
                per_model[m] = cols
        results.append((hourly["time"], per_model))
    return results


# ---------- 2. National Weather Service ----------

def fetch_nws(lat, lon):
    point = get_json(f"https://api.weather.gov/points/{lat},{lon}", NWS_HEADERS)["properties"]
    daily = get_json(point["forecast"], NWS_HEADERS)["properties"]["periods"]
    alerts = get_json(f"https://api.weather.gov/alerts/active?point={lat},{lon}", NWS_HEADERS)["features"]
    stations = get_json(point["observationStations"], NWS_HEADERS)["features"]
    obs = None
    if stations:
        station_id = stations[0]["properties"]["stationIdentifier"]
        p = get_json(f"https://api.weather.gov/stations/{station_id}/observations/latest", NWS_HEADERS)["properties"]
        temp_c = (p.get("temperature") or {}).get("value")
        wind_kmh = (p.get("windSpeed") or {}).get("value")
        obs = {
            "station": station_id,
            "time": p.get("timestamp"),
            "text": p.get("textDescription"),
            "temp_f": round(temp_c * 9 / 5 + 32) if temp_c is not None else None,
            "wind_mph": round(wind_kmh * 0.621) if wind_kmh is not None else None,
        }
    return {
        "periods": [
            {"name": p["name"], "date": p["startTime"][:10], "day": p["isDaytime"], "temp": p["temperature"], "pop": (p.get("probabilityOfPrecipitation") or {}).get("value"),
             "short": p["shortForecast"], "detail": p["detailedForecast"]}
            for p in daily[:14]
        ],
        "alerts": [
            {"event": a["properties"]["event"], "severity": a["properties"]["severity"],
             "headline": a["properties"]["headline"], "ends": a["properties"].get("ends")}
            for a in alerts
        ],
        "obs": obs,
    }


# ---------- 3. Blending ----------

def confidence(spread_f):
    """How much the models disagree, in °F, turned into plain words."""
    if spread_f <= 4:
        return "high"
    if spread_f <= 8:
        return "medium"
    return "low"


def blend_hourly(times, per_model, hours=48):
    now = datetime.now(LOCAL_TZ).strftime("%Y-%m-%dT%H:00")
    start = next((i for i, t in enumerate(times) if t >= now), 0)
    rows = []
    for i in range(start, min(start + hours, len(times))):
        temps = [m["temperature_2m"][i] for m in per_model.values() if m["temperature_2m"][i] is not None]
        precs = [m["precipitation"][i] for m in per_model.values() if m["precipitation"][i] is not None]
        winds = [m["wind_speed_10m"][i] for m in per_model.values() if m["wind_speed_10m"][i] is not None]
        gusts = [m["wind_gusts_10m"][i] for m in per_model.values() if m["wind_gusts_10m"][i] is not None]
        if not temps:
            continue
        rows.append({
            "t": times[i],
            "temp": round(statistics.median(temps)),
            "lo": round(min(temps)), "hi": round(max(temps)),
            # Share of models predicting measurable precipitation (0.01 in or more).
            "wet": round(100 * sum(p >= 0.01 for p in precs) / len(precs)) if precs else None,
            "wind": round(statistics.median(winds)) if winds else None,
            "gust": round(statistics.median(gusts)) if gusts else None,
        })
    return rows


def daily_by_model(times, per_model):
    """Each model's daytime high, overnight low, and total precipitation, by local date.

    The low is the overnight low that follows the day (6 pm to 9 am next morning),
    which is how people read "tonight's low". A midnight-to-midnight low would
    report this morning's temperature, which has already happened.
    """
    out = {}
    for m, cols in per_model.items():
        temps_at = {t: v for t, v in zip(times, cols["temperature_2m"]) if v is not None}
        days = defaultdict(lambda: {"temps": [], "precip": 0.0})
        for t, pr in zip(times, cols["precipitation"]):
            if t in temps_at:
                days[t[:10]]["temps"].append(temps_at[t])
                days[t[:10]]["precip"] += pr or 0
        result = {}
        for date, d in days.items():
            if len(d["temps"]) < 20:  # skip partial days
                continue
            next_day = (datetime.fromisoformat(date) + timedelta(days=1)).strftime("%Y-%m-%d")
            night = [v for t, v in temps_at.items()
                     if (t[:10] == date and t[11:13] >= "18") or (t[:10] == next_day and t[11:13] <= "09")]
            if len(night) < 12:  # no full night of data after the last day
                continue
            result[date] = {"high": round(max(d["temps"])), "low": round(min(night)), "precip": round(d["precip"], 2)}
        out[m] = result
    return out


def blend_daily(by_model):
    dates = sorted({d for days in by_model.values() for d in days})
    rows = []
    for date in dates:
        entries = [days[date] for days in by_model.values() if date in days]
        if len(entries) < 2:
            continue
        highs = [e["high"] for e in entries]
        lows = [e["low"] for e in entries]
        wet = sum(e["precip"] >= 0.02 for e in entries)
        spread = max(max(highs) - min(highs), max(lows) - min(lows))
        rows.append({
            "date": date,
            "high": round(statistics.median(highs)), "high_range": [min(highs), max(highs)],
            "low": round(statistics.median(lows)), "low_range": [min(lows), max(lows)],
            "wet_models": wet, "models": len(entries),
            "confidence": confidence(spread),
        })
    return rows


def summarize(hourly, daily, nws):
    """One plain-English line for today."""
    today_str = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    today = next((d for d in daily if d["date"] == today_str), None)
    if today is None:
        return "Forecast unavailable."
    parts = [f"High {today['high']}° today, low {today['low']}° tonight."]
    wet_hours = [h for h in hourly[:18] if h["wet"] is not None and h["wet"] >= 50]
    if wet_hours:
        first = datetime.fromisoformat(wet_hours[0]["t"]).strftime("%-I %p").lower()
        parts.append(f"Most models show precipitation starting around {first}.")
    else:
        parts.append("Dry for the next 18 hours.")
    peak_gust = max((h["gust"] or 0) for h in hourly[:18]) if hourly else 0
    if peak_gust >= 35:
        parts.append(f"Gusts up to {peak_gust} mph.")
    if today["confidence"] == "low":
        parts.append("The models disagree today, so check back later.")
    if nws and nws["alerts"]:
        parts.append(f"Active alert: {nws['alerts'][0]['event']}.")
    return " ".join(parts)


# ---------- 4. Run ----------

def run():
    os.makedirs(LOG_DIR, exist_ok=True)
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    model_data = fetch_models(list(PLACES.values()))
    for (key, place), (times, per_model) in zip(PLACES.items(), model_data):
        try:
            nws = fetch_nws(place["lat"], place["lon"])
        except Exception as e:  # NWS has outages; the model blend still works without it
            print(f"NWS failed for {key}: {e}")
            nws = None
        hourly = blend_hourly(times, per_model)
        by_model = daily_by_model(times, per_model)
        daily = blend_daily(by_model)
        current = {
            "place": place["name"], "updated": run_at,
            "summary": summarize(hourly, daily, nws),
            "models": [MODELS[m] for m in per_model],
            "hourly": hourly, "daily": daily,
            "by_model": {MODELS[m]: days for m, days in by_model.items()},
            "nws": nws,
        }
        # The log keeps every forecast and observation, so we can score models later.
        log = {"place": key, "run_at": run_at, "by_model": by_model, "obs": nws["obs"] if nws else None}
        with open(os.path.join(DATA_DIR, f"{key}.json"), "w") as f:
            json.dump(current, f)
        with open(os.path.join(LOG_DIR, f"{key}.jsonl"), "a") as f:
            f.write(json.dumps(log) + "\n")
        print(f"{place['name']}: {current['summary']} ({len(per_model)} models)")


if __name__ == "__main__":
    run()
