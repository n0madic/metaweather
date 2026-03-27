# MetaWeather

Multi-model weather aggregator that combines forecasts from up to 9 independent sources into a single, more reliable prediction using weighted ensemble averaging with outlier dampening.

## Live Demo

**[nomadic.name/metaweather](https://nomadic.name/metaweather/)**

Installable as a PWA — add to home screen on mobile for a native app-like experience.

## Sources

| Source | Type | Description |
|--------|------|-------------|
| DWD ICON | Open-Meteo | German Weather Service, 7km Europe |
| MET Norway | Independent | Norwegian Met Institute, ECMWF post-processed |
| GEM | Open-Meteo | Canadian Met Centre, 25km global |
| MétéoFrance | Open-Meteo | ARPEGE model, 10km global |
| ECMWF IFS | Open-Meteo | European Centre, 9km global |
| NOAA GFS | Open-Meteo | US Weather Service, 25km global |
| wttr.in | Independent | World Weather Online |
| Tomorrow.io | API key | High-resolution global forecast (optional) |
| OpenWeatherMap | API key | 3-hour global forecast (optional) |

Default sources work without API keys. Tomorrow.io and OpenWeatherMap can be added via the settings button (gear icon in the header) by providing free API keys.

Source weights are calibrated against real measurements using `calibrate.py`.

## Aggregation Algorithm

- **Temperature**: agreement-aware weighted mean — outliers near the weighted median are dampened using Gaussian falloff based on MAD (median absolute deviation)
- **Wind direction**: circular mean (handles 350°/10° wraparound correctly)
- **Precipitation**: median (robust to outliers), with noise filtering below 0.1mm
- **Weather conditions**: weighted majority vote across sources
- **Confidence score**: mean agreement factor — indicates how well sources agree

## Features

- Auto-detects location via browser geolocation
- Current conditions with per-source breakdown and confidence score
- Interactive 5-day temperature & precipitation chart (Chart.js)
- Toggle individual sources on/off via header badges (recalculates aggregated forecast)
- Hover chart legend to highlight source lines
- Celsius / Fahrenheit toggle
- Refresh button — re-fetches all sources without page reload
- Settings persisted in localStorage (disabled sources, temperature unit, API keys)
- API settings modal — add optional Tomorrow.io and OpenWeatherMap API keys for extra sources
- PWA — installable on mobile, standalone mode, service worker caching

## Calibration

```
python3 calibrate.py 17.5                          # auto-detect location via IP
python3 calibrate.py 17.5 --lat 50.34 --lon 30.32  # explicit coordinates

# Include API-key sources via environment variables:
TOMORROW_IO_API_KEY=... OWM_API_KEY=... python3 calibrate.py 17.5
```

Compares real temperature against all sources, suggests weight corrections, and logs results to `calibration_log.csv` for tracking accuracy over time.

## Tech

Single `index.html` file, no build step. Uses [Chart.js](https://www.chartjs.org/) from CDN.
