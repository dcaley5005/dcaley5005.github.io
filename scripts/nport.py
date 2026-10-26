"""Full ETF holdings from the SEC's Form N-PORT data sets.

Every registered fund (ETFs included) reports its complete portfolio to the SEC; the SEC posts the
quarter-end reports as one bulk ZIP per quarter at
  https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets
(about 450 MB, tab-separated tables inside). ETF ticker -> SEC series comes from company_tickers_mf.json.

Three steps (the backfill workflow runs them):
  python scripts/nport.py quarters --since 2019q4      # list the quarters on the SEC page (JSON)
  python scripts/nport.py quarter 2026q3 --out DIR     # one quarter -> DIR/2026q3.json.gz
  python scripts/nport.py combine --inp DIR --out OUT  # all quarters -> one history file per ETF
  python scripts/nport.py site --inp OUT/etf           # trimmed copies for the site build -> data/nport/

What the per-quarter step keeps for each ETF report: the top holdings (NPORT_TOP_N, default 50) by
share of net assets, after dropping cash pools (asset category STIV, short-term investment vehicles,
which is where securities-lending cash collateral sits, and RA, repurchase agreements). Their total
is kept as "cash". Holdings are keyed by CUSIP (so share classes like GOOG and GOOGL stay apart),
falling back to ISIN, then name.

Tickers: most filers leave the ticker blank, so each holding's ticker is a vote across every fund in
the SEC file that holds the same CUSIP or ISIN, then turned into a Yahoo symbol (BRK.B -> BRK-B,
Taiwan 2330 -> 2330.TW). US stocks with no votes fall back to a name match against SEC's company list.

The SEC asks automated tools to identify themselves; set SEC_USER_AGENT ("Name email@example.com").
"""
import argparse
import gzip
import io
import json
import os
import re
import sys
import tarfile
import time
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_FILE = ROOT / "data" / "universe.json"
YAHOO_HOLD = ROOT / "data" / "holdings.json"
STOCKS_FILE = ROOT / "data" / "stocks.json"
INDEX_URL = "https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
COMPANY_URL = "https://www.sec.gov/files/company_tickers.json"
UA = os.environ.get("SEC_USER_AGENT", "Portfolio Lab daniel.caley1@gmail.com")
TOP_N = int(os.environ.get("NPORT_TOP_N", "50"))
CASH_CATS = {"STIV", "RA"}
SAMPLES = ["SPY", "QQQ", "XLK", "IWM", "SCHD", "IEUR", "VWO", "ARKK", "EWJ", "EWY", "INDA", "MCHI"]


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


def blank(v):
    return v is None or (isinstance(v, float) and v != v) or str(v).strip().upper() in ("", "NAN", "N/A", "NONE", "0", "000000000")


def clean(v):
    return "" if blank(v) else str(v).strip()


# ---------- the SEC files ----------
def zip_links():
    html = get(INDEX_URL).decode("utf-8", "replace")
    links = sorted(set(re.findall(r'href="([^"]+\.zip)"', html, re.I)))
    out = {}
    for u in links:
        m = re.search(r"(\d{4}q[1-4])", u, re.I)
        if m:
            out[m.group(1).lower()] = u if u.startswith("http") else "https://www.sec.gov" + u
    return dict(sorted(out.items()))


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


def universe():
    uni = json.loads(UNIVERSE_FILE.read_text())
    uni = uni.get("etfs", uni) if isinstance(uni, dict) else uni
    return sorted({(r[0] if isinstance(r, list) else r.get("t")) for r in uni})


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


def iso(s):
    # the SEC writes dates like 30-JUN-2026
    return pd.to_datetime(s, format="mixed", errors="coerce", dayfirst=True).dt.strftime("%Y-%m-%d")


def holding_key(cusip, isin, name, title):
    if cusip and len(cusip) == 9:
        return "C:" + cusip.upper()
    if isin and len(isin) == 12:
        return "I:" + isin.upper()
    return "N:" + (name + "|" + title).upper()[:120]


def load_quarter(zpath, want_series):
    """-> {"reports": {sid: [report, ...]}, "sec": {key: {...}}}"""
    zf = zipfile.ZipFile(zpath)
    for t in ("SUBMISSION.tsv", "FUND_REPORTED_INFO.tsv", "FUND_REPORTED_HOLDING.tsv", "IDENTIFIERS.tsv"):
        if member(zf, t):
            log(f"  {t} columns: {headers(zf, t)}")

    sub = read(zf, "SUBMISSION.tsv")
    acc = pick(sub.columns, "ACCESSION_NUMBER")
    rdate = pick(sub.columns, "REPORT_DATE", "REPORT_ENDING_PERIOD", "PERIOD_OF_REPORT")
    fdate = pick(sub.columns, "FILING_DATE")
    sub = sub[[c for c in (acc, rdate, fdate) if c]].rename(columns={acc: "acc", rdate: "asof", fdate: "filed"})
    for c in ("asof", "filed"):
        if c in sub:
            sub[c] = iso(sub[c])

    fri = read(zf, "FUND_REPORTED_INFO.tsv")
    fri = fri[[pick(fri.columns, "ACCESSION_NUMBER"), pick(fri.columns, "SERIES_ID"),
               pick(fri.columns, "SERIES_NAME"), pick(fri.columns, "NET_ASSETS")]]
    fri.columns = ["acc", "sid", "name", "net"]
    fri = fri[fri["sid"].isin(want_series)].merge(sub, on="acc", how="left")
    fri = fri[fri["asof"].notna()]
    fri["filed"] = fri["filed"].fillna("") if "filed" in fri else ""
    # one report per series and period: the latest filing (amendments win)
    fri = fri.sort_values(["sid", "asof", "filed"]).groupby(["sid", "asof"]).tail(1)
    accs = set(fri["acc"])
    log(f"Reports: {len(fri)} for {fri['sid'].nunique()} of {len(want_series)} wanted series; "
        f"periods {fri['asof'].value_counts().sort_index().to_dict()}")

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
    hold["pct"] = pd.to_numeric(hold["pct"], errors="coerce").fillna(0)
    for c in ("name", "title", "cusip", "cat", "country"):
        hold[c] = hold[c].map(clean) if c in hold else ""
    log(f"Holdings rows for those reports: {len(hold):,}; asset categories: "
        f"{hold.groupby('cat')['pct'].sum().sort_values(ascending=False).head(12).round(0).to_dict()}")

    icols = headers(zf, "IDENTIFIERS.tsv")
    I = dict(hid=pick(icols, "HOLDING_ID"), isin=pick(icols, "IDENTIFIER_ISIN", "ISIN"),
             ticker=pick(icols, "IDENTIFIER_TICKER", "TICKER"))
    ident = {}
    hids = set(hold["hid"])
    for ch in read(zf, "IDENTIFIERS.tsv", usecols=[v for v in I.values() if v], chunks=True):
        ch = ch[ch[I["hid"]].isin(hids)]
        for h, i, t in zip(ch[I["hid"]], ch[I["isin"]], ch[I["ticker"]]):
            rec = ident.setdefault(h, ["", ""])
            rec[0] = rec[0] or clean(i)
            rec[1] = rec[1] or clean(t)
    hold["isin"] = hold["hid"].map(lambda h: ident.get(h, ["", ""])[0])
    hold["ticker"] = hold["hid"].map(lambda h: ident.get(h, ["", ""])[1])
    hold["key"] = [holding_key(c, i, n, t) for c, i, n, t in zip(hold["cusip"], hold["isin"], hold["name"], hold["title"])]

    # the reports
    sec = {}
    reports = defaultdict(list)
    info = fri.set_index("acc")
    for a, g in hold.groupby("acc"):
        i = info.loc[a]
        cash = g[g["cat"].isin(CASH_CATS)]["pct"].sum()
        g = g[~g["cat"].isin(CASH_CATS)]
        w = g.groupby("key")["pct"].sum().sort_values(ascending=False)
        top = [[k, round(float(p) / 100, 6)] for k, p in w.head(TOP_N).items()]
        for k, _ in top:
            sec.setdefault(k, None)
        reports[i["sid"]].append({"asof": i["asof"], "filed": clean(i.get("filed")), "name": clean(i["name"]),
                                  "net": clean(i["net"]), "n": int(len(w)), "cash": round(float(cash) / 100, 6),
                                  "top": top})
    first = hold.drop_duplicates("key").set_index("key")
    for k in sec:
        r = first.loc[k]
        sec[k] = {"name": r["name"], "title": r["title"], "cusip": r["cusip"], "isin": r["isin"],
                  "cat": r["cat"], "country": r["country"], "votes": Counter()}

    # ticker votes from every fund in the file that holds the same CUSIP or ISIN
    by_cusip = {s["cusip"].upper(): k for k, s in sec.items() if len(s["cusip"]) == 9}
    by_isin = {s["isin"].upper(): k for k, s in sec.items() if len(s["isin"]) == 12}
    hid2key = {}
    for ch in read(zf, "FUND_REPORTED_HOLDING.tsv", usecols=[H["hid"], H["cusip"]], chunks=True):
        c = ch[H["cusip"]].str.upper()
        m = c.isin(by_cusip.keys())
        hid2key.update(zip(ch.loc[m, H["hid"]], c[m].map(by_cusip)))
    for ch in read(zf, "IDENTIFIERS.tsv", usecols=[v for v in I.values() if v], chunks=True):
        ch = ch[ch[I["ticker"]].notna()]
        for h, i, t in zip(ch[I["hid"]], ch[I["isin"]], ch[I["ticker"]]):
            k = hid2key.get(h) or (by_isin.get(i.upper()) if isinstance(i, str) else None)
            if k and not blank(t):
                sec[k]["votes"][str(t).strip().upper()] += 1
    voted = sum(1 for s in sec.values() if s["votes"])
    log(f"Distinct top holdings: {len(sec):,}; with at least one ticker vote: {voted:,}")
    for s in sec.values():
        s["votes"] = dict(s["votes"].most_common(5))
    return {"reports": reports, "sec": sec}


# ---------- tickers -> Yahoo symbols ----------
EXCH_WORDS = {"US", "UN", "UW", "UQ", "UA", "UP", "UR", "EQUITY", "LN", "TT", "HK", "JP", "JT", "KS", "KP", "CH", "C1", "C2",
              "IN", "IS", "IB", "GR", "GY", "FP", "NA", "SW", "SE", "CN", "CT", "AU", "AT", "SM", "IM", "SS", "DC", "FH", "NO", "BB"}
SUFFIX = {"GB": ".L", "DE": ".DE", "FR": ".PA", "CH": ".SW", "NL": ".AS", "ES": ".MC", "IT": ".MI", "SE": ".ST",
          "DK": ".CO", "FI": ".HE", "NO": ".OL", "BE": ".BR", "IE": ".IR", "AT": ".VI", "PT": ".LS", "CA": ".TO",
          "AU": ".AX", "JP": ".T", "HK": ".HK", "TW": ".TW", "KR": ".KS", "IN": ".NS", "SG": ".SI", "BR": ".SA",
          "MX": ".MX", "ZA": ".JO", "IL": ".TA", "NZ": ".NZ", "ID": ".JK", "MY": ".KL", "TH": ".BK", "SA": ".SR",
          "PL": ".WA", "TR": ".IS"}


def norm_ticker(t):
    t = t.upper().replace(" EQUITY", "").strip()
    m = re.match(r"^([A-Z]{1,5})[ ./\-]([A-Z])$", t)  # BRK B, BRK/B, BRK.B
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    tok = t.split()
    if not tok:
        return ""
    t = tok[0]
    if t in EXCH_WORDS:
        return ""
    t = re.sub(r"[./]([A-Z])$", r"-\1", t) if re.match(r"^[A-Z]{1,5}[./][A-Z]$", t) else t
    return t if re.match(r"^[A-Z0-9\-]{1,12}$", t) else ""


# Bloomberg-style exchange codes some funds add to the ticker ("DG FP"): code -> Yahoo ending ("" = US)
BBG = {"US": "", "UN": "", "UW": "", "UQ": "", "UA": "", "UP": "", "UR": "", "LN": ".L", "FP": ".PA", "GY": ".DE",
       "GR": ".DE", "SW": ".SW", "SE": ".SW", "VX": ".SW", "NA": ".AS", "SM": ".MC", "IM": ".MI", "SS": ".ST",
       "DC": ".CO", "FH": ".HE", "NO": ".OL", "BB": ".BR", "ID": ".IR", "AV": ".VI", "PL": ".LS", "CN": ".TO",
       "CT": ".TO", "AU": ".AX", "AT": ".AX", "JP": ".T", "JT": ".T", "HK": ".HK", "TT": ".TW", "KS": ".KS",
       "KP": ".KS", "KQ": ".KQ", "IN": ".NS", "IS": ".NS", "IB": ".BO", "SP": ".SI", "BZ": ".SA", "MM": ".MX",
       "SJ": ".JO", "IT": ".TA", "NZ": ".NZ", "IJ": ".JK", "MK": ".KL", "TB": ".BK", "AB": ".SR", "PW": ".WA",
       "TI": ".IS"}


def same_company(a, b):
    """Rough check that two names are the same company: the same first word once suffixes are gone."""
    a, b = name_key(a or "").split(), name_key(b or "").split()
    return bool(a and b and a[0] == b[0])


def yahoo_symbol(raw, s, known=None):
    """Best guess at the Yahoo symbol for a holding ("" when there's nothing to go on).
    known: {symbol: name} for the stocks the site already has (data/stocks.json)."""
    known = known or {}
    raw = (raw or "").upper().replace(" EQUITY", "").strip()
    c, isin = s.get("country", ""), s.get("isin", "").upper()
    foreign = (bool(isin) and not isin.startswith("US")) or (not isin and c not in ("US", ""))

    def first(cands, default):
        return next((x for x in cands if x in known), default)

    if re.match(r"^[A-Z0-9\-]+\.[A-Z]{1,3}$", raw) and raw.split(".")[1] in {v[1:] for v in SUFFIX.values()} | {"TWO", "KQ", "SS", "SZ"}:
        return raw  # already Yahoo style, such as 2330.TW
    # local codes spelled out inside the ISIN
    if re.match(r"^TW000\d{4}", isin):
        return first([isin[5:9] + ".TW", isin[5:9] + ".TWO"], isin[5:9] + ".TW")
    if re.match(r"^KR7\d{6}", isin):
        return first([isin[3:9] + ".KS", isin[3:9] + ".KQ"], isin[3:9] + ".KS")
    tok = raw.split()
    code = tok[-1] if len(tok) >= 2 and tok[-1] in BBG else None
    t = norm_ticker(" ".join(tok[:-1]) if code else raw)
    if not t:
        return ""
    if foreign and re.match(r"^[A-Z]{4}[FY]$", t):
        return ""  # a US over-the-counter line for a foreign stock (TCTZF, BABAF), not its home listing
    if code is not None:
        # the fund said which exchange
        suf = BBG[code]
        if suf == ".HK" and t.isdigit():
            return t.zfill(4) + suf
        return t + suf
    if t.isdigit():
        if len(t) == 6 and (c == "CN" or isin.startswith("CNE")):
            cands = [t + (".SS" if t.startswith("6") else ".SZ")]
        elif c == "JP" or isin.startswith("JP"):
            cands = [t + ".T"]
        elif len(t) <= 5 and (c in ("HK", "CN") or isin.startswith(("HK", "KYG", "BMG", "CNE"))):
            cands = [t.zfill(4) + ".HK"]
        elif c in SUFFIX:
            cands = [t + SUFFIX[c]]
        else:
            return ""
        return first(cands, cands[0])
    if not foreign:
        return t
    if c in ("HK", "CN", "JP", "TW", "KR") or isin.startswith(("HK", "JP", "CNE", "TW", "KR")):
        return ""  # those markets use number codes, so a letter ticker is a US line
    # a foreign stock: its home listing, unless the plain ticker is a US listing of the same company
    # (Chubb is CB, but DG is Dollar General, not Vinci)
    local = t + SUFFIX[c] if c in SUFFIX else ""
    if local and local in known:
        return local
    if t in known and same_company(known[t], s.get("name", "")):
        return t
    return local or t


def name_key(n):
    n = re.sub(r"[^A-Z0-9 ]", " ", n.upper())
    n = re.sub(r"\b(THE|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|HOLDINGS?|GROUP|SA|NV|AG|LLC|LP|CLASS [A-Z]|CL [A-Z]|COM|NEW|DEL)\b", " ", n)
    return " ".join(n.split())


def company_names():
    """SEC company list: normalized name -> ticker, only where the name gives one ticker."""
    try:
        j = json.loads(get(COMPANY_URL))
    except Exception as e:
        log(f"  company list unavailable: {e}")
        return {}
    by = defaultdict(set)
    for r in j.values():
        by[name_key(r["title"])].add(r["ticker"].upper().replace(".", "-"))
    return {k: next(iter(v)) for k, v in by.items() if len(v) == 1 and k}


# ---------- steps ----------
def cmd_quarters(a):
    qs = [q for q in zip_links() if q >= a.since.lower()]
    print(json.dumps(qs))


def cmd_quarter(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    links = zip_links()
    q = max(links) if a.q == "latest" else a.q.lower()
    if q not in links:
        raise RuntimeError(f"no ZIP for {q}; the page has {list(links)}")
    smap = series_map()
    want = {smap[t] for t in universe() if t in smap}
    zpath = Path(a.work) / f"{q}.zip"
    zpath.parent.mkdir(parents=True, exist_ok=True)
    if not zpath.exists():
        t0 = time.time()
        get(links[q], zpath)
        log(f"Downloaded {links[q]}: {zpath.stat().st_size / 1e6:.0f} MB in {time.time() - t0:.0f}s")
    t0 = time.time()
    d = load_quarter(zpath, want)
    d["q"] = q
    log(f"Parsed {q} in {time.time() - t0:.0f}s")
    with gzip.open(out / f"{q}.json.gz", "wt") as f:
        json.dump(d, f, separators=(",", ":"))
    log(f"Wrote {out / f'{q}.json.gz'} ({(out / f'{q}.json.gz').stat().st_size / 1e6:.1f} MB)")


def cmd_combine(a):
    inp, out = Path(a.inp), Path(a.out)
    (out / "etf").mkdir(parents=True, exist_ok=True)
    files = sorted(inp.rglob("*.json.gz"))
    log(f"Combining {len(files)} quarter files: {[f.name for f in files]}")
    reports, sec = defaultdict(dict), {}
    for fp in files:
        with gzip.open(fp, "rt") as f:
            d = json.load(f)
        for sid, rs in d["reports"].items():
            for r in rs:
                old = reports[sid].get(r["asof"])
                if not old or (r.get("filed") or "") >= (old.get("filed") or ""):
                    reports[sid][r["asof"]] = r
        for k, s in d["sec"].items():
            cur = sec.get(k)
            if not cur:
                sec[k] = dict(s, votes=Counter(s["votes"]))
            else:
                cur["votes"].update(s["votes"])
                for f2 in ("name", "title", "isin", "cat", "country"):
                    cur[f2] = s[f2] or cur[f2]  # later quarters win

    # one key per security: funds report some stocks by CUSIP and others by ISIN only
    isin_of = {v["cusip"].upper(): v["isin"].upper() for v in sec.values() if len(v["cusip"]) == 9 and len(v["isin"]) == 12}
    canon = {}
    for k, v in sec.items():
        isin = v["isin"].upper() if len(v["isin"]) == 12 else isin_of.get(v["cusip"].upper(), "")
        canon[k] = "I:" + isin if isin else k
    merged = {}
    for k, v in sec.items():
        c = canon[k]
        if c not in merged:
            merged[c] = dict(v, votes=Counter(v["votes"]))
        else:
            m = merged[c]
            m["votes"].update(v["votes"])
            for f2 in ("name", "title", "cusip", "isin", "cat", "country"):
                m[f2] = m[f2] or v[f2]
    log(f"Distinct holdings: {len(sec):,} keys -> {len(merged):,} securities after joining CUSIP and ISIN")
    sec = merged
    for by_date in reports.values():
        for r in by_date.values():
            w = Counter()
            for k, p in r["top"]:
                w[canon[k]] += p
            r["top"] = [[k, round(p, 6)] for k, p in w.most_common()]

    names = company_names()
    stock_info = json.loads(STOCKS_FILE.read_text()) if STOCKS_FILE.exists() else {}
    stocks = {k: v.get("n", "") for k, v in stock_info.items()}
    yname = defaultdict(list)  # Yahoo name -> symbols the site already has
    for sym_, v in stock_info.items():
        if v.get("qt") == "EQUITY" and v.get("n"):
            yname[name_key(v["n"])].append(sym_)
    how = Counter()
    for k, s in sec.items():
        sym, src = "", ""
        for t, _ in s["votes"].most_common():
            sym = yahoo_symbol(t, s, stocks)
            if sym:
                src = "vote"
                break
        if not sym:
            sym = yahoo_symbol("", s, stocks)
            src = "isin" if sym else ""
        if not sym and s["cat"] in ("EC", "EP"):
            hits = yname.get(name_key(s["name"]), [])
            suf = SUFFIX.get(s["country"], "")
            local = [h for h in hits if suf and h.endswith(suf)] or [h for h in hits if "." not in h and s["isin"].startswith("US")]
            sym = (local or (hits if len(hits) == 1 else [""]))[0]
            src = "yahoo-name" if sym else ""
        if not sym and s["cat"] in ("EC", "EP") and (s["isin"].startswith("US") or s["country"] == "US"):
            sym = names.get(name_key(s["name"]), "")
            src = "name" if sym else ""
        s["sym"], s["src"] = sym, src
        how[src or "none"] += 1
    log(f"Symbols for {len(sec):,} distinct holdings: {dict(how)}")

    smap = series_map()
    etfs = universe()
    yh = json.loads(YAHOO_HOLD.read_text()) if YAHOO_HOLD.exists() else {}
    stats = {"etfs": len(etfs), "mapped": 0, "with_history": 0, "quarters_per_etf": Counter(), "first_period": Counter()}
    w_all = w_sym = w_known = 0.0
    e_all = e_sym = e_known = 0.0
    unresolved = Counter()
    for t in etfs:
        sid = smap.get(t)
        if not sid:
            continue
        stats["mapped"] += 1
        rs = [reports[sid][d] for d in sorted(reports.get(sid, {}))]
        if not rs:
            continue
        stats["with_history"] += 1
        stats["quarters_per_etf"][len(rs)] += 1
        stats["first_period"][rs[0]["asof"][:4]] += 1
        idx, table, hist = {}, [], []
        for r in rs:
            top = []
            for k, p in r["top"]:
                if k not in idx:
                    s = sec[k]
                    idx[k] = len(table)
                    table.append([s["sym"], s["name"], s["cusip"], s["isin"], s["cat"], s["country"]])
                top.append([idx[k], p])
            hist.append([r["asof"], r["net"], r["n"], r["cash"], top])
        last = rs[-1]
        for k, p in last["top"]:
            s = sec[k]
            w_all += p
            if s["sym"]:
                w_sym += p
                w_known += p if s["sym"] in stocks else 0
            if s["cat"] in ("EC", "EP"):
                e_all += p
                if s["sym"]:
                    e_sym += p
                    e_known += p if s["sym"] in stocks else 0
                else:
                    unresolved[f'{s["name"]} ({s["country"]})'] += p
        doc = {"t": t, "sid": sid, "name": last["name"],
               "cols": {"sec": ["sym", "name", "cusip", "isin", "cat", "country"], "hist": ["asof", "net", "n", "cash", "top"]},
               "sec": table, "hist": hist}
        (out / "etf" / f"{t}.json").write_text(json.dumps(doc, separators=(",", ":")))

    rep = {
        "quarter_files": [f.name for f in files],
        "universe": stats["etfs"], "mapped_to_series": stats["mapped"], "with_history": stats["with_history"],
        "reports_per_etf": dict(sorted(stats["quarters_per_etf"].items())),
        "first_report_year": dict(sorted(stats["first_period"].items())),
        "symbol_sources": dict(how),
        "latest_top50_weight_with_symbol": round(w_sym / max(w_all, 1e-9), 3),
        "latest_top50_weight_with_symbol_in_stocks_json": round(w_known / max(w_all, 1e-9), 3),
        "latest_top50_stock_weight_with_symbol": round(e_sym / max(e_all, 1e-9), 3),
        "latest_top50_stock_weight_with_symbol_in_stocks_json": round(e_known / max(e_all, 1e-9), 3),
        "top_unresolved_stocks_by_weight": [[n, round(w, 3)] for n, w in unresolved.most_common(40)],
    }
    cmp = {}
    for t in SAMPLES:
        fp = out / "etf" / f"{t}.json"
        if not fp.exists():
            cmp[t] = "no SEC report found"
            continue
        d = json.loads(fp.read_text())
        a0 = d["hist"][-1]
        cmp[t] = {"reports": len(d["hist"]), "first": d["hist"][0][0], "latest": a0[0], "holdings": a0[2], "cash": a0[3],
                  "sec_top10": [[d["sec"][i][0] or d["sec"][i][1], p] for i, p in a0[4][:10]],
                  "yahoo_top10": [[r[0] or r[1], r[2]] for r in (yh.get(t) or {}).get("top", [])[:10]]}
    rep["compare"] = cmp
    (out / "report.json").write_text(json.dumps(rep, indent=1))
    log(json.dumps({k: v for k, v in rep.items() if k != "compare"}, indent=1))
    for t, c in cmp.items():
        log(t, json.dumps(c)[:700])
    with tarfile.open(out / "nport-etfs.tar.gz", "w:gz") as tf:
        tf.add(out / "etf", arcname="etf")
    log(f"Wrote {stats['with_history']} ETF files; archive {(out / 'nport-etfs.tar.gz').stat().st_size / 1e6:.1f} MB")


def site_doc(d):
    """The trimmed copy kept in the repo for the nightly build: symbol (stocks only), name and country per holding,
    and for each report its date, cash share and top holdings as [holding index, share of the fund]."""
    keep, idx, sec = {}, {}, []
    for h in d["hist"]:
        for i, _ in h[4]:
            keep.setdefault(i, None)
    for i in keep:
        s = d["sec"][i]
        idx[i] = len(sec)
        # only stocks keep a symbol: the "tickers" some funds give bonds aren't real listings
        sec.append([s[0] if s[4] in ("EC", "EP") else "", s[1], s[5]])
    return {"t": d["t"], "asof": d["hist"][-1][0], "sec": sec,
            "hist": [[h[0], h[3], [[idx[i], p] for i, p in h[4]]] for h in d["hist"]]}


def cmd_site(a):
    """etf/<T>.json from combine -> data/nport/<T>.json (replacing what's there)."""
    inp, out = Path(a.inp), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):
        old.unlink()
    n = 0
    for fp in sorted(inp.glob("*.json")):
        (out / fp.name).write_text(json.dumps(site_doc(json.loads(fp.read_text())), separators=(",", ":")))
        n += 1
    log(f"Wrote {n} files to {out}")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("quarters")
    p.add_argument("--since", default="2019q4")
    p = sp.add_parser("quarter")
    p.add_argument("q", help="such as 2026q3, or latest")
    p.add_argument("--out", default="/tmp/nport/quarters")
    p.add_argument("--work", default="/tmp/nport/zips")
    p = sp.add_parser("combine")
    p.add_argument("--inp", default="/tmp/nport/quarters")
    p.add_argument("--out", default="/tmp/nport/out")
    p = sp.add_parser("site")
    p.add_argument("--inp", default="/tmp/nport/out/etf")
    p.add_argument("--out", default=str(ROOT / "data" / "nport"))
    a = ap.parse_args()
    {"quarters": cmd_quarters, "quarter": cmd_quarter, "combine": cmd_combine, "site": cmd_site}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
