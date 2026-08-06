"""Scan US-listed sub-$5 stocks for unusual daily activity.

Runs after the close via GitHub Actions. Stdlib only - no dependencies.

Writes:
  data/scan.json     today's candidates, ranked by how unusual the day was
  data/history.json  every past scan's candidates + what happened next

The history file is the point. A scanner that never shows you what happened
to yesterday's list is selling you survivorship bias.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")

EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]

# Filters. These exist to keep names you cannot exit out of the list.
MIN_PRICE = 0.10          # sub-dime stocks have spreads that eat 20%+ instantly
MAX_PRICE = 5.00          # SEC penny-stock ceiling
MIN_VOLUME = 300_000      # shares
MIN_DOLLAR_VOLUME = 1_000_000   # can you actually sell without moving the price
SHORTLIST = 60            # how many get a history lookup (1 API call each)
FINAL_LIST = 25

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")


def get(url, timeout=60, retries=3):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            if attempt == retries - 1:
                print("  ! failed %s (%s)" % (url[:70], e))
                return None
            time.sleep(2 * (attempt + 1))
    return None


def num(s):
    """'$1,234.50' -> 1234.5 ; '-4.2%' -> -4.2 ; junk -> 0.0"""
    s = re.sub(r"[^0-9.\-]", "", str(s or ""))
    if s in ("", "-", ".", "-."):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def is_tradeable_common(row):
    """Drop warrants, units, rights, preferreds - they don't behave like shares."""
    sym = (row.get("symbol") or "").strip().upper()
    name = (row.get("name") or "").lower()
    if any(w in name for w in ("warrant", " unit", "right", "preferred", "depositary")):
        return False
    if re.search(r"[.\-/](W|U|R|P)[A-Z]?$", sym):
        return False
    if len(sym) == 5 and sym[-1] in ("W", "U", "R"):
        return False
    return True


def fetch_universe():
    """One call per exchange returns every listed ticker with price + volume."""
    out = []
    seen = set()
    for ex in EXCHANGES:
        url = ("https://api.nasdaq.com/api/screener/stocks"
               "?tableonly=false&download=true&exchange=%s" % ex)
        d = get(url)
        rows = (d or {}).get("data", {}).get("rows") or []
        print("  %s: %d tickers" % (ex, len(rows)))
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            r["exchange"] = ex
            out.append(r)
        time.sleep(1)
    return out


def prefilter(rows):
    """Cheap filters first, so we only spend API calls on plausible names."""
    keep = []
    for r in rows:
        price = num(r.get("lastsale"))
        vol = num(r.get("volume"))
        if not (MIN_PRICE <= price <= MAX_PRICE):
            continue
        if vol < MIN_VOLUME or price * vol < MIN_DOLLAR_VOLUME:
            continue
        if not is_tradeable_common(r):
            continue
        r["_price"] = price
        r["_vol"] = vol
        r["_pct"] = num(r.get("pctchange"))
        r["_dollar_vol"] = price * vol
        keep.append(r)
    # Shortlist by raw move size - history lookups are the expensive part.
    keep.sort(key=lambda r: -abs(r["_pct"]))
    return keep[:SHORTLIST]


def fetch_history(sym, days=45):
    today = date.today()
    url = ("https://api.nasdaq.com/api/quote/%s/historical"
           "?assetclass=stocks&fromdate=%s&todate=%s&limit=60"
           % (sym, (today - timedelta(days=days)).isoformat(), today.isoformat()))
    d = get(url, timeout=30, retries=2)
    rows = (d or {}).get("data", {}).get("tradesTable", {}).get("rows") or []
    bars = []
    for b in rows:
        bars.append({
            "date": b.get("date"),
            "close": num(b.get("close")),
            "open": num(b.get("open")),
            "high": num(b.get("high")),
            "low": num(b.get("low")),
            "volume": num(b.get("volume")),
        })
    return bars


def analyse(r, bars):
    """Turn today's bar + 30 days of history into the numbers that matter."""
    if len(bars) < 6:
        return None
    today_bar = bars[0]
    prior = [b for b in bars[1:31] if b["volume"] > 0]
    if len(prior) < 5:
        return None

    avg_vol = sum(b["volume"] for b in prior) / len(prior)
    relvol = (today_bar["volume"] / avg_vol) if avg_vol > 0 else 0

    hi, lo, close = today_bar["high"], today_bar["low"], today_bar["close"]
    range_pos = ((close - lo) / (hi - lo)) if hi > lo else 0.5

    closes = [b["close"] for b in prior if b["close"] > 0]
    high_30 = max(closes) if closes else 0
    low_30 = min(closes) if closes else 0
    off_30d_high = ((close - high_30) / high_30 * 100) if high_30 > 0 else 0

    flags = []
    if relvol > 25:
        flags.append(("volume %.0fx normal" % relvol, "high"))
    if r["_pct"] > 15 and range_pos < 0.35:
        flags.append(("spiked then sold off into the close", "high"))
    if r["_price"] < 0.25:
        flags.append(("sub-25c - spread alone can cost you 10-20%", "high"))
    if r["_dollar_vol"] < 3_000_000:
        flags.append(("thin - hard to exit a real position", "med"))
    if high_30 > 0 and close > high_30 * 2:
        flags.append(("already doubled off its 30-day range", "med"))
    if r["_pct"] < -40:
        flags.append(("down hard today - falling knife", "med"))

    # "Activity score" = how UNUSUAL today was. Not a prediction of tomorrow.
    # Deliberately not called a buy score, because that is not what it measures.
    vol_component = min(relvol / 15.0, 1.0) * 55
    move_component = min(abs(r["_pct"]) / 40.0, 1.0) * 25
    liq_component = min(r["_dollar_vol"] / 20_000_000.0, 1.0) * 20
    score = round(vol_component + move_component + liq_component)

    return {
        "symbol": r["symbol"],
        "name": (r.get("name") or "").replace(" Common Stock", "").strip(),
        "exchange": r.get("exchange", ""),
        "sector": r.get("sector") or "",
        "price": round(r["_price"], 4),
        "pct_change": round(r["_pct"], 2),
        "volume": int(r["_vol"]),
        "avg_volume_30d": int(avg_vol),
        "rel_volume": round(relvol, 1),
        "dollar_volume": int(r["_dollar_vol"]),
        "day_high": hi,
        "day_low": lo,
        "range_position": round(range_pos, 2),
        "off_30d_high_pct": round(off_30d_high, 1),
        "market_cap": int(num(r.get("marketCap"))),
        "activity_score": score,
        "flags": [{"text": t, "level": lv} for t, lv in flags],
    }


def score_past_scans(history):
    """Look up what actually happened to every candidate we've listed before.

    This is the honest part. It runs on the previous scans, not today's.
    """
    if not history.get("scans"):
        return history

    # Only re-check scans that are 1-30 days old and not yet finalised.
    to_check = [s for s in history["scans"] if not s.get("final")][-10:]
    if not to_check:
        return history

    cache = {}
    for scan in to_check:
        scan_date = datetime.strptime(scan["date"], "%Y-%m-%d").date()
        age = (date.today() - scan_date).days
        if age < 1:
            continue
        for c in scan.get("candidates", []):
            sym = c["symbol"]
            if sym not in cache:
                cache[sym] = fetch_history(sym, days=45)
                time.sleep(0.35)
            bars = cache[sym]
            after = [b for b in bars
                     if _pdate(b["date"]) and _pdate(b["date"]) > scan_date
                     and b["close"] > 0]
            if not after:
                continue
            entry = c["price"]
            closes = [b["close"] for b in after]
            c["outcome"] = {
                "days_tracked": len(after),
                "latest": round(closes[0], 4),
                "pct_from_scan": round((closes[0] - entry) / entry * 100, 1),
                "best": round((max(closes) - entry) / entry * 100, 1),
                "worst": round((min(closes) - entry) / entry * 100, 1),
            }
        if age >= 30:
            scan["final"] = True
    return history


def _pdate(s):
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return None


def summarise(history):
    """Aggregate hit rate across everything we've ever listed."""
    tracked = []
    for s in history.get("scans", []):
        for c in s.get("candidates", []):
            if c.get("outcome"):
                tracked.append(c["outcome"]["pct_from_scan"])
    if not tracked:
        return {"tracked": 0}
    wins = [t for t in tracked if t > 0]
    return {
        "tracked": len(tracked),
        "pct_up": round(len(wins) / len(tracked) * 100, 1),
        "median": round(sorted(tracked)[len(tracked) // 2], 1),
        "average": round(sum(tracked) / len(tracked), 1),
        "worst": round(min(tracked), 1),
        "best": round(max(tracked), 1),
    }


def main():
    os.makedirs(DATA, exist_ok=True)
    print("Fetching universe...")
    universe = fetch_universe()
    print("  %d total tickers" % len(universe))

    shortlist = prefilter(universe)
    print("Shortlisted %d sub-$%.0f names; pulling history..." % (len(shortlist), MAX_PRICE))

    results = []
    for i, r in enumerate(shortlist, 1):
        bars = fetch_history(r["symbol"])
        row = analyse(r, bars)
        if row:
            results.append(row)
        if i % 10 == 0:
            print("  %d/%d" % (i, len(shortlist)))
        time.sleep(0.35)

    results.sort(key=lambda x: -x["activity_score"])
    results = results[:FINAL_LIST]
    print("Kept %d candidates" % len(results))

    hist_path = os.path.join(DATA, "history.json")
    history = {"scans": []}
    if os.path.exists(hist_path):
        try:
            with open(hist_path) as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    print("Scoring previous scans...")
    history = score_past_scans(history)

    today_iso = date.today().isoformat()
    history["scans"] = [s for s in history["scans"] if s["date"] != today_iso]
    history["scans"].append({
        "date": today_iso,
        "candidates": [{"symbol": c["symbol"], "price": c["price"],
                        "pct_change": c["pct_change"],
                        "activity_score": c["activity_score"]}
                       for c in results[:10]],
    })
    history["scans"] = history["scans"][-90:]
    history["summary"] = summarise(history)

    with open(hist_path, "w") as f:
        json.dump(history, f, indent=1)

    with open(os.path.join(DATA, "scan.json"), "w") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scan_date": today_iso,
            "universe_size": len(universe),
            "passed_filters": len(shortlist),
            "candidates": results,
            "track_record": history["summary"],
        }, f, indent=1)

    print("Wrote scan.json (%d) and history.json" % len(results))
    if history["summary"].get("tracked"):
        s = history["summary"]
        print("Track record: %d tracked, %.1f%% up, median %.1f%%"
              % (s["tracked"], s["pct_up"], s["median"]))


if __name__ == "__main__":
    main()
