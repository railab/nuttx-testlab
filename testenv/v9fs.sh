#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# testenv/v9fs.sh -- host directories shared over virtio 9P (v9fs scenario).
#
# Usage: testenv/v9fs.sh {start|stop|status}
#
# QEMU exports build/v9fs/<target>-<scenario> (-fsdev local,path=...) with
# mount tag tlshare and refuses to start if the directory is missing, so it
# must exist before the node boots.  The tests empty it and fill it per
# session.

set -eu

root="$(cd "$(dirname "$0")/.." && pwd -P)"
share="$root/build/v9fs"

# One directory per scenario: <target>-<scenario> for ntfc/configs/*/v9fs*

shares() {
  for conf in "$root"/ntfc/configs/*/v9fs*; do
    [ -d "$conf" ] || continue
    scenario="${conf##*/}"
    target="${conf%/*}"
    echo "${target##*/}-$scenario"
  done
}

start() {
  for name in $(shares); do
    mkdir -p "$share/$name"
  done
}

stop() {
  rm -rf "$share"
}

status() {
  for name in $(shares); do
    if [ -d "$share/$name" ]; then
      echo "$share/$name"
    else
      echo "$share/$name: missing"
    fi
  done
}

case "${1:-}" in
  start)  start; status ;;
  stop)   stop ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
