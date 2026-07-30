# SPDX-License-Identifier: AGPL-3.0-only
# Copyright © 2026 Aishwarya Agarwal. All rights reserved.
"""
SponsorMap discovery — find employers you have never heard of.

ingest.py answers "is this company hiring?" for a list you already wrote.
discover.py builds that list for you, from the sponsorship census itself.

Source: DOL LCA disclosure data (every H-1B/E-3 filing, quarterly XLSX)
        https://www.dol.gov/agencies/eta/foreign-labor/performance

Typical use:
  # every sponsoring employer running data roles in Michigan
  python scripts/discover.py --lca data/lca_*.csv --state MI

  # employers in the same industry as your current one
  python scripts/discover.py --lca data/lca_*.csv --naics 811310 333996 423830

  # anywhere you can work East-coast hours, obscure names only
  python scripts/discover.py --lca data/lca_*.csv --tz EST CST --obscure

  # the actual point: strong sponsors nobody talks about
  python scripts/discover.py --lca data/lca_*.csv --obscure --min-filings 5

  # then find their job boards
  python scripts/discover.py --lca data/lca_*.csv --state MI --probe-ats
"""

import argparse, csv, json, os, re, sys, time, unicodedata
from collections import defaultdict
import requests

# Anchored to this project's directory so output lands in the same place
# regardless of the working directory it was launched from.
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- SOC codes
# The occupational codes that map to data / analytics / AI work. LCA rows carry
# SOC_CODE, which is far more reliable than free-text job titles.
SOC = {
    "15-2051": "Data Scientists",
    "15-2041": "Statisticians",
    "15-1243": "Database Architects",
    "15-1242": "Database Administrators",
    "15-1211": "Computer Systems Analysts",
    "15-1221": "Computer & Information Research Scientists",
    "15-1252": "Software Developers",
    "15-1253": "Software QA & Testers",
    "11-3021": "Computer & Information Systems Managers",
    "13-1111": "Management Analysts",
    "15-2031": "Operations Research Analysts",
}
# Narrow set — use with --strict when 15-1252 floods results with generic SWE.
SOC_CORE = {"15-2051", "15-2041", "15-1243", "15-1211", "11-3021", "15-2031"}

# Codes a mid-market manufacturer files data work under. Mytee's analytics hire
# went in as 15-1299 "Computer Occupations, All Other"; AH Group's engineers as
# 17-2112. A tech company would have called both a Data Analyst. These codes are
# far too broad to trust on their own, so every one of them is gated on
# TITLE_HINT below — exactly the treatment 15-1252 already gets.
SOC_BROAD = {
    "15-1299": "Computer Occupations, All Other",
    "17-2112": "Industrial Engineers",
    "13-1081": "Logisticians",
    "15-1232": "Computer User Support Specialists",
    "13-1161": "Market Research Analysts",
    "43-9111": "Statistical Assistants",
}

# SOC codes broad enough that the job title, not the code, decides.
# 13-1111 "Management Analysts" is the worst offender after 15-1252: every
# consultancy files under it, which is how "Chief Of Staff", "Principal
# Consultant" and "VP, Plant Operational Technology" reached a data-roles list.
TITLE_GATED = {"15-1252", "13-1111", "11-3021", "15-1253"} | set(SOC_BROAD)

TITLE_HINT = re.compile(
    r"\b(data|analytic|analyst|bi\b|business intelligence|warehouse|etl|"
    r"pipeline|machine learning|ml\b|ai\b|scientist|informatics)\b", re.I)

# ---------------------------------------------------------------- time zones
# Shortcut for the real filter: "can I work their hours?" Assigned by the state's
# predominant zone, which is the only granularity LCA worksite data supports.
# Split states (FL, IN, KY, TN, TX, KS, NE, ND, SD) are filed under the zone
# holding the bulk of their population, so a Pensacola or Amarillo worksite is
# labelled by its state, not its longitude. Mountain and Pacific are absent on
# purpose — that is the whole point of the flag.
TZ_STATES = {
    "EST": {"CT", "DC", "DE", "FL", "GA", "IN", "KY", "MA", "MD", "ME", "MI",
            "NC", "NH", "NJ", "NY", "OH", "PA", "RI", "SC", "VA", "VT", "WV"},
    "CST": {"AL", "AR", "IA", "IL", "KS", "LA", "MN", "MO", "MS", "ND", "NE",
            "OK", "SD", "TN", "TX", "WI"},
}
TZ_ALIAS = {"ET": "EST", "EASTERN": "EST", "EDT": "EST",
            "CT": "CST", "CENTRAL": "CST", "CDT": "CST"}


def tz_states(names):
    """Expand ['EST','CT'] into the set of state abbreviations they cover."""
    out = set()
    for n in names:
        k = TZ_ALIAS.get(n.upper(), n.upper())
        if k not in TZ_STATES:
            sys.exit(f"unknown zone {n!r} — use EST/CST (Mountain and Pacific are out of scope)")
        out |= TZ_STATES[k]
    return out

# Household names — the whole point is to filter these OUT in --obscure mode.
# Not exhaustive by design; extend as you notice names you already knew.
WELL_KNOWN = {
    "amazon", "google", "microsoft", "meta", "apple", "netflix", "tesla",
    "nvidia", "intel", "ibm", "oracle", "salesforce", "adobe", "cisco",
    "uber", "lyft", "airbnb", "stripe", "databricks", "snowflake", "palantir",
    "linkedin", "twitter", "x", "openai", "anthropic", "deloitte", "accenture",
    "infosys", "tcs", "tataconsultancyservices", "cognizant", "wipro", "hcl",
    "capgemini", "ey", "ernstyoung", "pwc", "pricewaterhousecoopers", "kpmg",
    "mckinsey", "jpmorgan", "jpmorganchase", "goldmansachs", "morganstanley",
    "bankofamerica", "wellsfargo", "citigroup", "capitalone", "walmart",
    "target", "costco", "unitedhealth", "optum", "cvs", "johnsonjohnson",
    "pfizer", "boeing", "lockheedmartin", "generalmotors", "ford", "qualcomm",
    "broadcom", "amd", "dell", "hp", "hpe", "vmware", "sap", "servicenow",
    "workday", "intuit", "paypal", "block", "square", "shopify", "spotify",
    "datadog", "cloudflare", "mongodb", "elastic", "splunk", "twilio",
}


# Exact-match against WELL_KNOWN fails on real LCA legal names: Amazon files as
# "Amazon Development Center U.S., Inc." -> "amazondevelopmentcenteru", which is
# never equal to "amazon". Match by containment instead, but only for entries
# long enough to be unambiguous — "x", "ey" and "hp" appear inside innocent
# words and would blacklist half the census.
_KNOWN_LONG = None
_KNOWN_SHORT = None


def well_known(k):
    global _KNOWN_LONG, _KNOWN_SHORT
    if _KNOWN_LONG is None:
        _KNOWN_LONG = {w for w in WELL_KNOWN if len(w) > 4}
        _KNOWN_SHORT = {w for w in WELL_KNOWN if len(w) <= 4}
    return k in _KNOWN_SHORT or any(w in k for w in _KNOWN_LONG)


def norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(inc|llc|corp|corporation|company|co|ltd|limited|lp|llp|plc|"
               r"holdings|group|usa|us|america|american|technologies|technology|"
               r"solutions|services|systems|labs|international)\b", " ", s)
    return re.sub(r"\s+", "", s)


def money(v):
    try:
        x = float(str(v).replace(",", "").replace("$", ""))
        return x if 20000 < x < 900000 else None      # drop hourly/garbage rows
    except (TypeError, ValueError):
        return None


def med(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    return None if not n else xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


# ---------------------------------------------------------------- load
def load(paths, socs, strict=False, title_filter=True):
    """Aggregate LCA rows into per-employer records."""
    emp = {}
    keep_soc = SOC_CORE if strict else socs
    seen_rows = 0

    for p in paths:
        with open(p, newline="", encoding="utf-8", errors="ignore") as f:
            for row in csv.DictReader(f):
                seen_rows += 1
                if not (row.get("CASE_STATUS") or "").strip().lower().startswith("certified"):
                    continue
                soc = (row.get("SOC_CODE") or "").strip()[:7]
                if soc not in keep_soc:
                    continue
                title = row.get("JOB_TITLE") or ""
                if title_filter and soc in TITLE_GATED and not TITLE_HINT.search(title):
                    continue      # broad code with no data signal in the title

                name = (row.get("EMPLOYER_NAME") or "").strip()
                if not name:
                    continue
                k = norm(name)
                e = emp.setdefault(k, {
                    "name": name, "filings": 0, "titles": defaultdict(int),
                    "socs": defaultdict(int), "sites": defaultdict(int),
                    "wages": [], "naics": defaultdict(int),
                    "city": row.get("EMPLOYER_CITY", ""), "st": row.get("EMPLOYER_STATE", ""),
                })
                e["filings"] += 1
                e["titles"][title.strip().title()] += 1
                e["socs"][soc] += 1
                ws = f'{row.get("WORKSITE_CITY","").title()}, {row.get("WORKSITE_STATE","")}'
                e["sites"][ws.strip(", ")] += 1
                e["wages"].append(money(row.get("WAGE_RATE_OF_PAY_FROM")
                                        or row.get("PREVAILING_WAGE")))
                nc = (row.get("NAICS_CODE") or "").strip()[:6]
                if nc:
                    e["naics"][nc] += 1

    print(f"read {seen_rows:,} rows -> {len(emp):,} employers with data-role filings",
          file=sys.stderr)
    return emp


# ---------------------------------------------------------------- ATS probing
ATS_PATTERNS = [
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{s}/jobs"),
    ("lever",      "https://api.lever.co/v0/postings/{s}?mode=json&limit=1"),
    ("ashby",      "https://api.ashbyhq.com/posting-api/job-board/{s}"),
    ("workable",   "https://apply.workable.com/api/v1/widget/accounts/{s}"),
    ("recruitee",  "https://{s}.recruitee.com/api/offers"),
]


def slug_candidates(name):
    """Guess ATS slugs from an employer name. Cheap, and surprisingly effective."""
    base = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    base = re.sub(r"\b(inc|llc|corp|corporation|company|co|ltd|the)\b", " ", base)
    words = [w for w in base.split() if w]
    if not words:
        return []
    cands = ["".join(words), "-".join(words), words[0]]
    if len(words) > 1:
        cands.append("".join(w[0] for w in words))       # acronym: AH Group -> ahgroup/ah
        cands.append("".join(words[:2]))
    seen, out = set(), []
    for c in cands:
        if c and c not in seen and len(c) > 1:
            seen.add(c); out.append(c)
    return out[:5]


def probe_ats(name, pause=0.25):
    """Return (ats, slug) if a public job board is found, else None."""
    for slug in slug_candidates(name):
        for ats, tmpl in ATS_PATTERNS:
            try:
                r = requests.get(tmpl.format(s=slug), timeout=8,
                                 headers={"User-Agent": "sponsormap/2.0"})
                time.sleep(pause)
                if r.status_code == 200 and len(r.content) > 60:
                    return ats, slug
            except requests.RequestException:
                continue
    return None


# ---------------------------------------------------------------- scoring
def score(e, obscure_bonus=True):
    """
    Rank employers by how worth-your-time they are, not by size.
    Deliberately rewards small consistent sponsors over giant ones — a firm
    with 8 data filings sponsors on purpose; one with 4,000 runs a visa factory
    where you are a number.
    """
    f = e["filings"]
    s = min(f, 40) / 40 * 40                       # filings, saturating at 40
    s += min(len(e["titles"]), 10) / 10 * 15       # title variety = real team
    w = med(e["wages"])
    if w:
        s += min(max(w - 90000, 0) / 110000, 1) * 25   # wage level
    if obscure_bonus and not well_known(norm(e["name"])):
        s += 20
    if f > 500:                                     # mega-filer penalty
        s -= 15
    return round(s, 1)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lca", nargs="+", required=True)
    ap.add_argument("--state", nargs="*", help="filter by worksite or employer state, e.g. MI")
    ap.add_argument("--tz", nargs="*", help="filter by time zone: EST, CST. Unions with --state")
    ap.add_argument("--naics", nargs="*", help="6-digit NAICS codes to match")
    ap.add_argument("--min-filings", type=int, default=3)
    ap.add_argument("--max-filings", type=int, default=100000,
                    help="cap to exclude consultancies/mega-filers")
    ap.add_argument("--obscure", action="store_true", help="drop household names")
    ap.add_argument("--hq", action="store_true",
                    help="match --state/--tz against the employer HQ only, not every worksite")
    ap.add_argument("--strict", action="store_true", help="core data SOC codes only")
    ap.add_argument("--probe-ats", action="store_true", help="look for a public job board")
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--out", default=os.path.join(PROJECT, "data", "discovered.json"))
    args = ap.parse_args()

    emp = load(args.lca, set(SOC) | set(SOC_BROAD), strict=args.strict)
    states = {s.upper() for s in (args.state or [])} | tz_states(args.tz or [])
    if args.tz:
        print(f'zones {",".join(args.tz)} -> {len(states)} states', file=sys.stderr)
    naics = set(args.naics or [])
    rows = []

    for k, e in emp.items():
        if not (args.min_filings <= e["filings"] <= args.max_filings):
            continue
        if args.obscure and well_known(k):
            continue
        if states:
            # --hq asks "is this company based here?"; the default asks the looser
            # "does it employ anyone here?", which lets a California giant through
            # on the strength of one Chicago worksite.
            here = ({e["st"].upper()} if args.hq else
                    {s.split(", ")[-1] for s in e["sites"] if ", " in s} | {e["st"].upper()})
            if not (here & states):
                continue
        if naics and not (set(e["naics"]) & naics):
            continue

        rows.append({
            "name": e["name"],
            "hq": f'{e["city"].title()}, {e["st"]}',
            "filings": e["filings"],
            "top_titles": [t for t, _ in sorted(e["titles"].items(), key=lambda x: -x[1])[:3]],
            "top_sites": [s for s, _ in sorted(e["sites"].items(), key=lambda x: -x[1])[:3]],
            "median_wage": int(med(e["wages"])) if med(e["wages"]) else None,
            "naics": max(e["naics"], key=e["naics"].get) if e["naics"] else None,
            "score": score(e, args.obscure),
        })

    rows.sort(key=lambda r: -r["score"])
    rows = rows[:args.top]

    if args.probe_ats:
        print("probing job boards...", file=sys.stderr)
        for r in rows:
            hit = probe_ats(r["name"])
            r["ats"], r["slug"] = hit if hit else (None, None)
            print(f'  {"OK " if hit else "-- "} {r["name"][:44]:<46} {hit or ""}', file=sys.stderr)

    json.dump(rows, open(args.out, "w"), indent=1)

    print(f'\n{"EMPLOYER":<40} {"HQ":<20} {"FIL":>4} {"WAGE":>8}  TOP TITLE')
    print("-" * 108)
    for r in rows[:args.top]:
        w = f'${r["median_wage"]//1000}k' if r["median_wage"] else "--"
        t = (r["top_titles"] or [""])[0][:30]
        ats = f'  [{r.get("ats")}:{r.get("slug")}]' if r.get("ats") else ""
        print(f'{r["name"][:39]:<40} {r["hq"][:19]:<20} {r["filings"]:>4} {w:>8}  {t}{ats}')
    print(f"\nwrote {len(rows)} employers -> {args.out}")
    print("Feed the ones with an ATS hit straight into ingest.py TARGETS.")


if __name__ == "__main__":
    main()
