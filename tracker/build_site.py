"""Generate a web dashboard per course from courses/*.json + data/*.history.json.

Run:  python build_site.py
Writes <output_basename>.html to the parent Teams folder for each course.
"""
import json, os
from common import load_courses, load_history, team_activity

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.abspath(os.path.join(HERE, ".."))
TEMPLATE = os.path.join(HERE, "template.html")


def build_course(course, template):
    hist = load_history(course["id"])
    teams_out = []
    for t in course["teams"]:
        teams_out.append({
            "id": t["id"], "team": t["team"], "project": t["project"], "members": t["members"],
            "github": t["github"], "trello": t.get("trello", ""), "status": t["status"],
            "notes": t.get("notes", ""), "why": t.get("why", ""),
            "prop": t["prop"], "sprint": t.get("sprint"),
            "act": team_activity(t, hist),
        })
    course_meta = {"code": course.get("code", course["id"]),
                   "name": course.get("name", ""), "term": course.get("term", "")}

    html = template
    html = html.replace("__TEAMS__", json.dumps(teams_out, ensure_ascii=False))
    html = html.replace("__COURSE__", json.dumps(course_meta, ensure_ascii=False))
    html = html.replace("__COURSE_CODE__", course_meta["code"])
    html = html.replace("__REPORT_DATE__", course.get("report_date", ""))

    out = os.path.join(OUTDIR, course["output_basename"] + ".html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("Wrote", out)


def build():
    with open(TEMPLATE, "r", encoding="utf-8") as f:
        template = f.read()
    for course in load_courses():
        build_course(course, template)


if __name__ == "__main__":
    build()
