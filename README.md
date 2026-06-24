# Marginal Pilgrims — GA4 → Supabase Sync

Replaces the manual "export GA4 CSV, write SQL, run it" workflow with a script
that runs automatically every Monday morning via GitHub Actions.

## What it does

Pulls a rolling 8-day window from the GA4 Data API and writes it into six
Supabase tables:

- `ga_metrics` — page-level performance (full replace each run)
- `ga_acquisition` — channel-level user stats (full replace each run)
- `ga_demographics_country` / `ga_demographics_city` (full replace each run)
- `ga_tech_overview` — device/os/browser by day (replaces last 8 days only)
- `ga_traffic_acquisition` — channel by day (replaces last 8 days only)

The 8-day window means each run self-corrects any GA4 data that was still
processing in the previous run.

## One-time setup

### 1. Add repo secrets

GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add these four:

| Secret name | Value |
|---|---|
| `GA4_PROPERTY_ID` | `506718409` |
| `GA4_SERVICE_ACCOUNT_JSON` | paste the *entire contents* of the service account JSON key file |
| `SUPABASE_URL` | your Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | your Supabase service role key (Project Settings → API) |

### 2. Push these files to the repo

Either drag-and-drop all files (keeping the `.github/workflows/` folder
structure intact) through the GitHub web UI's "Add file → Upload files", or
via git:

```bash
git clone https://github.com/davepoppins5073/marginal-pilgrims-data-sync.git
cd marginal-pilgrims-data-sync
# copy ga4_sync.py, requirements.txt, .github/ into here
git add .
git commit -m "Add GA4 sync"
git push
```

### 3. Test it manually

Repo → **Actions** tab → **GA4 Supabase Sync** workflow → **Run workflow**
button. Watch the logs; each table prints how many rows it wrote.

## Schedule

Runs automatically every Monday at 6am EST. To change the time, edit the
`cron` line in `.github/workflows/ga4-sync.yml` (cron times are in UTC).
