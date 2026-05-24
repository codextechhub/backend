#!/usr/bin/env bash
set -e

CURRENT=$(git rev-parse --abbrev-ref HEAD)

echo "→ Current branch: $CURRENT"

# Make sure we're up to date on main
if [ "$CURRENT" != "main" ]; then
  echo "✗ Must be on main before deploying to staging. Switch to main first."
  exit 1
fi

# Check for uncommitted changes
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "✗ You have uncommitted changes. Commit or stash them first."
  exit 1
fi

echo "→ Switching to staging..."
git checkout staging

echo "→ Resetting staging to main..."
git reset --hard main

echo "→ Force-pushing staging to origin..."
git push origin staging --force

echo "→ Switching back to main..."
git checkout main

echo "✓ Done. Staging is now synced with main and pushed. Render will deploy automatically."
