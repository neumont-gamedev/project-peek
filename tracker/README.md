# PRO100 Team Tracker — toolchain

Generates two deliverables in the parent `Teams/` folder from one source of truth:

- **`PRO100_Team_Tracker.html`** — web dashboard (status, project summaries, **commit-trend charts**, editable notes). Also published as a shareable Claude artifact.
- **`PRO100_Team_Tracker.docx`** — Word tracker, one page per team.

## Files

| File | What it is |
|------|------------|
| `courses.json` | **Registry** of active courses — a list of `courses/*.json` files to process. |
| `courses/<id>.json` | **One course = one file.** All static info for that class (code, name, term, report_date, output_basename, and the `teams` list with projects, members, links, statuses, proposals, sprint plans, curated observations). **Edit these to change anything.** |
| `data/<id>.history.json` | That course's running log — per-team daily commit counts, contributors, and dated Trello snapshots. Generated; keep it (it's your accumulated history). |
| `collect.py` | For every course, fetches GitHub commit history (+ Trello, if keys are set) → writes `data/<id>.history.json`. |
| `common.py` | Loads courses + history and merges them. Shared by both builders. |
| `template.html` | The web page design + chart code (placeholders `__TEAMS__`, `__COURSE__`, `__REPORT_DATE__`). |
| `build_site.py` | For each course, merges data into `template.html` → writes `<output_basename>.html`. |
| `build_doc.py` | For each course, writes `<output_basename>.docx` (needs `pip install python-docx`). |
| `run_all.py` | Runs collect → build_site → build_doc for all courses. |
| `teams_data.py` | **Deprecated** — original PRO100 data, kept as reference. Source of truth is now `courses/pro100.json`. |
| `migrate_to_courses.py` | One-time migration that created the `courses/` + `data/` layout (already run). |
| `backups/` | Safety copies of the pre-migration `history.json` and `teams_data.py`. |

## Adding another class

1. Copy an existing course file: `courses/pro100.json` → `courses/bit221.json`.
2. Edit its `id`, `code`, `name`, `term`, `output_basename`, and the `teams` list.
3. Add its path to `courses.json`: `{"courses": ["courses/pro100.json", "courses/bit221.json"]}`.
4. Run `python run_all.py`. You get a `<output_basename>.html` + `.docx` for each course.

## Usage

```bash
cd tracker
python run_all.py
```

Then open `../PRO100_Team_Tracker.html` in a browser (double-click), or ask Claude to
re-publish the artifact so the shared link updates.

To refresh just the documents without re-fetching GitHub:
```bash
python build_site.py && python build_doc.py
```

## Optional credentials (environment variables)

Set these before running `collect.py` to unlock more:

| Variable | Effect |
|----------|--------|
| `GITHUB_TOKEN` | A GitHub personal access token. Raises the API rate limit and, if the token has access, pulls **private** repos automatically (e.g. Nerds with Ag) instead of using the seeded fallback. |
| `TRELLO_KEY` / `TRELLO_TOKEN` | From <https://trello.com/app-key>. Enables the Trello running log — card counts per list, snapshotted each run. Only works for public `/b/<id>` board links, not private `/invite/` links. |
| `FIREBASE_UID` / `GOOGLE_APPLICATION_CREDENTIALS` | Enable the push to the Project Peek web app. `FIREBASE_UID` = your account UID (shown on the app's "No data yet" screen); `GOOGLE_APPLICATION_CREDENTIALS` = path to your service-account key JSON (keep private). When both are set, `run_daily.ps1` runs `push_firestore.py` after building. |

PowerShell example:
```powershell
$env:GITHUB_TOKEN = "ghp_xxx"
python collect.py
```

## Charts / running log

`collect.py` rebuilds each reachable repo's **full** per-day commit history every run
(commit dates are authoritative, so history backfills automatically). All team charts
share one timeline with a dashed "today" marker — a team whose bars stop early has gone
quiet. Trello card counts, by contrast, can only accumulate going forward, so run the
collector regularly to build that log.

## Daily scheduling (set up)

A Windows Task Scheduler job named **"PRO100 Team Tracker"** runs `run_daily.ps1`
every day at **7:30am** (it catches up if the machine was off at that time).

`run_daily.ps1` loads `secrets.local.ps1` (if present), runs collect -> build_site ->
build_doc, and writes a dated log to `logs\run_YYYY-MM-DD.log`.

### One-time step: create the secrets file
So the daily run can read Trello (and optionally private repos), copy the template and
fill in your values:

```powershell
Copy-Item secrets.local.ps1.template secrets.local.ps1
notepad secrets.local.ps1
```

Without `secrets.local.ps1` the daily run still works — it refreshes commit history and
**keeps the last Trello snapshot** (it just won't fetch new Trello numbers).

`secrets.local.ps1` stays on this machine. Do not publish or share it.

### Managing the task

```powershell
Start-ScheduledTask  -TaskName "PRO100 Team Tracker"   # run it now
Get-ScheduledTaskInfo -TaskName "PRO100 Team Tracker"  # last run time + result
Disable-ScheduledTask -TaskName "PRO100 Team Tracker"  # pause
Enable-ScheduledTask  -TaskName "PRO100 Team Tracker"  # resume
Unregister-ScheduledTask -TaskName "PRO100 Team Tracker" -Confirm:$false   # remove
```

To change the time, re-run the `Register-ScheduledTask` command with a different
`-At` value (or edit the task in Task Scheduler).

### What the daily job does and does not do
- **Does:** refresh `history.json` (the running log), regenerate the local
  `PRO100_Team_Tracker.html` and `.docx`, and (when the Firebase vars are set) **push the
  data to the Project Peek web app** (https://project-peek.firebaseapp.com).
- **Does not:** re-publish the standalone Claude artifact — that needs Claude. The Firebase
  web app updates automatically; the Claude artifact only updates when you ask Claude.

### Alternative: GitHub Actions
If you later move the tooling into a git repo, a cron workflow could run `collect.py`,
commit `history.json`, and publish the site to GitHub Pages (store tokens as repo
secrets). Ask Claude to scaffold this when you're ready.

## Notes

- Repos that can't be reached (private without a token, deleted, or renamed) keep their
  last known data; if there's none, the `manual` seed in `teams_data.py` is used so the
  chart still shows what we know.
- Some GitHub usernames are matched to students by inference — shown as "likely …".
