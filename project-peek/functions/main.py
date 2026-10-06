"""Project Peek — Cloud Functions.

sync_now: an HTTPS callable the web app's "Sync now" button invokes. It reads the
signed-in instructor's teams from Firestore, fetches GitHub commit history + Trello
(card counts, activity, assignees) using the instructor's Trello secrets, and writes
back only the measured `act` field — mirroring the local collector.

Requires (Firebase secrets, set once):  TRELLO_KEY, TRELLO_TOKEN
GitHub is read unauthenticated (public repos); private repos keep their last-known data.
"""
import os, re, json, urllib.request, urllib.parse, urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from firebase_functions import https_fn, options, scheduler_fn
from firebase_admin import initialize_app
from firebase_admin import firestore as admin_firestore
from google.cloud import firestore as gcf   # kept for SERVER_TIMESTAMP

initialize_app()


def get_db():
    """Firestore client for the project's default database.

    Do NOT pass database="(default)" explicitly: google-cloud-firestore
    percent-encodes it into the resource path ("%28default%29") and the backend
    rejects every query with InvalidArgument. Letting firebase_admin resolve the
    default database sidesteps it -- this is what the local collector
    (tracker/sync_firestore.py) has always done, which is why it never hit this.

    ("transport='rest'" is not an option here: firestore.Client.__init__ takes
    no such parameter and raises TypeError.)
    """
    return admin_firestore.client()

ALLOWED_DOMAIN = "neumont.edu"
ACTIVITY_DAYS = 14
DONE_RE = re.compile(r"\b(?:done|complete|completed|finished|shipped)\b", re.I)
BACKLOG_RE = re.compile(r"\bbacklog\b", re.I)
MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _tkey():   return os.environ.get("TRELLO_KEY", "").strip()
def _ttok():   return os.environ.get("TRELLO_TOKEN", "").strip()


def trello_list_stage(name, index=None):
    """Map a board-specific list name to Project Peek's stable workflow stages."""
    if DONE_RE.search(name or ""):
        return "complete"
    if index == 0 or BACKLOG_RE.search(name or ""):
        return "backlog"
    return "active"


# A Secret Manager secret cannot hold an empty payload, so GITHUB_TOKEN may be set
# to the sentinel below to mean "not configured". Real tokens all carry a known
# prefix, so anything without one is treated as absent rather than sent to GitHub
# (a bogus Authorization header would turn every request into a 401).
GH_TOKEN_PREFIXES = ("ghp_", "github_pat_", "gho_", "ghs_", "ghu_")


def _ghtok():
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    return t if t.startswith(GH_TOKEN_PREFIXES) else ""


def fmt_date(iso):
    try:
        y, m, d = iso.split("-"); return f"{MON[int(m)]} {int(d)}"
    except Exception:
        return iso


# ---------- GitHub ----------
# --- GitHub access is read-only, enforced here ------------------------------
# GITHUB_TOKEN is a classic PAT with the `repo` scope, which GitHub also allows
# to WRITE. There is no narrower classic scope that can read a private repo, so
# the read-only guarantee cannot come from the token -- it lives here instead.
#
# Every GitHub call goes through _gh_request(), which builds an explicit,
# bodyless GET against the API host. A GET cannot create, modify, or delete
# anything. Anything else raises before a socket is opened. Do not add a `data=`
# argument or a `method=` other than GET to this file.
GITHUB_API = "https://api.github.com/"


def _gh_request(url, headers):
    """Build a GitHub request that is structurally incapable of modifying a repo."""
    if not url.startswith(GITHUB_API):
        raise ValueError(f"refusing non-GitHub-API URL: {url!r}")
    req = urllib.request.Request(url, headers=headers, method="GET")
    if req.data is not None or req.get_method() != "GET":
        raise ValueError("refusing a GitHub request that is not a bodyless GET")
    return req


def gh_get(url):
    headers = {"User-Agent": "project-peek", "Accept": "application/vnd.github+json"}
    tok = _ghtok()
    if tok:
        # Raises the rate limit from 60 to 5000 requests/hour, and reads any private
        # repo the token's account can actually see.
        headers["Authorization"] = f"Bearer {tok}"
    with urllib.request.urlopen(_gh_request(url, headers), timeout=30) as r:
        return json.load(r)


def slug_from_github(url):
    s = url.replace("https://github.com/", "").replace("http://github.com/", "").strip("/")
    return s[:-4] if s.endswith(".git") else s


def fetch_commits(slug):
    # by_day_author is what lets the app split contributions per sprint: the sprint window
    # is a date range, and a date -> author -> count map slices to it. The whole history is
    # re-read on every sync, so this backfills for existing repos on the next run.
    by_day, authors = Counter(), Counter()
    by_day_author = defaultdict(Counter)
    page = 1
    while page <= 6:
        batch = gh_get(f"https://api.github.com/repos/{slug}/commits?per_page=100&page={page}")
        if not batch:
            break
        for c in batch:
            commit = c.get("commit", {}) or {}
            a = commit.get("author", {}) or {}
            login = (c.get("author") or {}).get("login")
            who = login or a.get("name", "unknown")
            authors[who] += 1
            date = (a.get("date") or "")[:10]
            if date:
                by_day[date] += 1
                by_day_author[date][who] += 1
        if len(batch) < 100:
            break
        page += 1
    return (dict(by_day),
            [{"n": n, "c": c} for n, c in authors.most_common()],
            {d: dict(v) for d, v in by_day_author.items()})


# ---------- Trello ----------
def trello_board_id(url):
    if "/b/" not in url:
        return None
    try:
        return url.split("/b/")[1].split("/")[0].split("?")[0]
    except Exception:
        return None


def _tget(path, extra):
    params = {"key": _tkey(), "token": _ttok(), **extra}
    url = f"https://api.trello.com/1/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "project-peek"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_trello(url):
    """Return (snapshot|None, reason). `reason` explains skips so the UI can show them."""
    if not (_tkey() and _ttok()):
        return None, "no Trello credentials configured"
    if not url:
        return None, "no board link"
    if "/invite/" in url:
        return None, "invite link (API can't read - needs the plain /b/<id> URL)"
    bid = trello_board_id(url)
    if not bid:
        return None, "unrecognized board link"
    try:
        lists = _tget(f"boards/{bid}/lists", {"cards": "open", "card_fields": "idMembers", "fields": "name"})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, f"{e.code} - board is private and the account isn't a member"
        if e.code == 404:
            return None, "404 - board not found"
        return None, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        return None, f"error: {e}"
    out = [{"name": l.get("name", "?"), "count": len(l.get("cards", [])),
            "stage": trello_list_stage(l.get("name", ""), i)} for i, l in enumerate(lists)]
    result = {"total_cards": sum(x["count"] for x in out), "lists": out}
    result["cards_by_stage"] = {
        stage: sum(x["count"] for x in out if x["stage"] == stage)
        for stage in ("backlog", "active", "complete")
    }
    stages = {l.get("id"): trello_list_stage(l.get("name", ""), i) for i, l in enumerate(lists)}

    # assignees
    try:
        members = {m["id"]: (m.get("fullName") or m.get("username") or m["id"])
                   for m in _tget(f"boards/{bid}/members", {"fields": "fullName,username"})}
    except Exception:
        members = {}
    assignees, unassigned = Counter(), 0
    for l in lists:
        for card in l.get("cards", []):
            ids = card.get("idMembers") or []
            if not ids:
                unassigned += 1
            for mid in ids:
                assignees[members.get(mid, mid)] += 1
    result["assignees"] = {"by": [{"n": n, "c": c} for n, c in assignees.most_common()], "unassigned": unassigned}

    # activity (last 14 days)
    since = (datetime.now(timezone.utc) - timedelta(days=ACTIVITY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        actions = _tget(f"boards/{bid}/actions", {"filter": "createCard,updateCard", "limit": "1000", "since": since})
    except Exception:
        actions = None
    if actions is not None:
        moves = into_done = reopened = created = 0
        by, recent = Counter(), []
        for a in actions:
            typ, data = a.get("type"), (a.get("data", {}) or {})
            who = (a.get("memberCreator") or {}).get("fullName") or (a.get("memberCreator") or {}).get("username") or "?"
            if typ == "updateCard" and data.get("listBefore") and data.get("listAfter"):
                moves += 1; by[who] += 1
                before_list, after_list = data["listBefore"], data["listAfter"]
                before_stage = stages.get(before_list.get("id")) or trello_list_stage(before_list.get("name", ""))
                after_stage = stages.get(after_list.get("id")) or trello_list_stage(after_list.get("name", ""))
                after = after_list.get("name", "")
                if before_stage != "complete" and after_stage == "complete":
                    into_done += 1
                elif before_stage == "complete" and after_stage != "complete":
                    reopened += 1
                if len(recent) < 8:
                    recent.append({"date": (a.get("date") or "")[:10],
                                   "card": (data.get("card") or {}).get("name", "")[:70],
                                   "frm": before_list.get("name", ""), "to": after, "by": who,
                                   "from_stage": before_stage, "to_stage": after_stage})
            elif typ == "createCard":
                created += 1
        result["activity"] = {"window_days": ACTIVITY_DAYS, "moves": moves, "into_done": into_done,
                              "reopened": reopened,
                              "created": created, "by_member": [{"n": n, "c": c} for n, c in by.most_common()],
                              "recent": recent}
    return result, "ok"


# ---------- activity object ----------
def build_act(commits_by_day, contributors, trello=None, contrib_notes=None, obs="", by_day_author=None):
    cbd = commits_by_day or {}
    if not cbd:
        return {"ok": False, "obs": obs, "commits_by_day": {}, "contribs": [], "trello": trello}
    days = sorted(cbd.keys()); total = sum(cbd.values()); notes = contrib_notes or {}
    contribs = []
    for c in sorted(contributors or [], key=lambda x: -x["c"]):
        pct = round(100 * c["c"] / total) if total else 0
        contribs.append({"n": c["n"], "c": c["c"], "p": pct, "note": notes.get(c["n"], "")})
    return {"ok": True, "total": total, "days": len(days), "first": days[0], "last": days[-1],
            "last_fmt": fmt_date(days[-1]), "span": f"{fmt_date(days[0])} – {fmt_date(days[-1])}",
            "commits_by_day": cbd, "by_day_author": by_day_author or {},
            "contribs": contribs, "obs": obs, "trello": trello}


def _github_reason(exc):
    """Human-readable explanation for a failed GitHub fetch."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return "404 - repo not found, renamed, or private (no token configured)"
        if exc.code == 403:
            return "403 - API rate limit reached (60/hour unauthenticated) or access denied"
        if exc.code == 401:
            return "401 - not authorized"
        return f"HTTP {exc.code} {exc.reason}"
    return f"error: {exc}"


def _sync_uid(db, uid):
    """Sync every team's activity. Returns per-team results so the UI can show
    what actually happened rather than a bare success count."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    courses = teams = 0
    issues = []          # only the entries that need attention
    results = []         # every team, for a full report

    for c in db.collection(f"users/{uid}/courses").stream():
        courses += 1
        code = (c.to_dict() or {}).get("code", c.id)
        cref = db.document(f"users/{uid}/courses/{c.id}")
        for tdoc in cref.collection("teams").stream():
            d = tdoc.to_dict() or {}
            name = d.get("team", tdoc.id)
            github = d.get("github", "") or ""; trello = d.get("trello", "") or ""
            old_act = d.get("act", {}) or {}; contrib_notes = d.get("contrib_notes", {}) or {}

            gh_status = "ok"
            cbd, contributors, bda = {}, [], {}
            if not github:
                gh_status = "no repo link"
            else:
                try:
                    cbd, contributors, bda = fetch_commits(slug_from_github(github))
                    if not cbd:
                        gh_status = "no commits found"
                except Exception as e:
                    gh_status = _github_reason(e)

            tr_status = "ok"
            trello_snap = old_act.get("trello")
            if not trello:
                tr_status = "no board link"
            else:
                snap, reason = fetch_trello(trello)
                if snap:
                    snap["date"] = today; trello_snap = {"date": today, **snap}
                else:
                    tr_status = reason

            # Preserve prior commit data rather than zeroing a team we couldn't reach.
            stale = False
            if not cbd and old_act.get("commits_by_day"):
                act = dict(old_act); act["trello"] = trello_snap or old_act.get("trello")
                stale = True
            else:
                act = build_act(cbd, contributors, trello_snap, contrib_notes, old_act.get("obs", ""), bda)
                if cbd:
                    act["synced"] = now_iso   # commit data genuinely refreshed just now

            cref.collection("teams").document(tdoc.id).set({"act": act}, merge=True)
            teams += 1

            entry = {"course": code, "team": name, "id": tdoc.id,
                     "github": gh_status, "trello": tr_status,
                     "commits": act.get("total", 0) if act.get("ok") else 0,
                     "stale": stale}
            results.append(entry)
            if gh_status != "ok" or tr_status not in ("ok", "no board link"):
                issues.append(entry)

    db.document(f"users/{uid}").set({"lastSync": gcf.SERVER_TIMESTAMP}, merge=True)
    return {"courses": courses, "teams": teams,
            "ok": teams - len(issues), "issues": issues, "results": results,
            "syncedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@https_fn.on_request()
def diag(req: https_fn.Request) -> https_fn.Response:
    import traceback
    from importlib.metadata import version
    out = {}
    try:
        out["firestore_version"] = version("google-cloud-firestore")
        out["firebase_admin_version"] = version("firebase-admin")
    except Exception as e:
        out["version_error"] = str(e)
    try:
        db = get_db()
        docs = list(db.collection("users").limit(1).stream())
        out["read_ok"] = True
        out["users_docs_seen"] = len(docs)
    except Exception as e:
        out["read_ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"
        out["trace"] = traceback.format_exc()[-1500:]
    return https_fn.Response(json.dumps(out, indent=2), mimetype="application/json")


@https_fn.on_call(secrets=["TRELLO_KEY", "TRELLO_TOKEN", "GITHUB_TOKEN"], timeout_sec=300,
                  memory=options.MemoryOption.MB_512)
def sync_now(req: https_fn.CallableRequest):
    if not req.auth:
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.UNAUTHENTICATED, "Sign in first.")
    token = req.auth.token or {}
    email = (token.get("email") or "").lower()
    if not token.get("email_verified") or not email.endswith("@" + ALLOWED_DOMAIN):
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.PERMISSION_DENIED,
                                  "Neumont accounts only.")
    try:
        db = get_db()
        return _sync_uid(db, req.auth.uid)
    except Exception as e:
        import traceback
        print("SYNC_ERROR:", repr(e))
        print(traceback.format_exc())
        raise https_fn.HttpsError(https_fn.FunctionsErrorCode.INTERNAL,
                                  f"{type(e).__name__}: {e}")


def _schedule_is_due(schedule, now_utc):
    """Return (is_due, local_date) for one instructor's saved weekly schedule."""
    if not schedule.get("enabled"):
        return False, None
    try:
        local_now = now_utc.astimezone(ZoneInfo(schedule.get("timezone") or "America/Denver"))
    except ZoneInfoNotFoundError:
        return False, None
    local_date = local_now.strftime("%Y-%m-%d")
    if schedule.get("lastRunDate") == local_date:
        return False, local_date
    weekdays = schedule.get("weekdays")
    if isinstance(weekdays, list) and weekdays:
        selected_days = {str(day) for day in weekdays}
    else:
        selected_days = {str(schedule.get("weekday", "daily"))}
    if "daily" not in selected_days and str((local_now.weekday() + 1) % 7) not in selected_days:
        return False, local_date
    try:
        hour, minute = [int(x) for x in str(schedule.get("time", "")).split(":", 1)]
    except (TypeError, ValueError):
        return False, local_date
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return False, local_date
    return (local_now.hour, local_now.minute) >= (hour, minute), local_date


def _roll_course_sprints(cref, today):
    """Create the next sprint once the newest has ended, keeping its length and target,
    so a course always has a current sprint. Courses whose last sprint ended more than
    a sprint-length ago are left alone. Mirrors rollSprints() in the web app."""
    sprints = sorted(((d.id, d.to_dict() or {}) for d in cref.collection("sprints").stream()),
                     key=lambda x: x[1].get("n") or 0)
    if not sprints:
        return 0
    _, last = sprints[-1]
    start_s, end_s = last.get("start"), last.get("end")
    if not start_s or not end_s:
        return 0
    start, end = datetime.fromisoformat(start_s).date(), datetime.fromisoformat(end_s).date()
    length = (end - start).days + 1
    if (end + timedelta(days=length)).isoformat() < today:
        return 0   # ended over a sprint-length ago: course is finished, don't backfill
    ids = {sid for sid, _ in sprints}
    n, made = last.get("n") or len(sprints), 0
    while end.isoformat() < today and made < 52:
        n += 1
        start = end + timedelta(days=1)
        end = start + timedelta(days=length - 1)
        sid = f"s{n}"
        while sid in ids:
            sid += "b"
        ids.add(sid)
        cref.collection("sprints").document(sid).set({
            "n": n, "name": f"Sprint {n}", "start": start.isoformat(), "end": end.isoformat(),
            "targetHours": last.get("targetHours") or 20,
            "auto": True, "createdAt": gcf.SERVER_TIMESTAMP})
        made += 1
    return made


@scheduler_fn.on_schedule(schedule="5 0 * * *", timezone="America/Denver")
def roll_sprints(event: scheduler_fn.ScheduledEvent) -> None:
    """Shortly after midnight, start the next sprint in any course whose sprint ended."""
    db = get_db()
    today = datetime.now(ZoneInfo("America/Denver")).strftime("%Y-%m-%d")
    for user_doc in db.collection("users").stream():
        for c in user_doc.reference.collection("courses").stream():
            try:
                made = _roll_course_sprints(c.reference, today)
                if made:
                    print(f"ROLL_SPRINTS uid={user_doc.id} course={c.id} created={made}")
            except Exception as exc:
                print(f"ROLL_SPRINTS_ERROR uid={user_doc.id} course={c.id} error={exc!r}")


@scheduler_fn.on_schedule(
    schedule="*/5 * * * *",
    secrets=["TRELLO_KEY", "TRELLO_TOKEN", "GITHUB_TOKEN"],
    timeout_sec=540,
    memory=options.MemoryOption.MB_512,
)
def scheduled_sync(event: scheduler_fn.ScheduledEvent) -> None:
    """Run each instructor once when their saved local schedule becomes due."""
    db = get_db()
    now_utc = datetime.now(timezone.utc)
    for user_doc in db.collection("users").stream():
        data = user_doc.to_dict() or {}
        schedule = data.get("syncSchedule") or {}
        due, local_date = _schedule_is_due(schedule, now_utc)
        if not due:
            continue

        schedule_ref = user_doc.reference
        # Claim today's run before starting. This prevents the next five-minute
        # invocation from launching a duplicate if a large course sync is still running.
        schedule_ref.set({"syncSchedule": {
            **schedule,
            "lastRunDate": local_date,
            "lastStartedAt": gcf.SERVER_TIMESTAMP,
            "lastStatus": "running",
            "lastError": "",
        }}, merge=True)
        try:
            result = _sync_uid(db, user_doc.id)
            schedule_ref.set({"syncSchedule": {
                **schedule,
                "lastRunDate": local_date,
                "lastCompletedAt": gcf.SERVER_TIMESTAMP,
                "lastStatus": "ok",
                "lastError": "",
                "lastTeams": result.get("teams", 0),
                "lastIssues": len(result.get("issues", [])),
            }}, merge=True)
            print(f"SCHEDULED_SYNC_OK uid={user_doc.id} teams={result.get('teams', 0)}")
        except Exception as exc:
            schedule_ref.set({"syncSchedule": {
                **schedule,
                "lastRunDate": local_date,
                "lastCompletedAt": gcf.SERVER_TIMESTAMP,
                "lastStatus": "error",
                "lastError": f"{type(exc).__name__}: {exc}"[:500],
            }}, merge=True)
            print(f"SCHEDULED_SYNC_ERROR uid={user_doc.id} error={exc!r}")
