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

# Only what GitHub has, and only once CI passed on it. Production waits for
# green CI on the host; staging is pushed from here, so this is its gate.
echo "→ Checking main matches GitHub..."
git fetch --quiet origin main
LOCAL=$(git rev-parse main)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" != "$REMOTE" ]; then
  if git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
    echo "✗ main is behind GitHub. Pull first (git pull --ff-only)."
  elif git merge-base --is-ancestor "$REMOTE" "$LOCAL"; then
    echo "✗ main has commits GitHub does not. Push them and let CI pass first."
  else
    echo "✗ main and GitHub's main have gone separate ways. Reconcile them first."
  fi
  exit 1
fi

if command -v gh >/dev/null 2>&1; then
  RUN=$(gh run list --commit "$LOCAL" --branch main --limit 1 \
    --json status,conclusion --jq '.[0] | "\(.status) \(.conclusion)"' 2>/dev/null || true)
  case "$RUN" in
    "completed success") echo "→ CI passed on ${LOCAL:0:8}." ;;
    completed*) echo "✗ CI failed on ${LOCAL:0:8} (${RUN#completed }). Fix main first."; exit 1 ;;
    "") echo "! No CI run found for ${LOCAL:0:8}; deploying without that check." ;;
    *) echo "✗ CI is still running on ${LOCAL:0:8}. Deploy once it passes."; exit 1 ;;
  esac
else
  echo "! The GitHub CLI (gh) is not installed, so CI was not checked."
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
