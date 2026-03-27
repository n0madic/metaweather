#!/usr/bin/env python3
"""
Weather source calibration tool.

Usage:
    python3 calibrate.py 17.5
    python3 calibrate.py 17.5 --lat 50.3398 --lon 30.3199

Fetches current forecasts from all sources, compares to your measured
temperature, computes optimal weight corrections, and logs results
to calibration_log.csv for tracking over time.
"""

import argparse
import json
import math
import os
import csv
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

LOG_FILE = Path(__file__).parent / "calibration_log.csv"

# Must match index.html weights
SOURCES = {
    "ecmwf":       {"name": "ECMWF IFS",    "endpoint": "https://api.open-meteo.com/v1/ecmwf",       "weight": 0.6},
    "icon":        {"name": "DWD ICON",      "endpoint": "https://api.open-meteo.com/v1/dwd-icon",    "weight": 1.0},
    "gfs":         {"name": "NOAA GFS",      "endpoint": "https://api.open-meteo.com/v1/gfs",         "weight": 0.5},
    "meteofrance": {"name": "MétéoFrance",   "endpoint": "https://api.open-meteo.com/v1/meteofrance", "weight": 0.7},
    "gem":         {"name": "GEM",           "endpoint": "https://api.open-meteo.com/v1/gem",          "weight": 0.85},
}

INDEPENDENT = {
    "wttr":       {"name": "wttr.in",    "weight": 0.45},
    "met_norway": {"name": "MET Norway", "weight": 1.0},
}

# API-key sources (keys via env: TOMORROW_IO_API_KEY, OWM_API_KEY)
API_KEY_SOURCES = {
    "tomorrow_io": {"name": "Tomorrow.io",      "weight": 0.7},
    "owm":         {"name": "OpenWeatherMap",    "weight": 0.55},
}

FALLBACK_LAT = 50.4501  # Kyiv
FALLBACK_LON = 30.5234


def detect_location() -> tuple[float, float, str]:
    """Try to detect current location via IP geolocation, fall back to Kyiv."""
    services = [
        ("http://ip-api.com/json/?fields=lat,lon,city,country", lambda d: (d["lat"], d["lon"], f"{d.get('city','')}, {d.get('country','')}")),
        ("https://ipwho.is/", lambda d: (d["latitude"], d["longitude"], f"{d.get('city','')}, {d.get('country','')}")),
    ]
    for url, parse in services:
        try:
            data = json.loads(urllib.request.urlopen(url, timeout=5).read())
            lat, lon, name = parse(data)
            if lat and lon:
                return float(lat), float(lon), name.strip(", ")
        except (URLError, json.JSONDecodeError, KeyError, ValueError, OSError):
            continue
    return FALLBACK_LAT, FALLBACK_LON, "Kyiv, Ukraine (default)"


def fetch_open_meteo(cfg: dict, lat: float, lon: float) -> float | None:
    url = f"{cfg['endpoint']}?latitude={lat}&longitude={lon}&current=temperature_2m&hourly=temperature_2m&wind_speed_unit=ms&timezone=auto&forecast_days=1"
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=10).read())
        # Try current first
        temp = data.get("current", {}).get("temperature_2m")
        if temp is not None:
            return temp
        # Fallback: nearest hourly point to now
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        if not times:
            return None
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:")
        for i, t in enumerate(times):
            if t >= now and temps[i] is not None:
                return temps[i]
        # If all future points exhausted, use last available
        for t in reversed(temps):
            if t is not None:
                return t
        return None
    except (URLError, json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def fetch_wttr(lat: float, lon: float) -> float | None:
    url = f"https://wttr.in/{lat},{lon}?format=j1"
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=10).read())
        return float(data["current_condition"][0]["temp_C"])
    except (URLError, json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def fetch_met_norway(lat: float, lon: float) -> float | None:
    url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat}&lon={lon}"
    req = urllib.request.Request(url, headers={"User-Agent": "MetaWeather/1.0 github.com/metaweather"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return data["properties"]["timeseries"][0]["data"]["instant"]["details"]["air_temperature"]
    except (URLError, json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def fetch_tomorrow_io(lat: float, lon: float, api_key: str) -> float | None:
    url = f"https://api.tomorrow.io/v4/weather/forecast?location={lat},{lon}&apikey={api_key}&timesteps=1h&units=metric"
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=10).read())
        hourly = data.get("timelines", {}).get("hourly", [])
        if hourly:
            return hourly[0]["values"]["temperature"]
        return None
    except (URLError, json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def fetch_owm(lat: float, lon: float, api_key: str) -> float | None:
    url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={api_key}&units=metric"
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=10).read())
        lst = data.get("list", [])
        if lst:
            return lst[0]["main"]["temp"]
        return None
    except (URLError, json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def smart_mean(values: list[float], weights: list[float]) -> tuple[float, float]:
    """Agreement-aware weighted mean. Returns (value, confidence)."""
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        return values[0], 0.5

    pairs = sorted(zip(values, weights), key=lambda x: x[0])
    cum, total = 0.0, sum(weights)
    med = pairs[-1][0]
    for v, w in pairs:
        cum += w
        if cum >= total / 2:
            med = v
            break

    devs = sorted(abs(v - med) for v in values)
    mad = max(devs[len(devs) // 2], 0.1)

    agree = [math.exp(-0.5 * ((abs(v - med) / mad) / 2) ** 2) for v in values]
    final_w = [w * a for w, a in zip(weights, agree)]
    sw = sum(final_w)
    result = sum(v * w for v, w in zip(values, final_w)) / sw
    confidence = sum(agree) / len(agree)
    return result, confidence


def compute_suggested_weights(
    readings: dict[str, float | None],
    current_weights: dict[str, float],
    real_temp: float,
) -> dict[str, float]:
    """Compute suggested weights based on inverse error."""
    errors = {}
    for key, temp in readings.items():
        if temp is not None:
            errors[key] = abs(real_temp - temp)

    if not errors:
        return current_weights

    # Inverse error scoring: weight ~ 1 / (1 + error)
    raw_scores = {k: 1.0 / (1.0 + e) for k, e in errors.items()}
    max_score = max(raw_scores.values())
    # Normalize so best source = 1.0
    suggested = {}
    for key in current_weights:
        if key in raw_scores:
            suggested[key] = round(raw_scores[key] / max_score, 2)
        else:
            # Source failed — decay weight but enforce a floor
            suggested[key] = round(max(current_weights[key] * 0.75, 0.1), 2)

    return suggested


def append_log(timestamp: str, real_temp: float, readings: dict, current_w: dict, suggested_w: dict):
    """Append one row to calibration_log.csv."""
    file_exists = LOG_FILE.exists() and LOG_FILE.stat().st_size > 0

    all_keys = list(SOURCES.keys()) + list(INDEPENDENT.keys()) + [k for k in API_KEY_SOURCES if readings.get(k) is not None or k in current_w]
    fieldnames = ["timestamp", "real_temp"]
    for k in all_keys:
        fieldnames.extend([f"{k}_temp", f"{k}_error", f"{k}_weight_current", f"{k}_weight_suggested"])
    fieldnames.extend(["agg_temp_current", "agg_temp_suggested", "agg_error_current", "agg_error_suggested"])

    row = {"timestamp": timestamp, "real_temp": real_temp}

    # Per-source
    cur_vals, cur_w_list = [], []
    sug_vals, sug_w_list = [], []
    for k in all_keys:
        temp = readings.get(k)
        row[f"{k}_temp"] = temp if temp is not None else ""
        row[f"{k}_error"] = round(abs(real_temp - temp), 1) if temp is not None else ""
        row[f"{k}_weight_current"] = current_w.get(k, "")
        row[f"{k}_weight_suggested"] = suggested_w.get(k, "")
        if temp is not None:
            cur_vals.append(temp)
            cur_w_list.append(current_w.get(k, 0.5))
            sug_vals.append(temp)
            sug_w_list.append(suggested_w.get(k, 0.5))

    # Aggregated values
    agg_cur, _ = smart_mean(cur_vals, cur_w_list)
    agg_sug, _ = smart_mean(sug_vals, sug_w_list)
    row["agg_temp_current"] = round(agg_cur, 1)
    row["agg_temp_suggested"] = round(agg_sug, 1)
    row["agg_error_current"] = round(abs(real_temp - agg_cur), 1)
    row["agg_error_suggested"] = round(abs(real_temp - agg_sug), 1)

    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Calibrate weather source weights")
    parser.add_argument("real_temp", type=float, help="Measured temperature in °C")
    parser.add_argument("--lat", type=float, default=None)
    parser.add_argument("--lon", type=float, default=None)
    args = parser.parse_args()

    if args.lat is not None and (args.lat < -90 or args.lat > 90):
        parser.error("Latitude must be between -90 and 90")
    if args.lon is not None and (args.lon < -180 or args.lon > 180):
        parser.error("Longitude must be between -180 and 180")

    real = args.real_temp
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.lat is not None and args.lon is not None:
        lat, lon, loc_name = args.lat, args.lon, f"{args.lat}, {args.lon}"
    else:
        print("Detecting location...")
        lat, lon, loc_name = detect_location()

    print(f"Real temperature: {real}°C")
    print(f"Location: {loc_name} ({lat}, {lon})")
    print(f"Time: {now}")
    print()

    # Fetch all sources
    readings: dict[str, float | None] = {}
    print("Fetching forecasts...")
    for key, cfg in SOURCES.items():
        temp = fetch_open_meteo(cfg, lat, lon)
        readings[key] = temp
        status = f"{temp}°C" if temp is not None else "FAILED"
        print(f"  {cfg['name']:15s}  {status}")

    temp = fetch_wttr(lat, lon)
    readings["wttr"] = temp
    print(f"  {'wttr.in':15s}  {temp}°C" if temp is not None else f"  {'wttr.in':15s}  FAILED")

    temp = fetch_met_norway(lat, lon)
    readings["met_norway"] = temp
    print(f"  {'MET Norway':15s}  {temp}°C" if temp is not None else f"  {'MET Norway':15s}  FAILED")

    # API-key sources (from environment variables)
    tomorrow_key = os.environ.get("TOMORROW_IO_API_KEY", "")
    owm_key = os.environ.get("OWM_API_KEY", "")

    if tomorrow_key:
        temp = fetch_tomorrow_io(lat, lon, tomorrow_key)
        readings["tomorrow_io"] = temp
        print(f"  {'Tomorrow.io':15s}  {temp}°C" if temp is not None else f"  {'Tomorrow.io':15s}  FAILED")

    if owm_key:
        temp = fetch_owm(lat, lon, owm_key)
        readings["owm"] = temp
        print(f"  {'OpenWeatherMap':15s}  {temp}°C" if temp is not None else f"  {'OpenWeatherMap':15s}  FAILED")

    if not tomorrow_key and not owm_key:
        print("  (set TOMORROW_IO_API_KEY / OWM_API_KEY env vars to include API sources)")

    # Current weights
    current_weights = {k: v["weight"] for k, v in SOURCES.items()}
    current_weights.update({k: v["weight"] for k, v in INDEPENDENT.items()})
    for k, v in API_KEY_SOURCES.items():
        if k in readings:
            current_weights[k] = v["weight"]

    # Suggested weights
    suggested = compute_suggested_weights(readings, current_weights, real)

    # Aggregated temps
    cur_vals = [readings[k] for k in current_weights if readings.get(k) is not None]
    cur_w = [current_weights[k] for k in current_weights if readings.get(k) is not None]
    sug_w = [suggested[k] for k in current_weights if readings.get(k) is not None]

    agg_cur, conf_cur = smart_mean(cur_vals, cur_w)
    agg_sug, conf_sug = smart_mean(cur_vals, sug_w)

    # Display results
    print()
    print("=" * 78)
    print(f"{'Source':15s} {'Temp':>7s} {'Error':>7s} {'Current W':>10s} {'Suggested W':>12s} {'Delta':>7s}")
    print("-" * 78)

    all_keys = list(SOURCES.keys()) + list(INDEPENDENT.keys()) + [k for k in API_KEY_SOURCES if readings.get(k) is not None or k in current_weights]
    all_names = {**{k: v["name"] for k, v in SOURCES.items()}, **{k: v["name"] for k, v in INDEPENDENT.items()}, **{k: v["name"] for k, v in API_KEY_SOURCES.items()}}

    for key in all_keys:
        name = all_names[key]
        temp = readings.get(key)
        if temp is None:
            print(f"  {name:15s}    N/A       -  {current_weights[key]:>9.2f}  {suggested.get(key, 0):>11.2f}       -")
            continue
        err = abs(real - temp)
        cw = current_weights[key]
        sw = suggested.get(key, cw)
        delta = sw - cw
        sign = "+" if delta >= 0 else ""
        print(f"  {name:15s} {temp:>6.1f}° {err:>6.1f}°  {cw:>9.2f}  {sw:>11.2f}  {sign}{delta:>5.2f}")

    print("-" * 78)
    print(f"  {'AGGREGATED':15s} {agg_cur:>6.1f}° {abs(real - agg_cur):>6.1f}°  {'(current)':>9s}  {agg_sug:>6.1f}° err={abs(real-agg_sug):.1f}°")
    print(f"  {'Confidence':15s} {conf_cur:>6.0%}         {'':>9s}  {conf_sug:>6.0%}")
    print("=" * 78)

    # Log
    append_log(now, real, readings, current_weights, suggested)
    print(f"\nLogged to {LOG_FILE}")

    # Show history if exists
    if LOG_FILE.exists():
        with open(LOG_FILE, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if len(rows) > 1:
            print(f"\n--- Calibration History ({len(rows)} entries) ---")
            print(f"{'Date':20s} {'Real':>6s} {'Agg(cur)':>9s} {'Err(cur)':>9s} {'Agg(sug)':>9s} {'Err(sug)':>9s}")
            for r in rows[-10:]:
                print(f"  {r['timestamp'][:16]:18s} {r['real_temp']:>6s} {r['agg_temp_current']:>9s} {r['agg_error_current']:>9s} {r['agg_temp_suggested']:>9s} {r['agg_error_suggested']:>9s}")


if __name__ == "__main__":
    main()
