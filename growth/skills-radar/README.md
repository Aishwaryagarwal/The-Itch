# Skills Radar

A living radar (a.k.a. spider / competency chart) of my data & AI engineering
skills — technical and professional — self-assessed on a 0–5 scale. The dashed
outline is the baseline; the filled shape is now. It grows as I do.

Companion to [NextGig](../../job-market/nextgig); the "Coming soon" tab there links here.

## Files

```
index.html      the radar + progress panel, edit mode, and table view (self-contained)
skills.json     current scores per skill — the source of truth
history.json    dated snapshots; the first one is the baseline the radar compares against
```

## Recording progress

Two ways to update your scores:

1. **On the page** — click **Edit scores**, drag the sliders, then
   **Copy skills.json** and paste it over `skills.json`. To log a dated point in
   your history, also **Copy history.json** (it appends today's snapshot) and
   paste it over `history.json`.
2. **By hand** — edit the `score` values in `skills.json` directly.

Commit the changed files. Each snapshot in `history.json` is a point in time; the
radar always overlays the earliest one as your baseline, so the gap you close is
visible.

## Adjusting the skills themselves

Add, remove, or rename axes in `skills.json` (`name` + `group` + `score`). The
radar rebuilds around whatever is there — 5 skills or 20.

## Deploying

Static, no build. As a second Netlify site from this repo, set the **base
directory** to `growth/skills-radar`; it reads `netlify.toml` and publishes this
folder. Custom subdomain (e.g. `skills.aishwaryagarwal.com`) works the same way
as NextGig.

## License

© 2026 Aishwarya Agarwal. Licensed under AGPL-3.0 (see the repository-root `LICENSE`).
