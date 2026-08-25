"""Project Peek — Cloud Functions.

sync_now: an HTTPS callable the web app's "Sync now" button invokes. It reads the
signed-in instructor's teams from Firestore, fetches GitHub commit history + Trello
(card counts, activity, assignees) using the instructor's Trello secrets, and writes
back only the measured `act` field — mirroring the local collector.

Requires (Firebase secrets, set once):  TRELLO_KEY, TRELLO_TOKEN
GitHub is read unauthenticated (public repos); private repos keep their last-known data.
"""
import os, re, json, urllib.request, urllib.parse, urllib.error
from collections import Counter
from datetime import datetime, timezone, timedelta

from firebase_functions import https_fn, options
from firebase_admin import initialize_app
from google.cloud import firestore as gcf

initialize_app()


def get_db():
    # REST transport avoids a Cloud Run gRPC bug that percent-encodes the default
    # database id ("(default)" -> "%28default%29"), which the backend rejects.
    return gcf.Client(database="(default)", transport="rest")

ALLOWED_DOMAIN = "neumont.edu"
ACTIVITY_DAYS = 14
DONE_RE = re.compile(r"done|complete|finished|shipped", re.I)
MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _tkey():   return os.environ.get("TRELLO_KEY", "").strip()
def _ttok():   return os.environ.get("TRELLO_TOKEN", "").strip()


def fmt_date(iso):
    try:
        y, m, d = iso.split("-"); return f"{MON[int(m)]} {int(d)}"
    except Exception:
        return iso


# ---------- GitHub ----------
def gh_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "project-peek",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def slug_from_github(url):
    s = url.replace("https://github.com/", "").replace("http://github.com/", "").strip("/")
    return s[:-4] if s.endswith(".git") else s


def fetch_commits(slug):
    by_day, authors = Counter(), Counter()
    page = 1
    while page <= 6:
        batch = gh_get(f"https://api.github.com/repos/{slug}/commits?per_page=100&page={page}")
        if not batch:
            break
        for c in batch:
            commit = c.get("commit", {}) or {}
            a = commit.get("author", {}) or {}
            login = (c.get("author") or {}).get("login")
            authors[login or a.get("name", "unknown")] += 1
            date = (a.get("date") or "")[:10]
            if date:
                by_day[date] += 1
        if len(batch) < 100:
            break
        page += 1
    return dict(by_day), [{"n": n, "c": c} for n, c in authors.most_common()]


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
    if not (_tkey() and _ttok()) or not url or "/invite/" in url:
        return None
    bid = trello_board_id(url)
    if not bid:
        return None
    try:
        lists = _tget(f"boards/{bid}/lists", {"cards": "open", "card_fields": "idMembers", "fields": "name"})
    except Exception:
        return None
    out = [{"name": l.get("name", "?"), "count": len(l.get("cards", []))} for l in lists]
    result = {"total_cards": sum(x["count"] for x in out), "lists": out}

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
        moves = into_done = created = 0
        by, recent = Counter(), []
        for a in actions:
            typ, data = a.get("type"), (a.get("data", {}) or {})
            who = (a.get("memberCreator") or {}).get("fullName") or (a.get("memberCreator") or {}).get("username") or "?"
            if typ == "updateCard" and data.get("listBefore") and data.get("listAfter"):
                moves += 1; by[who] += 1
                after = data["listAfter"].get("name", "")
                if DONE_RE.search(after):
                    into_done += 1
                if len(recent) < 8:
                    recent.append({"date": (a.get("date") or "")[:10],
                                   "card": (data.get("card") or {}).get("name", "")[:70],
                                   "frm": data["listBefore"].get("name", ""), "to": after, "by": who})
            elif typ == "createCard":
                created += 1
        result["activity"] = {"window_days": ACTIVITY_DAYS, "moves": moves, "into_done": into_done,
                              "created": created, "by_member": [{"n": n, "c": c} for n, c in by.most_common()],
                              "recent": recent}
    return result


# ---------- activity object ----------
def build_act(commits_by_day, contributors, trello=None, contrib_notes=None, obs=""):
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
            "commits_by_day": cbd, "contribs": contribs, "obs": obs, "trello": trello}


def _sync_uid(db, uid):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    courses = teams = 0
    for c in db.collection(f"users/{uid}/courses").stream():
        courses += 1
        cref = db.document(f"users/{uid}/courses/{c.id}")
        for tdoc in cref.collection("teams").stream():
            d = tdoc.to_dict() or {}
            github = d.get("github", "") or ""; trello = d.get("trello", "") or ""
            old_act = d.get("act", {}) or {}; contrib_notes = d.get("contrib_notes", {}) or {}
            cbd, contributors = {}, []
            if github:
                try:
                    cbd, contributors = fetch_commits(slug_from_github(github))
                except Exception:
                    pass
            trello_snap = old_act.get("trello")
            if trello:
                snap = fetch_trello(trello)
                if snap:
                    snap["date"] = today; trello_snap = {"date": today, **snap}
            if not cbd and old_act.get("commits_by_day"):
                act = dict(old_act); act["trello"] = trello_snap or old_act.get("trello")
            else:
                act = build_act(cbd, contributors, trello_snap, contrib_notes, old_act.get("obs", ""))
            cref.collection("teams").document(tdoc.id).set({"act": act}, merge=True)
            teams += 1
    db.document(f"users/{uid}").set({"lastSync": gcf.SERVER_TIMESTAMP}, merge=True)
    return {"courses": courses, "teams": teams}


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


@https_fn.on_call(secrets=["TRELLO_KEY", "TRELLO_TOKEN"], timeout_sec=300,
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
