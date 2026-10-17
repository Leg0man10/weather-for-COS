"""Morning weather message for Colorado Springs and Falcon, sent as a push notification.

Reads the blended forecast that fetch.py just wrote (data/<place>.json) and sends a
short breakdown through ntfy (https://ntfy.sh), a free push-notification app.

GitHub's schedule is unreliable, so the workflow tries several times each morning.
This script sends at most once per Colorado day, and only between 4 and 9 am.

Run:  NTFY_TOPIC=<your topic> python3 morning.py           (normal)
      NTFY_TOPIC=<your topic> python3 morning.py --force   (send now, for testing)
      python3 morning.py --dry-run                          (print, don't send)
"""

import json
import os
import sys
import urllib.request
from datetime import datetime

from fetch import DATA_DIR, LOCAL_TZ, PLACES

PAGE_URL = "https://leg0man10.github.io/weather-for-COS/"
SENT_FILE = os.path.join(DATA_DIR, "morning_sent.txt")
WINDOW = (4, 9)  # local hours: send no earlier than 4 am, give up after 9 am


def fmt_hour(t):
    h = int(t[11:13])
    return f"{h % 12 or 12}{'a' if h < 12 else 'p'}"


def place_lines(d, today):
    day = next((r for r in d["daily"] if r["date"] == today), None)
    if day is None:
        return [f"{d['place']}: forecast unavailable."], {}
    rows = [h for h in d["hourly"] if h["t"][:10] == today]
    lines = [f"{d['place']}: high {day['high']}°, low tonight {day['low']}°"]

    # How the day unfolds: a few points so you can dress for the morning and the afternoon.
    marks = [h for h in rows if int(h["t"][11:13]) in (6, 9, 12, 15, 18, 21)]
    if marks:
        lines.append("  " + " → ".join(f"{fmt_hour(h['t'])} {h['temp']}°" for h in marks))

    # Rain or snow: NWS chance plus the models' amount.
    pops = [h["pop"] for h in rows if h.get("pop") is not None]
    peak = max(pops) if pops else None
    snow = sum(h.get("snow") or 0 for h in rows)
    if peak is not None and peak >= 20:
        first = next(h for h in rows if (h.get("pop") or 0) >= 20)
        amt = f", about {snow:.1f} in of snow" if snow >= 0.1 else (f", about {day['precip']:.2f} in" if day["precip"] >= 0.01 else "")
        lines.append(f"  Rain/snow: up to {peak}% chance from {fmt_hour(first['t'])}{amt}")
    elif day["precip_hi"] >= 0.05:
        lines.append(f"  Rain/snow: unlikely (wettest model {day['precip_hi']:.2f} in)")
    else:
        lines.append("  Rain/snow: dry")

    gust = max((h.get("gust") or 0) for h in rows) if rows else 0
    if gust >= 25:
        lines.append(f"  Wind: gusts up to {gust} mph")
    if day["confidence"] == "low":
        hs = day["high_range"][1] - day["high_range"][0]
        ls = day["low_range"][1] - day["low_range"][0]
        what = f"tonight's low ({day['low_range'][0]}–{day['low_range'][1]}°)" if ls >= hs else f"the high ({day['high_range'][0]}–{day['high_range'][1]}°)"
        lines.append(f"  Uncertain: models disagree on {what}")
    return lines, {"peak": peak or 0, "snow": snow, "day": day}


def build_message(now):
    today = now.strftime("%Y-%m-%d")
    data = {}
    for key in PLACES:
        with open(os.path.join(DATA_DIR, f"{key}.json")) as f:
            data[key] = json.load(f)

    body, info = [], {}
    for key, d in data.items():
        lines, info[key] = place_lines(d, today)
        body += lines + [""]

    alerts = {}
    for d in data.values():
        for a in (d.get("nws") or {}).get("alerts") or []:
            alerts.setdefault(a["event"], a)
    for a in alerts.values():
        ends = ""
        if a.get("ends"):
            e = datetime.fromisoformat(a["ends"]).astimezone(LOCAL_TZ)
            ends = f" until {e.strftime('%a')} {e.hour % 12 or 12}{'a' if e.hour < 12 else 'p'}"
        body.append(f"Alert: {a['event']}{ends}")

    title = f"{now.strftime('%a')}: " + " · ".join(
        f"{d['place'].replace('Colorado ', '')} {i['day']['high']}°/{i['day']['low']}°"
        for d, i in zip(data.values(), info.values()) if i.get("day"))
    snow = any(i.get("snow", 0) >= 0.1 for i in info.values())
    wet = any(i.get("peak", 0) >= 40 for i in info.values())
    tags = ["warning"] if alerts else []
    tags.append("snowflake" if snow else "cloud_with_rain" if wet else "partly_sunny")
    severe = any(a.get("severity") in ("Severe", "Extreme") for a in alerts.values())
    return title, "\n".join(body).strip(), tags, ("high" if severe else "default")


def already_sent(today):
    try:
        with open(SENT_FILE) as f:
            return f.read().strip() == today
    except FileNotFoundError:
        return False


def send(topic, title, body, tags, priority):
    # JSON publishing keeps the title's ° and · intact (HTTP headers are ASCII-only).
    payload = {"topic": topic, "title": title, "message": body, "tags": tags,
               "priority": 4 if priority == "high" else 3, "click": PAGE_URL}
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def main():
    force, dry = "--force" in sys.argv, "--dry-run" in sys.argv
    now = datetime.now(LOCAL_TZ)
    today = now.strftime("%Y-%m-%d")
    if not (force or dry):
        if already_sent(today):
            print(f"Already sent for {today}.")
            return
        if not WINDOW[0] <= now.hour < WINDOW[1]:
            print(f"Outside the morning window ({now:%H:%M} local); not sending.")
            return
    title, body, tags, priority = build_message(now)
    print(title, "\n" + body, f"\n[tags={tags} priority={priority}]")
    if dry:
        return
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        sys.exit("NTFY_TOPIC is not set; add it under Settings → Secrets and variables → Actions.")
    send(topic, title, body, tags, priority)
    if not force:
        with open(SENT_FILE, "w") as f:
            f.write(today + "\n")
    print("Sent.")


if __name__ == "__main__":
    main()
