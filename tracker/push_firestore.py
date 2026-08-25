"""Push course + team data (static info merged with collected activity) to Firestore.

This runs LOCALLY with the Firebase Admin SDK, so your API tokens never leave your
machine and no cloud scheduler is needed. It writes course/team docs; it never touches
the review-notes docs (those are edited in the web app).

Setup (one time):
  1. Firebase console -> Project settings -> Service accounts -> "Generate new private
     key". Save the JSON somewhere local, e.g. tracker/serviceAccountKey.json  (KEEP IT
     PRIVATE - never publish/commit it).
  2. Sign in to the web app once; it shows your account UID on the "no data" screen.

Run:
  # PowerShell, from the tracker folder:
  $env:GOOGLE_APPLICATION_CREDENTIALS = "C:\\...\\tracker\\serviceAccountKey.json"
  $env:FIREBASE_UID = "your-uid-from-the-app"
  python push_firestore.py

Writes to:  users/{uid}/courses/{courseId}            (course meta)
            users/{uid}/courses/{courseId}/teams/{id}  (team static + activity)
"""
import os, sys
import firebase_admin
from firebase_admin import credentials, firestore

from common import load_courses, load_history, team_activity

UID = os.environ.get("FIREBASE_UID", "").strip()
CRED = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

if not UID:
    sys.exit("Set FIREBASE_UID to your account UID (shown in the web app's 'no data' screen).")
if not CRED or not os.path.exists(CRED):
    sys.exit("Set GOOGLE_APPLICATION_CREDENTIALS to the path of your service-account key JSON.")

firebase_admin.initialize_app(credentials.Certificate(CRED))
db = firestore.client()


def team_doc(t, hist):
    return {
        "team": t["team"], "project": t["project"], "members": t["members"],
        "github": t["github"], "trello": t.get("trello", ""), "status": t["status"],
        "notes": t.get("notes", ""), "why": t.get("why", ""),
        "prop": t["prop"], "sprint": t.get("sprint"),
        "act": team_activity(t, hist),
    }


def main():
    for course in load_courses():
        cid = course["id"]
        hist = load_history(cid)
        db.document(f"users/{UID}/courses/{cid}").set({
            "code": course.get("code", cid), "name": course.get("name", ""),
            "term": course.get("term", ""), "report_date": course.get("report_date", ""),
            "updatedAt": firestore.SERVER_TIMESTAMP,
        }, merge=True)
        n = 0
        for t in course["teams"]:
            db.document(f"users/{UID}/courses/{cid}/teams/{t['id']}").set(team_doc(t, hist))
            n += 1
        print(f"pushed {course.get('code', cid)}: {n} teams -> users/{UID}/courses/{cid}")
    print("Done.")


if __name__ == "__main__":
    main()
