# NextGig

A live map of data, AI and analytics engineering roles at employers that have
actually sponsored work visas.

Job boards tell you who is hiring. They do not tell you who sponsors. NextGig
joins public job postings against US Department of Labor visa filing records so
you can filter to employers with a real sponsorship track record, including the
mid-market companies nobody has heard of.

## What it does

- **Open roles**: map and list of current postings, filtered by state, city,
  title, level, work setup, salary floor, posting age and sponsorship record.
- **Worth watching**: employers with a sponsorship record and a data team who
  have nothing open right now, ranked by how overdue they are against their own
  posting cadence.

## Layout

```
public/          the deployed site — this is the Pages build output directory
  index.html     reads jobs.json, watchlist.json and meta.json beside it
  jobs.json      generated, committed by the refresh workflow
  meta.json      generated — last-updated stamp and counts
  watchlist.json generated — employers worth watching
scripts/
  ingest.py      fetches current postings from company job boards
  discover.py    mines DOL visa records for employers worth tracking
data/            inputs and accumulated state
  sponsors.json  collapsed DOL filing counts per employer
  usgeo.json     offline city/state coordinate table
  history.json   per-employer posting history, grows every run
```

Both scripts resolve `data/` and `public/` relative to this project directory
rather than the working directory, so they work the same run from here, from
inside `scripts/`, or from the repository root in CI.

## Running it locally

All commands assume you are in `job-market/nextgig/`.

```bash
pip install -r requirements.txt

# fetch current postings, writes public/jobs.json
python scripts/ingest.py --max-age 30 --no-phd

# serve the site
python -m http.server 8000 --directory public
# open http://localhost:8000
```

Without `jobs.json` present, the site shows bundled sample data so you can see
the interface before wiring up real feeds.

## Adding sponsorship data

Download the quarterly LCA disclosure files from
[DOL Foreign Labor Certification Performance Data](https://www.dol.gov/agencies/eta/foreign-labor/performance),
convert to CSV, and drop them in `data/`.

```bash
# collapse the raw quarterly files into data/sponsors.json once
python scripts/ingest.py --build-sponsors data/lca_*.csv

# then every run reads that instead of the multi-hundred-MB originals
python scripts/ingest.py --max-age 30 --no-phd
```

The raw CSVs are gitignored; `data/sponsors.json` is the committed artifact.

## Finding companies to track

```bash
# every sponsoring employer running data roles in Michigan
python scripts/discover.py --lca data/lca_*.csv --state MI

# same industry as a current employer, by NAICS code
python scripts/discover.py --lca data/lca_*.csv --naics 811310 333996

# strong sponsors that are not household names, with job board lookup
python scripts/discover.py --lca data/lca_*.csv --obscure --max-filings 300 --probe-ats
```

Paste the results into `TARGETS` in `scripts/ingest.py`.

## Automation

`.github/workflows/nextgig-refresh.yml`, at the repository root, re-runs the
ingest on weekday mornings and commits the updated data. Enable it under the
repository Actions tab. It lives at the root because GitHub only runs workflows
from there, not from project subdirectories.

## Deploying

The site is fully static. Point any host at `job-market/nextgig/public/`:

- **Cloudflare Pages** — build command: none, build output directory:
  `job-market/nextgig/public`. Optionally add a `CF_DEPLOY_HOOK` secret and the
  refresh workflow will poke Cloudflare after each data push.
- **GitHub Pages** — needs the site at the repository root or in `/docs`, so
  from a subdirectory it requires a deploy workflow rather than the branch
  setting. Cloudflare Pages is the simpler route here.

## Caveats

Sponsorship signal reflects past filings. It is evidence an employer has
sponsored before, not a promise they will sponsor now. Salary ranges come from
postings that disclose them, which is a minority in most states.

## License

MIT
