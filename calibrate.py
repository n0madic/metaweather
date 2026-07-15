#!/usr/bin/env python3
"""
Weather source calibration tool.

Usage:
    python3 calibrate.py 17.5
    python3 calibrate.py 17.5 --lat 50.3398 --lon 30.3199

Fetches current forecasts from all sources, compares to your measured
temperature, computes optimal weight corrections, and logs results
to calibration_log.csv for tracking over time.

Source weights and smart-mean tuning constants are parsed from index.html,
which is the single source of truth — the two can never silently disagree.
"""

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError

LOG_FILE = Path(__file__).parent / "calibration_log.csv"
INDEX_HTML = Path(__file__).parent / "index.html"

HTTP_TIMEOUT = 10  # seconds, per weather-source request
GEO_TIMEOUT = 5    # seconds, per IP-geolocation request

# Endpoints only — weights live in index.html and are parsed at startup
SOURCES = {
    "ecmwf":       {"name": "ECMWF IFS",    "endpoint": "https://api.open-meteo.com/v1/ecmwf"},
    "icon":        {"name": "DWD ICON",      "endpoint": "https://api.open-meteo.com/v1/dwd-icon"},
    "gfs":         {"name": "NOAA GFS",      "endpoint": "https://api.open-meteo.com/v1/gfs"},
    "meteofrance": {"name": "MétéoFrance",   "endpoint": "https://api.open-meteo.com/v1/meteofrance"},
    "gem":         {"name": "GEM",           "endpoint": "https://api.open-meteo.com/v1/gem"},
}

INDEPENDENT = {
    "wttr":       {"name": "wttr.in"},
    "met_norway": {"name": "MET Norway"},
}

# API-key sources (keys via env: TOMORROW_IO_API_KEY, OWM_API_KEY)
API_KEY_SOURCES = {
    "tomorrow_io": {"name": "Tomorrow.io"},
    "owm":         {"name": "OpenWeatherMap"},
}

FALLBACK_LAT = 50.4501  # Kyiv
FALLBACK_LON = 30.5234

# index.html const name -> source key, for the non-Open-Meteo weight blocks
_INDEP_CONST_KEYS = (
    ("WTTR", "wttr"),
    ("MET_NORWAY", "met_norway"),
    ("TOMORROW_IO", "tomorrow_io"),
    ("OWM", "owm"),
)


def load_app_config() -> tuple[dict[str, float], float, float]:
    """Parse source weights and smart-mean constants from index.html.

    Raises RuntimeError when parsing fails, so the tool never calibrates
    against weights that may have drifted from the app.
    """
    try:
        text = INDEX_HTML.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Cannot read {INDEX_HTML}: {exc}") from exc

    weights: dict[str, float] = {}

    models_block = re.search(r"const MODELS = \{(.*?)\n\};", text, re.S)
    if not models_block:
        raise RuntimeError("Cannot find the MODELS block in index.html")
    for key, w in re.findall(r"(\w+):\s*\{[^{}]*?weight:\s*([\d.]+)", models_block.group(1)):
        weights[key] = float(w)

    for const_name, key in _INDEP_CONST_KEYS:
        m = re.search(rf"const {const_name} = \{{[^{{}}]*?weight:\s*([\d.]+)", text)
        if not m:
            raise RuntimeError(f"Cannot parse weight for {const_name} from index.html")
        weights[key] = float(m.group(1))

    missing = [k for k in (*SOURCES, *INDEPENDENT, *API_KEY_SOURCES) if k not in weights]
    if missing:
        raise RuntimeError(f"Weights missing from index.html for: {', '.join(missing)}")

    def parse_const(name: str) -> float:
        m = re.search(rf"const {name} = ([\d.]+)", text)
        if not m:
            raise RuntimeError(f"Cannot parse const {name} from index.html")
        return float(m.group(1))

    return weights, parse_const("MAD_FLOOR"), parse_const("AGREEMENT_SIGMA")


def detect_location() -> tuple[float, float, str]:
    """Try to detect current location via IP geolocation, fall back to Kyiv."""
    services = [
        ("http://ip-api.com/json/?fields=lat,lon,city,country", lambda d: (d["lat"], d["lon"], f"{d.get('city','')}, {d.get('country','')}")),
        ("https://ipwho.is/", lambda d: (d["latitude"], d["longitude"], f"{d.get('city','')}, {d.get('country','')}")),
    ]
    for url, parse in services:
        try:
            data = json.loads(urllib.request.urlopen(url, timeout=GEO_TIMEOUT).read())
            lat, lon, name = parse(data)
            # `is not None` — latitude/longitude 0.0 (equator/prime meridian) is valid
            if lat is not None and lon is not None:
                return float(lat), float(lon), name.strip(", ")
        except (URLError, json.JSONDecodeError, KeyError, ValueError, OSError):
            continue
    return FALLBACK_LAT, FALLBACK_LON, "Kyiv, Ukraine (default)"


def fetch_open_meteo(cfg: dict, lat: float, lon: float) -> float | None:
    url = f"{cfg['endpoint']}?latitude={lat}&longitude={lon}&current=temperature_2m&hourly=temperature_2m&wind_speed_unit=ms&timezone=auto&forecast_days=1"
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=HTTP_TIMEOUT).read())
        # Try current first (this is the same field the app aggregates)
        temp = data.get("current", {}).get("temperature_2m")
        if temp is not None:
            return temp
        # Fallback: nearest hourly point to now. ECMWF never has a `current`
        # block; its weight still drives the app's hourly aggregation, so the
        # nearest forecast hour is the best available calibration signal.
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        if not times:
            return None
        # hourly.time is location-local (timezone=auto) — compare against
        # location-local "now", not UTC
        offset = data.get("utc_offset_seconds", 0) or 0
        now_local = (datetime.now(timezone.utc) + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:")
        for i, t in enumerate(times):
            if t >= now_local and temps[i] is not None:
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
        data = json.loads(urllib.request.urlopen(url, timeout=HTTP_TIMEOUT).read())
        return float(data["current_condition"][0]["temp_C"])
    except (URLError, json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def fetch_met_norway(lat: float, lon: float) -> float | None:
    url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat}&lon={lon}"
    req = urllib.request.Request(url, headers={"User-Agent": "MetaWeather/1.0 github.com/n0madic/metaweather"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read())
        return data["properties"]["timeseries"][0]["data"]["instant"]["details"]["air_temperature"]
    except (URLError, json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def fetch_tomorrow_io(lat: float, lon: float, api_key: str) -> float | None:
    # Same endpoint and field the app aggregates (forecast hourly[0]), so the
    # calibrated weight applies to the signal the app actually uses
    url = f"https://api.tomorrow.io/v4/weather/forecast?location={lat},{lon}&apikey={api_key}&timesteps=1h&units=metric"
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=HTTP_TIMEOUT).read())
        return data["timelines"]["hourly"][0]["values"]["temperature"]
    except (URLError, json.JSONDecodeError, KeyError, IndexError, ValueError, OSError):
        return None


def fetch_owm(lat: float, lon: float, api_key: str) -> float | None:
    # Same endpoint and field the app aggregates (/forecast list[0])
    url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={api_key}&units=metric"
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=HTTP_TIMEOUT).read())
        return data["list"][0]["main"]["temp"]
    except (URLError, json.JSONDecodeError, KeyError, IndexError, ValueError, OSError):
        return None


def smart_mean(
    values: list[float],
    weights: list[float],
    mad_floor: float,
    sigma: float,
) -> tuple[float | None, float]:
    """Agreement-aware weighted mean (port of smartMean in index.html).

    Returns (value, confidence); value is None when there is no data.
    """
    if not values:
        return None, 0.0
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

    # statistics.median averages the two middle deviations for even-length
    # samples — same as arrMedian in index.html
    mad = max(statistics.median(abs(v - med) for v in values), mad_floor)

    agree = [math.exp(-0.5 * ((abs(v - med) / mad) / sigma) ** 2) for v in values]
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


def _log_fieldnames() -> list[str]:
    """Stable CSV schema: every source always has its columns, regardless of
    which API keys were configured for a particular run."""
    fieldnames = ["timestamp", "real_temp"]
    for k in (*SOURCES, *INDEPENDENT, *API_KEY_SOURCES):
        fieldnames.extend([f"{k}_temp", f"{k}_error", f"{k}_weight_current", f"{k}_weight_suggested"])
    fieldnames.extend(["agg_temp_current", "agg_temp_suggested", "agg_error_current", "agg_error_suggested"])
    return fieldnames


def _migrate_log_if_needed(fieldnames: list[str]) -> None:
    """Rewrite an existing log whose header differs from the current schema,
    mapping old rows by column name so appended rows never misalign."""
    if not (LOG_FILE.exists() and LOG_FILE.stat().st_size > 0):
        return
    with open(LOG_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == fieldnames:
            return
        old_rows = list(reader)
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in old_rows:
            writer.writerow({k: row.get(k, "") or "" for k in fieldnames})


def append_log(
    timestamp: str,
    real_temp: float,
    readings: dict[str, float | None],
    current_w: dict[str, float],
    suggested_w: dict[str, float],
    mad_floor: float,
    sigma: float,
) -> None:
    """Append one row to calibration_log.csv."""
    fieldnames = _log_fieldnames()
    _migrate_log_if_needed(fieldnames)
    file_exists = LOG_FILE.exists() and LOG_FILE.stat().st_size > 0

    row: dict[str, object] = {"timestamp": timestamp, "real_temp": real_temp}

    # Per-source
    cur_vals, cur_w_list = [], []
    sug_vals, sug_w_list = [], []
    for k in (*SOURCES, *INDEPENDENT, *API_KEY_SOURCES):
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

    # Aggregated values (caller guarantees at least one reading)
    agg_cur, _ = smart_mean(cur_vals, cur_w_list, mad_floor, sigma)
    agg_sug, _ = smart_mean(sug_vals, sug_w_list, mad_floor, sigma)
    row["agg_temp_current"] = round(agg_cur, 1) if agg_cur is not None else ""
    row["agg_temp_suggested"] = round(agg_sug, 1) if agg_sug is not None else ""
    row["agg_error_current"] = round(abs(real_temp - agg_cur), 1) if agg_cur is not None else ""
    row["agg_error_suggested"] = round(abs(real_temp - agg_sug), 1) if agg_sug is not None else ""

    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate weather source weights")
    parser.add_argument("real_temp", type=float, help="Measured temperature in °C")
    parser.add_argument("--lat", type=float, default=None)
    parser.add_argument("--lon", type=float, default=None)
    args = parser.parse_args()

    if args.lat is not None and (args.lat < -90 or args.lat > 90):
        parser.error("Latitude must be between -90 and 90")
    if args.lon is not None and (args.lon < -180 or args.lon > 180):
        parser.error("Longitude must be between -180 and 180")

    weights, mad_floor, sigma = load_app_config()

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

    # Fetch all sources in parallel — wall clock ~= the slowest source
    tomorrow_key = os.environ.get("TOMORROW_IO_API_KEY", "")
    owm_key = os.environ.get("OWM_API_KEY", "")

    jobs: list[tuple[str, object]] = [
        (key, lambda cfg=cfg: fetch_open_meteo(cfg, lat, lon)) for key, cfg in SOURCES.items()
    ]
    jobs.append(("wttr", lambda: fetch_wttr(lat, lon)))
    jobs.append(("met_norway", lambda: fetch_met_norway(lat, lon)))
    if tomorrow_key:
        jobs.append(("tomorrow_io", lambda: fetch_tomorrow_io(lat, lon, tomorrow_key)))
    if owm_key:
        jobs.append(("owm", lambda: fetch_owm(lat, lon, owm_key)))

    all_names = {
        **{k: v["name"] for k, v in SOURCES.items()},
        **{k: v["name"] for k, v in INDEPENDENT.items()},
        **{k: v["name"] for k, v in API_KEY_SOURCES.items()},
    }

    print("Fetching forecasts...")
    readings: dict[str, float | None] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
        futures = {key: executor.submit(fn) for key, fn in jobs}
        for key, future in futures.items():
            readings[key] = future.result()

    for key in readings:
        temp = readings[key]
        status = f"{temp}°C" if temp is not None else "FAILED"
        print(f"  {all_names[key]:15s}  {status}")

    if not tomorrow_key and not owm_key:
        print("  (set TOMORROW_IO_API_KEY / OWM_API_KEY env vars to include API sources)")

    if all(v is None for v in readings.values()):
        print("\nAll sources failed — nothing to calibrate, nothing logged.")
        sys.exit(1)

    # Current weights (from index.html)
    current_weights = {k: weights[k] for k in (*SOURCES, *INDEPENDENT)}
    for k in API_KEY_SOURCES:
        if k in readings:
            current_weights[k] = weights[k]

    # Suggested weights
    suggested = compute_suggested_weights(readings, current_weights, real)

    # Aggregated temps
    cur_vals = [readings[k] for k in current_weights if readings.get(k) is not None]
    cur_w = [current_weights[k] for k in current_weights if readings.get(k) is not None]
    sug_w = [suggested[k] for k in current_weights if readings.get(k) is not None]

    agg_cur, conf_cur = smart_mean(cur_vals, cur_w, mad_floor, sigma)
    agg_sug, conf_sug = smart_mean(cur_vals, sug_w, mad_floor, sigma)

    # Display results
    print()
    print("=" * 78)
    print(f"{'Source':15s} {'Temp':>7s} {'Error':>7s} {'Current W':>10s} {'Suggested W':>12s} {'Delta':>7s}")
    print("-" * 78)

    all_keys = list(SOURCES) + list(INDEPENDENT) + [k for k in API_KEY_SOURCES if k in readings]

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
    append_log(now, real, readings, current_weights, suggested, mad_floor, sigma)
    print(f"\nLogged to {LOG_FILE}")

    # Show history if exists
    if LOG_FILE.exists():
        with open(LOG_FILE, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if len(rows) > 1:
            print(f"\n--- Calibration History ({len(rows)} entries) ---")
            print(f"{'Date':20s} {'Real':>6s} {'Agg(cur)':>9s} {'Err(cur)':>9s} {'Agg(sug)':>9s} {'Err(sug)':>9s}")
            for r in rows[-10:]:
                cells = [
                    (r.get("timestamp") or "")[:16],
                    r.get("real_temp") or "",
                    r.get("agg_temp_current") or "",
                    r.get("agg_error_current") or "",
                    r.get("agg_temp_suggested") or "",
                    r.get("agg_error_suggested") or "",
                ]
                print(f"  {cells[0]:18s} {cells[1]:>6s} {cells[2]:>9s} {cells[3]:>9s} {cells[4]:>9s} {cells[5]:>9s}")


if __name__ == "__main__":
    main()
