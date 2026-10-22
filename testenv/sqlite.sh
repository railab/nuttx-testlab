#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# testenv/sqlite.sh -- host share and disk images (sqlite scenario).
#
# Usage: testenv/sqlite.sh {start|stop|status}
#
# For each ntfc/configs/<target>/sqlite*: build/sqlite/<target>-<scenario>/
# with share/ (exported over virtio 9P, tag tlshare), disk0.img (128 MiB) and
# disk1.img (8 MiB), two sparse raw images for virtio-blk.  QEMU refuses
# to start if they are missing.  The tests format the disks.

set -eu

root="$(cd "$(dirname "$0")/.." && pwd -P)"
base="$root/build/sqlite"

names() {
  for conf in "$root"/ntfc/configs/*/sqlite*; do
    [ -d "$conf" ] || continue
    scenario="${conf##*/}"
    target="${conf%/*}"
    echo "${target##*/}-$scenario"
  done
}

start() {
  for name in $(names); do
    mkdir -p "$base/$name/share"
    truncate -s 128M "$base/$name/disk0.img"
    truncate -s 8M "$base/$name/disk1.img"
  done
}

stop() {
  rm -rf "$base"
}

status() {
  for name in $(names); do
    for f in share disk0.img disk1.img; do
      if [ -e "$base/$name/$f" ]; then
        echo "$base/$name/$f"
      else
        echo "$base/$name/$f: missing"
      fi
    done
  done
}

case "${1:-}" in
  start)  start; status ;;
  stop)   stop ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
