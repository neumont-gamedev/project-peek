"""Shared helpers: load course configs + per-course history, and merge them.

Data model (Phase 1, file-based):
  courses.json                  -> registry: {"courses": ["courses/<id>.json", ...]}
  courses/<id>.json             -> one course: {id, code, name, term, report_date,
                                                output_basename, teams:[...]}
  data/<id>.history.json        -> that course's running log (written by collect.py)
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

STATUS_LABEL = {"healthy": "Healthy", "watch": "Watch", "risk": "At risk", "noaccess": "No access"}
STATUS_ORDER = ["risk", "watch", "healthy", "noaccess"]


def fmt_date(iso):
    """'2026-08-05' -> 'Aug 5'"""
    try:
        y, m, d = iso.split("-")
        return f"{MON[int(m)]} {int(d)}"
    except Exception:
        return iso


def load_courses():
    """Return a list of course dicts (each includes its 'teams')."""
    reg_path = os.path.join(HERE, "courses.json")
    with open(reg_path, "r", encoding="utf-8") as f:
        reg = json.load(f)
    courses = []
    for rel in reg.get("courses", []):
        with open(os.path.join(HERE, rel), "r", encoding="utf-8") as cf:
            courses.append(json.load(cf))
    return courses


def load_course(course_id):
    for c in load_courses():
        if c["id"] == course_id:
            return c
    raise KeyError(f"course '{course_id}' not found in courses.json")


def history_path(course_id):
    return os.path.join(HERE, "data", f"{course_id}.history.json")


def load_history(course_id):
    p = history_path(course_id)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"teams": {}, "collected_at": None}


def build_act(commits_by_day, contributors, trello=None, contrib_notes=None, obs=""):
    """Build the activity object from raw fetched data. Used by the Firestore-driven sync."""
    cbd = commits_by_day or {}
    if not cbd:
        return {"ok": False, "obs": obs, "commits_by_day": {}, "contribs": [], "trello": trello}
    days = sorted(cbd.keys())
    total = sum(cbd.values())
    notes = contrib_notes or {}
    contribs = []
    for c in sorted(contributors or [], key=lambda x: -x["c"]):
        pct = round(100 * c["c"] / total) if total else 0
        contribs.append({"n": c["n"], "c": c["c"], "p": pct, "note": notes.get(c["n"], "")})
    return {
        "ok": True, "total": total, "days": len(days),
        "first": days[0], "last": days[-1], "last_fmt": fmt_date(days[-1]),
        "span": f"{fmt_date(days[0])} – {fmt_date(days[-1])}",
        "commits_by_day": cbd, "contribs": contribs, "obs": obs, "trello": trello,
    }


def team_activity(team, history):
    """Merge a team's static obs/notes with live numbers from its course history.

    Returns: { ok, total, days, first, last, last_fmt, span, commits_by_day,
               contribs:[{n,c,p,note}], obs, source, trello }
    """
    h = (history.get("teams") or {}).get(team["id"]) or {}
    cbd = h.get("commits_by_day") or {}
    obs = team.get("activity_obs", "")

    if not cbd:
        return {"ok": False, "obs": obs, "source": h.get("source", "none"),
                "commits_by_day": {}, "contribs": [], "trello": h.get("trello")}

    days_sorted = sorted(cbd.keys())
    total = sum(cbd.values())
    notes = team.get("contrib_notes", {})
    contribs = []
    for c in sorted(h.get("contributors", []), key=lambda x: -x["c"]):
        pct = round(100 * c["c"] / total) if total else 0
        contribs.append({"n": c["n"], "c": c["c"], "p": pct, "note": notes.get(c["n"], "")})

    return {
        "ok": True,
        "total": total,
        "days": len(days_sorted),
        "first": days_sorted[0],
        "last": days_sorted[-1],
        "last_fmt": fmt_date(days_sorted[-1]),
        "span": f"{fmt_date(days_sorted[0])} – {fmt_date(days_sorted[-1])}",
        "commits_by_day": cbd,
        "contribs": contribs,
        "obs": obs,
        "source": h.get("source", "api"),
        "trello": h.get("trello"),
    }
