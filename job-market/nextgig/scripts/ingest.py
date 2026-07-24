"""
SponsorMap ingestion v2

Changes from v1:
  * posted-date capture + --max-age (default 30 days)
  * degree-requirement parsing; --no-phd drops PhD-required roles
  * open taxonomy: broad match + explicit exclude + review queue for new titles
  * work-setup detection rewritten (hybrid no longer loses to remote)
  * country configurable; sponsorship source swaps per country

Run, from job-market/nextgig/:
  python scripts/ingest.py --lca data/lca_*.csv --max-age 30 --no-phd
  python scripts/ingest.py --review  # show unmatched titles, most frequent first
"""

import argparse, json, re, time, unicodedata, os, sys
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
import requests

# Anchored to this project's directory rather than the working directory, so the
# script behaves the same run from here, from scripts/, or from the repo root in
# CI. Note this is job-market/nextgig/, not the repository root.
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COUNTRY = "us"          # "us" | "gb" | "ca" | "de" | "nl"

TARGETS = [
    ("greenhouse", "databricks"), ("greenhouse", "snowflake"),
    ("greenhouse", "stripe"),     ("greenhouse", "datadog"),
    ("greenhouse", "cloudflare"), ("greenhouse", "wayfair"),
    ("greenhouse", "klaviyo"),    ("greenhouse", "duolingo"),
    ("greenhouse", "chime"),      ("greenhouse", "zillow"),
    ("lever", "sigmacomputing"),  ("lever", "palantir"),
    ("lever", "spotify"),         ("ashby", "ramp"),
]

# ============================================================ TAXONOMY
# Broad: a title qualifies if it has a DOMAIN word AND a ROLE word, or hits
# STANDALONE. New titles get caught by composition, not by enumeration.

DOMAIN = re.compile(
    r"\b(data|analytics|analytic|ml|machine learning|ai|artificial intelligence|"
    r"llm|genai|gen ai|deep learning|forward.?deployed|applied|decision|"
    r"business intelligence|bi|warehouse|lakehouse|streaming|pipeline)\b", re.I)

ROLE = re.compile(
    r"\b(engineer|engineering|architect|developer|scientist|manager|lead|"
    r"director|head|principal|specialist|analyst)\b", re.I)

STANDALONE = re.compile(
    r"\b(fde|forward deployed|solutions? architect|platform engineer|"
    r"infrastructure engineer|dbt|airflow|databricks|snowflake)\b", re.I)

EXCLUDE = re.compile(
    r"\b(intern|internship|apprentice|co.?op|new grad|graduate program|"
    r"sales|account executive|recruit|talent acquisition|marketing|"
    r"customer success|support engineer|qa|test engineer|"
    r"nurse|clinical|attorney|paralegal|driver|technician|warehouse associate|"
    r"security engineer|network engineer|site reliability|devops)\b", re.I)

review_queue = Counter()

LEVEL_RULES = [
    (r"\b(vp|vice president|head of|director)\b", "Director"),
    (r"\b(manager|engineering manager)\b",        "Manager"),
    (r"\b(lead|principal)\b",                     "Lead"),
    (r"\bstaff\b",                                "Staff"),
    (r"\b(senior|sr\.?|iii|\bii\b)\b",            "Senior"),
]

def level_of(t):
    for pat, lvl in LEVEL_RULES:
        if re.search(pat, t, re.I):
            return lvl
    return "Mid"


def title_matches(title):
    """True = keep. False = drop. Near-misses go to the review queue."""
    if EXCLUDE.search(title):
        return False
    if STANDALONE.search(title):
        return True
    d, r = DOMAIN.search(title), ROLE.search(title)
    if d and r:
        return True
    if d or r:
        review_queue[title.strip()] += 1
    return False


# ============================================================ DEGREE
PHD_REQUIRED = re.compile(
    r"(ph\.?\s?d|doctorate|doctoral)[^.\n]{0,80}?\b(required|must have|is required)\b"
    r"|\b(required|must have|minimum)\b[^.\n]{0,80}?(ph\.?\s?d|doctorate)", re.I)

PHD_OPTIONAL = re.compile(
    r"(ph\.?\s?d|doctorate)[^.\n]{0,60}?\b(preferred|a plus|nice to have|or equivalent|bonus)\b"
    r"|\b(ms|master|bs|bachelor)[^.\n]{0,40}?(or|/)[^.\n]{0,20}(ph\.?\s?d)", re.I)


def degree_floor(body):
    """'phd' | 'masters' | 'bachelors' | 'none'"""
    if PHD_REQUIRED.search(body) and not PHD_OPTIONAL.search(body):
        return "phd"
    if re.search(r"\b(master'?s?|m\.?s\.?|m\.?eng)\b[^.\n]{0,40}\b(required|must)\b", body, re.I):
        return "masters"
    if re.search(r"\b(bachelor'?s?|b\.?s\.?)\b", body, re.I):
        return "bachelors"
    return "none"


# ============================================================ WORK SETUP
HYBRID = re.compile(
    r"\bhybrid\b|\b\d\s?(days?|x)\s?(per week|a week|/week|weekly)?\s?(in.?office|onsite|on.?site)\b"
    r"|\bin.?office\s?\d\s?days?\b|\bpartially remote\b", re.I)
NOT_REMOTE = re.compile(r"\bnot (a )?(fully )?remote\b|\bno remote\b", re.I)
REMOTE = re.compile(r"\b(fully remote|100% remote|remote.?first|work from anywhere|remote)\b", re.I)
ONSITE = re.compile(r"\b(on.?site|in.?person|in.?office)\b", re.I)


def work_type(location, body):
    """Location string beats body text. Hybrid and explicit negation checked first."""
    loc = location or ""
    head = (body or "")[:3000]      # setup is stated early; boilerplate lives at the bottom
    for src in (loc, head):
        if HYBRID.search(src):
            return "Hybrid"
    if NOT_REMOTE.search(loc + " " + head):
        return "Onsite"
    if REMOTE.search(loc):
        return "Remote"
    if ONSITE.search(loc):
        return "Onsite"
    if REMOTE.search(head):
        return "Remote"
    return "Onsite"


SALARY = re.compile(
    r"\$\s?(\d{2,3})[,.]?(\d{3})?\s?(?:k\b)?\s?(?:-|\u2013|to)\s?\$?\s?(\d{2,3})[,.]?(\d{3})?\s?(?:k\b)?")

def parse_salary(text):
    m = SALARY.search(text or "")
    if not m:
        return None, None
    def norm(a, b):
        v = int(a + (b or ""))
        return round(v / 1000) if v > 1000 else v
    lo, hi = norm(m.group(1), m.group(2)), norm(m.group(3), m.group(4))
    return (lo, hi) if 50 <= lo <= hi <= 900 else (None, None)


def parse_date(v):
    """ATS APIs disagree on format; normalize to aware UTC or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):                  # Lever / Ashby epoch millis
        return datetime.fromtimestamp(v / 1000, timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# ============================================================ FETCHERS
def fetch_greenhouse(slug):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", timeout=25)
    for j in r.json().get("jobs", []):
        yield {"co": slug, "title": j["title"], "loc": j.get("location", {}).get("name", ""),
               "body": j.get("content", ""), "url": j["absolute_url"], "src": "Greenhouse",
               "posted": parse_date(j.get("first_published") or j.get("updated_at"))}

def fetch_lever(slug):
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=25)
    for j in r.json():
        yield {"co": slug, "title": j["text"], "loc": j.get("categories", {}).get("location", ""),
               "body": j.get("descriptionPlain", ""), "url": j["hostedUrl"], "src": "Lever",
               "posted": parse_date(j.get("createdAt"))}

def fetch_ashby(slug):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", timeout=25)
    for j in r.json().get("jobs", []):
        yield {"co": slug, "title": j["title"], "loc": j.get("location", ""),
               "body": j.get("descriptionPlain", ""), "url": j["jobUrl"], "src": "Ashby",
               "posted": parse_date(j.get("publishedAt"))}

FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


# ============================================================ GEOCODE
# Offline lookup. Nominatim fuzzy-matched "Remote - USA" onto small towns and
# put pins in places nobody is hiring, so we parse the string ourselves and
# resolve against a bundled city table instead.

GEO = os.path.join(PROJECT, "data", "usgeo.json")
_geo = json.load(open(GEO)) if os.path.exists(GEO) else {"cities": {}, "states": {}}

STATE_ABBR = {
 "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA","colorado":"CO",
 "connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA","hawaii":"HI","idaho":"ID",
 "illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS","kentucky":"KY","louisiana":"LA",
 "maine":"ME","maryland":"MD","massachusetts":"MA","michigan":"MI","minnesota":"MN",
 "mississippi":"MS","missouri":"MO","montana":"MT","nebraska":"NE","nevada":"NV",
 "new hampshire":"NH","new jersey":"NJ","new mexico":"NM","new york":"NY",
 "north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK","oregon":"OR",
 "pennsylvania":"PA","rhode island":"RI","south carolina":"SC","south dakota":"SD",
 "tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT","virginia":"VA",
 "washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY",
 "district of columbia":"DC","washington dc":"DC","washington d.c.":"DC",
}
VALID_ST = set(STATE_ABBR.values())

# Names that are both a city and a state, plus common shorthand. Without these,
# a bare "New York" resolves to the state and the city is lost.
ALIAS = {
 "new york": ("New York", "NY"), "nyc": ("New York", "NY"),
 "new york city": ("New York", "NY"), "manhattan": ("New York", "NY"),
 "washington": ("Washington", "DC"), "washington dc": ("Washington", "DC"),
 "washington d.c.": ("Washington", "DC"), "dc": ("Washington", "DC"),
 "sf": ("San Francisco", "CA"), "san francisco bay area": ("San Francisco", "CA"),
 "bay area": ("San Francisco", "CA"), "sf bay area": ("San Francisco", "CA"),
 "la": ("Los Angeles", "CA"), "nj": ("Newark", "NJ"),
 "greater boston": ("Boston", "MA"), "greater seattle area": ("Seattle", "WA"),
}

# Multi-location postings list several sites. Take the first, ignore the rest.
SPLIT = re.compile(r"\s*(?:;|\||/|\bor\b|\band\b)\s*", re.I)
STRIP = re.compile(r"^\s*(remote|hybrid|onsite|on-site|in office)\s*[-,:]?\s*", re.I)


def geocode(place):
    """Parse 'City, ST' out of a messy ATS location string. None if unresolvable."""
    if not place:
        return None
    raw = SPLIT.split(place.strip())[0]
    raw = STRIP.sub("", raw).strip(" ,-")
    if not raw or re.fullmatch(r"(usa?|united states|remote|anywhere|multiple locations)", raw, re.I):
        return None

    if raw.lower() in ALIAS:
        city, state = ALIAS[raw.lower()]
        lat, lng = _geo["cities"].get(f"{city.lower()}|{state}", _geo["states"].get(state, (0, 0)))
        return {"lat": lat, "lng": lng, "city": city, "st": state, "zip": ""}

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    city = state = ""

    for p in reversed(parts):                       # state is usually last
        u = p.upper()
        if u in VALID_ST:
            state = u; break
        if p.lower() in STATE_ABBR:
            state = STATE_ABBR[p.lower()]; break
    if parts:
        cand = parts[0]
        if cand.lower() in ALIAS:
            city, state = ALIAS[cand.lower()]
        elif cand.upper() not in VALID_ST and cand.lower() not in STATE_ABBR:
            city = cand

    if not state:
        return None                                 # no state, no pin

    key = f"{city.lower()}|{state}"
    if city and key in _geo["cities"]:
        lat, lng = _geo["cities"][key]
        return {"lat": lat, "lng": lng, "city": city.title(), "st": state, "zip": ""}
    if state in _geo["states"]:                     # known state, unknown town
        lat, lng = _geo["states"][state]
        return {"lat": lat, "lng": lng, "city": city.title(), "st": state, "zip": ""}
    return None


# ============================================================ SPONSORSHIP
def norm_employer(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(inc|llc|corp|corporation|co|ltd|lp|holdings|usa|technologies|technology|labs)\b", " ", s)
    return re.sub(r"\s+", "", s)


SPONSORS = os.path.join(PROJECT, "data", "sponsors.json")


def build_sponsors(paths, out=SPONSORS):
    """
    Collapse the huge quarterly LCA files into one small {employer: filings} map.
    Run this locally whenever new DOL data drops, then commit the result.
    Raw LCA CSVs stay out of the repo; this file is a few hundred KB.
    """
    counts = load_sponsors(paths)
    counts = {k: v for k, v in counts.items() if v >= 2}   # drop one-off filings
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    json.dump(counts, open(out, "w"))
    print(f"wrote {len(counts):,} employers -> {out}")
    return counts


def read_sponsors(path=SPONSORS):
    """Load the precomputed map. Missing file is fine, sponsorship just shows 0."""
    if os.path.exists(path):
        d = json.load(open(path))
        print(f"sponsorship data: {len(d):,} employers")
        return d
    print("no data/sponsors.json, sponsorship will show as 0 "
          "(run --build-sponsors once to create it)")
    return {}


def load_sponsors(paths, country=COUNTRY):
    """us -> DOL LCA filing counts. others -> licensed-sponsor registry presence."""
    import csv
    counts = defaultdict(int)
    for p in paths:
        with open(p, newline="", encoding="utf-8", errors="ignore") as f:
            for row in csv.DictReader(f):
                if country == "us":
                    if (row.get("CASE_STATUS") or "").strip().lower().startswith("certified"):
                        counts[norm_employer(row.get("EMPLOYER_NAME", ""))] += 1
                else:
                    name = row.get("Organisation Name") or row.get("Employer") or ""
                    if name:
                        counts[norm_employer(name)] = max(counts[norm_employer(name)], 1)
    return counts



# ============================================================ WATCHLIST
# "Worth watching" = employers with a real sponsorship record and a demonstrated
# habit of hiring for your titles, who have nothing open right now. This is
# derived from posting history, so it maintains itself. Curated lists rot.

HISTORY = os.path.join(PROJECT, "data", "history.json")


def update_history(rows):
    """Append today's sighting per employer. Run every ingest, commit the file."""
    hist = json.load(open(HISTORY)) if os.path.exists(HISTORY) else {}
    today = datetime.now(timezone.utc).date().isoformat()
    for r in rows:
        h = hist.setdefault(r["co"], {"seen": [], "titles": [], "lca": 0,
                                      "city": "", "st": ""})
        if today not in h["seen"]:
            h["seen"].append(today)
        if r["posted"] not in h["titles"]:
            h["titles"].append(r["posted"])       # distinct posting dates
        h["lca"] = max(h["lca"], r["lca"])
        h["city"], h["st"] = r["city"], r["st"]
    os.makedirs(os.path.dirname(HISTORY) or ".", exist_ok=True)
    json.dump(hist, open(HISTORY, "w"), indent=1)
    return hist


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return None if not n else xs[n // 2] if n % 2 else (xs[n//2 - 1] + xs[n//2]) / 2


def cadence_days(posting_dates):
    """Median gap between distinct posting dates. Needs 3+ to mean anything."""
    ds = sorted(datetime.fromisoformat(d).date() for d in posting_dates)
    if len(ds) < 3:
        return None
    gaps = [(b - a).days for a, b in zip(ds, ds[1:]) if (b - a).days > 0]
    return median(gaps)


def build_watchlist(rows, hist, adjacency=None, min_lca=10):
    """
    Emit employers worth a warm approach even with nothing open.
    adjacency: {employer_slug: "why this is close to your domain"} — the one
    genuinely hand-written input, and the one worth writing by hand.
    """
    adjacency = adjacency or {}
    open_now = {r["co"] for r in rows}
    today = datetime.now(timezone.utc).date()
    out = []

    for co, h in hist.items():
        if co in open_now:                      # it's on the map already
            continue
        if h["lca"] < min_lca:                  # no meaningful sponsorship record
            continue
        if not h["titles"]:
            continue

        last = max(datetime.fromisoformat(d).date() for d in h["titles"])
        quiet = (today - last).days
        cad = cadence_days(h["titles"])

        # Rank: employers whose quiet period is approaching their normal cadence
        # are the ones about to post. That is the moment to be in the inbox.
        if cad:
            due = quiet / cad                   # 1.0 == due now
            tag = "cadence"
        else:
            due = 0.4
            tag = "signal"
        if co in adjacency:
            tag = "adjacent"
            due += 0.3                          # domain fit beats timing

        out.append({
            "co": co, "city": h["city"], "st": h["st"], "lca": h["lca"],
            "why": adjacency.get(co, f'Posted {len(h["titles"])} matching roles historically'),
            "cad": f"~{round(cad/7)} wk cadence" if cad else "irregular",
            "quiet": quiet, "due": round(due, 2), "tag": tag,
        })

    out.sort(key=lambda w: -w["due"])
    return out


# Hand-written: the only part that should be manual. Keep it short and specific
# to why the domain overlaps what you already do.
ADJACENCY = {
    "Samsara":       "Industrial IoT telemetry — closest adjacency to your current domain",
    "Uptake":        "Predictive maintenance for industrial fleets — direct domain match",
    "Cognite":       "Industrial DataOps — near-identical problem space",
    "ServiceTitan":  "Field service management SaaS — your current industry, productized",
    "Augury":        "Machine health monitoring; building out analytics",
    "Xylem":         "Water infrastructure telemetry; industrial repair adjacency",
}


# ============================================================ MAIN
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(PROJECT, "public", "jobs.json"))
    ap.add_argument("--lca", nargs="*", default=[])
    ap.add_argument("--max-age", type=int, default=30)
    ap.add_argument("--no-phd", action="store_true")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--build-sponsors", nargs="*", metavar="CSV",
                    help="collapse LCA files into data/sponsors.json, then exit")
    args = ap.parse_args()

    if args.build_sponsors is not None:
        build_sponsors(args.build_sponsors or args.lca)
        return

    sponsors = load_sponsors(args.lca) if args.lca else read_sponsors()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.max_age)
    rows, seen, dropped = [], set(), Counter()

    for kind, slug in TARGETS:
        try:
            postings = list(FETCHERS[kind](slug))
        except Exception as e:
            print(f"  ! {slug}: {e}", file=sys.stderr)
            continue

        kept = 0
        for p in postings:
            if not title_matches(p["title"]):
                dropped["title"] += 1; continue
            if p["posted"] is None:
                dropped["no_date"] += 1; continue      # unknown age == unusable
            if p["posted"] < cutoff:
                dropped["stale"] += 1; continue

            deg = degree_floor(p["body"])
            if args.no_phd and deg == "phd":
                dropped["phd"] += 1; continue
            if p["url"] in seen:
                continue
            seen.add(p["url"])

            g = geocode(p["loc"])
            if not g:
                dropped["geo"] += 1; continue
            lo, hi = parse_salary(p["body"])

            rows.append({
                "co": slug.replace("-", " ").title(), "title": p["title"],
                "city": g["city"], "st": g["st"], "zip": g["zip"],
                "lat": g["lat"], "lng": g["lng"],
                "lvl": level_of(p["title"]),
                "type": work_type(p["loc"], p["body"]),
                "min": lo, "max": hi,
                "lca": sponsors.get(norm_employer(slug), 0),
                "deg": deg,
                "age": (datetime.now(timezone.utc) - p["posted"]).days,
                "posted": p["posted"].date().isoformat(),
                "src": p["src"], "url": p["url"],
            })
            kept += 1
        print(f"  {slug:<18} {kept:>3} kept / {len(postings)} total")


    if args.review:
        print("\nUnmatched titles worth adding to the taxonomy:")
        for t, n in review_queue.most_common(40):
            print(f"  {n:>3}  {t}")
        return

    print(f"\ndropped: {dict(dropped)}")
    print(f"review queue: {len(review_queue)} unmatched titles (run --review)")
    if os.path.exists(args.out):
        try:
            prev = json.load(open(args.out))
            if len(prev) > 20 and len(rows) < 0.5 * len(prev):
                print(f"ABORT: {len(rows)} rows vs {len(prev)} yesterday. "
                      f"Looks like a broken fetch, refusing to overwrite.", file=sys.stderr)
                sys.exit(1)
        except (ValueError, OSError):
            pass

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump(rows, open(args.out, "w"), indent=1)
    print(f"wrote {len(rows)} roles -> {args.out}")

    meta = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "roles": len(rows),
        "employers": len({r["co"] for r in rows}),
        "sponsors_loaded": len(sponsors),
    }
    json.dump(meta, open(os.path.join(os.path.dirname(args.out) or ".", "meta.json"), "w"), indent=1)
    print(f'stamped {meta["updated"]}')

    hist = update_history(rows)
    watch = build_watchlist(rows, hist, ADJACENCY)
    wpath = os.path.join(os.path.dirname(args.out) or ".", "watchlist.json")
    json.dump(watch, open(wpath, "w"), indent=1)
    print(f"wrote {len(watch)} watchlist employers -> {wpath}")

    # Emerging titles: postings that name a data/AI domain OR a senior role word
    # but not the clean pairing the taxonomy keeps, so they never make the map.
    # Surfaced as a list precisely because they are the vocabulary the market is
    # using that the filter does not yet recognise. Ranked by how often they
    # appear; the count is the whole point.
    emerging = [{"title": t, "n": n} for t, n in review_queue.most_common(120)]
    epath = os.path.join(os.path.dirname(args.out) or ".", "emerging.json")
    json.dump(emerging, open(epath, "w"), indent=1)
    print(f"wrote {len(emerging)} emerging titles -> {epath}")


if __name__ == "__main__":
    main()
