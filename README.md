# Los Cabos trip weather

The most likely weather for Los Cabos, November 2 to 9, 2026, updated at 6am and 9pm Vancouver time.

Live site: https://cabo-forecast.netlify.app

## How it works

- `index.html` is the whole site: one static page with no build step.
- `forecast.json` is the data the page reads. A scheduled Claude task rewrites it twice a day and commits it here, and Netlify redeploys on each push.
- `update.py` builds `forecast.json`. It reads global model output from ECMWF, NOAA, DWD and Environment and Climate Change Canada (through Open-Meteo), two long-range ensembles, marine data and the US National Hurricane Center. It discards implausible values and outlier models, then blends the result with 15 years of NOAA station records for Los Cabos.

Run it locally with Python 3.9 or later (standard library only):

```
python3 update.py forecast.json new.json message.json
```

The page also runs as a claude.ai artifact, where it reads the same data from the artifact's database instead of `forecast.json`.
