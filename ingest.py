"""
SponsorMap ingestion v2

Changes from v1:
  * posted-date capture + --max-age (default 30 days)
  * degree-requirement parsing; --no-phd drops PhD-required roles
  * open taxonomy: broad match + explicit exclude + review queue for new titles
  * work-setup detection rewritten (hybrid no longer loses to remote)
  * country configurable; sponsorship source swaps per country

Run:
  python ingest.py --out public/jobs.json --lca data/lca_*.csv --max-age 30 --no-phd
  python ingest.py --review          # show unmatched titles, most frequent first
"""

import argparse, json, re, time, unicodedata, os, sys
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
import requests

COUNTRY = "us"          # "us" | "gb" | "ca" | "de" | "nl"
CACHE = ".geocache.json"

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
_cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}

def geocode(place):
    if not place:
        return None
    key = place.lower().strip()
    if key in _cache:
        return _cache[key]
    r = requests.get("https://nominatim.openstreetmap.org/search",
                     params={"q": place, "countrycodes": COUNTRY, "format": "json",
                             "limit": 1, "addressdetails": 1},
                     headers={"User-Agent": "sponsormap/2.0"}, timeout=25)
    time.sleep(1.1)
    js = r.json()
    if not js:
        _cache[key] = None
    else:
        a = js[0].get("address", {})
        _cache[key] = {"lat": float(js[0]["lat"]), "lng": float(js[0]["lon"]),
                       "city": a.get("city") or a.get("town") or a.get("village") or "",
                       "st": a.get("ISO3166-2-lvl4", "--").split("-")[-1],
                       "zip": a.get("postcode", "")}
    return _cache[key]


# ============================================================ SPONSORSHIP
def norm_employer(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(inc|llc|corp|corporation|co|ltd|lp|holdings|usa|technologies|technology|labs)\b", " ", s)
    return re.sub(r"\s+", "", s)


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

HISTORY = "data/history.json"


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
    ap.add_argument("--out", default="jobs.json")
    ap.add_argument("--lca", nargs="*", default=[])
    ap.add_argument("--max-age", type=int, default=30)
    ap.add_argument("--no-phd", action="store_true")
    ap.add_argument("--review", action="store_true")
    args = ap.parse_args()

    sponsors = load_sponsors(args.lca) if args.lca else {}
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

    json.dump(_cache, open(CACHE, "w"))

    if args.review:
        print("\nUnmatched titles worth adding to the taxonomy:")
        for t, n in review_queue.most_common(40):
            print(f"  {n:>3}  {t}")
        return

    print(f"\ndropped: {dict(dropped)}")
    print(f"review queue: {len(review_queue)} unmatched titles (run --review)")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump(rows, open(args.out, "w"), indent=1)
    print(f"wrote {len(rows)} roles -> {args.out}")

    hist = update_history(rows)
    watch = build_watchlist(rows, hist, ADJACENCY)
    wpath = os.path.join(os.path.dirname(args.out) or ".", "watchlist.json")
    json.dump(watch, open(wpath, "w"), indent=1)
    print(f"wrote {len(watch)} watchlist employers -> {wpath}")


if __name__ == "__main__":
    main()
