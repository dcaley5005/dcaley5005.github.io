"""Build the Portfolio Lab data files and assemble the site in _site/.

Runs in GitHub Actions (see .github/workflows/refresh.yml).

Output, under _site/portfolio-lab/data/:
  core.csv        daily adjusted closes for the core tickers, one column each
  t/<TICKER>.csv  daily adjusted closes for every other ETF in the universe
  tw.csv          daily closes without dividends for the 12% Solution's ETFs, so it
                  matches the newsletter, which reports price change only
  universe.json   the ETF list the app offers (ticker, name)
  peers/<T>.json  "Explore alternatives" picks for each ETF, from the last 3 years of prices
  er.json         expense ratio per ETF (percent)
  fund_info.json  per-ETF details from Yahoo: fund size, expense ratio, average volume, price,
                  category, fund family, inception date (refreshed weekly, a slice per run)

The universe is every US-listed ETF that is not leveraged or inverse, ranked by
3-month average dollar volume (set UNIVERSE_SIZE to keep only the top N), plus the core tickers. It is
re-ranked weekly (Saturday runs) and saved to data/universe.json in the repo.
"""
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
SITE_SRC = ROOT / "site"
OUT_SITE = ROOT / "_site"
OUT = OUT_SITE / "portfolio-lab" / "data"
UNIVERSE_FILE = ROOT / "data" / "universe.json"
FUND_FILE = ROOT / "data" / "fund_info.json"
FUND_FIELDS = ["aum", "er", "er_ann", "vol", "px", "cat", "fam", "type", "inc", "asof"]
FUND_MAX_AGE_DAYS = 7
FUND_MINUTES = float(os.environ.get("FUND_INFO_MINUTES", "30"))
LIVE = "https://danielcaley.com/portfolio-lab/data"  # previous deploy, used as a fallback

UNIVERSE_SIZE = int(os.environ.get("UNIVERSE_SIZE", "0"))  # 0 = every non-leveraged ETF
START = "2007-01-01"
CORE = ("SPY QQQ MDY IWM IJR VTI XLK EFA VEA VWO VXUS VT ACWI AGG BND TLT IEF SHY "
        "TIP LQD JNK HYG GLD FCNTX ACWX IEUR IEMG").split()
LEVERAGED = re.compile(
    r"(\b\d(\.\d+)?x\b|-\dx\b|ultra|leverag|inverse|\bbear\b|\bbull\b|daily target|"
    r"2x|3x|short (qqq|s&p|dow|russell|20)|\bvix\b)",
    re.I,
)
TW = "SPY QQQ MDY IWM TLT JNK SHY".split()  # the 12% Solution's ETFs
UA = {"User-Agent": "Mozilla/5.0 (portfolio-lab data refresh)"}


def log(*a):
    print(*a, flush=True)


def ysym(t):
    return t.replace(".", "-")


def get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------- universe ----------
def nasdaq_etfs():
    """All US-listed ETFs from Nasdaq Trader's symbol directory: {ticker: name}."""
    out = {}
    for f, sym_col in (("otherlisted.txt", "ACT Symbol"), ("nasdaqlisted.txt", "Symbol")):
        text = get("https://www.nasdaqtrader.com/dynamic/SymDir/" + f)
        lines = [l for l in text.replace("\r", "\n").split("\n") if l.strip()]
        head = lines[0].split("|")
        si, ni, ei, ti = (head.index(sym_col), head.index("Security Name"),
                          head.index("ETF"), head.index("Test Issue"))
        for l in lines[1:]:
            c = l.split("|")
            if len(c) < len(head) or l.startswith("File Creation Time"):
                continue
            if c[ei] == "Y" and c[ti] == "N" and re.fullmatch(r"[A-Z][A-Z0-9.]{0,7}", c[si]):
                out[c[si]] = c[ni].strip()
    return out


def dollar_volume(tickers):
    """3-month average daily dollar volume per ticker."""
    dv = {}
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        for attempt in range(3):
            try:
                df = yf.download([ysym(t) for t in chunk], period="3mo", auto_adjust=False,
                                 actions=False, threads=True, progress=False, group_by="column")
                close, vol = df["Close"], df["Volume"]
                if isinstance(close, pd.Series):
                    close, vol = close.to_frame(ysym(chunk[0])), vol.to_frame(ysym(chunk[0]))
                m = (close * vol).mean()
                for t in chunk:
                    v = m.get(ysym(t))
                    if v is not None and pd.notna(v):
                        dv[t] = float(v)
                break
            except Exception as e:  # rate limits and network hiccups
                log(f"  volume chunk {i} attempt {attempt + 1} failed: {e}")
                time.sleep(10 * (attempt + 1))
        time.sleep(1.5)
        log(f"  ranked {min(i + 200, len(tickers))}/{len(tickers)}")
    return dv


def build_universe():
    etfs = nasdaq_etfs()
    log(f"Nasdaq directory: {len(etfs)} ETFs")
    plain = [t for t, n in etfs.items() if not LEVERAGED.search(n)]
    log(f"Excluding leveraged/inverse: {len(plain)}")
    dv = dollar_volume(sorted(plain))
    ranked = sorted(dv, key=dv.get, reverse=True)
    total = sum(dv.values()) or 1
    top = ranked[:UNIVERSE_SIZE] if UNIVERSE_SIZE > 0 else ranked
    log(f"Top {len(top)} cover {sum(dv[t] for t in top) / total:.1%} of non-leveraged dollar volume")
    if len(top) < 300:
        raise SystemExit("Ranking returned too few ETFs; keeping the previous universe.")
    etfs_list = [[t, etfs[t]] for t in top]
    return {
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "size": UNIVERSE_SIZE,
        "coverage": round(sum(dv[t] for t in top) / total, 4),
        "etfs": etfs_list,
    }


# ---------- fund details ----------
def _num(x):
    try:
        x = float(x)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def fetch_fund(t):
    """One ETF's details from Yahoo. Raises on a failed request."""
    info = yf.Ticker(ysym(t)).get_info() or {}
    if not info or (info.get("quoteType") is None and info.get("totalAssets") is None):
        raise ValueError("empty response")
    inc = info.get("fundInceptionDate")
    inc = datetime.fromtimestamp(inc, timezone.utc).strftime("%Y-%m-%d") if isinstance(inc, (int, float)) else None
    px = _num(info.get("navPrice")) or _num(info.get("regularMarketPrice")) or _num(info.get("previousClose"))
    return {
        "aum": _num(info.get("totalAssets")),
        "er": _num(info.get("netExpenseRatio")),          # percent, e.g. 0.09 = 0.09%
        "er_ann": _num(info.get("annualReportExpenseRatio")),
        "vol": _num(info.get("averageVolume")),            # 3-month average shares a day
        "px": px,
        "cat": info.get("category"),
        "fam": info.get("fundFamily"),
        "type": info.get("legalType") or info.get("quoteType"),
        "inc": inc,
        "asof": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def update_fund_info(tickers):
    """Refresh the oldest or missing entries within a time budget; keep everything else."""
    old = {}
    if FUND_FILE.exists():
        try:
            j = json.loads(FUND_FILE.read_text())
            old = {t: dict(zip(j["fields"], row)) for t, row in j["f"].items()}
        except Exception as e:
            log(f"Couldn't read the saved fund details ({e}); starting fresh.")
    today = datetime.now(timezone.utc).date()

    def age(t):
        a = old.get(t, {}).get("asof")
        return 9999 if not a else (today - datetime.strptime(a, "%Y-%m-%d").date()).days

    todo = sorted([t for t in tickers if age(t) >= FUND_MAX_AGE_DAYS], key=age, reverse=True)
    log(f"Fund details: {len(tickers) - len(todo)} fresh, {len(todo)} to refresh (budget {FUND_MINUTES:.0f} min)")
    # Yahoo starts refusing after several hundred quick requests, so go steadily and,
    # when refused, pause and pick up again (up to a few times) within the time budget.
    def paced(t):
        time.sleep(0.4)
        return fetch_fund(t)

    start, done, failed, pauses = time.time(), 0, 0, 0
    queue = list(todo)
    while queue and time.time() - start < FUND_MINUTES * 60 and pauses <= 4:
        batch, queue = queue[:40], queue[40:]
        errs = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(paced, t): t for t in batch}
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    old[t] = fut.result()
                    done += 1
                except Exception:
                    errs.append(t)
        if len(errs) > len(batch) * 0.6:      # mostly refused: back off and retry this batch later
            pauses += 1
            queue = errs + queue
            log(f"  refused by Yahoo; pausing 90s (pause {pauses})")
            time.sleep(90)
        else:
            failed += len(errs)
        if (done + failed) // 250 != (done + failed - len(batch)) // 250:
            log(f"  fund details {done + failed}/{len(todo)} ({failed} failed)")
    keep = {t: old[t] for t in tickers if t in old}
    FUND_FILE.parent.mkdir(parents=True, exist_ok=True)
    FUND_FILE.write_text(json.dumps({
        "asof": today.isoformat(), "fields": FUND_FIELDS,
        "f": {t: [v.get(k) for k in FUND_FIELDS] for t, v in sorted(keep.items())},
    }, separators=(",", ":")))
    log(f"Fund details: refreshed {done}, failed {failed}, have {len(keep)} of {len(tickers)}")


# ---------- explore alternatives ----------
EXPLORE_DAYS = 756            # about 3 years of trading days
COMPLEX_CAT = re.compile(r"^(Defined Outcome|Derivative Income|Trading)")
COMPLEX_NAME = re.compile(r"buffer|defined outcome|option income|covered call|premium income|"
                          r"yieldmax|single.stock|autocallable", re.I)


def load_fund_info():
    if not FUND_FILE.exists():
        return {}
    j = json.loads(FUND_FILE.read_text())
    return {t: dict(zip(j["fields"], row)) for t, row in j["f"].items()}


def period_returns(s, last):
    """1M 3M 6M 1Y return, then 3Y and 5Y per year; None where history is too short."""
    out = []
    for m in (1, 3, 6, 12, 36, 60):
        cut = last - pd.DateOffset(months=m)
        prev = s[:cut]
        if prev.empty or s.index[0] > cut:
            out.append(None)
            continue
        r = float(s.iloc[-1] / prev.iloc[-1] - 1)
        out.append(round((1 + r) ** (12 / m) - 1 if m > 12 else r, 4))
    return out


def build_explore(series, names, fund):
    """For every ETF with 3 years of prices: up to 3 that track it and beat it on return and
    Sharpe, up to 3 that move differently with a Sharpe within 20% of it, and its closest peers."""
    import numpy as np
    spy = series["SPY"]
    cut = spy.index[-1] - pd.DateOffset(years=3)
    idx = spy.index[spy.index >= spy[:cut].index[-1]]   # same 3-year window as the 3Y column
    start, end = idx[0], idx[-1]
    cols = {t: s.reindex(idx).ffill(limit=5) for t, s in series.items()
            if len(s) and s.index[0] <= start and s.index[-1] >= end - pd.Timedelta(days=7)}
    px = pd.DataFrame(cols).dropna(axis=1)
    rets = px.pct_change().iloc[1:]
    tick = list(px.columns)
    years = (end - start).days / 365.25
    growth = px.iloc[-1] / px.iloc[0]
    r3 = growth ** (1 / years) - 1
    vol = rets.std() * np.sqrt(252)
    rf = rets["SHY"] if "SHY" in rets else 0.0
    sharpe = rets.sub(rf, axis=0).mean() * 252 / vol
    dd = (px / px.cummax() - 1).min()
    z = ((rets - rets.mean()) / rets.std(ddof=0)).to_numpy()
    n = z.shape[0]

    def ok(t):
        f = fund.get(t) or {}
        return ((f.get("aum") or 0) >= 50e6 and (f.get("type") or "").upper() != "MUTUALFUND"
                and not COMPLEX_CAT.search(f.get("cat") or "")
                and not COMPLEX_NAME.search(names.get(t, "")))

    cand = [i for i, t in enumerate(tick) if ok(t)]
    zc = z[:, cand]
    pts = [round(k * (len(px) - 1) / 24) for k in range(25)]
    spark = {t: [round(float(v) * 100) for v in (px[t].iloc[pts] / px[t].iloc[0] - 1)] for t in tick}
    last = series["SPY"].index[-1]
    pr = {t: period_returns(series[t], last) for t in tick}

    def card(t, c=None):
        f = fund.get(t) or {}
        d = {"t": t, "n": names.get(t, ""), "er": f.get("er"), "r": pr[t],
             "r3": round(float(r3[t]), 4), "sh": round(float(sharpe[t]), 3),
             "dd": round(float(dd[t]), 4), "vol": round(float(vol[t]), 4), "sp": spark[t]}
        if c is not None:
            d["c"] = round(float(c), 3)
        return d

    out = OUT / "peers"
    out.mkdir(parents=True, exist_ok=True)
    meta = {"asof": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d")}
    written = 0
    for i, t in enumerate(tick):
        if t not in names:
            continue
        corr = (z[:, i] @ zc) / n
        rows = [(tick[cand[k]], float(corr[k])) for k in range(len(cand)) if tick[cand[k]] != t]
        sb, rb = float(sharpe[t]), float(r3[t])
        same = sorted([(u, c) for u, c in rows if c >= 0.9 and r3[u] > rb and sharpe[u] > sb],
                      key=lambda x: -sharpe[x[0]])[:3]
        closest = [] if same else sorted([(u, c) for u, c in rows if c >= 0.9], key=lambda x: -x[1])[:3]
        floor = sb * 0.8 if sb > 0 else sb
        div = sorted([(u, c) for u, c in rows if c < 0.6 and sharpe[u] >= floor],
                     key=lambda x: -sharpe[x[0]])[:3]
        doc = dict(meta, self=card(t), same=[card(u, c) for u, c in same],
                   closest=[card(u, c) for u, c in closest], div=[card(u, c) for u, c in div],
                   divFloor=round(floor, 3), pool=len(cand))
        (out / f"{t}.json").write_text(json.dumps(doc, separators=(",", ":")))
        written += 1
    log(f"Explore: {written} ETFs with 3 years of prices, {len(cand)} candidates to suggest")


def load_universe():
    force = os.environ.get("REBUILD_UNIVERSE", "").lower() in ("1", "true", "yes")
    uni = None
    if UNIVERSE_FILE.exists():
        uni = json.loads(UNIVERSE_FILE.read_text())
    stale = True
    if uni:
        age = (datetime.now(timezone.utc).date()
               - datetime.strptime(uni["asof"], "%Y-%m-%d").date()).days
        stale = age >= 8 or uni.get("size") != UNIVERSE_SIZE
    saturday = datetime.now(timezone.utc).weekday() == 5
    if force or stale or saturday or not uni:
        log("Re-ranking the ETF universe…")
        try:
            uni = build_universe()
            UNIVERSE_FILE.parent.mkdir(parents=True, exist_ok=True)
            UNIVERSE_FILE.write_text(json.dumps(uni, separators=(",", ":")))
        except (Exception, SystemExit) as e:
            if not uni:
                raise
            log(f"Universe refresh failed ({e}); using the saved list from {uni['asof']}.")
    return uni


# ---------- prices ----------
def fmt(v):
    return f"{v:.6g}"


def download_closes(tickers, adjust=True):
    """Daily closes since START: {ticker: pd.Series}. adjust=False leaves dividends out (splits are still applied)."""
    got = {}
    todo = list(tickers)
    for rnd in range(3):
        if not todo:
            break
        size = 50 if rnd == 0 else 10
        failed = []
        for i in range(0, len(todo), size):
            chunk = todo[i:i + size]
            try:
                df = yf.download([ysym(t) for t in chunk], start=START, auto_adjust=adjust,
                                 actions=False, threads=True, progress=False, group_by="column")
                close = df["Close"]
                if isinstance(close, pd.Series):
                    close = close.to_frame(ysym(chunk[0]))
                for t in chunk:
                    s = close[ysym(t)].dropna() if ysym(t) in close else pd.Series(dtype=float)
                    s = s[s > 0]
                    if len(s):
                        got[t] = s
                    else:
                        failed.append(t)
            except Exception as e:
                log(f"  chunk failed ({e}); will retry")
                failed.extend(chunk)
                time.sleep(15)
            time.sleep(1)
            log(f"  round {rnd + 1}: {min(i + size, len(todo))}/{len(todo)}")
        todo = failed
        if todo:
            time.sleep(20)
    return got, todo


def write_series(path, t, s):
    lines = ["Date," + t] + [f"{d:%Y-%m-%d},{fmt(v)}" for d, v in s.items()]
    path.write_text("\n".join(lines) + "\n")


def fallback(url, dest):
    try:
        text = get(url)
        if text.startswith("Date,"):
            dest.write_text(text)
            return True
    except Exception:
        pass
    return False


def write_table(path, series, cols):
    df = pd.DataFrame(series)[cols]
    df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
    lines = ["Date," + ",".join(cols)]
    for d, row in df.iterrows():
        lines.append(d + "," + ",".join("" if pd.isna(v) else fmt(v) for v in row))
    path.write_text("\n".join(lines) + "\n")
    return df.index[-1]


def main():
    if OUT_SITE.exists():
        shutil.rmtree(OUT_SITE)
    shutil.copytree(SITE_SRC, OUT_SITE)
    (OUT / "t").mkdir(parents=True, exist_ok=True)

    uni = load_universe()
    names = {t: n for t, n in uni["etfs"]}
    others = [t for t in names if t not in CORE]
    log(f"Universe: {len(names)} ETFs ({len(others)} beyond the core)")

    try:
        update_fund_info(list(names))
    except Exception as e:  # never let this block the price refresh
        log(f"Fund details step failed: {e}")
    if FUND_FILE.exists():
        shutil.copy(FUND_FILE, OUT / "fund_info.json")

    log("Downloading core prices…")
    core, core_missing = download_closes(CORE)
    if core_missing:
        log(f"Core tickers missing: {core_missing}")
        if not fallback(f"{LIVE}/core.csv", OUT / "core.csv"):
            raise SystemExit("Core prices are incomplete and no previous copy is available.")
        log("Kept the previous core.csv")
    else:
        log(f"core.csv through {write_table(OUT / 'core.csv', core, CORE)}")

    log("Downloading 12% Solution prices without dividends…")
    tw, tw_missing = download_closes(TW, adjust=False)
    if tw_missing:
        log(f"12% Solution tickers missing: {tw_missing}")
        log("Kept the previous tw.csv" if fallback(f"{LIVE}/tw.csv", OUT / "tw.csv")
            else "No tw.csv this run; the app falls back to prices with dividends")
    else:
        log(f"tw.csv through {write_table(OUT / 'tw.csv', tw, TW)}")

    log("Downloading the rest of the universe…")
    got, missing = download_closes(others)
    for t, s in got.items():
        write_series(OUT / "t" / f"{t}.csv", t, s)
    kept = [t for t in missing if fallback(f"{LIVE}/t/{t}.csv", OUT / "t" / f"{t}.csv")]
    lost = [t for t in missing if t not in kept]
    log(f"Fresh: {len(got)}, kept previous: {len(kept)}, unavailable: {len(lost)}")
    if len(got) < 0.75 * len(others):
        raise SystemExit("Too many downloads failed; not deploying so the live site keeps yesterday's data.")

    avail = [[t, names[t]] for t in names if t in CORE or t not in lost]
    (OUT / "universe.json").write_text(json.dumps(
        {"asof": uni["asof"], "built": datetime.now(timezone.utc).isoformat(timespec="minutes"),
         "etfs": avail}, separators=(",", ":")))
    try:
        fund = load_fund_info()
        (OUT / "er.json").write_text(json.dumps(
            {t: f["er"] for t, f in fund.items() if f.get("er") is not None}, separators=(",", ":")))
        build_explore({**core, **got}, names, fund)
    except Exception as e:  # never let this block the price refresh
        log(f"Explore step failed: {e}")
    log("Done.")


if __name__ == "__main__":
    sys.exit(main())
