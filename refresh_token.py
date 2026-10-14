"""Refreshes the long-lived Instagram token (valid 60 days) and prints the new one.
The GitHub workflow saves it back into the IG_TOKEN secret."""
import os
import sys

import requests

token = os.environ["IG_TOKEN"].strip()
r = requests.get("https://graph.instagram.com/refresh_access_token",
                 params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=60)
j = r.json()
if "access_token" not in j:
    print("Refresh failed:", j.get("error", j), file=sys.stderr)
    sys.exit(1)
days = int(j.get("expires_in", 0)) // 86400
print(f"Token refreshed, valid for ~{days} days.", file=sys.stderr)
with open(os.environ["GITHUB_OUTPUT"], "a") as f:
    f.write(f"token={j['access_token']}\n")
