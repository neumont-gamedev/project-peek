"""Revoke (delete) the Trello token in TRELLO_TOKEN. Use this to kill an exposed token.

Set TRELLO_KEY and TRELLO_TOKEN to the token you want to revoke, then run:
    python revoke_token.py

After it succeeds, generate a NEW token from the Power-Up (it will be different now that
the old one is gone) and put that new token in secrets.local.ps1.
"""
import os, urllib.request, urllib.error

key = os.environ.get("TRELLO_KEY", "").strip()
token = os.environ.get("TRELLO_TOKEN", "").strip()

if not (key and token):
    raise SystemExit("Set TRELLO_KEY and TRELLO_TOKEN in this shell first (quoted).")

url = f"https://api.trello.com/1/tokens/{token}?key={key}&token={token}"
req = urllib.request.Request(url, method="DELETE", headers={"User-Agent": "pro100-tracker"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"Revoked. HTTP {r.status}. That token no longer works.")
except urllib.error.HTTPError as e:
    if e.code == 401:
        print("401 — token already invalid/revoked, or key/token mismatch. Nothing to do.")
    else:
        print(f"HTTP {e.code} {e.reason}")
except Exception as e:
    print("error:", e)
