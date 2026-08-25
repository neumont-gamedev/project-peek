"""Convenience runner: collect fresh data, then rebuild both the web page and the Word doc.

    python run_all.py

Equivalent to running, in order:
    python collect.py
    python build_site.py
    python build_doc.py
"""
import subprocess, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
for script in ["collect.py", "build_site.py", "build_doc.py"]:
    print(f"\n=== {script} ===")
    r = subprocess.run([sys.executable, os.path.join(HERE, script)], cwd=HERE)
    if r.returncode != 0:
        print(f"!! {script} failed (exit {r.returncode}) — stopping.")
        sys.exit(r.returncode)
print("\nDone. Outputs are in the Teams folder: PRO100_Team_Tracker.html and .docx")
print("Re-publish the web page by re-running Claude, or open the .html locally.")
