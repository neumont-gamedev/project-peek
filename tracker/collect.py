"""Fetch GitHub commit history (and Trello board state, if credentials are set) for
every team, and write a running log to history.json.

Run:  python collect.py

Environment variables (all optional):
  GITHUB_TOKEN   A GitHub personal access token. Raises the rate limit and, if the
                 token has access, lets private repos (e.g. Nerds with Ag) be pulled.
  TRELLO_KEY     Trello API key   (https://trello.com/app-key)
  TRELLO_TOKEN   Trello API token (generate from the same page)

Behaviour:
  - For each reachable repo, commit history is rebuilt from scratch (authoritative) — it
    backfills the full per-day commit timeline, so charts populate immediately.
  - For repos the API cannot reach, prior history is preserved; if there is none, the
    'manual' seed in teams_data.py is used so the chart still shows what we know.
  - Trello card counts are appended per day (a running log; they cannot be backfilled).
"""
import json, os, re, sys, urllib.request, urllib.error, urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

from common import load_courses, load_history, history_path

GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
TRELLO_KEY = os.environ.get("TRELLO_KEY", "").strip()
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN", "").strip()

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
NOW_ISO = datetime.now(timezone.utc).isoformat(timespec="seconds")
ACTIVITY_DAYS = 14
SINCE_ISO = (datetime.now(timezone.utc) - timedelta(days=ACTIVITY_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
DONE_RE = re.compile(r"\b(?:done|complete|completed|finished|shipped)\b", re.I)
BACKLOG_RE = re.compile(r"\bbacklog\b", re.I)


def trello_list_stage(name, index=None):
    """Map a board-specific list name to Project Peek's stable workflow stages."""
    if DONE_RE.search(name or ""):
        return "complete"
    if index == 0 or BACKLOG_RE.search(name or ""):
        return "backlog"
    return "active"


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
    headers = {"User-Agent": "pro100-tracker", "Accept": "application/vnd.github+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    with urllib.request.urlopen(_gh_request(url, headers), timeout=30) as r:
        return json.load(r)


def slug_from_github(url):
    # https://github.com/owner/repo(.git) -> owner/repo
    s = url.replace("https://github.com/", "").replace("http://github.com/", "")
    s = s.strip("/")
    if s.endswith(".git"):
        s = s[:-4]
    return s


def fetch_commits(slug):
    """Return (commits_by_day dict, contributors list, ok bool)."""
    by_day = Counter()
    authors = Counter()
    page = 1
    while page <= 6:  # up to 600 commits
        url = f"https://api.github.com/repos/{slug}/commits?per_page=100&page={page}"
        batch = gh_get(url)
        if not batch:
            break
        for c in batch:
            commit = c.get("commit", {}) or {}
            a = commit.get("author", {}) or {}
            login = (c.get("author") or {}).get("login")
            name = login or a.get("name", "unknown")
            authors[name] += 1
            date = (a.get("date") or "")[:10]
            if date:
                by_day[date] += 1
        if len(batch) < 100:
            break
        page += 1
    contributors = [{"n": n, "c": c} for n, c in authors.most_common()]
    return dict(by_day), contributors, True


def trello_board_id(url):
    # accept https://trello.com/b/<id>/... ; skip private /invite/ links
    if "/b/" not in url:
        return None
    try:
        return url.split("/b/")[1].split("/")[0].split("?")[0]
    except Exception:
        return None


def fetch_trello(url):
    """Return (snapshot|None, reason). reason explains skips/errors for logging."""
    if not (TRELLO_KEY and TRELLO_TOKEN):
        return None, "no creds"
    if not url:
        return None, "no board link"
    if "/invite/" in url:
        return None, "invite link (API can't read — need the plain /b/<id> URL)"
    bid = trello_board_id(url)
    if not bid:
        return None, "unrecognized board link"
    q = urllib.parse.urlencode({"cards": "open", "card_fields": "idMembers", "fields": "name",
                                "key": TRELLO_KEY, "token": TRELLO_TOKEN})
    api = f"https://api.trello.com/1/boards/{bid}/lists?{q}"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "pro100-tracker"})
        with urllib.request.urlopen(req, timeout=30) as r:
            lists = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return None, f"{e.code} — board is private and your account isn't a member"
        if e.code == 404:
            return None, "404 — board not found"
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

    # Assignees: who has how many cards (and how many are unassigned)
    members = fetch_trello_members(bid)
    assignees, unassigned = Counter(), 0
    for l in lists:
        for card in l.get("cards", []):
            ids = card.get("idMembers") or []
            if not ids:
                unassigned += 1
            for mid in ids:
                assignees[members.get(mid, mid)] += 1
    result["assignees"] = {"by": [{"n": n, "c": c} for n, c in assignees.most_common()],
                           "unassigned": unassigned}

    stages = {l.get("id"): trello_list_stage(l.get("name", ""), i) for i, l in enumerate(lists)}
    activity = fetch_trello_activity(bid, stages)
    if activity:
        result["activity"] = activity
    return result, "ok"


def fetch_trello_members(bid):
    q = urllib.parse.urlencode({"fields": "fullName,username", "key": TRELLO_KEY, "token": TRELLO_TOKEN})
    api = f"https://api.trello.com/1/boards/{bid}/members?{q}"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "pro100-tracker"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception:
        return {}
    return {m["id"]: (m.get("fullName") or m.get("username") or m["id"]) for m in data}


def fetch_trello_activity(bid, stages=None):
    """Pull the board's action log (last ACTIVITY_DAYS) and summarize card movement."""
    q = urllib.parse.urlencode({"filter": "createCard,updateCard", "limit": "1000",
                                "since": SINCE_ISO, "key": TRELLO_KEY, "token": TRELLO_TOKEN})
    api = f"https://api.trello.com/1/boards/{bid}/actions?{q}"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "pro100-tracker"})
        with urllib.request.urlopen(req, timeout=30) as r:
            actions = json.load(r)
    except Exception:
        return None
    moves = into_done = reopened = created = 0
    by = Counter()
    recent = []
    for a in actions:
        typ = a.get("type")
        data = a.get("data", {}) or {}
        mc = a.get("memberCreator") or {}
        who = mc.get("fullName") or mc.get("username") or "?"
        if typ == "updateCard" and data.get("listBefore") and data.get("listAfter"):
            moves += 1
            by[who] += 1
            before_list, after_list = data["listBefore"], data["listAfter"]
            before_stage = (stages or {}).get(before_list.get("id")) or trello_list_stage(before_list.get("name", ""))
            after_stage = (stages or {}).get(after_list.get("id")) or trello_list_stage(after_list.get("name", ""))
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
    return {"window_days": ACTIVITY_DAYS, "moves": moves, "into_done": into_done,
            "reopened": reopened,
            "created": created, "by_member": [{"n": n, "c": c} for n, c in by.most_common()],
            "recent": recent}


def collect_course(course):
    cid = course["id"]
    teams = course["teams"]
    hist = load_history(cid)
    hist.setdefault("teams", {})
    hist["collected_at"] = NOW_ISO
    print(f"\n===== {course.get('code', cid)} — {course.get('name','')} ({len(teams)} teams) =====")

    for t in teams:
        tid, name = t["id"], t["team"]
        entry = hist["teams"].get(tid, {})
        slug = slug_from_github(t["github"])
        got_live = False
        try:
            cbd, contribs, ok = fetch_commits(slug)
            if cbd:
                entry["commits_by_day"] = cbd
                entry["contributors"] = contribs
                entry["source"] = "api"
                entry["accessible"] = True
                got_live = True
                print(f"{name:26} {sum(cbd.values()):>4} commits  ({len(cbd)} active days)  [live]")
        except urllib.error.HTTPError as e:
            entry.setdefault("accessible", False)
            print(f"{name:26} {e.code} {e.reason}  [no access]")
        except Exception as e:
            print(f"{name:26} error: {e}")

        # Fallback to manual seed only if we have no data at all for this team.
        if not got_live and not entry.get("commits_by_day") and t.get("manual"):
            m = t["manual"]
            entry["commits_by_day"] = dict(m["commits_by_day"])
            entry["contributors"] = list(m["contributors"])
            entry["source"] = "manual"
            entry["accessible"] = False
            print(f"{name:26} {sum(m['commits_by_day'].values()):>4} commits  [seeded / manual]")

        # Trello running log (append today's snapshot; replace if same day)
        snap, reason = fetch_trello(t.get("trello", "") or "")
        if snap:
            snap["date"] = TODAY
            hist_snaps = [s for s in entry.get("trello_snapshots", []) if s.get("date") != TODAY]
            hist_snaps.append(snap)
            entry["trello_snapshots"] = hist_snaps
            entry["trello"] = {"date": TODAY, **snap}
            print(f"{'':26}   trello: {snap['total_cards']} cards across {len(snap['lists'])} lists")
        elif reason != "no creds":
            print(f"{'':26}   trello: skipped — {reason}")

        hist["teams"][tid] = entry

    out = history_path(cid)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(hist, f, indent=2)
    print(f"Wrote {out}")


def main():
    if not GH_TOKEN:
        print("(no GITHUB_TOKEN set — public repos only; private repos fall back to seeded data)")
    print(f"(Trello: {'credentials detected' if (TRELLO_KEY and TRELLO_TOKEN) else 'no TRELLO_KEY/TRELLO_TOKEN set — skipping'})")
    for course in load_courses():
        collect_course(course)


if __name__ == "__main__":
    main()
