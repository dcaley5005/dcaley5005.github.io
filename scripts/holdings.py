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
import math
import re
import warnings
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
HOLD_FILE = ROOT / "data" / "holdings.json"
NPORT_DIR = ROOT / "data" / "nport"     # real holdings history from the SEC (scripts/nport.py)
NPORT_MAX_AGE_DAYS = 270                # an SEC report older than this means the fund stopped filing; use Yahoo
NPORT_CARDS = 12                        # the biggest holdings get a full card with a line chart, the rest just numbers
STOCK_FILE = ROOT / "data" / "stocks.json"
HOLD_MAX_AGE_DAYS = 14
STOCK_MAX_AGE_DAYS = 120
HOLD_MINUTES = float(os.environ.get("HOLDINGS_MINUTES", "15"))
STOCK_MINUTES = float(os.environ.get("STOCK_INFO_MINUTES", "10"))


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


def fetch_stock(sym):
    info = yf.Ticker(sym).get_info() or {}
    if not info:
        raise ValueError("empty response")
    return {"n": info.get("shortName") or info.get("longName") or "", "sec": info.get("sector"),
            "ind": info.get("industry"), "qt": info.get("quoteType"), "asof": today()}


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
                    old[k] = fut.result()
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
    old = _refresh(old, list(syms if refresh is None else refresh), fetch_stock, STOCK_MAX_AGE_DAYS, minutes, "Holding details")
    keep = {s: old[s] for s in sorted(syms) if s in old}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keep, separators=(",", ":")))
    return keep


def holding_symbols(hold):
    return sorted({r[0] for h in hold.values() for r in h.get("top", []) if r[0]})


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
                        got[s] = x
                    else:
                        failed.append(s)
            except Exception as e:
                log(f"  chunk failed ({e})")
                failed.extend(chunk)
                time.sleep(10)
            time.sleep(1)
        todo = failed
        if not todo:
            break
        time.sleep(10)
    return got, todo


# ---------- holding prices, cached between runs ----------
PRICE_CACHE = ROOT / ".cache" / "holding_prices.pkl"   # kept by actions/cache, not in git
PRICE_SLICES = 4          # holdings outside the priority set refresh one slice per run
NEW_PER_RUN = int(os.environ.get("NEW_HOLDING_PRICES", "1000"))   # first downloads per run, to stay under Yahoo's limit


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
    failed = cache.pop("__failed__", {})   # symbol -> day it last failed; skipped for 30 days
    failed = {x: d for x, d in failed.items() if day - d < 30}
    syms = sorted(set(syms))
    pri = set(priority) & set(syms)
    new = [x for x in syms if x not in cache and x not in pri and x not in failed][:NEW_PER_RUN]
    due = [x for x in syms if x in cache and x not in pri and (zlib.crc32(x.encode()) + day) % PRICE_SLICES == 0]
    todo = sorted(pri | set(new) | set(due))
    log(f"Holding prices: {len(syms)} symbols, {len(cache)} cached; downloading {len(todo)} "
        f"({len(pri)} priority, {len(due)} due, {len(new)} new)")
    got, missing = download(todo, start)
    cut = pd.Timestamp(start)
    for x, ser in got.items():
        cache[x] = ser[ser.index >= cut]
    failed.update({x: day for x in missing if x not in cache})
    PRICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PRICE_CACHE.write_bytes(pickle.dumps({**cache, "__failed__": failed}))
    return {x: cache[x] for x in syms if x in cache}, missing


# ---------- SEC holdings history ----------
def load_nport(tickers):
    """data/nport/<T>.json for the ETFs that have one and still file: {T: doc}."""
    out = {}
    if not NPORT_DIR.exists():
        return out
    cut = (datetime.now(timezone.utc) - pd.Timedelta(days=NPORT_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    for t in tickers:
        fp = NPORT_DIR / f"{t}.json"
        if fp.exists():
            d = json.loads(fp.read_text())
            if d.get("hist") and d["asof"] >= cut:
                out[t] = d
    return out


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
    """The symbols that get a full card: the biggest holdings in each fund's latest report."""
    out = set()
    for d in nport.values():
        for j, _ in d["hist"][-1][2][:NPORT_CARDS]:
            if d["sec"][j][0]:
                out.add(d["sec"][j][0])
    return out


def nport_symbols(nport, end):
    """Every symbol in the SEC reports the site will use."""
    starts = period_starts(end)
    syms = set()
    for d in nport.values():
        for i in set(nport_bases(d, starts, end)):
            syms.update(d["sec"][j][0] for j, _ in d["hist"][i][2] if d["sec"][j][0])
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
    written = sec_n = 0
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
                if n_ >= NPORT_CARDS or W[q][6] <= 0:
                    c.pop("d", None)
                    c.pop("w", None)
                c["hn"] = nm
                c["wt"] = round(W[q][6], 5)
                c["pw"] = [round(x, 5) for x in W[q][:6]]
                top.append(c)
            extra = {"src": "sec", "pb": pb}
            h = dict(h, asof=nport[t]["asof"])
            sec_n += 1
        else:
            for sym, nm, w in h.get("top", []):
                c = dict(card(sym)) if sym else {"t": "", "n": nm}
                c["hn"] = nm          # the name the fund uses
                c["wt"] = w
                top.append(c)
        tw = sum(c["wt"] for c in top)
        rest = None
        if extra and top:
            # the rest of the fund at the start of each period, from that period's own shares
            rest = {"wt": round(max(0.0, 1 - tw), 5), "r": []}
            for k in range(5):
                known = [c for c in top if c.get("r") and c["r"][k] is not None and c["pw"][k] > 0]
                left = 1 - sum(c["pw"][k] for c in known)
                if er[k] is None or left < 0.02:
                    rest["r"].append(None)
                    continue
                added = sum(c["pw"][k] * c["r"][k] for c in known)
                rest["r"].append(round((er[k] - added) / left, 4))
            rest["r"].append(None)
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
        doc = dict(meta, self=me, t=t, n=names.get(t, ""), desc=h.get("desc", ""), hasof=h.get("asof"),
                   sec=h.get("sec", {}), ac=h.get("ac", {}), er=er, top=top, rest=rest,
                   f={k: (fund or {}).get(t, {}).get(k) for k in ("aum", "cat", "fam", "inc")}, **extra)
        (out / f"{t}.json").write_text(json.dumps(doc, separators=(",", ":")))
        written += 1
    log(f"What's inside: wrote {written} ETFs ({sec_n} from SEC reports), {len(cards)} holding cards")
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
