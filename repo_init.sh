#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# repo_init.sh -- fetch NuttX sources into external/ and link testlab apps.
#
# Usage: ./repo_init.sh [-f|--force] [-h|--help]
#
# Sources come from sources.env; each value can be overridden with an
# environment variable (NUTTX_REPO, NUTTX_REF, APPS_REPO, APPS_REF).

set -eu

FORCE=0

usage() {
  sed -n '3,9p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -f|--force) FORCE=1; shift ;;
    -h|--help)  usage 0 ;;
    *) echo "repo_init.sh: unknown option $1" >&2; usage 2 ;;
  esac
done

if [ ! -f sources.env ] || [ ! -f repo_init.sh ]; then
  echo "repo_init.sh: run from the nuttx-testlab root" >&2
  exit 1
fi

# shellcheck disable=SC1091
. ./sources.env

if [ -d external ]; then
  if [ "$FORCE" -eq 1 ]; then
    rm -rf external
  else
    echo "repo_init.sh: external/ exists, use --force" >&2
    exit 1
  fi
fi

mkdir -p external

# fetch REPO REF DIR -- shallow fetch that works for branch, tag or SHA

fetch() {
  git init -q "$3"
  git -C "$3" remote add origin "$1"
  git -C "$3" fetch -q --depth 1 origin "$2"
  git -C "$3" checkout -q FETCH_HEAD
}

fetch "$NUTTX_REPO" "$NUTTX_REF" external/nuttx
fetch "$APPS_REPO" "$APPS_REF" external/apps

ln -s "$(pwd -P)/apps" external/apps/external

{
  echo "nuttx $NUTTX_REPO $NUTTX_REF $(git -C external/nuttx rev-parse HEAD)"
  echo "apps $APPS_REPO $APPS_REF $(git -C external/apps rev-parse HEAD)"
} > external/sources.txt

cat external/sources.txt
