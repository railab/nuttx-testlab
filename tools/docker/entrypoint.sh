#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Container entrypoint: build and run one nuttx-testlab manifest.
#
# Usage: testlab-entrypoint <manifest-name>   (e.g. ci-sim)
#
# Optional environment:
#   TESTLAB_SESSION   run only this manifest session
#   TESTLAB_TESTPATH  narrow the session to this test path (needs session)

set -eu

name="${1:?manifest name required}"
session="${TESTLAB_SESSION:-}"
testpath="${TESTLAB_TESTPATH:-}"
manifest="ntfc/manifest-$name.yaml"

if [ -n "$testpath" ] && [ -z "$session" ]; then
  echo "testlab: TESTLAB_TESTPATH requires TESTLAB_SESSION" >&2
  exit 2
fi

rsync -a --exclude external --exclude build --exclude result \
  --exclude .venv "/src/" /work/
cd /work

[ -f "$manifest" ] || {
  echo "testlab: no such manifest: $manifest" >&2
  exit 2
}

sh repo_init.sh

python -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -r ntfc/requirements.txt
[ -z "${NTFC_PIP_SPEC:-}" ] || \
  pip install -q --force-reinstall --no-deps "$NTFC_PIP_SPEC"

# shellcheck disable=SC2317,SC2329 # invoked via trap
cleanup() {
  for envscript in testenv/*.sh; do
    if [ -f "$envscript" ]; then
      sh "$envscript" stop 2>/dev/null || true
    fi
  done

  mkdir -p /out
  cp -r result external/sources.txt /out/ 2>/dev/null || true

  # Hand results back to the invoking host user

  if [ -n "${TESTLAB_OWNER:-}" ]; then
    chown -R "$TESTLAB_OWNER" /out 2>/dev/null || true
  fi
}

trap cleanup EXIT

for envscript in testenv/*.sh; do
  if [ -f "$envscript" ]; then
    sh "$envscript" start
  fi
done

status=0

if [ -z "$session" ]; then
  python -m ntfc test --manifest "$manifest" || status=$?
else
  # Resolve the session's confpath/testpath from the manifest

  sel=$(python - "$manifest" "$session" <<'EOF'
import sys

import yaml

manifest, name = sys.argv[1], sys.argv[2]
sessions = yaml.safe_load(open(manifest))["sessions"]
for s in sessions:
    if s["name"] == name:
        print(s["confpath"], s["testpath"])
        sys.exit(0)
print("unknown session %s, valid: %s"
      % (name, " ".join(s["name"] for s in sessions)), file=sys.stderr)
sys.exit(1)
EOF
  ) || exit 2

  confpath="${sel% *}"
  [ -n "$testpath" ] || testpath="${sel#* }"
  python -m ntfc test --confpath "$confpath" --testpath "$testpath" \
    || status=$?
fi

exit "$status"
