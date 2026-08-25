"""One-time migration: move the single hardcoded course into the per-course layout.

Safe by design: it BACKS UP and COPIES; it never deletes or moves originals.
  - backups/history.json.bak, backups/teams_data.py.bak   (safety copies)
  - courses/pro100.json        (course config + teams, generated from teams_data.py)
  - data/pro100.history.json   (copy of the existing history.json — the running log)
  - courses.json               (registry of active course files)

Run once:  python migrate_to_courses.py
"""
import json, os, shutil
from teams_data import TEAMS, REPORT_DATE

HERE = os.path.dirname(os.path.abspath(__file__))
for d in ("courses", "data", "backups"):
    os.makedirs(os.path.join(HERE, d), exist_ok=True)

# 1. Back up originals (copy2 preserves timestamps; never deletes)
for f in ("history.json", "teams_data.py"):
    src = os.path.join(HERE, f)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(HERE, "backups", f + ".bak"))
        print("backed up", f, "-> backups/" + f + ".bak")

# 2. Write the course config from the current data
course = {
    "id": "pro100",
    "code": "PRO100",
    "name": "PRO100 – Introductory Software Projects",
    "term": "",
    "report_date": REPORT_DATE,
    "output_basename": "PRO100_Team_Tracker",
    "teams": TEAMS,
}
cpath = os.path.join(HERE, "courses", "pro100.json")
with open(cpath, "w", encoding="utf-8") as f:
    json.dump(course, f, indent=2, ensure_ascii=False)
print("wrote courses/pro100.json  (%d teams)" % len(TEAMS))

# 3. Copy the running log into the per-course data folder (do NOT delete the original)
hp = os.path.join(HERE, "history.json")
if os.path.exists(hp):
    shutil.copy2(hp, os.path.join(HERE, "data", "pro100.history.json"))
    print("copied history.json -> data/pro100.history.json")
else:
    print("NOTE: no history.json found to copy")

# 4. Registry of active courses
with open(os.path.join(HERE, "courses.json"), "w", encoding="utf-8") as f:
    json.dump({"courses": ["courses/pro100.json"]}, f, indent=2)
print("wrote courses.json")
print("\nMigration complete. Originals preserved in backups/.")
