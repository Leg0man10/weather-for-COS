# Springs & Falcon Weather

A blended forecast for Colorado Springs and Falcon, CO.

Most weather apps show one forecast model. This one asks six independent models
(European, American, German, Canadian, Japanese and the short-range HRRR) plus the
National Weather Service, then shows:

- **The forecast**: the median of the models, so one model that is way off barely moves it.
- **The range**: lowest to highest model. A narrow range means the models agree.
- **Confidence**: models within 4°F is "agree", within 8°F is "some disagreement", more is "disagree".
- **Chance of rain or snow**: the National Weather Service hourly chance.
- **Inches**: the median model, plus the wettest model ("up to"). Snow is shown separately in inches of snow.

## How it runs

| Piece | What it does |
|---|---|
| `fetch.py` | Downloads the forecasts, blends them, writes `data/<place>.json`, and adds a line to `data/log/<place>.jsonl`. Python standard library only. |
| `.github/workflows/refresh.yml` | GitHub Actions runs `fetch.py` every hour and commits the new data. Free. |
| `index.html` | The page. GitHub Pages serves it, and it reads the `data/` files. |

| `morning.py` + `.github/workflows/morning.yml` | Sends a morning breakdown as a push notification through [ntfy](https://ntfy.sh), once a day around 4–5 am Colorado time. The channel name is kept in the `NTFY_TOPIC` repository secret, not in the code. |

Run either by hand: **Actions** tab → pick the workflow → **Run workflow**. Running **Morning message** by hand sends one immediately.

## Accuracy scoring

Every run logs each model's forecast and the real reading from the nearest weather
station. After about 30 days there is enough history to score which model is most
accurate for each place.

Data: [Open-Meteo](https://open-meteo.com) (CC BY 4.0) and the [National Weather Service](https://www.weather.gov).
