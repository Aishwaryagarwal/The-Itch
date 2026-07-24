# The Itch

Personal projects, one directory each, grouped by the itch they scratch.

| Project | What it is |
|---|---|
| [job-market/nextgig](job-market/nextgig) | A live map of data, AI and analytics roles at employers with a real visa sponsorship record. |
| [growth/skills-radar](growth/skills-radar) | A living radar chart of my data & AI engineering skills, tracked over time. |

## Conventions

Each project is self-contained: its own `README.md`, its own dependencies, its
own data. Nothing at the repository root belongs to a single project.

Scripts resolve paths relative to their own project directory rather than the
working directory, so they behave the same run from anywhere.

GitHub Actions only reads workflows from `.github/workflows/` at the repository
root, so per-project workflows live there under a `<project>-` name prefix and
reach into the project folder.
