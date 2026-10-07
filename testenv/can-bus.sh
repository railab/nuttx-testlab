#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# testenv/can-bus.sh -- host vcan interface for the can-bus scenario.
#
# Usage: testenv/can-bus.sh {start|stop|status}
#
# sim nodes bind the host vcan interface directly (CONFIG_SIM_CANDEV_SOCK);
# QEMU nodes attach via -object can-host-socketcan,if=can0. All processes
# and the host share the same vcan bus, which delivers every frame to every
# other socket on it.

set -eu

TL_CAN="${TL_CAN:-can0}"

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

run() {
  if [ -n "$SUDO" ]; then
    "$SUDO" "$@"
  else
    "$@"
  fi
}

start() {
  ip link show "$TL_CAN" >/dev/null 2>&1 ||
    run ip link add dev "$TL_CAN" type vcan
  run ip link set "$TL_CAN" up
}

stop() {
  run ip link del "$TL_CAN" 2>/dev/null || true
}

status() {
  ip -br link show "$TL_CAN" 2>/dev/null || echo "$TL_CAN: missing"
}

case "${1:-}" in
  start)  start; status ;;
  stop)   stop ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
