#!/usr/bin/env bash
# Refresh a staging checkout only when it can fast-forward its configured upstream.
set -euo pipefail

fail() {
  echo "staging-refresh: $1" >&2
  exit 2
}

if [ "$#" -ne 1 ]; then
  fail "expected one checkout path"
fi

checkout="$(cd "$1" 2>/dev/null && pwd -P)" || fail "checkout path is not accessible"
root="$(git -C "$checkout" rev-parse --show-toplevel 2>/dev/null)" || fail "checkout is not a Git worktree"
root="$(cd "$root" 2>/dev/null && pwd -P)" || fail "checkout root is not accessible"

if [ "$checkout" != "$root" ]; then
  fail "checkout path must be the Git worktree root"
fi

if [ -n "$(git -C "$root" status --porcelain=v1 --untracked-files=all 2>/dev/null)" ]; then
    fail "checkout has tracked or untracked changes"
fi

branch="$(git -C "$root" symbolic-ref --quiet --short HEAD 2>/dev/null)" || fail "checkout HEAD is detached"
remote="$(git -C "$root" config --get "branch.$branch.remote" 2>/dev/null)" || fail "branch has no configured upstream remote"
merge_ref="$(git -C "$root" config --get "branch.$branch.merge" 2>/dev/null)" || fail "branch has no configured upstream branch"

if [ -z "$remote" ] || [ -z "$merge_ref" ] || ! git -C "$root" remote get-url "$remote" >/dev/null 2>&1; then
    fail "configured upstream remote is unavailable"
fi

if ! git -C "$root" fetch --prune "$remote" >/dev/null 2>&1; then
    fail "fetch failed"
fi

upstream_sha="$(git -C "$root" rev-parse --verify --quiet '@{upstream}^{commit}' 2>/dev/null)" || fail "configured upstream could not be resolved"

if ! git -C "$root" merge-base --is-ancestor HEAD "$upstream_sha" >/dev/null 2>&1; then
    fail "checkout HEAD is not an ancestor of its upstream"
fi

if ! git -C "$root" merge --ff-only "$upstream_sha" >/dev/null 2>&1; then
    fail "fast-forward failed"
fi

head_sha="$(git -C "$root" rev-parse --verify --quiet 'HEAD^{commit}' 2>/dev/null)" || fail "checkout HEAD could not be resolved"
upstream_sha="$(git -C "$root" rev-parse --verify --quiet '@{upstream}^{commit}' 2>/dev/null)" || fail "configured upstream could not be resolved"
if [ "$head_sha" != "$upstream_sha" ]; then
  fail "checkout HEAD does not match its upstream after refresh"
fi

printf 'staging-refresh: root=%s head=%s\n' "$root" "$head_sha"
