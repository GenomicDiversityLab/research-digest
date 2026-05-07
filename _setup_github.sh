#!/bin/bash
# One-time GitHub setup for research-digest.
# Run this from YOUR MAC TERMINAL (not from Cowork bash):
#
#   cd "/Users/jiny/Documents/Claude/Projects/Research digest"
#   bash _setup_github.sh
#
# Prerequisite: empty repo created at https://github.com/yoojinha/research-digest
# (just go to github.com → "New repository" → name: research-digest, Public,
#  do NOT initialize with README/.gitignore/license)

set -e
cd "$(dirname "$0")"

echo "==> Cleaning any stale git state from sandbox..."
rm -f .git/index.lock

echo "==> Ensuring git is initialized on main..."
if [ ! -d .git ]; then
    git init -b main
else
    git symbolic-ref HEAD refs/heads/main 2>/dev/null || true
fi

git config user.email "yoojinha@hanyang.ac.kr"
git config user.name "Yoojin Ha"

echo "==> Adding files and making initial commit..."
git add -A
git diff --cached --stat | tail -5
git commit -m "Initial commit — daily Europe PMC paper digest"

echo "==> Adding GitHub remote (SSH)..."
git remote remove origin 2>/dev/null || true
git remote add origin git@github.com:yoojinha/research-digest.git

echo "==> Pushing to GitHub..."
git push -u origin main

echo
echo "✅ Done. Now enable GitHub Pages:"
echo "  1. Open https://github.com/yoojinha/research-digest/settings/pages"
echo "  2. Source: Deploy from a branch"
echo "  3. Branch: main · Folder: / (root)"
echo "  4. Save"
echo
echo "Site will be live within ~1 minute at:"
echo "  https://yoojinha.github.io/research-digest/"
