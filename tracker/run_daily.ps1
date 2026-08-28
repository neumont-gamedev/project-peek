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

# Publish the finished deliverables to OneDrive. This is a ONE-WAY copy of the
# generated .html/.docx only -- the working tree stays on the local drive, because
# OneDrive syncing .git\ can corrupt the object store or grab index.lock mid-write.
# Override the destination with TRACKER_PUBLISH_DIR in secrets.local.ps1.
if ($rc -eq 0) {
    $publishDir = $env:TRACKER_PUBLISH_DIR
    if (-not $publishDir -and $env:OneDriveCommercial) {
        $publishDir = Join-Path $env:OneDriveCommercial "Neumont\PRO100\Teams"
    }
    if (-not $publishDir) {
        "skipping publish (set TRACKER_PUBLISH_DIR to publish the .docx/.html somewhere)" | Out-File -FilePath $log -Append -Encoding utf8
    } else {
        if (-not (Test-Path $publishDir)) { New-Item -ItemType Directory -Path $publishDir -Force | Out-Null }
        $root = Split-Path -Parent $here
        $files = @(Get-ChildItem -LiteralPath $root -File | Where-Object { ($_.Extension -eq ".html" -or $_.Extension -eq ".docx") -and $_.Name -notlike "~`$*" })
        if ($files.Count -eq 0) {
            "WARN: no .html/.docx deliverables found in $root to publish" | Out-File -FilePath $log -Append -Encoding utf8
        }
        foreach ($f in $files) {
            try {
                Copy-Item -LiteralPath $f.FullName -Destination $publishDir -Force -ErrorAction Stop
                "published $($f.Name) -> $publishDir" | Out-File -FilePath $log -Append -Encoding utf8
            } catch {
                "WARN: could not publish $($f.Name): $($_.Exception.Message)" | Out-File -FilePath $log -Append -Encoding utf8
            }
        }
    }
}

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
