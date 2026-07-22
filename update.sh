#!/bin/zsh
# Rebuild data.json and publish it. Run by launchd every afternoon; safe to run
# by hand too. Never prompts - if git needs a credential it fails instead of
# hanging forever in the background.

set -eu

REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"

export GIT_TERMINAL_PROMPT=0
export SSH_ASKPASS=/usr/bin/false

echo "--- $(date '+%Y-%m-%d %H:%M:%S') ---"

/usr/bin/python3 "$REPO/maptap.py"

# Show the current standings so the log (and anyone running this by hand) can
# see who's winning without opening data.json.
/usr/bin/python3 - "$REPO/data.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
print("\n%s" % data["week"]["label"])
for p in data["players"]:
    medal = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}.get(p["rank"], "%2d." % p["rank"])
    print("  %s  %-9s %5d  (%dd)" % (medal, p["name"], p["total"], p["days_played"]))
print()
PY

if [ -z "$(git status --porcelain data.json)" ]; then
  echo "no change to data.json, nothing to publish"
  exit 0
fi

git add data.json
git -c user.name="maptap-bot" -c user.email="maptap-bot@localhost" \
    commit -m "Update leaderboard $(date '+%Y-%m-%d')"
git push origin HEAD

echo "published"
