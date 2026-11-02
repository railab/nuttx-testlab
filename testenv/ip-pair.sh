#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# testenv/ip-pair.sh -- host bridge + TAPs for the ip-pair scenario.
#
# Usage: testenv/ip-pair.sh {start|stop|status}
#
# QEMU nodes attach to tl-tap0/tl-tap1; sim nodes create their own TAP and
# attach it to tl-br0 (CONFIG_SIM_NET_BRIDGE).  The host side of the bridge
# is 10.42.0.1/24 and fc00::1/112 and acts as the Linux peer.

set -eu

TL_BRIDGE="${TL_BRIDGE:-tl-br0}"
TL_BRIDGE_IP="${TL_BRIDGE_IP:-10.42.0.1/24}"
TL_BRIDGE_MAC="${TL_BRIDGE_MAC:-52:54:00:2a:00:01}"
TL_BRIDGE_IP6="${TL_BRIDGE_IP6:-fc00::1/112}"
TL_TAPS="${TL_TAPS:-tl-tap0 tl-tap1}"
# veth pair for host daemons that need software TX timestamps (ptp4l):
# the bridge device itself reports none.  tl-ptp1 is a bridge port,
# tl-ptp0 the daemon's interface; its address is off the node subnet
# and it never answers ARP/ND, so the nodes keep seeing one host.
TL_PTP_VETH="${TL_PTP_VETH:-tl-ptp0}"
TL_PTP_PEER="${TL_PTP_PEER:-tl-ptp1}"
TL_PTP_MAC="${TL_PTP_MAC:-52:54:00:2a:00:02}"
TL_PTP_IP="${TL_PTP_IP:-10.42.1.2/24}"

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

  # IPv6 may be disabled by default (e.g. in a container); no DAD, so the
  # address is usable at once.

  run sysctl -qw "net.ipv6.conf.$TL_BRIDGE.disable_ipv6=0"
  run ip -6 addr replace "$TL_BRIDGE_IP6" dev "$TL_BRIDGE" nodad

  # IGMP/MLD snooping with short timers (centiseconds): when a test turns
  # the querier on, General Query every 5 s, membership expires after 15 s
  # without a report.

  run ip link set "$TL_BRIDGE" type bridge mcast_snooping 1 \
    mcast_querier 0 mcast_query_interval 500 \
    mcast_query_response_interval 100 mcast_membership_interval 1500 \
    mcast_last_member_interval 100 mcast_startup_query_interval 300
  run ip link set "$TL_BRIDGE" up

  for tap in $TL_TAPS; do
    ip link show "$tap" >/dev/null 2>&1 ||
      run ip tuntap add dev "$tap" mode tap user "$(id -un)"
    run ip link set "$tap" master "$TL_BRIDGE"
    run ip link set "$tap" up
  done

  ip link show "$TL_PTP_VETH" >/dev/null 2>&1 ||
    run ip link add "$TL_PTP_VETH" address "$TL_PTP_MAC" type veth \
      peer name "$TL_PTP_PEER"
  run ip link set "$TL_PTP_PEER" master "$TL_BRIDGE"
  run sysctl -qw "net.ipv4.conf.$TL_PTP_VETH.arp_ignore=1" \
    "net.ipv4.conf.$TL_PTP_VETH.arp_announce=2" \
    "net.ipv6.conf.$TL_PTP_VETH.disable_ipv6=1"
  run ip addr replace "$TL_PTP_IP" dev "$TL_PTP_VETH"
  run ip link set "$TL_PTP_PEER" up
  run ip link set "$TL_PTP_VETH" up
}

stop() {
  run ip link del "$TL_PTP_VETH" 2>/dev/null || true

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
  ip -br link show "$TL_PTP_VETH" 2>/dev/null || echo "$TL_PTP_VETH: missing"
}

case "${1:-}" in
  start)  start; status ;;
  stop)   stop ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
