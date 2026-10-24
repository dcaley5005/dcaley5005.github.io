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


def update_stocks(syms, path=STOCK_FILE, minutes=STOCK_MINUTES):
    old = _load(path)
    old = _refresh(old, syms, fetch_stock, STOCK_MAX_AGE_DAYS, minutes, "Holding details")
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


def build_inside(out_dir, etf_series, hold, stocks, stock_series, names):
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

    def card(sym):
        if sym in cards:
            return cards[sym]
        s = stock_series.get(sym)
        st = stocks.get(sym) or {}
        c = {"t": sym, "n": st.get("n") or "", "ind": st.get("ind"), "sec": st.get("sec")}
        if s is not None and len(s) > 30 and s.index[-1] >= end - pd.Timedelta(days=7):
            c["r"] = period_returns(s, end)
            px = s.reindex(idx).ffill(limit=5)
            full = px.notna().all() and s.index[0] <= start
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
                relp = (s.reindex(idx).ffill(limit=5) / px.iloc[-1] * 10000).round()
                c["d"] = [None if v != v else int(v) for v in relp.iloc[-nd:]]
                c["w"] = [None if v != v else int(v) for v in relp.iloc[wk]]
        cards[sym] = c
        return c

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for t, h in hold.items():
        s = etf_series.get(t)
        er = period_returns(s, end) if s is not None and len(s) else [None] * 6
        top = []
        for sym, nm, w in h.get("top", []):
            c = dict(card(sym)) if sym else {"t": "", "n": nm}
            c["hn"] = nm          # the name the fund uses
            c["wt"] = w
            top.append(c)
        tw = sum(c["wt"] for c in top)
        rest = None
        if top and tw < 0.999:
            rest = {"wt": round(1 - tw, 5), "r": []}
            for k in range(5):        # 1M 3M 6M YTD 1Y; 3Y per year doesn't add up this way
                # a holding without a return for this period counts as part of the rest
                known = [c for c in top if c.get("r") and c["r"][k] is not None]
                left = 1 - sum(c["wt"] for c in known)
                if er[k] is None or left < 0.02:
                    rest["r"].append(None)
                    continue
                added = sum(c["wt"] * c["r"][k] for c in known)
                rest["r"].append(round((er[k] - added) / left, 4))
            rest["r"].append(None)
        doc = dict(meta, t=t, n=names.get(t, ""), desc=h.get("desc", ""), hasof=h.get("asof"),
                   sec=h.get("sec", {}), ac=h.get("ac", {}), er=er, top=top, rest=rest)
        (out / f"{t}.json").write_text(json.dumps(doc, separators=(",", ":")))
        written += 1
    log(f"What's inside: wrote {written} ETFs, {len(cards)} holding cards")
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
    names = {t: t for t in tickers}
    build_inside(tmp / "inside", etf, hold, stocks, got, names)
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
