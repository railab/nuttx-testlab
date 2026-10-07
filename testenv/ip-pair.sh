#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# testenv/ip-pair.sh -- host bridge + TAPs for the ip-pair scenario.
#
# Usage: testenv/ip-pair.sh {start|stop|status}
#
# QEMU nodes attach to tl-tap0/tl-tap1; sim nodes create their own TAP and
# attach it to tl-br0 (CONFIG_SIM_NET_BRIDGE).  The host side of the bridge
# is 10.42.0.1/24 and acts as the Linux peer.

set -eu

TL_BRIDGE="${TL_BRIDGE:-tl-br0}"
TL_BRIDGE_IP="${TL_BRIDGE_IP:-10.42.0.1/24}"
TL_BRIDGE_MAC="${TL_BRIDGE_MAC:-52:54:00:2a:00:01}"
TL_TAPS="${TL_TAPS:-tl-tap0 tl-tap1}"

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
  if [ ! -e /dev/net/tun ]; then
    run mkdir -p /dev/net
    run mknod /dev/net/tun c 10 200
  fi

  ip link show "$TL_BRIDGE" >/dev/null 2>&1 ||
    run ip link add name "$TL_BRIDGE" type bridge
  # Fixed MAC: otherwise the bridge takes the lowest port MAC and changes
  # it whenever a sim node TAP joins or leaves, leaving stale ARP entries
  # for the host on the nodes.

  run ip link set "$TL_BRIDGE" address "$TL_BRIDGE_MAC"
  run ip addr replace "$TL_BRIDGE_IP" dev "$TL_BRIDGE"
  run ip link set "$TL_BRIDGE" up

  for tap in $TL_TAPS; do
    ip link show "$tap" >/dev/null 2>&1 ||
      run ip tuntap add dev "$tap" mode tap user "$(id -un)"
    run ip link set "$tap" master "$TL_BRIDGE"
    run ip link set "$tap" up
  done
}

stop() {
  for tap in $TL_TAPS; do
    run ip link del "$tap" 2>/dev/null || true
  done

  run ip link del "$TL_BRIDGE" 2>/dev/null || true
}

status() {
  ip -br addr show "$TL_BRIDGE" 2>/dev/null || echo "$TL_BRIDGE: missing"
  for tap in $TL_TAPS; do
    ip -br link show "$tap" 2>/dev/null || echo "$tap: missing"
  done
}

case "${1:-}" in
  start)  start; status ;;
  stop)   stop ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
