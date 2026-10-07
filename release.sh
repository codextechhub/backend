#!/usr/bin/env bash
# Promote what is live on staging to production, and step back if it goes wrong.
#
#   ./release.sh status             where main, staging and production stand
#   ./release.sh promote            staging's tip becomes production, tagged
#   ./release.sh rollback <tag>     production's files return to <tag>'s
#
# `production` only ever moves forward, so it is never reset or force-pushed:
# promote fast-forwards it, and rollback adds a commit whose files are exactly
# the tagged release's. Every release stays reachable by its tag, and the
# history shows what was live and when.
#
# Render deploys production by hand: this script moves the branch and tag, and
# the deploy is a separate click in the dashboard, at a time of your choosing.
#
# Code rolls back; the database does not. A migration that has run stays run,
# so rollback lists the migrations that arrived after the tag, and the old code
# has to tolerate them. Keep each migration backward-compatible for one release.
set -euo pipefail

REMOTE=origin
PROD=production
STAGING=staging

die() { echo "✗ $*" >&2; exit 1; }
confirm() {
  local answer
  read -r -p "$1 Type '$2' to continue: " answer
  [ "$answer" = "$2" ] || die "Cancelled."
}

require_clean_tree() {
  git diff --quiet && git diff --cached --quiet \
    || die "Uncommitted changes. Commit or stash them first."
}

fetch_all() { git fetch --quiet --tags "$REMOTE"; }

prod_exists() { git rev-parse --verify --quiet "$REMOTE/$PROD" >/dev/null; }

migrations_between() {
  git diff --name-only --diff-filter=A "$1" "$2" -- '*/migrations/0*.py' || true
}

next_tag() {
  local day n
  day=$(date +%Y.%m.%d)
  n=1
  while git rev-parse --verify --quiet "refs/tags/v$day.$n" >/dev/null; do
    n=$((n + 1))
  done
  echo "v$day.$n"
}

cmd_status() {
  fetch_all
  echo "main:       $(git rev-parse --short "$REMOTE/main")"
  echo "staging:    $(git rev-parse --short "$REMOTE/$STAGING")"
  if prod_exists; then
    echo "production: $(git rev-parse --short "$REMOTE/$PROD")  ($(git describe --tags --abbrev=0 "$REMOTE/$PROD" 2>/dev/null || echo untagged))"
    echo "Staging is $(git rev-list --count "$REMOTE/$PROD..$REMOTE/$STAGING") commit(s) ahead of production."
  else
    echo "production: not created yet (the first promote creates it)"
  fi
  echo "Recent releases:"
  git tag --list 'v*' --sort=-creatordate | head -5 | sed 's/^/  /'
}

cmd_promote() {
  require_clean_tree
  fetch_all
  local target base tag
  target=$(git rev-parse "$REMOTE/$STAGING")
  git merge-base --is-ancestor "$target" "$REMOTE/main" \
    || die "staging holds a commit that main does not. Staging is meant to be reset to main by deploy-staging.sh."

  if prod_exists; then
    base=$(git rev-parse "$REMOTE/$PROD")
    [ "$base" != "$target" ] || die "production already runs staging's tip."
    git merge-base --is-ancestor "$base" "$target" \
      || die "production is not an ancestor of staging, so this cannot fast-forward. Look at ./release.sh status."
    echo "→ Commits going to production:"
    git log --oneline "$base..$target"
    echo
    local migs
    migs=$(migrations_between "$base" "$target")
    if [ -n "$migs" ]; then
      echo "⚠ Migrations this release will run:"
      echo "$migs" | sed 's/^/    /'
    else
      echo "→ No new migrations."
    fi
  else
    echo "→ production does not exist yet. It will be created at staging's tip: $(git log --oneline -1 "$target")"
  fi

  echo
  echo "Before continuing: staging must have deployed this exact commit and be healthy"
  echo "(Render shows the deploy, and you have tried the change there)."
  confirm "Promote $(git rev-parse --short "$target") to production?" release

  tag=$(next_tag)
  git tag -a "$tag" "$target" -m "Production release $tag"
  git push "$REMOTE" "$target:refs/heads/$PROD" || { git tag -d "$tag" >/dev/null; die "push refused; no tag kept."; }
  git push "$REMOTE" "$tag"
  echo "✓ production is at $(git rev-parse --short "$target"), tagged $tag."
  echo "  Render does not deploy on its own: open the production web service, then the worker, and press Deploy."
}

cmd_rollback() {
  local tag=${1:-}
  [ -n "$tag" ] || die "Usage: ./release.sh rollback <tag>   (see ./release.sh status)"
  require_clean_tree
  fetch_all
  prod_exists || die "production does not exist yet."
  git rev-parse --verify --quiet "refs/tags/$tag" >/dev/null || die "No tag named $tag."
  git merge-base --is-ancestor "$tag" "$REMOTE/$PROD" || die "$tag is not part of production's history."

  local head migs
  head=$(git rev-parse "$REMOTE/$PROD")
  [ "$(git rev-parse "$tag^{tree}")" != "$(git rev-parse "$head^{tree}")" ] || die "production already has $tag's files."

  migs=$(migrations_between "$tag" "$head")
  if [ -n "$migs" ]; then
    echo "⚠ These migrations arrived after $tag and have already run on the database."
    echo "  The older code must work with them, or restore a database backup instead:"
    echo "$migs" | sed 's/^/    /'
  fi
  confirm "Return production's files to $tag?" rollback

  local new rb
  new=$(git commit-tree "$tag^{tree}" -p "$head" -m "Roll production back to $tag")
  rb="${tag}-rollback-$(date +%H%M%S)"
  git tag -a "$rb" "$new" -m "Production rolled back to $tag"
  git push "$REMOTE" "$new:refs/heads/$PROD" || { git tag -d "$rb" >/dev/null; die "push refused."; }
  git push "$REMOTE" "$rb"
  echo "✓ production now carries $tag's files as $(git rev-parse --short "$new") ($rb)."
  echo "  Press Deploy on the production web service and worker."
}

case "${1:-}" in
  status)   cmd_status ;;
  promote)  cmd_promote ;;
  rollback) shift; cmd_rollback "${1:-}" ;;
  *) die "Usage: ./release.sh status | promote | rollback <tag>" ;;
esac
