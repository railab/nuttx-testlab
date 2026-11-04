#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Run one hardware session on the single board connected to this host.
#
# Usage: tools/hw/run.sh <board> <session> [testpath]
#
# <board> selects ntfc/manifest-hw-<board>.yaml, <session> one of its
# sessions; [testpath] narrows the session's test path. Sources come
# from sources.env (NUTTX_REPO, NUTTX_REF, ... overrides), fetched by
# repo_init.sh if external/ does not exist yet.
#
# Environment:
#   HW_PORT   board console (default: the only /dev/ttyACM*)
#   HW_PROBE  debug probe serial (default: USB serial of HW_PORT)

set -eu

usage() {
  sed -n '4,15p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

die() {
  echo "hw: $*" >&2
  exit 1
}

[ $# -eq 2 ] || [ $# -eq 3 ] || usage

board="$1"
session="$2"
testpath="${3:-}"

root="$(cd "$(dirname "$0")/../.." && pwd -P)"
cd "$root"

manifest="ntfc/manifest-hw-$board.yaml"
if [ ! -f "$manifest" ]; then
  boards=""
  for m in ntfc/manifest-hw-*.yaml; do
    m="${m#ntfc/manifest-hw-}"
    boards="$boards ${m%.yaml}"
  done
  die "no manifest for board '$board', boards:$boards"
fi

# Sources: fetch once; refuse to run against external/ fetched from other
# sources than the ones requested now

# shellcheck disable=SC1091
. ./sources.env

if [ ! -d external ]; then
  sh repo_init.sh
fi

check_source() {
  awk -v n="$1" -v r="$2" -v f="$3" \
    '$1 == n && $2 == r && $3 == f { ok = 1 } END { exit !ok }' \
    external/sources.txt 2>/dev/null \
    || die "external/ was not fetched from $1 $2 $3" \
      "(see external/sources.txt), run ./repo_init.sh --force"
}

check_source nuttx "$NUTTX_REPO" "$NUTTX_REF"
check_source apps "$APPS_REPO" "$APPS_REF"
check_source ntfc "$NTFC_REPO" "$NTFC_REF"

# Host Python: .venv if present

if [ -x .venv/bin/python ]; then
  python=.venv/bin/python
else
  python=python3
fi

# Session lookup: confpath, testpath and modules, one per line

sel=$("$python" - "$manifest" "$session" <<'PYEOF'
import sys

import yaml

manifest, name = sys.argv[1], sys.argv[2]
sessions = yaml.safe_load(open(manifest))["sessions"]
for s in sessions:
    if s["name"] == name:
        print(s["confpath"])
        print(s["testpath"])
        print(s.get("modules", ""))
        sys.exit(0)
print("hw: unknown session %s, valid: %s"
      % (name, " ".join(s["name"] for s in sessions)), file=sys.stderr)
sys.exit(1)
PYEOF
) || exit 2

template=$(echo "$sel" | sed -n 1p)
[ -n "$testpath" ] || testpath=$(echo "$sel" | sed -n 2p)
modules=$(echo "$sel" | sed -n 3p)

# Console port: HW_PORT or the only /dev/ttyACM*

port="${HW_PORT:-}"
if [ -z "$port" ]; then
  set -- /dev/ttyACM*
  if [ ! -e "$1" ]; then
    die "no /dev/ttyACM* found, connect the board or set HW_PORT"
  fi
  if [ $# -ne 1 ]; then
    die "more than one /dev/ttyACM* ($*), connect one board or set HW_PORT"
  fi
  port="$1"
fi
[ -e "$port" ] || die "$port does not exist"
port=$(readlink -f "$port")
[ -c "$port" ] || die "$port is not a character device"

# Probe serial: HW_PROBE or the USB serial number of the console's
# debug probe (ST-LINK: as is; J-Link: without leading zeros)

probe="${HW_PROBE:-}"
if [ -z "$probe" ]; then
  usbdev=$(readlink -f "/sys/class/tty/${port##*/}/device/.." || true)
  vid=$(cat "$usbdev/idVendor" 2>/dev/null || true)
  serial=$(cat "$usbdev/serial" 2>/dev/null || true)
  case "$vid" in
    0483) probe="$serial" ;;
    1366) probe=$(echo "$serial" | sed 's/^0*//') ;;
    *) die "$port is not on an ST-LINK or J-Link probe, set HW_PROBE" ;;
  esac
  [ -n "$probe" ] || die "no USB serial number for $port, set HW_PROBE"
fi

# Render the config template

outdir="result/hw/$board/$session"
mkdir -p "$outdir"
sed -e "s|@PORT@|$port|g" -e "s|@PROBE@|$probe|g" "$template" \
  > "$outdir/config.yaml"

# NTFC must be the one fetched into external/nuttx-ntfc

ntfc_src=$("$python" -c 'import ntfc, os; print(os.path.realpath(ntfc.__file__))' \
  2>/dev/null) || ntfc_src=""
case "$ntfc_src" in
  "$root"/external/nuttx-ntfc/*) ;;
  *)
    die "NTFC from external/nuttx-ntfc not installed, run:" \
      "python3 -m venv .venv && .venv/bin/pip install" \
      "-r ntfc/requirements.txt -e ./external/nuttx-ntfc"
    ;;
esac

echo "hw: $board $session port=$port probe=$probe testpath=$testpath" \
  "modules=$modules"
cat external/sources.txt

exec "$python" -m ntfc test --confpath "$outdir/config.yaml" \
  --testpath "$testpath" ${modules:+--modules "$modules"} --flash
