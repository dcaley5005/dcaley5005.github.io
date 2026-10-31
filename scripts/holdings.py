"""What's inside each ETF, for the Portfolio Lab "What's inside" tab.

From Yahoo, per ETF: a short description, the top 10 holdings with weights, sector weights
and the stock/bond/cash split. Per holding: name, sector and industry (cached for months,
since they rarely change) and daily prices, so the site can show a card for each holding
the same way it does for ETFs.

Kept in the repo so each run only refreshes a slice:
  data/holdings.json  per-ETF holdings, refreshed every HOLD_MAX_AGE_DAYS
  data/stocks.json    per-holding name, sector, industry, refreshed every STOCK_MAX_AGE_DAYS

Run directly for a trial on a few ETFs (prints what came back, writes nothing to the repo):
  python scripts/holdings.py XLK SPY AGG
"""
import json
from collections import defaultdict
import math
import re
import warnings
import os
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
HOLD_FILE = ROOT / "data" / "holdings.json"
BLURB_FILE = ROOT / "data" / "blurbs.json"   # plain-English summaries for the biggest ETFs, written once and edited by hand
MAP_FILE = ROOT / "data" / "holding_map.json"   # company name -> industry/sector for foreign holdings the SEC data leaves unclassified
NPORT_DIR = ROOT / "data" / "nport"     # real holdings history from the SEC (scripts/nport.py)
NPORT_MAX_AGE_DAYS = 270                # an SEC report older than this means the fund stopped filing; use Yahoo
NPORT_CARDS = 12                        # each fund's biggest holdings get fresh prices every night; the rest every few days
STOCK_FILE = ROOT / "data" / "stocks.json"

_MAP_SUFFIX = {"SA", "ESP", "LTD", "PLC", "AG", "NV", "BV", "SPA", "SRL", "INC", "CORP", "CO",
               "DE", "CV", "THE", "PJSC", "JSC", "OOO", "KK", "GK", "SDN", "BHD", "TBK", "SC",
               "SAS", "SAB", "SAOG"}
_HOLDING_MAP = None


def _norm_name(n):
    """Canonical company-name key for the holding map: accents stripped, legal suffixes dropped."""
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode()
    n = re.sub(r"[^A-Z0-9 ]", " ", n.upper())
    return " ".join(t for t in n.split() if t not in _MAP_SUFFIX)


_INDUSTRIES = None


def _industries():
    """The standard list: each industry and the sector it belongs to (Yahoo's names)."""
    global _INDUSTRIES
    if _INDUSTRIES is None:
        try:
            _INDUSTRIES = _load(ROOT / "data" / "industries.json") or {}
        except Exception:
            _INDUSTRIES = {}
    return _INDUSTRIES


def _holding_map():
    global _HOLDING_MAP
    if _HOLDING_MAP is None:
        try:
            _HOLDING_MAP = _load(MAP_FILE) or {}
        except Exception:
            _HOLDING_MAP = {}
    return _HOLDING_MAP
HOLD_MAX_AGE_DAYS = 14
STOCK_MAX_AGE_DAYS = 120
HOLD_MINUTES = float(os.environ.get("HOLDINGS_MINUTES", "15"))
# How much each run pulls is set by data/pull_status.json, which steps itself up while Yahoo answers and
# backs off when it refuses (see pull_limits and save_pull_status). STOCK_INFO_MINUTES=0 turns lookups off.
PULL_FILE = ROOT / "data" / "pull_status.json"
PULL_START = {"new_prices": 1500, "lookup_minutes": 20}
PULL_MAX = {"new_prices": 2500, "lookup_minutes": 30}
PULL_MIN = {"new_prices": 600, "lookup_minutes": 10}
STATS = {"price_refusals": 0, "lookup_pauses": 0, "new_wanted": 0, "new_got": 0, "lookups_done": 0, "lookups_left": 0}


def pull_limits():
    try:
        s = json.loads(PULL_FILE.read_text())
    except Exception:
        s = {}
    return {k: s.get(k, v) for k, v in PULL_START.items()}


def save_pull_status():
    """After a run: step up if Yahoo never refused, back off if it did, and keep a short record."""
    lim = pull_limits()
    refused = STATS["price_refusals"] + STATS["lookup_pauses"]
    if not refused and not (STATS["new_wanted"] or STATS["lookups_done"]):
        return      # nothing was pulled (a site-only run), so nothing to learn from
    nxt = {}
    for k in lim:
        step = 500 if k == "new_prices" else 5
        nxt[k] = min(PULL_MAX[k], lim[k] + step) if not refused else max(PULL_MIN[k], round(lim[k] * 0.6))
    rec = {**nxt, "last_run": {"at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "limits": lim, **STATS}}
    PULL_FILE.write_text(json.dumps(rec, indent=1))
    log(f"Pull summary: {json.dumps(rec['last_run'])}; next run {nxt}")


_env_min = os.environ.get("STOCK_INFO_MINUTES", "auto")
STOCK_MINUTES = float(_env_min) if _env_min not in ("", "auto") else pull_limits()["lookup_minutes"]


def log(*a):
    print(*a, flush=True)


def _num(x):
    try:
        x = float(x)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------- fetching ----------
def fetch_holdings(t):
    """One ETF's description, top holdings, sectors and asset split. Raises on a failed request."""
    fd = yf.Ticker(t.replace(".", "-")).funds_data
    top = fd.top_holdings
    rows = []
    if top is not None and len(top):
        for sym, r in top.iterrows():
            w = _num(r.get("Holding Percent"))
            if w is None:
                continue
            sym = (str(sym).strip() if sym is not None else "")
            if re.fullmatch(r"0\d{4}", sym):      # Hong Kong listings come back as 00939; Yahoo wants 0939.HK
                sym = sym[1:] + ".HK"
            rows.append([sym if sym and sym.lower() != "nan" else "", str(r.get("Name") or "").strip(), round(w, 5)])
    sec = {k: round(v, 5) for k, v in (fd.sector_weightings or {}).items() if _num(v)}
    ac = {k.replace("Position", ""): round(v, 4) for k, v in (fd.asset_classes or {}).items() if _num(v) is not None}
    desc = (fd.description or "").strip()
    if not rows and not sec and not desc:
        raise ValueError("empty response")
    return {"desc": desc, "top": rows, "sec": sec, "ac": ac, "asof": today()}


_ABBR = re.compile(r"(?:\b(?:Inc|Corp|Co|Ltd|Plc|S\.A|N\.V|L\.P|Bros|St|Mr|Ms|Dr|No|vs|etc|approx)|\b[A-Z]|\bU\.S)\.$")


def short_desc(text, most=320):
    """The first two sentences of Yahoo's company summary (one if two run long), for the stock's card."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    out, start = [], 0
    for m in re.finditer(r"[.!?](?=\s+[A-Z(\"])", text):
        piece = text[start:m.end()].strip()
        if _ABBR.search(piece):              # "Apple Inc." or "U.S." is not the end of a sentence
            continue
        out.append(piece)
        start = m.end()
        if len(out) == 2:
            break
    if len(out) < 2 and start < len(text):
        out.append(text[start:].strip())
    first, both = out[0], " ".join(out[:2])
    return both if len(both) <= most else first if len(first) <= most + 80 else first[:most].rsplit(" ", 1)[0] + "…"


def fetch_stock(sym):
    info = yf.Ticker(sym).get_info() or {}
    # when Yahoo is throttling it answers with a near-empty dict; count that as a refusal, not as "no data"
    if not (info.get("shortName") or info.get("longName") or info.get("quoteType")):
        raise ValueError("empty response")
    return {"n": info.get("shortName") or info.get("longName") or "", "sec": info.get("sector"),
            "ind": info.get("industry"), "qt": info.get("quoteType"),
            "ds": short_desc(info.get("longBusinessSummary")), "asof": today()}


def _refresh(old, keys, fetch, max_age, minutes, label):
    """Refresh the oldest or missing entries within a time budget, pausing when Yahoo refuses."""
    now = datetime.now(timezone.utc).date()

    def age(k):
        a = (old.get(k) or {}).get("asof")
        return 9999 if not a else (now - datetime.strptime(a, "%Y-%m-%d").date()).days

    todo = sorted([k for k in keys if age(k) >= max_age], key=age, reverse=True)
    log(f"{label}: {len(keys) - len(todo)} fresh, {len(todo)} to refresh (budget {minutes:.0f} min)")

    def paced(k):
        time.sleep(0.4)
        return fetch(k)

    start, done, failed, pauses = time.time(), 0, 0, 0
    queue = list(todo)
    while queue and time.time() - start < minutes * 60 and pauses <= 4:
        batch, queue = queue[:40], queue[40:]
        errs = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(paced, k): k for k in batch}
            for fut in as_completed(futs):
                k = futs[fut]
                try:
                    new = fut.result()
                    # never let a blank answer wipe a name, sector or description we already have
                    prev = old.get(k) or {}
                    old[k] = {**new, **{f: prev[f] for f in ("n", "sec", "ind", "qt", "ds") if prev.get(f) and not new.get(f)}}
                    done += 1
                except Exception as e:
                    errs.append((k, str(e)[:80]))
        if len(errs) > len(batch) * 0.6 and len(batch) >= 10:
            pauses += 1
            queue = [k for k, _ in errs] + queue
            log(f"  refused by Yahoo; pausing 90s (pause {pauses})")
            time.sleep(90)
        else:
            failed += len(errs)
            for k, e in errs[:3]:
                log(f"  {k}: {e}")
    log(f"{label}: refreshed {done}, failed {failed}, left {len(queue)}")
    STATS["lookup_pauses"] += pauses
    STATS["lookups_done"] += done
    STATS["lookups_left"] += len(queue)
    return old


def _load(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            log(f"Couldn't read {path.name} ({e}); starting fresh.")
    return {}


def update_holdings(tickers, path=HOLD_FILE, minutes=HOLD_MINUTES):
    old = _load(path)
    old = _refresh(old, tickers, fetch_holdings, HOLD_MAX_AGE_DAYS, minutes, "Holdings")
    keep = {t: old[t] for t in sorted(tickers) if t in old}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keep, separators=(",", ":")))
    return keep


def update_stocks(syms, path=STOCK_FILE, minutes=STOCK_MINUTES, refresh=None):
    """Names and sectors for `syms`; only the symbols in `refresh` (in that order) are looked up."""
    old = _load(path)
    # entries saved before descriptions were collected count as due, so they fill in over the next few runs
    for v in old.values():
        if "ds" not in v:
            v["asof"] = None
    old = _refresh(old, list(syms if refresh is None else refresh), fetch_stock, STOCK_MAX_AGE_DAYS, minutes, "Holding details")
    keep = {s: old[s] for s in sorted(syms) if s in old}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keep, separators=(",", ":")))
    return keep


def holding_symbols(hold):
    return sorted({r[0] for h in hold.values() for r in h.get("top", []) if r[0]})


def drop_open_day(s):
    """While the US market is open, today's bar is a price so far, not a close: leave it out."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 30) and len(s):
        return s[s.index.date < now.date()]
    return s


def download(syms, start):
    """Daily adjusted closes for the holdings, from `start`: {symbol: pd.Series}."""
    got, todo = {}, list(syms)
    for rnd in range(2):
        failed = []
        for i in range(0, len(todo), 50):
            chunk = todo[i:i + 50]
            try:
                df = yf.download(chunk, start=start, auto_adjust=True, actions=False, threads=True,
                                 progress=False, group_by="column")
                close = df["Close"]
                if isinstance(close, pd.Series):
                    close = close.to_frame(chunk[0])
                for s in chunk:
                    x = close[s].dropna() if s in close else pd.Series(dtype=float)
                    x = x[x > 0]
                    if len(x):
                        got[s] = drop_open_day(x)
                    else:
                        failed.append(s)
            except Exception as e:
                log(f"  chunk failed ({e})")
                STATS["price_refusals"] += 1
                failed.extend(chunk)
                time.sleep(10)
            time.sleep(2.5)   # gentler on Yahoo, which starts refusing after a few thousand quick requests
        todo = failed
        if not todo:
            break
        time.sleep(10)
    return got, todo


# ---------- holding prices, cached between runs ----------
PRICE_CACHE = ROOT / ".cache" / "holding_prices.pkl"   # kept by actions/cache, not in git
FAILED_NOW = set()        # symbols Yahoo returned nothing for, this run or recently (for resolve_symbols)
PRICE_SLICES = 6          # holdings outside the priority set refresh one slice per run
NEW_PER_RUN = int(os.environ.get("NEW_HOLDING_PRICES") or pull_limits()["new_prices"])   # first downloads per run


def download_cached(syms, start, priority):
    """Daily closes for the holdings, using the cache from earlier runs. Every run downloads the priority
    symbols (the biggest holdings, which get line charts) and one slice of the rest, so the others are at
    most PRICE_SLICES days old; symbols never seen before are added up to NEW_PER_RUN a run."""
    import pickle
    import zlib
    cache = {}
    if PRICE_CACHE.exists():
        try:
            cache = pickle.loads(PRICE_CACHE.read_bytes())
        except Exception as e:
            log(f"  price cache unreadable ({e}); starting over")
    day = datetime.now(timezone.utc).toordinal()
    cache.pop("__failed__", None)          # the old list blamed symbols for Yahoo's refusals; start over
    failed = cache.pop("__failed2__", {})  # symbol -> day it last failed; skipped for 30 days
    failed = {x: d for x, d in failed.items() if day - d < 30}
    syms = sorted(set(syms))
    rank = priority if isinstance(priority, dict) else {x: 0 for x in priority}
    pri = set(rank) & set(syms)
    new = [x for x in syms if x not in cache and x not in pri and x not in failed][:NEW_PER_RUN]
    due = [x for x in syms if x in cache and x not in pri and (zlib.crc32(x.encode()) + day) % PRICE_SLICES == 0]
    # most important first, so if Yahoo starts refusing it's the small holdings that wait (they keep their cached prices)
    todo = sorted(pri, key=lambda x: -rank[x]) + sorted(due) + new
    log(f"Holding prices: {len(syms)} symbols, {len(cache)} cached; downloading {len(todo)} "
        f"({len(pri)} priority, {len(due)} due, {len(new)} new)")
    got, missing = download(todo, start)
    STATS["new_wanted"] += len(new)
    STATS["new_got"] += sum(1 for x in new if x in got)
    # Yahoo often refuses quietly, with empty answers: the big holdings, which normally always answer, show it
    pri_miss = sum(1 for x in pri if x not in got)
    if pri and pri_miss > 0.05 * len(pri):
        log(f"  {pri_miss} of {len(pri)} priority symbols came back empty; counting it as Yahoo refusing")
        STATS["price_refusals"] += 1
    cut = pd.Timestamp(start)
    for x, ser in got.items():
        cache[x] = ser[ser.index >= cut]
    if STATS["price_refusals"]:
        log("  Yahoo was refusing, so this run's misses get another try next run")
    else:
        failed.update({x: day for x in missing if x not in cache})
    PRICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PRICE_CACHE.write_bytes(pickle.dumps({**cache, "__failed2__": failed}))
    STATS["skipped_failed"] = len(failed)
    global FAILED_NOW
    FAILED_NOW = set(failed) | set(missing)
    return {x: cache[x] for x in syms if x in cache}, missing


# ---------- SEC holdings history ----------
# Funds that don't file N-PORT (unit investment trusts and the like) but track the same index as one that does
NPORT_TWIN = {"SPY": "IVV", "MDY": "IJH", "DLCU": "IVV"}


def load_nport(tickers):
    """data/nport/<T>.json for the ETFs that have one and still file: {T: doc}. A fund in NPORT_TWIN without
    its own file borrows its twin's, marked with "via"."""
    out = {}
    if not NPORT_DIR.exists():
        return out
    cut = (datetime.now(timezone.utc) - pd.Timedelta(days=NPORT_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    for t in tickers:
        fp = NPORT_DIR / f"{t}.json"
        via = None
        if not fp.exists() and t in NPORT_TWIN:
            via = NPORT_TWIN[t]
            fp = NPORT_DIR / f"{via}.json"
        if fp.exists():
            d = json.loads(fp.read_text())
            if d.get("hist") and d["asof"] >= cut:
                out[t] = dict(d, via=via) if via else d
    apply_alias(out)
    return out


# ---------- tickers Yahoo didn't know, and the symbols that work instead ----------
ALIAS_FILE = ROOT / "data" / "sym_alias.json"   # {"map": {symbol from the SEC data: Yahoo symbol}, "tried": {symbol: day}}
ALIAS_RETRY_DAYS = 14
ALIAS_PER_RUN = 3000          # candidate symbols tried per run (they go 50 to a request)
# the same company under the other exchange code: KOSDAQ vs KOSPI, Taiwan's OTC market vs the main board,
# Shenzhen vs Shanghai, Bombay vs the National exchange, TSX Venture and other Canadian boards
SWAP = {"KS": ["KQ"], "KQ": ["KS"], "TW": ["TWO"], "TWO": ["TW"], "SS": ["SZ"], "SZ": ["SS"], "NS": ["BO"],
        "BO": ["NS"], "TO": ["V", "NE", "CN"], "V": ["TO"], "T": ["F"]}
# exchange codes for countries the SEC data leaves without one
COUNTRY = {"GR": [".AT"], "KW": [".KW"], "AE": [".AE", ".AD"], "QA": [".QA"], "SA": [".SR"], "PH": [".PS"],
           "CO": [".CL"], "CL": [".SN"], "PE": [".LM"], "TR": [".IS"], "EG": [".CA"], "HU": [".BD"], "CZ": [".PR"],
           "PL": [".WA"], "MY": [".KL"], "TH": [".BK"], "ID": [".JK"], "VN": [".VN"], "CN": [".SS", ".SZ", ".HK"],
           "KR": [".KS", ".KQ"], "TW": [".TW", ".TWO"], "IN": [".NS", ".BO"], "BR": [".SA"], "MX": [".MX"],
           "ZA": [".JO"], "JP": [".T"], "HK": [".HK"], "SG": [".SI"], "GB": [".L"], "DE": [".DE"], "FR": [".PA"],
           "CH": [".SW"], "NL": [".AS"], "ES": [".MC"], "IT": [".MI"], "SE": [".ST"], "DK": [".CO"], "FI": [".HE"],
           "NO": [".OL"], "BE": [".BR"], "IE": [".IR"], "AT": [".VI"], "PT": [".LS"], "CA": [".TO", ".V"],
           "AU": [".AX"], "NZ": [".NZ"], "IL": [".TA"], "AR": [".BA"]}


def _alias_doc():
    try:
        a = json.loads(ALIAS_FILE.read_text())
        return {"map": a.get("map", {}), "tried": a.get("tried", {})}
    except Exception:
        return {"map": {}, "tried": {}}


def apply_alias(nport, amap=None):
    """Swap symbols Yahoo didn't know for the ones that worked, in the SEC files as loaded."""
    amap = _alias_doc()["map"] if amap is None else amap
    if not amap:
        return
    for d in nport.values():
        for r in d.get("sec", []):
            if r[0] in amap:
                r[0] = amap[r[0]]
        f = d.get("full")
        if f:
            w = {}
            for s, p in f["h"]:
                s = amap.get(s, s)
                w[s] = w.get(s, 0) + p
            f["h"] = sorted(([s, p] for s, p in w.items()), key=lambda x: -x[1])


def resolve_symbols(nport, stuck, start):
    """Try other spellings for symbols Yahoo returned nothing for: the other exchange code, the code for the
    stock's country, or, for US companies, the ticker the SEC lists for that company name today (old tickers like
    SQ for Block). Returns {old symbol: (new symbol, prices)} and remembers the answers in ALIAS_FILE."""
    if not stuck:
        return {}
    doc = _alias_doc()
    today_s = today()
    now = datetime.now(timezone.utc).date()
    info, weight = {}, {}
    for d in nport.values():
        for r in d.get("sec", []):
            if r[0]:
                info.setdefault(r[0], (r[1], r[2] if len(r) > 2 else ""))
        for s, p in (d.get("full") or {}).get("h", []):
            weight[s] = weight.get(s, 0) + p
    def due(s):
        a = doc["tried"].get(s)
        return not a or (now - datetime.strptime(a, "%Y-%m-%d").date()).days >= ALIAS_RETRY_DAYS
    todo = sorted((s for s in stuck if s not in doc["map"] and due(s)), key=lambda s: -weight.get(s, 0))
    if not todo:
        return {}
    sec_names = {}
    if any("." not in s for s in todo):
        try:
            import nport as NP
            sec_names, _ = NP.company_names()
        except Exception as e:
            log(f"  SEC company list unavailable ({e})")
    cands, n = {}, 0
    for s in todo:
        name, ctry = info.get(s, ("", ""))
        c = []
        if "." in s:
            base, suf = s.rsplit(".", 1)
            c += [f"{base}.{x}" for x in SWAP.get(suf, [])]
        else:
            if ctry and ctry != "US":
                c += [s + x for x in COUNTRY.get(ctry, [])]
            if name:
                try:
                    import nport as NP
                    cur = sec_names.get(NP.name_key(name))
                except Exception:
                    cur = None
                if cur and cur != s:
                    c.append(cur)
        c = [x for x in dict.fromkeys(c) if x != s]
        if c:
            cands[s] = c
            n += len(c)
        doc["tried"][s] = today_s
        if n >= ALIAS_PER_RUN:
            break
    flat = sorted({x for c in cands.values() for x in c})
    log(f"Other spellings: {len(cands)} stuck symbols, {len(flat)} candidates to try")
    got, _ = download(flat, start) if flat else ({}, [])
    found = {}
    for s, c in cands.items():
        hit = next((x for x in c if x in got), None)
        if hit:
            found[s] = (hit, got[hit])
            doc["map"][s] = hit
    log(f"Other spellings: found {len(found)}" + (": " + ", ".join(f"{a}->{b}" for a, (b, _) in list(found.items())[:15]) if found else ""))
    ALIAS_FILE.write_text(json.dumps(doc, indent=0, sort_keys=True))
    if found:
        # into the price cache, so the next runs refresh them like any other holding
        import pickle
        try:
            cache = pickle.loads(PRICE_CACHE.read_bytes()) if PRICE_CACHE.exists() else {}
            cut = pd.Timestamp(start)
            for s, (alt, ser) in found.items():
                cache[alt] = ser[ser.index >= cut]
            PRICE_CACHE.write_bytes(pickle.dumps(cache))
        except Exception as e:
            log(f"  couldn't add them to the price cache ({e})")
    return found


def period_starts(end):
    """Start dates of 1M 3M 6M YTD 1Y 3Y, the same cuts period_returns uses."""
    return [end - pd.DateOffset(months=m) if m != "ytd" else pd.Timestamp(end.year - 1, 12, 31)
            for m in (1, 3, 6, "ytd", 12, 36)]


def nport_bases(doc, starts, end):
    """For each period start, then today: the SEC report to start from, the latest one on or before
    that date (or the first report, for a fund younger than the period)."""
    dates = [pd.Timestamp(h[0]) for h in doc["hist"]]
    out = []
    for d in list(starts) + [end]:
        i = max((j for j, x in enumerate(dates) if x <= d), default=0)
        out.append(i)
    return out


def nport_priority(nport):
    """The symbols priced every night, the biggest holdings in each fund's latest report: {symbol: score},
    the score being its share summed over the funds, so the most important download first."""
    out = {}
    for d in nport.values():
        for j, w in d["hist"][-1][2][:NPORT_CARDS]:
            sym = d["sec"][j][0]
            if sym:
                out[sym] = out.get(sym, 0) + w
    return out


def nport_symbols(nport, end):
    """Every symbol in the SEC reports the site will use."""
    starts = period_starts(end)
    syms = set()
    for d in nport.values():
        for i in set(nport_bases(d, starts, end)):
            syms.update(d["sec"][j][0] for j, _ in d["hist"][i][2] if d["sec"][j][0])
        syms.update(s for s, _ in (d.get("full") or {}).get("h", []))
    return syms


_AT = {}


def _at(s, d):
    """Price on date d, or the last one before it (the lookups are cached per series, since this runs
    a few hundred times per ETF)."""
    if s is None or not len(s):
        return None
    k = id(s)
    if k not in _AT:
        _AT[k] = (s.index.values.astype("datetime64[ns]"), s.to_numpy(dtype=float), s)
    ix, v, _ = _AT[k]
    i = int(ix.searchsorted(pd.Timestamp(d).to_datetime64(), side="right")) - 1
    return float(v[i]) if i >= 0 else None


def nport_weights(doc, etf_s, stock_series, end):
    """{key: [share at the start of 1M 3M 6M YTD 1Y 3Y, share today]} plus the report date each
    period starts from. A report's shares are moved to the date needed with each holding's price
    move against the fund's own: share x (1 + holding return) / (1 + fund return)."""
    starts = period_starts(end)
    bases = nport_bases(doc, starts, end)
    W, info = {}, {}
    for k, (b, d) in enumerate(zip(bases, list(starts) + [end])):
        h = doc["hist"][b]
        d0 = pd.Timestamp(h[0])
        f0, f1 = _at(etf_s, d0), _at(etf_s, d)
        for j, w in h[2]:
            sym, name, ctry = doc["sec"][j]
            key = sym or "~" + name
            info.setdefault(key, (sym, name, ctry))
            ss = stock_series.get(sym) if sym else None
            p0, p1 = _at(ss, d0), _at(ss, d)
            m = (p1 / p0) / (f1 / f0) if p0 and p1 and f0 and f1 else 1.0
            W.setdefault(key, [0.0] * 7)[k] += w * m
    for k in range(7):  # moved shares can't add up to more than the fund
        tot = sum(v[k] for v in W.values())
        if tot > 0.999:
            for v in W.values():
                v[k] *= 0.999 / tot
    return W, info, [doc["hist"][b][0] for b in bases]


# ---------- building the site files ----------
GRP_LINE_MIN = 0.002     # industries smaller than this get a return but no line chart, to keep the files small


_REL = {}     # symbol -> its prices on the build's dates, relative to today (shared by every fund that holds it)


def group_doc(full, etf_s, stock_series, stocks, idx, nd, wk, end):
    """Industry and sector totals from every stock in the fund's latest SEC report: each group's share of the
    fund today, its return over each period and its line. The line is today's holdings priced back in time,
    which is the same as holding the group from the start of any period."""
    import numpy as np
    ind_sec = _industries()
    asof = pd.Timestamp(full["asof"])
    f0, f1 = _at(etf_s, asof), _at(etf_s, end)
    fm = (f1 / f0) if f0 and f1 else 1.0
    members = defaultdict(list)       # ("ind"|"sec", name) -> [(weight today, relative prices)]
    for sym, p in full["h"]:
        st = stocks.get(sym) or {}
        ind = st.get("ind") if st.get("ind") in ind_sec else None
        sec = ind_sec[ind] if ind else (st.get("sec") if st.get("sec") in set(ind_sec.values()) else None)
        ss = stock_series.get(sym)
        if not sec or ss is None or not len(ss) or ss.index[-1] < end - pd.Timedelta(days=7):
            continue
        p0, p1 = _at(ss, asof), _at(ss, end)
        w = p * (p1 / p0) / fm if p0 and p1 else p
        rel = _REL.get(sym)
        if rel is None:
            px = ss.reindex(ss.index.union(idx)).ffill().reindex(idx)
            rel = _REL[sym] = (px / px.iloc[-1]).to_numpy(dtype=float)      # NaN before a stock's first price
        if not np.isfinite(rel[-1]):
            continue
        members[("sec", sec)].append((w, rel))
        if ind:
            members[("ind", ind)].append((w, rel))
    out = {"asof": full["asof"], "eq": full.get("eq"), "ind": {}, "sec": {}}
    for (kind, name), ms in members.items():
        W = sum(w for w, _ in ms)
        if W <= 0:
            continue
        # each day's move is the value-weighted move of the stocks trading on both days, so a stock that
        # listed partway through only counts from its first price
        R = np.array([r for _, r in ms])
        V = np.array([w for w, _ in ms])[:, None] * R
        step = R[:, 1:] / R[:, :-1]
        ok = np.isfinite(step) & np.isfinite(V[:, :-1])
        den = np.where(ok, V[:, :-1], 0).sum(0)
        num = np.where(ok, V[:, :-1] * np.where(ok, step, 0), 0).sum(0)
        g_step = np.where(den > 0, num / np.where(den > 0, den, 1), 1.0)
        line = np.concatenate([[1.0], np.cumprod(g_step)])
        line = line / line[-1]
        s = pd.Series(line, index=idx)
        g = {"w": round(W, 5), "n": len(ms), "r": period_returns(s, end)}
        if kind == "ind":
            g["s"] = ind_sec[name]
        if kind == "sec" or W >= GRP_LINE_MIN:
            rel = np.round(line * 10000)
            g["d"] = [int(v) for v in rel[-nd:]]
            g["wk"] = [int(rel[i]) for i in wk]
        out[kind][name] = g
    return out


def period_returns(s, last):
    """1M 3M 6M YTD 1Y return, then 3Y per year; None where history is too short."""
    out = []
    for m in (1, 3, 6, "ytd", 12, 36):
        cut = pd.Timestamp(last.year - 1, 12, 31) if m == "ytd" else last - pd.DateOffset(months=m)
        prev = s[:cut]
        if prev.empty or s.index[0] > cut:
            out.append(None)
            continue
        r = float(s.iloc[-1] / prev.iloc[-1] - 1)
        out.append(round((1 + r) ** (12 / m) - 1 if m != "ytd" and m > 12 else r, 4))
    return out


def build_inside(out_dir, etf_series, hold, stocks, stock_series, names, fund=None, nport=None):
    """Write inside/<T>.json per ETF: description, sectors, asset split, top 10 with a card each
    (same shape as the Explore cards), and the return of everything outside the top 10."""
    import numpy as np
    spy = etf_series["SPY"]
    end = spy.index[-1]
    cut = end - pd.DateOffset(years=3)
    idx = spy.index[spy.index >= spy[:cut].index[-1]]
    start = idx[0]
    nd = min(140, len(idx))
    cuts = [pd.Timestamp(end.year - 1, 12, 31), end - pd.DateOffset(years=1)]
    anchors = {int(idx.searchsorted(c, side="right")) - 1 for c in cuts}
    wk = sorted((set(range(len(idx) - 1, -1, -5)) | {0} | anchors) - {-1})
    meta = {"asof": today(), "from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d"),
            "dd": [d.strftime("%Y-%m-%d") for d in idx[-nd:]], "wd": [idx[i].strftime("%Y-%m-%d") for i in wk]}
    rf = None
    if "SHY" in etf_series:
        shy = etf_series["SHY"].reindex(idx).ffill()
        rf = shy.pct_change().iloc[1:]
        rel = (shy / shy.iloc[-1] * 10000).round()
        meta["rf"] = {"d": rel.iloc[-nd:].astype(int).tolist(), "w": rel.iloc[wk].astype(int).tolist()}

    cards = {}

    def card(sym, s=None, c=None):
        stock = c is None
        if stock:
            if sym in cards:
                return cards[sym]
            s = stock_series.get(sym)
            st = stocks.get(sym) or {}
            c = {"t": sym, "n": st.get("n") or "", "ind": st.get("ind"), "sec": st.get("sec")}
        if s is not None and len(s) > 30 and s.index[-1] >= end - pd.Timedelta(days=7):
            c["r"] = period_returns(s, end)
            px = s.reindex(s.index.union(idx)).ffill().reindex(idx)   # markets abroad close on other days
            full = bool(px.notna().all())
            px = px.dropna()
            if len(px) > 30:
                r = px.pct_change().iloc[1:]
                vol = float(r.std() * math.sqrt(252))
                c["vol"] = round(vol, 4)
                c["dd"] = round(float((px / px.cummax() - 1).min()), 4)
                if full:
                    yrs = (px.index[-1] - px.index[0]).days / 365.25
                    c["r3"] = round(float((px.iloc[-1] / px.iloc[0]) ** (1 / yrs) - 1), 4)
                    if rf is not None and vol > 0:
                        c["sh"] = round(float(r.sub(rf.reindex(r.index).fillna(0)).mean() * 252 / vol), 3)
                relp = (s.reindex(s.index.union(idx)).ffill().reindex(idx) / px.iloc[-1] * 10000).round()
                c["d"] = [None if v != v else int(v) for v in relp.iloc[-nd:]]
                c["w"] = [None if v != v else int(v) for v in relp.iloc[wk]]
        if stock:
            cards[sym] = c
        return c

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    nport = nport or {}
    blurbs = _load(BLURB_FILE)
    written = sec_n = grp_n = 0
    gdir = out.parent / "grp"
    for t in sorted(set(hold) | set(nport)):
        h = hold.get(t) or {}
        s = etf_series.get(t)
        er = period_returns(s, end) if s is not None and len(s) else [None] * 6
        top = []
        extra = {}
        if t in nport:
            # real holdings from the SEC: the top 50 of the report each period starts from
            W, info, pb = nport_weights(nport[t], s, stock_series, end)
            keys = sorted(W, key=lambda q: (-W[q][6], -max(W[q][:6])))
            for n_, q in enumerate(keys):
                sym, nm, ctry = info[q]
                c = dict(card(sym)) if sym else {"t": "", "n": nm}
                c.pop("d", None)   # line charts live in stock/<symbol>.json, loaded when a card opens
                c.pop("w", None)
                c["hn"] = nm
                c["wt"] = round(W[q][6], 5)
                c["pw"] = [round(x, 5) for x in W[q][:6]]
                top.append(c)
            extra = {"src": "sec", "pb": pb, **({"via": nport[t]["via"]} if nport[t].get("via") else {})}
            h = dict(h, asof=nport[t]["asof"])
            sec_n += 1
        else:
            for sym, nm, w in h.get("top", []):
                c = dict(card(sym)) if sym else {"t": "", "n": nm}
                c.pop("d", None)
                c.pop("w", None)
                c["hn"] = nm          # the name the fund uses
                c["wt"] = w
                top.append(c)
        for c in top:
            if not c.get("ind"):
                m = _holding_map().get(_norm_name(c.get("hn") or c.get("n") or ""))
                if m:
                    c["ind"] = m.get("ind")
                    if not c.get("sec"):
                        c["sec"] = m.get("sec")
        # keep every holding on the standard list, so the industry and sector maps never get a one-off tile
        ind_sec = _industries()
        if ind_sec:
            secs = set(ind_sec.values())
            for c in top:
                i = c.get("ind")
                if i and i not in ind_sec:
                    log(f"  {t}: unknown industry {i!r} for {c.get('t') or c.get('hn')}; left under its sector")
                    c["ind"] = None
                elif i:
                    c["sec"] = ind_sec[i]
                if c.get("sec") and c["sec"] not in secs:
                    log(f"  {t}: unknown sector {c['sec']!r} for {c.get('t') or c.get('hn')}")
                    c["sec"] = None
        tw = sum(c["wt"] for c in top)
        rest = None
        if extra and top:
            # the rest of the fund at the start of each period, from that period's own shares
            # 3Y returns are per year, so they're turned into the full 3-year return to split, then back
            cum = [lambda x: x] * 5 + [lambda x: (1 + x) ** 3 - 1]
            per = [lambda x: x] * 5 + [lambda x: (1 + x) ** (1 / 3) - 1 if x > -1 else None]
            rest = {"wt": round(max(0.0, 1 - tw), 5), "r": [], "cash": nport[t]["hist"][-1][1]}
            for k in range(6):
                known = [c for c in top if c.get("r") and c["r"][k] is not None and c["pw"][k] > 0]
                left = 1 - sum(c["pw"][k] for c in known)
                if er[k] is None or left < 0.02:
                    rest["r"].append(None)
                    continue
                added = sum(c["pw"][k] * cum[k](c["r"][k]) for c in known)
                x = per[k]((cum[k](er[k]) - added) / left)
                rest["r"].append(None if x is None else round(x, 4))
        elif top and tw < 0.999:
            rest = {"wt": round(1 - tw, 5), "r": []}
            for k in range(5):        # 1M 3M 6M YTD 1Y; 3Y per year doesn't add up this way
                # Today's weights are after the period's moves, so roll each back to where it started:
                # start weight = weight now x (1 + fund return) / (1 + its return). A holding without a
                # return for this period counts as part of the rest.
                known = [c for c in top if c.get("r") and c["r"][k] is not None]
                if er[k] is None:
                    rest["r"].append(None)
                    continue
                w0 = [c["wt"] * (1 + er[k]) / (1 + c["r"][k]) for c in known]
                left = 1 - sum(w0)
                if left < 0.02:
                    rest["r"].append(None)
                    continue
                added = sum(w * c["r"][k] for w, c in zip(w0, known))
                rest["r"].append(round((er[k] - added) / left, 4))
            rest["r"].append(None)
        me = card(t, s, {"t": t, "n": names.get(t, ""), "er": (fund or {}).get(t, {}).get("er")}) if s is not None else {"t": t}
        doc = dict(meta, self=me, t=t, n=names.get(t, ""), desc=h.get("desc", ""), blurb=blurbs.get(t, ""), hasof=h.get("asof"),
                   sec=h.get("sec", {}), ac=h.get("ac", {}), er=er, top=top, rest=rest,
                   f={k: (fund or {}).get(t, {}).get(k) for k in ("aum", "cat", "fam", "inc")}, **extra)
        (out / f"{t}.json").write_text(json.dumps(doc, separators=(",", ":")))
        written += 1
        full = (nport.get(t) or {}).get("full")
        if full and s is not None and len(s):
            gdir.mkdir(parents=True, exist_ok=True)
            (gdir / f"{t}.json").write_text(json.dumps(group_doc(full, s, stock_series, stocks, idx, nd, wk, end), separators=(",", ":")))
            grp_n += 1
    # one small file per holding with its line chart, shared by every fund that holds it
    sdir = out.parent / "stock"
    sdir.mkdir(parents=True, exist_ok=True)
    lines = 0
    for sym, c in cards.items():
        ds = (stocks.get(sym) or {}).get("ds") or ""
        if c.get("d") and c.get("w"):
            (sdir / f"{sym}.json").write_text(json.dumps({"d": c["d"], "w": c["w"], **({"ds": ds} if ds else {})}, separators=(",", ":")))
            lines += 1
        elif ds:     # no line chart, but the card still gets its description
            (sdir / f"{sym}.json").write_text(json.dumps({"ds": ds}, separators=(",", ":")))
    log(f"What's inside: wrote {written} ETFs ({sec_n} from SEC reports, {grp_n} with industry totals), {len(cards)} holding cards, {lines} line charts")
    return written


# ---------- trial ----------
def trial(tickers):
    t0 = time.time()
    tmp = Path("/tmp/inside_trial")
    hold = update_holdings(tickers, tmp / "holdings.json", minutes=5)
    log(f"[{time.time() - t0:.0f}s] holdings for {len(hold)}/{len(tickers)}")
    for t, h in hold.items():
        tw = sum(r[2] for r in h["top"])
        log(f"  {t}: {len(h['top'])} holdings = {tw:.1%}, sectors {len(h['sec'])}, asset split {h['ac']}, "
            f"desc {len(h['desc'])} chars")
        log("      " + ", ".join(r[0] or r[1] for r in h["top"]))
    syms = holding_symbols(hold)
    stocks = update_stocks(syms, tmp / "stocks.json", minutes=5)
    log(f"[{time.time() - t0:.0f}s] holding details for {len(stocks)}/{len(syms)}")
    log("  no details for: " + ", ".join(s for s in syms if not (stocks.get(s) or {}).get("ind")))
    start = (pd.Timestamp.today() - pd.DateOffset(years=3, months=2)).strftime("%Y-%m-%d")
    got, miss = download(syms, start)
    log(f"[{time.time() - t0:.0f}s] holding prices for {len(got)}/{len(syms)}; missing {miss}")
    etf, miss2 = download(sorted(set(tickers) | {"SPY", "SHY"}), start)
    log(f"ETF prices for {len(etf)}; missing {miss2}")
    names, fund = {t: t for t in tickers}, {}
    try:
        names.update({t: n for t, n in json.loads((ROOT / "data" / "universe.json").read_text())["etfs"]})
        j = json.loads((ROOT / "data" / "fund_info.json").read_text())
        fund = {t: dict(zip(j["fields"], row)) for t, row in j["f"].items()}
    except Exception as e:
        log(f"no names or fund details: {e}")
    build_inside(tmp / "inside", etf, hold, stocks, got, names, fund)
    for t in tickers:
        p = tmp / "inside" / f"{t}.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        log(f"== {t}.json  {p.stat().st_size / 1024:.0f} KB  ETF returns {d['er']}")
        log(f"   rest {d['rest']}")
        for c in d["top"]:
            log(f"   {c['t']:<10} wt {c['wt']:.4f} r {c.get('r')} r3 {c.get('r3')} vol {c.get('vol')} dd {c.get('dd')} "
                f"ind {c.get('ind')} pts {len(c.get('w') or [])}")
    log(f"Trial took {time.time() - t0:.0f}s")


if __name__ == "__main__":
    trial([a.upper() for a in sys.argv[1:]] or ["XLK", "SPY", "AGG"])
