"""Full ETF holdings from the SEC's Form N-PORT data sets.

Every registered fund (ETFs included) reports its complete portfolio to the SEC; the SEC posts the
quarter-end reports as one bulk ZIP per quarter at
  https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets
(about 400-700 MB, tab-separated tables inside). This script downloads a quarter, keeps only the
ETFs in data/universe.json, and writes each one's top holdings with their share of the fund.

ETF ticker -> SEC series comes from https://www.sec.gov/files/company_tickers_mf.json.

Trial mode (no repo changes, writes a report and a few sample files to OUT_DIR):
  python scripts/nport.py --quarter 2026q3 --trial SPY QQQ XLK IWM SCHD

The SEC asks automated tools to identify themselves; set SEC_USER_AGENT (for example
"Portfolio Lab yourname@example.com") in the workflow.
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_FILE = ROOT / "data" / "universe.json"
YAHOO_HOLD = ROOT / "data" / "holdings.json"
INDEX_URL = "https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
UA = os.environ.get("SEC_USER_AGENT", "Portfolio Lab research script (danielcaley.com)")
TOP_N = int(os.environ.get("NPORT_TOP_N", "50"))


def log(*a):
    print(*a, flush=True)


def get(url, dest=None, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=300) as r:
                if dest is None:
                    return r.read()
                with open(dest, "wb") as f:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        f.write(b)
                return dest
        except Exception as e:
            log(f"  {url}: {e} (try {k + 1})")
            time.sleep(5 * (k + 1))
    raise RuntimeError(f"couldn't download {url}")


# ---------- the SEC files ----------
def quarter_url(q):
    """Find the ZIP link for a quarter such as 2026q3 (or "latest") on the data sets page."""
    html = get(INDEX_URL).decode("utf-8", "replace")
    links = sorted(set(re.findall(r'href="([^"]+\.zip)"', html, re.I)))
    if q.lower() == "latest":
        qs = sorted({m.lower() for u in links for m in re.findall(r"\d{4}q[1-4]", u, re.I)})
        log(f"Quarters on the page: {qs[-6:]} (newest last)")
        q = qs[-1] if qs else q
    hit = [u for u in links if q.lower() in u.lower()]
    log(f"Data sets page lists {len(links)} ZIPs; {len(hit)} match {q}: {hit[:3]}")
    if not hit:
        raise RuntimeError(f"no ZIP for {q} on {INDEX_URL}; first links: {links[:5]}")
    u = hit[0]
    return u if u.startswith("http") else "https://www.sec.gov" + u


def series_map():
    """ETF/fund ticker -> SEC series id."""
    j = json.loads(get(TICKERS_URL))
    f = [x.lower() for x in j["fields"]]
    si, ti = f.index("seriesid"), f.index("symbol")
    m = {}
    for row in j["data"]:
        if row[ti] and row[si]:
            m[str(row[ti]).upper()] = row[si]
    log(f"SEC ticker list: {len(m)} fund tickers with a series id")
    return m


def member(zf, name):
    for n in zf.namelist():
        if n.split("/")[-1].upper() == name.upper():
            return n
    return None


def pick(cols, *cands):
    up = {c.upper(): c for c in cols}
    for c in cands:
        if c.upper() in up:
            return up[c.upper()]
    return None


def read(zf, name, usecols=None, chunks=False):
    n = member(zf, name)
    if not n:
        raise RuntimeError(f"{name} not in ZIP; members: {zf.namelist()[:30]}")
    kw = dict(sep="\t", dtype=str, quoting=3, on_bad_lines="skip", encoding_errors="replace", low_memory=False)
    if usecols:
        kw["usecols"] = usecols
    if chunks:
        return pd.read_csv(zf.open(n), chunksize=500_000, **kw)
    return pd.read_csv(zf.open(n), **kw)


def headers(zf, name):
    n = member(zf, name)
    with zf.open(n) as f:
        return io.TextIOWrapper(f, encoding="utf-8", errors="replace").readline().rstrip("\n").split("\t")


def load_quarter(zpath, want_series):
    """{series_id: {"asof", "name", "net", "n", "top": [[name, ticker, cusip, isin, pct, cat, country], ...]}}"""
    zf = zipfile.ZipFile(zpath)
    log("ZIP members:", [n.split('/')[-1] for n in zf.namelist()])
    for t in ("SUBMISSION.tsv", "FUND_REPORTED_INFO.tsv", "FUND_REPORTED_HOLDING.tsv", "IDENTIFIERS.tsv"):
        if member(zf, t):
            log(f"  {t} columns: {headers(zf, t)}")

    sub = read(zf, "SUBMISSION.tsv")
    acc = pick(sub.columns, "ACCESSION_NUMBER")
    rdate = pick(sub.columns, "REPORT_DATE", "REPORT_ENDING_PERIOD", "PERIOD_OF_REPORT")
    fdate = pick(sub.columns, "FILING_DATE")
    sub = sub[[c for c in (acc, rdate, fdate) if c]].rename(columns={acc: "acc", rdate: "asof", fdate: "filed"})
    log("  sample SUBMISSION dates:", sub.head(3).to_dict("records"))
    # the SEC writes dates like 30-JUN-2026; store them as 2026-06-30 so they sort
    for c in ("asof", "filed"):
        if c in sub:
            sub[c] = pd.to_datetime(sub[c], format="mixed", errors="coerce", dayfirst=True).dt.strftime("%Y-%m-%d")

    fri = read(zf, "FUND_REPORTED_INFO.tsv")
    sid = pick(fri.columns, "SERIES_ID")
    sname = pick(fri.columns, "SERIES_NAME")
    net = pick(fri.columns, "NET_ASSETS")
    fri = fri[[c for c in (pick(fri.columns, "ACCESSION_NUMBER"), sid, sname, net) if c]]
    fri.columns = ["acc", "sid", "name", "net"][:len(fri.columns)]
    fri = fri[fri["sid"].isin(want_series)].merge(sub, on="acc", how="left")
    # one report per series: the latest period, latest filing (amendments win)
    fri = fri.sort_values(["sid", "asof", "filed"]).groupby("sid").tail(1)
    accs = set(fri["acc"])
    log(f"Reports for {fri['sid'].nunique()} of {len(want_series)} wanted series in this quarter")

    hcols = headers(zf, "FUND_REPORTED_HOLDING.tsv")
    H = dict(acc=pick(hcols, "ACCESSION_NUMBER"), hid=pick(hcols, "HOLDING_ID"),
             name=pick(hcols, "ISSUER_NAME"), title=pick(hcols, "ISSUER_TITLE"),
             cusip=pick(hcols, "ISSUER_CUSIP"), pct=pick(hcols, "PERCENTAGE"),
             cat=pick(hcols, "ASSET_CAT"), country=pick(hcols, "INVESTMENT_COUNTRY"))
    use = [v for v in H.values() if v]
    rows = []
    for ch in read(zf, "FUND_REPORTED_HOLDING.tsv", usecols=use, chunks=True):
        ch = ch[ch[H["acc"]].isin(accs)]
        if len(ch):
            rows.append(ch)
    hold = pd.concat(rows) if rows else pd.DataFrame(columns=use)
    hold = hold.rename(columns={v: k for k, v in H.items() if v})
    hold["pct"] = pd.to_numeric(hold["pct"], errors="coerce")
    log(f"Holdings rows for those reports: {len(hold):,}")

    # tickers and ISINs live in IDENTIFIERS (one row per identifier)
    ident = {}
    if member(zf, "IDENTIFIERS.tsv") and "hid" in hold:
        icols = headers(zf, "IDENTIFIERS.tsv")
        I = dict(hid=pick(icols, "HOLDING_ID"), isin=pick(icols, "IDENTIFIER_ISIN", "ISIN"),
                 ticker=pick(icols, "IDENTIFIER_TICKER", "TICKER"))
        hids = set(hold["hid"])
        for ch in read(zf, "IDENTIFIERS.tsv", usecols=[v for v in I.values() if v], chunks=True):
            ch = ch[ch[I["hid"]].isin(hids)]
            for r in ch.itertuples(index=False):
                d = r._asdict() if hasattr(r, "_asdict") else dict(zip(ch.columns, r))
                rec = ident.setdefault(d[I["hid"]], {})
                if I["isin"] and isinstance(d.get(I["isin"]), str):
                    rec["isin"] = d[I["isin"]]
                if I["ticker"] and isinstance(d.get(I["ticker"]), str):
                    rec["ticker"] = d[I["ticker"]]

    out = {}
    info = fri.set_index("acc")
    for a, g in hold.groupby("acc"):
        g = g.sort_values("pct", ascending=False)
        top = []
        for r in g.head(TOP_N).itertuples(index=False):
            d = r._asdict()
            idn = ident.get(d.get("hid"), {})
            top.append([str(d.get("name") or ""), idn.get("ticker", ""), str(d.get("cusip") or ""),
                        idn.get("isin", ""), round(float(d["pct"]) / 100, 6) if d["pct"] == d["pct"] else None,
                        str(d.get("cat") or ""), str(d.get("country") or "")])
        i = info.loc[a]
        out[i["sid"]] = {"asof": str(i.get("asof", ""))[:10], "name": i.get("name", ""),
                         "net": i.get("net"), "n": int(len(g)), "top": top}
    return out


# ---------- trial ----------
def trial(q, tickers, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    smap = series_map()
    uni = json.loads(UNIVERSE_FILE.read_text())
    uni = uni.get("etfs", uni) if isinstance(uni, dict) else uni
    etfs = sorted({(r[0] if isinstance(r, list) else r.get("t")) for r in uni})
    mapped = {t: smap[t] for t in etfs if t in smap}
    log(f"Universe: {len(etfs)} ETFs, {len(mapped)} map to an SEC series ({len(etfs) - len(mapped)} don't)")
    zpath = out_dir.parent / f"{q}.zip"
    if not zpath.exists():
        url = quarter_url(q)
        log(f"Downloading {url}")
        t0 = time.time()
        get(url, zpath)
        log(f"  {zpath.stat().st_size / 1e6:.0f} MB in {time.time() - t0:.0f}s")
    t0 = time.time()
    got = load_quarter(zpath, set(mapped.values()))
    log(f"Parsed in {time.time() - t0:.0f}s")

    by_t = {t: got.get(s) for t, s in mapped.items()}
    have = {t: v for t, v in by_t.items() if v}
    tick_share = [sum(1 for h in v["top"] if h[1]) / max(1, len(v["top"])) for v in have.values()]
    stock_share = [sum(1 for h in v["top"] if h[5] in ("EC", "EP")) / max(1, len(v["top"])) for v in have.values()]
    dates = defaultdict(int)
    for v in have.values():
        dates[v["asof"][:7]] += 1
    rep = {
        "quarter": q, "universe": len(etfs), "mapped_to_series": len(mapped), "with_holdings": len(have),
        "missing_examples": [t for t in etfs if t not in mapped][:40],
        "mapped_but_no_report_examples": [t for t, v in by_t.items() if not v][:40],
        "report_months": dict(sorted(dates.items())),
        "avg_share_of_top_holdings_with_ticker": round(sum(tick_share) / max(1, len(tick_share)), 3),
        "avg_share_of_top_holdings_that_are_stock": round(sum(stock_share) / max(1, len(stock_share)), 3),
        "unique_holding_tickers_top50": len({h[1] for v in have.values() for h in v["top"] if h[1]}),
        "unique_holding_ids_top50": len({h[3] or h[2] or h[0] for v in have.values() for h in v["top"]}),
    }
    # side by side with the Yahoo top 10 we already have
    yh = json.loads(YAHOO_HOLD.read_text()) if YAHOO_HOLD.exists() else {}
    cmp = {}
    for t in tickers:
        v = have.get(t)
        if not v:
            cmp[t] = "no SEC report found"
            continue
        (out_dir / f"{t}.json").write_text(json.dumps(v, indent=1))
        y = (yh.get(t) or {}).get("top", [])
        cmp[t] = {"sec_asof": v["asof"], "sec_holdings": v["n"],
                  "sec_top10": [[h[1] or h[0], h[4]] for h in v["top"][:10]],
                  "yahoo_top10": [[r[0] or r[1], r[2]] for r in y[:10]]}
    rep["compare"] = cmp
    (out_dir / "report.json").write_text(json.dumps(rep, indent=1))
    log(json.dumps({k: v for k, v in rep.items() if k != "compare"}, indent=1))
    for t, c in cmp.items():
        log(t, json.dumps(c)[:600])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarter", required=True, help="such as 2026q3")
    ap.add_argument("--trial", nargs="*", default=["SPY", "QQQ", "XLK", "IWM", "SCHD", "IEUR", "VWO", "ARKK"])
    ap.add_argument("--out", default="/tmp/nport_trial/out")
    a = ap.parse_args()
    trial(a.quarter, [t.upper() for t in a.trial], a.out)


if __name__ == "__main__":
    sys.exit(main())
