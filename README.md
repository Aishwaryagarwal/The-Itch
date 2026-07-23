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

## The three pieces

| File | Job |
|---|---|
| `index.html` | The site. Reads `jobs.json` and `watchlist.json`. |
| `ingest.py` | Fetches current postings from company job boards, writes `jobs.json`. |
| `discover.py` | Mines DOL visa records to find employers worth adding to the target list. |

## Running it locally

```bash
pip install -r requirements.txt

# fetch current postings
python ingest.py --max-age 30 --no-phd

# serve the site
python -m http.server 8000
# open http://localhost:8000
```

Without `jobs.json` present, the site shows bundled sample data so you can see
the interface before wiring up real feeds.

## Adding sponsorship data

Download the quarterly LCA disclosure files from
[DOL Foreign Labor Certification Performance Data](https://www.dol.gov/agencies/eta/foreign-labor/performance),
convert to CSV, and drop them in `data/`.

```bash
python ingest.py --lca data/lca_*.csv --max-age 30 --no-phd
```

## Finding companies to track

```bash
# every sponsoring employer running data roles in Michigan
python discover.py --lca data/lca_*.csv --state MI

# same industry as a current employer, by NAICS code
python discover.py --lca data/lca_*.csv --naics 811310 333996

# strong sponsors that are not household names, with job board lookup
python discover.py --lca data/lca_*.csv --obscure --max-filings 300 --probe-ats
```

Paste the results into `TARGETS` in `ingest.py`.

## Automation

`.github/workflows/refresh.yml` re-runs the ingest on weekday mornings and
commits the updated data. Enable it under the repository Actions tab.

## Caveats

Sponsorship signal reflects past filings. It is evidence an employer has
sponsored before, not a promise they will sponsor now. Salary ranges come from
postings that disclose them, which is a minority in most states.

## License

MIT
