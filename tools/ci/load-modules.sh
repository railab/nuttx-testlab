#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Load the host kernel modules the CAN sessions need (can, can_raw, vcan).
# Only if one is missing, install linux-modules-extra for the running
# kernel and retry. Run as root.
#
# Usage: tools/ci/load-can-modules.sh

set -eu

modules="can can_raw vcan"
kernel="$(uname -r)"

# load -- modprobe every module; $missing names the ones that failed

load() {
  missing=""
  for m in $modules; do
    modprobe "$m" || missing="$missing $m"
  done
  [ -z "$missing" ]
}

if load; then
  exit 0
fi

echo "load-can-modules: missing:$missing," \
     "installing linux-modules-extra-$kernel"
apt-get update
apt-get install -y "linux-modules-extra-$kernel" || true

if ! load; then
  echo "load-can-modules: kernel $kernel has no module(s):$missing" \
       "(not in linux-modules or linux-modules-extra-$kernel)" >&2
  exit 1
fi
