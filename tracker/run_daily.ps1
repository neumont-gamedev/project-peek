# Daily refresh for the PRO100 Team Tracker.
# Run by Windows Task Scheduler (see README). Can also be run by hand to test:
#     powershell -ExecutionPolicy Bypass -File .\run_daily.ps1
#
# It loads secrets.local.ps1 (if present) for TRELLO_KEY / TRELLO_TOKEN /
# GITHUB_TOKEN, then runs collect -> build_site -> build_doc, logging to logs\.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

# Log file (one per day)
$logDir = Join-Path $here "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("run_" + (Get-Date -Format "yyyy-MM-dd") + ".log")
"=== run started $(Get-Date -Format s) ===" | Out-File -FilePath $log -Append -Encoding utf8

# Load local secrets, if present
$secrets = Join-Path $here "secrets.local.ps1"
if (Test-Path $secrets) {
    . $secrets
    "loaded secrets.local.ps1" | Out-File -FilePath $log -Append -Encoding utf8
} else {
    "no secrets.local.ps1 found - Trello and private repos will be skipped" | Out-File -FilePath $log -Append -Encoding utf8
}

# Find Python
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $py) {
    "ERROR: python not found on PATH" | Out-File -FilePath $log -Append -Encoding utf8
    exit 1
}

# Run the local pipeline (collect -> build_site -> build_doc); capture output to the log
& $py run_all.py *>> $log
$rc = $LASTEXITCODE

# Sync activity to Firestore (the web app). The app is the source of truth for team
# setup; this reads the team list from Firestore and writes back only measured activity.
if ($env:FIREBASE_UID -and $env:GOOGLE_APPLICATION_CREDENTIALS) {
    "syncing activity to Firestore..." | Out-File -FilePath $log -Append -Encoding utf8
    & $py sync_firestore.py *>> $log
} else {
    "skipping Firestore sync (set FIREBASE_UID and GOOGLE_APPLICATION_CREDENTIALS in secrets.local.ps1 to enable)" | Out-File -FilePath $log -Append -Encoding utf8
}

"=== run finished $(Get-Date -Format s) (exit $rc) ===`n" | Out-File -FilePath $log -Append -Encoding utf8
exit $rc
