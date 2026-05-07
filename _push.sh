#!/bin/bash
# Idempotent commit + push helper for the daily digest.
# Reads PAT from .pat (this directory) and pushes to GitHub.
# Safe to call from the Cowork bash sandbox or from a Mac terminal.
#
# Usage:
#   bash _push.sh "commit message"
#
# If .pat is missing, prints a helpful message and exits 0 (so the daily
# task doesn't fail just because the token isn't configured yet).

set -e
cd "$(dirname "$0")"

MSG="${1:-Daily digest update $(date -Iseconds)}"

# Stage everything that changed (gitignored files like .pat are excluded by .gitignore)
git add -A

# Bail if nothing changed (no-op commit)
if git diff --cached --quiet; then
    echo "no changes to commit"
    exit 0
fi

git -c user.email="digest-bot@research-digest" -c user.name="Digest Bot" commit -m "$MSG"

# Push: prefer .pat if present (sandbox path), fall back to whatever is configured
if [ -f .pat ]; then
    PAT=$(tr -d '[:space:]' < .pat)
    REMOTE=$(git remote get-url origin)
    # Strip any embedded token, then re-inject (idempotent)
    CLEAN=$(echo "$REMOTE" | sed -E 's#https://[^@]+@github.com#https://github.com#')
    AUTHED=$(echo "$CLEAN" | sed "s#https://github.com#https://x-access-token:${PAT}@github.com#")
    git push "$AUTHED" main 2>&1 | sed "s/${PAT}/[REDACTED]/g"
else
    echo "(.pat not found — skipping push. Run 'git push' manually from your Mac, or drop a PAT into ./.pat to enable auto-push from Cowork.)"
fi
