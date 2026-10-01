"""Build the Portfolio Lab data files and assemble the site in _site/.

Runs in GitHub Actions (see .github/workflows/refresh.yml).

Output, under _site/portfolio-lab/data/:
  core.csv        daily adjusted closes for the core tickers, one column each
  t/<TICKER>.csv  daily adjusted closes for every other ETF in the universe
  tw.csv          daily closes without dividends for the 12% Solution's ETFs, so it
                  matches the newsletter, which reports price change only
  universe.json   the ETF list the app offers (ticker, name)

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
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
SITE_SRC = ROOT / "site"
OUT_SITE = ROOT / "_site"
OUT = OUT_SITE / "portfolio-lab" / "data"
UNIVERSE_FILE = ROOT / "data" / "universe.json"
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
    log("Done.")


if __name__ == "__main__":
    sys.exit(main())
