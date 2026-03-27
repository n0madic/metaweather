# MetaWeather

Multi-model weather aggregator that combines forecasts from 7 independent sources into a single, more reliable prediction using weighted ensemble averaging with outlier dampening.

## Live Demo

**[nomadic.name/metaweather](https://nomadic.name/metaweather/)**

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

## Aggregation Algorithm

- **Temperature**: agreement-aware weighted mean — outliers near the weighted median are dampened using Gaussian falloff based on MAD (median absolute deviation)
- **Wind direction**: circular mean (handles 350°/10° wraparound correctly)
- **Precipitation**: median (robust to outliers), with noise filtering below 0.1mm
- **Weather conditions**: weighted majority vote across sources
- **Confidence score**: mean agreement factor — indicates how well sources agree

## Features

- Auto-detects location via browser geolocation
- Current conditions with per-source breakdown
- Interactive 5-day temperature & precipitation chart
- Toggle individual sources on/off (recalculates aggregated forecast)
- Hover chart legend to highlight source lines
- Celsius / Fahrenheit toggle
- No API keys required — all sources are free and open

## Calibration

```
python3 calibrate.py 17.5                          # auto-detect location
python3 calibrate.py 17.5 --lat 50.34 --lon 30.32  # explicit coordinates
```

Compares real temperature against all sources, suggests weight corrections, and logs results to `calibration_log.csv` for tracking accuracy over time.

## Tech

Single `index.html` file, no build step. Uses [Chart.js](https://www.chartjs.org/) from CDN.
