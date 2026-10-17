#!/usr/bin/env python3
"""Los Cabos trip forecast updater (Nov 2-9, 2026).

Usage: python3 -I update.py PREV_STATE.json OUT_STATE.json OUT_MESSAGE.json ARTIFACT_URL

Pulls raw model output from national weather services (via Open-Meteo), extended-range
ensembles, and the US National Hurricane Center, validates every value, blends it with a
16-year station climatology, and writes the new state plus an optional Slack message.
Standard library only.
"""
import json, math, sys, urllib.request, urllib.error, urllib.parse, datetime as dt, re, statistics as st
from zoneinfo import ZoneInfo

LAT, LON = 22.89, -109.91            # Cabo San Lucas / corridor midpoint
TZ_LOCAL = ZoneInfo("America/Mazatlan")   # Baja California Sur, UTC-7 year-round
TZ_USER = ZoneInfo("America/Vancouver")
TRIP = [dt.date(2026, 11, d) for d in range(2, 10)]
DAILY_HOUR = 5                         # daily Slack summary goes out with the 6am run

# Deterministic global models and the agency that runs them, with blend weights
MODELS = {
    "ecmwf_ifs025":  ("ECMWF IFS (Europe)", 1.5),
    "ecmwf_aifs025": ("ECMWF AIFS (Europe, AI)", 1.2),
    "gfs_seamless":  ("NOAA GFS (USA)", 1.0),
    "icon_seamless": ("DWD ICON (Germany)", 1.0),
    "gem_seamless":  ("ECCC GEM (Canada)", 0.8),
}
ENSEMBLES = {  # model id -> (label, forecast days)
    "ecmwf_ifs025": ("ECMWF ENS, 51 members", 15),
    "gfs05":        ("NOAA GEFS, 31 members", 35),
}
VARS = ["temperature_2m_max", "temperature_2m_min", "relative_humidity_2m_mean",
        "precipitation_sum", "wind_speed_10m_max", "cloud_cover_mean"]
SHORT = {"temperature_2m_max": "hi", "temperature_2m_min": "lo", "relative_humidity_2m_mean": "rh",
         "precipitation_sum": "mm", "wind_speed_10m_max": "wind", "cloud_cover_mean": "cloud"}
PLAUSIBLE = {"hi": (15, 42), "lo": (8, 32), "rh": (5, 100), "mm": (0, 300), "wind": (0, 200), "cloud": (0, 100)}

# NOAA NCEI Global Summary of the Day, Los Cabos Intl (MMSD) + Cabo San Lucas (MMSL), 2010-2024
CLIMO = {
    "11-02": {"hi": 30.0, "lo": 20.5, "rh": 58, "rain": 4},
    "11-03": {"hi": 30.0, "lo": 20.0, "rh": 60, "rain": 4},
    "11-04": {"hi": 30.0, "lo": 21.0, "rh": 57, "rain": 3},
    "11-05": {"hi": 29.0, "lo": 21.0, "rh": 55, "rain": 3},
    "11-06": {"hi": 29.5, "lo": 20.0, "rh": 58, "rain": 3},
    "11-07": {"hi": 29.0, "lo": 20.0, "rh": 59, "rain": 3},
    "11-08": {"hi": 30.0, "lo": 20.0, "rh": 56, "rain": 4},
    "11-09": {"hi": 29.0, "lo": 20.0, "rh": 55, "rain": 4},
}

def get(url, timeout=40, tries=3):
    import time
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cabo-trip-forecast/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                break  # daily quota on a shared IP: retrying won't help this hour
        except Exception as e:
            last = e
        time.sleep(4 * (i + 1))
    raise last

def ok(k, v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return False
    lo, hi = PLAUSIBLE[k]
    return lo <= v <= hi

def wmean(pairs):
    tw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tw if tw else None

prev = {}
try:
    prev = json.load(open(sys.argv[1]))
    if not isinstance(prev, dict): prev = {}
except Exception:
    prev = {}
raw_prev = prev.get("raw", {})
now_utc = dt.datetime.now(dt.timezone.utc)
today_local = now_utc.astimezone(TZ_LOCAL).date()
sources, rejected = [], []

# ---------- 1. Deterministic models ----------
det = {d.isoformat(): {} for d in TRIP}
q = urllib.parse.urlencode({"latitude": LAT, "longitude": LON, "daily": ",".join(VARS),
                            "models": ",".join(MODELS), "timezone": "America/Mazatlan",
                            "forecast_days": 16, "cell_selection": "land"})
try:
    j = json.loads(get("https://api.open-meteo.com/v1/forecast?" + q))
    if j.get("error"):
        raise RuntimeError(j.get("reason"))
    times = j["daily"]["time"]
    for m in MODELS:
        got = 0
        for i, t in enumerate(times):
            if t not in det:
                continue
            row = {}
            for v in VARS:
                val = j["daily"].get(f"{v}_{m}", [None] * len(times))[i]
                if val is None:
                    continue
                if ok(SHORT[v], val):
                    row[SHORT[v]] = round(val, 1)
                else:
                    rejected.append(f"{MODELS[m][0]} {t} {SHORT[v]}={val} (implausible)")
            if "hi" in row and "lo" in row:
                if row["lo"] > row["hi"]:
                    rejected.append(f"{MODELS[m][0]} {t}: low above high, dropped")
                    continue
                det[t][m] = row
                got += 1
        sources.append({"name": MODELS[m][0], "kind": "Deterministic model", "status": "ok" if got else "out of range",
                        "days": got})
except Exception as e:
    sources.append({"name": "Global deterministic models", "kind": "Deterministic model", "status": f"error: {str(e)[:120]}", "days": 0})

# Outlier screen: drop a model whose high/low is >5 C from the median of the others
for t, rows in det.items():
    if len(rows) >= 3:
        for k in ("hi", "lo"):
            for m in list(rows):
                others = [r[k] for mm, r in rows.items() if mm != m and k in r]
                if k in rows[m] and others and abs(rows[m][k] - st.median(others)) > 5:
                    rejected.append(f"{MODELS[m][0]} {t}: {k} {rows[m][k]} is >5°C from consensus, dropped")
                    rows.pop(m)
                    break

# ---------- 2. Ensembles ----------
ens = {d.isoformat(): {} for d in TRIP}
for em, (label, fdays) in ENSEMBLES.items():
    q = urllib.parse.urlencode({"latitude": LAT, "longitude": LON,
                                "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                                "models": em, "timezone": "America/Mazatlan", "forecast_days": fdays,
                                "cell_selection": "land"})
    try:
        j = json.loads(get("https://ensemble-api.open-meteo.com/v1/ensemble?" + q))
        if j.get("error"):
            raise RuntimeError(j.get("reason"))
        d = j["daily"]; times = d["time"]; got = 0
        for i, t in enumerate(times):
            if t not in ens:
                continue
            def members(prefix):
                vals = [d[k][i] for k in d if (k == prefix or k.startswith(prefix + "_member")) and d[k][i] is not None]
                return vals
            his = [v for v in members("temperature_2m_max") if ok("hi", v)]
            los = [v for v in members("temperature_2m_min") if ok("lo", v)]
            pr = [v for v in members("precipitation_sum") if ok("mm", v)]
            if len(his) >= 10 and len(pr) >= 10:
                ens[t][em] = {"hi": round(st.mean(his), 1), "lo": round(st.mean(los), 1) if los else None,
                              "hi_sd": round(st.pstdev(his), 1), "rain": round(100 * sum(p >= 1 for p in pr) / len(pr)),
                              "mm": round(st.mean(pr), 1), "n": len(his)}
                got += 1
        sources.append({"name": label, "kind": "Ensemble", "status": "ok" if got else "out of range", "days": got})
    except Exception as e:
        sources.append({"name": label, "kind": "Ensemble", "status": f"error: {str(e)[:120]}", "days": 0})

# ---------- 2b. Trip extras: sun times, UV, feels-like, hourly, sea ----------
extras = {d.isoformat(): {} for d in TRIP}
sea = {}
try:
    q = urllib.parse.urlencode({"latitude": LAT, "longitude": LON, "timezone": "America/Mazatlan", "forecast_days": 16,
                                "cell_selection": "land",
                                "daily": "sunrise,sunset,uv_index_max,apparent_temperature_max",
                                "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,cloud_cover"})
    j = json.loads(get("https://api.open-meteo.com/v1/forecast?" + q))
    if j.get("error"): raise RuntimeError(j.get("reason"))
    dd = j["daily"]
    for i, t in enumerate(dd["time"]):
        if t in extras:
            e = extras[t]
            if dd["sunrise"][i]: e["sunrise"] = dd["sunrise"][i][11:16]
            if dd["sunset"][i]: e["sunset"] = dd["sunset"][i][11:16]
            u = dd["uv_index_max"][i]
            if u is not None and 0 <= u <= 16: e["uv"] = round(u, 1)
            a = dd["apparent_temperature_max"][i]
            if a is not None and 10 <= a <= 50: e["feels"] = round(a, 1)
    hh = j["hourly"]
    for i, ts in enumerate(hh["time"]):
        t, hr = ts[:10], int(ts[11:13])
        if t not in extras or hr % 3: continue
        if (dt.date.fromisoformat(t) - today_local).days > 7: continue
        tv, pv, wv, cv = (hh[k][i] for k in ("temperature_2m", "precipitation_probability", "wind_speed_10m", "cloud_cover"))
        if tv is None or not ok("hi", tv) and not ok("lo", tv): continue
        extras[t].setdefault("hourly", []).append({"h": hr, "t": round(tv, 1), "p": pv, "w": round(wv) if wv is not None else None, "c": cv})
except Exception as e:
    sources.append({"name": "Trip details (sun, UV, hourly)", "kind": "Detail", "status": f"error: {str(e)[:120]}", "days": 0})
try:
    q = urllib.parse.urlencode({"latitude": 22.87, "longitude": -109.9, "timezone": "America/Mazatlan", "forecast_days": 16,
                                "current": "sea_surface_temperature", "daily": "wave_height_max"})
    j = json.loads(get("https://marine-api.open-meteo.com/v1/marine?" + q))
    if j.get("error"): raise RuntimeError(j.get("reason"))
    sst = j.get("current", {}).get("sea_surface_temperature")
    if sst is not None and 15 <= sst <= 34:
        sea = {"now": round(sst, 1), "at": j["current"]["time"]}
    for i, t in enumerate(j["daily"]["time"]):
        w = j["daily"]["wave_height_max"][i]
        if t in extras and w is not None and 0 <= w <= 12: extras[t]["wave"] = round(w, 1)
except Exception as e:
    sources.append({"name": "Marine (sea temperature, waves)", "kind": "Detail", "status": f"error: {str(e)[:120]}", "days": 0})

# ---------- 3. National Hurricane Center ----------
tropical = {"active": [], "outlook": "", "outlook_issued": "", "threat": False, "status": "ok"}
def dist_km(a, b, c, d):
    p = math.pi / 180
    h = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))
try:
    cs = json.loads(get("https://www.nhc.noaa.gov/CurrentStorms.json"))
    for s in cs.get("activeStorms", []):
        if not str(s.get("id", "")).startswith(("ep", "cp")):
            continue
        dk = dist_km(LAT, LON, s["latitudeNumeric"], s["longitudeNumeric"])
        cls = {"HU": "Hurricane", "TS": "Tropical Storm", "TD": "Tropical Depression", "PTC": "Potential Tropical Cyclone",
               "STS": "Subtropical Storm", "PC": "Post-tropical"}.get(s.get("classification"), s.get("classification"))
        tropical["active"].append({"name": s.get("name"), "class": cls, "wind_kt": int(s.get("intensity") or 0),
                                   "dist_km": round(dk), "heading": s.get("movementDir"), "speed_kt": s.get("movementSpeed"),
                                   "lat": s["latitudeNumeric"], "lon": s["longitudeNumeric"],
                                   "url": f"https://www.nhc.noaa.gov/graphics_{s.get('binNumber','').lower()}.shtml?cone"})
    txt = get("https://tgftp.nws.noaa.gov/data/raw/ab/abpz20.knhc.two.ep.txt")
    m = re.search(r"(\d{3,4} [AP]M [A-Z]{3} \w{3} \w{3} \d+ \d{4})", txt)
    tropical["outlook_issued"] = m.group(1) if m else ""
    body = txt.split("east of 180 longitude:", 1)[-1].split("$$")[0]
    tropical["outlook"] = re.sub(r"\s*\n\s*", " ", body).strip()[:1500]
    def bearing(a, b, c, d):
        p = math.pi / 180
        y = math.sin((d - b) * p) * math.cos(c * p)
        x = math.cos(a * p) * math.sin(c * p) - math.sin(a * p) * math.cos(c * p) * math.cos((d - b) * p)
        return (math.degrees(math.atan2(y, x)) + 360) % 360
    for a in tropical["active"]:
        to_cabo = bearing(a["lat"], a["lon"], LAT, LON)
        diff = abs(((a["heading"] or 0) - to_cabo + 180) % 360 - 180)
        a["toward"] = diff <= 35
        a["level"] = "warning" if a["dist_km"] < 600 or (a["toward"] and a["dist_km"] < 1500) else "watching" if a["dist_km"] < 2000 else "far"
    baja = bool(re.search(r"Baja California|Los Cabos|Cabo San Lucas", tropical["outlook"], re.I))
    levels = [a["level"] for a in tropical["active"]]
    tropical["level"] = "warning" if ("warning" in levels or baja) else "watching" if "watching" in levels else "none"
    tropical["threat"] = tropical["level"] == "warning"
    sources.append({"name": "US National Hurricane Center", "kind": "Tropical cyclones", "status": "ok",
                    "days": len(tropical["active"])})
except Exception as e:
    tropical["status"] = f"error: {str(e)[:120]}"
    sources.append({"name": "US National Hurricane Center", "kind": "Tropical cyclones", "status": tropical["status"], "days": 0})

# ---------- 3b. Keep last good data when a feed fails (max 36 h old) ----------
def fresh(key):
    r = raw_prev.get(key)
    if not r: return None
    age = (now_utc - dt.datetime.fromisoformat(r["at"])).total_seconds() / 3600
    return r if age <= 36 else None
raw = {}
det_failed = any(x["kind"] == "Deterministic model" and x["status"].startswith("error") for x in sources)
if det_failed and fresh("det"):
    det = fresh("det")["data"]; raw["det"] = raw_prev["det"]
    for x in sources:
        if x["kind"] == "Deterministic model": x["status"] = "error: kept data from " + raw_prev["det"]["at"]
elif not det_failed:
    raw["det"] = {"at": now_utc.isoformat(timespec="minutes"), "data": det}
for em, (label, _) in ENSEMBLES.items():
    failed = any(x["name"] == label and x["status"].startswith("error") for x in sources)
    key = "ens_" + em
    if failed and fresh(key):
        raw[key] = raw_prev[key]
        for t, v in raw_prev[key]["data"].items():
            if t in ens: ens[t][em] = v
        for x in sources:
            if x["name"] == label: x["status"] = "error: kept data from " + raw_prev[key]["at"]
    elif not failed:
        raw[key] = {"at": now_utc.isoformat(timespec="minutes"), "data": {t: v[em] for t, v in ens.items() if em in v}}

# ---------- 4. Blend ----------
def sky(cloud, rain):
    if rain is not None and rain >= 50: return "Showers likely"
    if rain is not None and rain >= 25: return "Chance of showers"
    if cloud is None: return "Mostly sunny"
    if cloud < 25: return "Sunny"
    if cloud < 55: return "Partly cloudy"
    if cloud < 80: return "Mostly cloudy"
    return "Overcast"

days = []
for day in TRIP:
    t = day.isoformat(); lead = (day - today_local).days
    c = CLIMO[day.strftime("%m-%d")]
    rows = det[t]; erows = ens[t]
    det_hi = wmean([(r["hi"], MODELS[m][1]) for m, r in rows.items()])
    det_lo = wmean([(r["lo"], MODELS[m][1]) for m, r in rows.items()])
    det_rh = wmean([(r["rh"], MODELS[m][1]) for m, r in rows.items() if "rh" in r])
    det_mm = wmean([(r["mm"], MODELS[m][1]) for m, r in rows.items() if "mm" in r])
    det_wind = wmean([(r["wind"], MODELS[m][1]) for m, r in rows.items() if "wind" in r])
    det_cloud = wmean([(r["cloud"], MODELS[m][1]) for m, r in rows.items() if "cloud" in r])
    det_rain = round(100 * sum(r.get("mm", 0) >= 1 for r in rows.values()) / len(rows)) if rows else None
    ens_hi = st.mean([e["hi"] for e in erows.values()]) if erows else None
    ens_lo = st.mean([e["lo"] for e in erows.values() if e["lo"] is not None]) if erows else None
    ens_rain = st.mean([e["rain"] for e in erows.values()]) if erows else None
    ens_sd = max([e["hi_sd"] for e in erows.values()], default=None)

    # forecast skill weight vs climatology, by lead time
    w = 1.0 if lead <= 7 else 0.85 if lead <= 10 else 0.65 if lead <= 14 else 0.4 if lead <= 21 else 0.25
    def fc(dv, ev):
        if dv is not None and ev is not None: return 0.65 * dv + 0.35 * ev
        return dv if dv is not None else ev
    f_hi, f_lo = fc(det_hi, ens_hi), fc(det_lo, ens_lo)
    f_rain = fc(det_rain, ens_rain)
    if f_hi is None:
        basis, w = "Climate normal only (no model reaches this date yet)", 0.0
    elif rows:
        basis = f"{len(rows)} global models" + (" + ensembles" if erows else "") + (" blended with climate normal" if w < 1 else "")
    else:
        basis = "Extended-range ensemble blended with climate normal"
    hi = w * f_hi + (1 - w) * c["hi"] if f_hi is not None else c["hi"]
    lo = w * f_lo + (1 - w) * c["lo"] if f_lo is not None else c["lo"]
    rh = w * det_rh + (1 - w) * c["rh"] if det_rh is not None else c["rh"]
    rain = w * f_rain + (1 - w) * c["rain"] if f_rain is not None else c["rain"]
    spread = (max(r["hi"] for r in rows.values()) - min(r["hi"] for r in rows.values())) if len(rows) >= 2 else ens_sd
    conf = "High" if lead <= 4 else "Medium" if lead <= 8 else "Low" if lead <= 15 else "Very low"
    if spread is not None and spread > 3 and conf in ("High", "Medium"):
        conf = {"High": "Medium", "Medium": "Low"}[conf]
    days.append({
        "date": t, "lead": lead, "hi": round(hi, 1), "lo": round(lo, 1), "rh": round(rh), "rain": round(rain),
        "mm": round(det_mm, 1) if det_mm is not None else (round(st.mean([e["mm"] for e in erows.values()]), 1) if erows else 0.0),
        "wind": round(det_wind) if det_wind is not None else None,
        "cloud": round(det_cloud) if det_cloud is not None else None,
        "sky": sky(det_cloud, rain), "confidence": conf, "basis": basis, "forecast_weight": round(w, 2),
        "spread": round(spread, 1) if spread is not None else None,
        "climo": c,
        "models": {MODELS[m][0]: r for m, r in rows.items()},
        "ensembles": {ENSEMBLES[e][0]: v for e, v in erows.items()},
        "range": None, **extras[t],
    })
    base = 2.5 if lead > 14 else 2.0 if lead > 10 else 1.5 if lead > 7 else 1.0 if lead > 4 else 0.5
    half = max(base, (spread or 0) / 2)
    days[-1]["range"] = {"hi": [round(hi - half), round(hi + half)], "lo": [round(lo - half), round(lo + half)]}

# ---------- 5. Change detection + Slack ----------
notify = prev.get("notify", {})
baseline = {d["date"]: d for d in notify.get("baseline", [])}
prev_days = {d["date"]: d for d in prev.get("days", [])}

def fmt_day(t):
    return dt.date.fromisoformat(t).strftime("%a %b %-d")
def diffs(old_map, threshold_scale=True):
    out = []
    for d in days:
        o = old_map.get(d["date"])
        if not o: continue
        far = d["lead"] > 14 and threshold_scale
        tT, tR, tH = (3, 25, 15) if far else (2, 20, 10)
        bits = []
        if abs(d["hi"] - o["hi"]) >= tT: bits.append(f"high {o['hi']:.0f}→{d['hi']:.0f}°C")
        if abs(d["lo"] - o["lo"]) >= tT: bits.append(f"low {o['lo']:.0f}→{d['lo']:.0f}°C")
        if abs(d["rain"] - o["rain"]) >= tR: bits.append(f"rain chance {o['rain']}%→{d['rain']}%")
        if abs(d["rh"] - o["rh"]) >= tH: bits.append(f"humidity {o['rh']}%→{d['rh']}%")
        if bits: out.append(f"{fmt_day(d['date'])}: " + ", ".join(bits))
    return out

changes_vs_last_run = diffs(prev_days, threshold_scale=False) if prev_days else []
sig = diffs(baseline) if baseline else []
prev_threat = notify.get("tropical_threat", False)
names_now = sorted(a["name"] for a in tropical["active"] if a.get("level") == "warning")
if tropical["status"] == "ok" and (tropical["threat"] != prev_threat or names_now != notify.get("tropical_names", [])):
    if tropical["threat"]:
        sig.append("Tropical warning: " + (", ".join(f"{a['class']} {a['name']} is {a['dist_km']} km away" + (" and heading toward Los Cabos" if a.get("toward") else "") for a in tropical["active"] if a.get("level") == "warning")
                                         or "NHC outlook mentions Baja California"))
    elif prev_threat:
        sig.append("Tropical threat near Los Cabos has cleared")

now_user = now_utc.astimezone(TZ_USER)
daily_due = now_user.hour >= DAILY_HOUR and notify.get("last_daily") != now_user.date().isoformat()
url = sys.argv[4] if len(sys.argv) > 4 else ""
send, kind = False, None
if daily_due:
    send, kind = True, "daily"
elif sig and baseline:
    send, kind = True, "change"

lines = []
if send:
    if kind == "daily":
        lines.append("*Los Cabos forecast, Nov 2–9* · " + ("daily update" if baseline else "tracking started"))
    else:
        lines.append("*Los Cabos forecast changed* (Nov 2–9)")
    if sig:
        lines.append("\n*What changed" + (" since the last update" if (kind == "daily" and baseline) else "") + ":*" if baseline else "\n*Watch:*")
        lines += [f"• {s}" for s in sig]
    elif kind == "daily":
        lines.append("\nNo significant changes since the last update.")
    lines.append("\n| Day | Sky | High / Low | Rain | Humidity | Confidence |")
    lines.append("|---|---|---|---|---|---|")
    for d in days:
        hl = (f"{d['range']['hi'][0]}–{d['range']['hi'][1]}° / {d['range']['lo'][0]}–{d['range']['lo'][1]}°C" if d["lead"] > 10
              else f"{d['hi']:.0f}° / {d['lo']:.0f}°C")
        lines.append(f"| {fmt_day(d['date'])} | {d['sky']} | {hl} | {d['rain']}% | {d['rh']}% | {d['confidence']} |")
    active = [a for a in tropical["active"]]
    if active:
        lines.append("\n*Tropics (NHC):* " + "; ".join(f"{a['class']} {a['name']}, {a['dist_km']} km from Cabo" for a in active))
    lead0 = min(d["lead"] for d in days)
    if lead0 > 10:
        lines.append(f"\n_Early outlook: the trip is {lead0} days away, so temperatures are shown as ranges. They narrow as the date gets closer._")
    if url:
        lines.append(f"\n[Open the forecast dashboard]({url})")
    notify = {"baseline": [{k: d[k] for k in ("date", "hi", "lo", "rain", "rh")} for d in days],
              "last_daily": now_user.date().isoformat() if kind == "daily" else notify.get("last_daily"),
              "last_sent": now_utc.isoformat(timespec="minutes"), "last_kind": kind,
              "tropical_threat": tropical["threat"], "tropical_names": names_now}
elif not baseline:
    notify["baseline"] = [{k: d[k] for k in ("date", "hi", "lo", "rain", "rh")} for d in days]
    notify["tropical_threat"] = tropical["threat"]; notify["tropical_names"] = names_now

# history for the drift chart (one point per run, keep ~7 days hourly)
hist = prev.get("history", [])
hist.append({"t": now_utc.isoformat(timespec="minutes"),
             "d": {d["date"]: [d["hi"], d["lo"], d["rain"], d["rh"]] for d in days}})
hist = hist[-170:]
log = prev.get("changes", [])
if changes_vs_last_run:
    log.insert(0, {"t": now_utc.isoformat(timespec="minutes"), "items": changes_vs_last_run})
log = log[:40]

state = {"location": "Los Cabos, Baja California Sur", "lat": LAT, "lon": LON,
         "updated": now_utc.isoformat(timespec="minutes"), "days": days, "tropical": tropical,
         "sources": sources, "rejected": rejected[:30], "history": hist, "changes": log, "notify": notify,
         "runs": prev.get("runs", 0) + 1, "raw": raw, "sea": sea or prev.get("sea", {})}
json.dump(state, open(sys.argv[2], "w"))
json.dump({"send": send, "kind": kind, "text": "\n".join(lines)}, open(sys.argv[3], "w"))
print(json.dumps({"send": send, "kind": kind, "sources": [(s["name"], s["status"]) for s in sources],
                  "rejected": len(rejected), "changes": changes_vs_last_run, "sig": sig}, indent=1))
