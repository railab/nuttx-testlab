#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Print a markdown summary of a result directory (for GITHUB_STEP_SUMMARY).
#
# Usage: tools/ci/summary.sh <result-dir> <title>

set -eu

dir="$1"
title="$2"

echo "## $title"
echo
if [ -f "$dir/sources.txt" ]; then
  echo "| source | repo | ref | sha |"
  echo "|---|---|---|---|"
  while read -r n r f s; do
    echo "| $n | $r | $f | \`$s\` |"
  done < "$dir/sources.txt"
  echo
fi

# Manifest runs write one report per session; single-session runs
# (TESTLAB_SESSION) write it directly under the timestamp directory.

found=0
for f in "$dir"/result/*/*/report/result_summary.txt; do
  [ -f "$f" ] || continue
  found=1
  echo '```'
  cat "$f"
  echo '```'
done

if [ "$found" -eq 0 ]; then
  for f in "$dir"/result/*/report/result_summary.txt; do
    [ -f "$f" ] || continue
    echo '```'
    cat "$f"
    echo '```'
  done
fi
