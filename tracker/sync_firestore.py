"""Firestore-driven activity sync (app is the source of truth for team setup).

Reads the team list from Firestore (the teams you manage in the web app), fetches each
team's GitHub commit history + Trello card counts with your LOCAL tokens, and writes back
ONLY the measured `act` field (merge). It never touches the fields you edit in the app
(name, project, members, github, trello, status) or your review notes.

Env (same as collect.py / push_firestore.py):
  FIREBASE_UID, GOOGLE_APPLICATION_CREDENTIALS  (required)
  TRELLO_KEY, TRELLO_TOKEN                        (optional, for Trello counts)
  GITHUB_TOKEN                                    (optional, for private repos)

Run:  python sync_firestore.py
"""
import os, sys
import firebase_admin
from firebase_admin import credentials, firestore

from collect import fetch_commits, fetch_trello, slug_from_github, TODAY
from common import build_act, load_history

UID = os.environ.get("FIREBASE_UID", "").strip()
CRED = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
if not UID:
    sys.exit("Set FIREBASE_UID (your account UID from the web app).")
if not CRED or not os.path.exists(CRED):
    sys.exit("Set GOOGLE_APPLICATION_CREDENTIALS to your service-account key JSON path.")

firebase_admin.initialize_app(credentials.Certificate(CRED))
db = firestore.client()


def sync_team(course_ref, tdoc, local_hist):
    d = tdoc.to_dict() or {}
    github = d.get("github", "") or ""
    trello = d.get("trello", "") or ""
    contrib_notes = d.get("contrib_notes", {}) or {}
    old_act = d.get("act", {}) or {}
    name = d.get("team", tdoc.id)

    cbd, contributors = {}, []
    if github:
        try:
            cbd, contributors, _ = fetch_commits(slug_from_github(github))
        except Exception as e:
            print(f"  {name:26} github error: {e}")

    # Fall back to the local seed (e.g. private repos) if the live fetch came back empty.
    if not cbd:
        lh = (local_hist.get("teams") or {}).get(tdoc.id) or {}
        if lh.get("commits_by_day"):
            cbd = lh["commits_by_day"]; contributors = lh.get("contributors", [])

    trello_snap = old_act.get("trello")  # keep last-known if we can't fetch now
    if trello:
        snap, reason = fetch_trello(trello)
        if snap:
            snap["date"] = TODAY
            trello_snap = {"date": TODAY, **snap}
        elif reason != "no creds":
            print(f"  {name:26} trello skipped: {reason}")

    if not cbd and old_act.get("commits_by_day"):
        # Still nothing, but we already have data in Firestore — preserve it, refresh Trello.
        act = dict(old_act); act["trello"] = trello_snap or old_act.get("trello")
    else:
        act = build_act(cbd, contributors, trello_snap, contrib_notes, obs=old_act.get("obs", ""))

    course_ref.collection("teams").document(tdoc.id).set({"act": act}, merge=True)
    cnt = act["total"] if act.get("ok") else 0
    print(f"  {name:26} {cnt:>4} commits" + (f", trello {trello_snap['total_cards']} cards" if trello_snap else ""))


def main():
    courses = db.collection(f"users/{UID}/courses").stream()
    any_course = False
    for c in courses:
        any_course = True
        print(f"=== {c.to_dict().get('code', c.id)} ===")
        local_hist = load_history(c.id)   # fallback seed for repos we can't fetch live
        course_ref = db.document(f"users/{UID}/courses/{c.id}")
        for tdoc in course_ref.collection("teams").stream():
            sync_team(course_ref, tdoc, local_hist)
    if not any_course:
        print("No courses found in Firestore for this UID.")
    print("Done.")


if __name__ == "__main__":
    main()
