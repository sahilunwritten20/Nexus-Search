#!/bin/sh
# WP12-B9: release packaging. The review zip shipped untracked junk
# (nexus_search_frontier.db, __pycache__, .git internals) because it was
# built by zipping the working tree. Release artifacts come from
# `git archive` ONLY: tracked content, one clean zip, no local state.
# Usage:  scripts/package.sh [out-file.zip]
set -eu
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"
rev="$(git rev-parse --short HEAD)"
out="${1:-nexus-search-$rev.zip}"
git archive --format=zip --prefix="nexus-search/" -o "$out" HEAD
echo "wrote $out from HEAD $rev (tracked files only)"
