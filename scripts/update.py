#!/usr/bin/env python3
"""薔琵颱風 (CHAMPI, 2026) 儀表板資料更新腳本

由 GitHub Actions 定時執行，抓取：
  1. 中央氣象署 (CWA) 颱風消息 TY_NEWS-Data.js  -> 現況與預報
  2. 日本氣象廳 (JMA) bosai typhoon JSON        -> 現況與預報
  3. Google News RSS (中/英/日)                 -> 最新新聞、漁業相關新聞
並把每次的現況位置累積進 data/track_history.json，輸出 data/latest.json。
任何來源失敗都不會中斷，保留前一次成功的資料。
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LATEST = DATA / "latest.json"
HISTORY = DATA / "track_history.json"
TW = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (FRI typhoon dashboard; +https://github.com/petercttseng-ux)"}
NAME_EN = "CHAMPI"


def get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------------------------------------------------------------- CWA
def num(pat, s, cast=float):
    m = re.search(pat, s)
    if not m:
        return None
    try:
        return cast(m.group(1))
    except ValueError:
        return None


def grab(pat, s):
    m = re.search(pat, s)
    return m.group(1) if m else None


def fetch_cwa():
    raw = get("https://www.cwa.gov.tw/Data/js/typhoon/TY_NEWS-Data.js").decode("utf-8", "replace")
    dt = re.search(r"var TY_DataTime = '(\d{12})'", raw)
    data_time = dt.group(1) if dt else None
    m = re.search(r"TY_LIST_2\['E'\] = (.*?)';\s*\n", raw, re.S)
    if not m:
        return None
    block = m.group(1)
    # 切出 CHAMPI 這一個 panel
    i = block.find(NAME_EN)
    if i < 0:
        return {"active": False, "data_time": data_time}
    nxt = block.find('<div class="panel panel-default">', i)
    seg = block[i: nxt if nxt > 0 else len(block)]
    text = re.sub(r"<[^>]+>", "\n", re.sub(r"'\+\s*'", "", seg))
    text = re.sub(r"\n+", "\n", text)

    ana_part, _, fc_part = text.partition("Forecast")
    analysis = {
        "time_utc": grab(r"(\d{4}UTC \d+ \w+ \d{4})", ana_part),
        "lat": num(r"Center Location ([\d.]+)N", ana_part),
        "lon": num(r"Center Location [\d.]+N ([\d.]+)E", ana_part),
        "movement": grab(r"Movement ([A-Z]+ [\d-]+ ?km/h)", ana_part),
        "pressure": num(r"Minimum Pressure (\d+)", ana_part, int),
        "wind": num(r"Maximum Wind Speed (\d+)", ana_part, int),
        "gust": num(r"Gust (\d+)", ana_part, int),
        "r15": num(r"over 15m/s winds (\d+)km", ana_part, int),
        "r25": num(r"over 25m/s winds (\d+)km", ana_part, int),
    }
    forecasts = []
    for chunk in re.split(r"\n(?=\d+ hours valid at:)", fc_part):
        h = num(r"^(\d+) hours valid at:", chunk.strip(), int)
        if h is None:
            continue
        forecasts.append({
            "tau": h,
            "time_utc": grab(r"(\d{4}UTC \d+ \w+ \d{4})", chunk),
            "lat": num(r"Center Position ([\d.]+)N", chunk),
            "lon": num(r"Center Position [\d.]+N ([\d.]+)E", chunk),
            "pressure": num(r"Minimum Pressure (\d+)", chunk, int),
            "wind": num(r"Maximum Wind Speed (\d+)", chunk, int),
            "r15": num(r"over 15m/s winds (\d+)km", chunk, int),
            "r25": num(r"over 25m/s winds (\d+)km", chunk, int),
            "p70": num(r"70% probability circle (\d+)km", chunk, int),
        })
    cat = None
    mcat = re.search(r"(TYPHOON|TROPICAL STORM|TROPICAL DEPRESSION)", block[max(0, i - 200):i])
    if mcat:
        cat = mcat.group(1)
    # 中文強度
    w = analysis.get("wind") or 0
    cat_zh = "強烈颱風" if w >= 51 else "中度颱風" if w >= 33 else "輕度颱風" if w >= 17.2 else "熱帶性低氣壓"
    return {
        "active": True,
        "data_time": data_time,
        "category": cat,
        "category_zh": cat_zh,
        "analysis": analysis,
        "forecasts": forecasts,
        "img_pta": f"https://www.cwa.gov.tw/Data/typhoon/TY_NEWS/PTA_{data_time}-120_zhtw.png" if data_time else None,
        "img_wsp": f"https://www.cwa.gov.tw/Data/typhoon/TY_NEWS/WSP-MAP_{data_time}_zhtw.png" if data_time else None,
    }


# ---------------------------------------------------------------- JMA
def fetch_jma():
    lst = json.loads(get("https://www.jma.go.jp/bosai/typhoon/data/targetTc.json"))
    for tc in lst:
        tcid = tc.get("tropicalCyclone")
        try:
            spec = json.loads(get(f"https://www.jma.go.jp/bosai/typhoon/data/{tcid}/specifications.json"))
        except Exception:
            continue
        title = spec[0] if spec else {}
        if (title.get("name") or {}).get("en", "").upper() != NAME_EN:
            continue
        pts = []
        for p in spec[1:]:
            pos = (p.get("position") or {}).get("deg")
            if not pos:
                continue
            mw = (p.get("maximumWind") or {})
            sw = p.get("stormWarning") or []
            gw = p.get("galeWarning") or []
            pts.append({
                "hours": p.get("advancedHours"),
                "valid_jst": (p.get("validtime") or {}).get("JST"),
                "valid_utc": (p.get("validtime") or {}).get("UTC"),
                "lat": pos[0], "lon": pos[1],
                "pressure": int(p["pressure"]) if str(p.get("pressure", "")).isdigit() else None,
                "wind_ms": int(mw.get("sustained", {}).get("m/s", 0) or 0) or None,
                "gust_ms": int(mw.get("gust", {}).get("m/s", 0) or 0) or None,
                "category": (p.get("category") or {}).get("en"),
                "intensity": p.get("intensity"),
                "scale": p.get("scale"),
                "course": p.get("course"),
                "location": p.get("location"),
                "storm_km": max([r.get("range", {}).get("km", 0) for r in sw], default=None),
                "gale_km": max([r.get("range", {}).get("km", 0) for r in gw], default=None),
                "prob_km": (p.get("probabilityCircleRadius") or {}).get("km"),
            })
        return {"tc_id": tcid, "number": title.get("typhoonNumber"),
                "name_jp": (title.get("name") or {}).get("jp"),
                "issue_jst": (title.get("issue") or {}).get("JST"), "points": pts}
    return None


# ---------------------------------------------------------------- News
NEWS_QUERIES = [
    ("tw", "薔琵 颱風", "zh-TW", "TW", "TW:zh-Hant"),
    ("fish", "薔琵 (漁業 OR 養殖 OR 漁船 OR 漁港 OR 漁民 OR 魚塭 OR 箱網 OR 定置網 OR 魚價)", "zh-TW", "TW", "TW:zh-Hant"),
    ("agri", "薔琵 (農損 OR 農業部 OR 菜價 OR 停電)", "zh-TW", "TW", "TW:zh-Hant"),
    ("en", "Typhoon Champi", "en-US", "US", "US:en"),
    ("jp", "台風30号 チャンパー", "ja", "JP", "JP:ja"),
]


def fetch_news():
    out = {}
    for key, q, hl, gl, ceid in NEWS_QUERIES:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q + " when:3d", "hl": hl, "gl": gl, "ceid": ceid})
        items = []
        try:
            root = ET.fromstring(get(url))
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                src = it.find("source")
                pub = it.findtext("pubDate")
                try:
                    ts = parsedate_to_datetime(pub).astimezone(TW).isoformat()
                except Exception:
                    ts = None
                items.append({"title": title, "link": it.findtext("link"),
                              "source": src.text if src is not None else "", "time": ts})
        except Exception as e:
            print(f"[news:{key}] {e}", file=sys.stderr)
            continue
        items.sort(key=lambda x: x["time"] or "", reverse=True)
        seen, uniq = set(), []
        for x in items:
            k = re.sub(r"\s+-\s+[^-]+$", "", x["title"])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(x)
        out[key] = uniq[:25]
        time.sleep(1)
    return out


# ---------------------------------------------------------------- main
def main():
    DATA.mkdir(exist_ok=True)
    prev = load(LATEST, {})
    hist = load(HISTORY, [])
    status = {}
    result = dict(prev)

    for key, fn in (("cwa", fetch_cwa), ("jma", fetch_jma), ("news", fetch_news)):
        try:
            v = fn()
            if v:
                result[key] = v
                status[key] = "ok"
            else:
                status[key] = "empty"
        except Exception as e:
            status[key] = f"error: {e}"
            print(f"[{key}] {e}", file=sys.stderr)

    # 累積路徑（以 JMA 實況為主，CWA 補充）
    def add_point(t, lat, lon, p, w, src):
        if lat is None or lon is None or not t:
            return
        if any(h.get("t") == t and h.get("src") == src for h in hist):
            return
        hist.append({"t": t, "lat": lat, "lon": lon, "p": p, "w": w, "src": src})

    jma = result.get("jma") or {}
    if status.get("jma") == "ok":
        a = next((x for x in jma.get("points", []) if x.get("hours") == 0), None)
        if a:
            add_point(a["valid_utc"], a["lat"], a["lon"], a["pressure"], a["wind_ms"], "JMA")
    cwa = result.get("cwa") or {}
    if status.get("cwa") == "ok" and cwa.get("active"):
        a = cwa["analysis"]
        t = cwa.get("data_time")
        if t:
            t = datetime.strptime(t, "%Y%m%d%H%M").replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        add_point(t, a.get("lat"), a.get("lon"), a.get("pressure"), a.get("wind"), "CWA")
    hist.sort(key=lambda h: h["t"])

    result["updated"] = datetime.now(TW).isoformat(timespec="seconds")
    result["status"] = status
    LATEST.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    HISTORY.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    main()
