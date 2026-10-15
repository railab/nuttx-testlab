#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# testenv/router.sh -- two host networks joined only by a NuttX router.
#
# Usage: testenv/router.sh {start|stop|status}
#
# Host A lives in network namespace tl-ra, host B in tl-rb, so the host
# kernel cannot route between them: A <-> B traffic must pass the node.
# Each namespace has a veth pair whose other end is a port of a bridge
# (tl-rbr0 for A, tl-rbr1 for B); the node's two NICs attach to the
# bridges through TAPs tl-rtap0 (A side) and tl-rtap1 (B side).
#
#   tl-ra: tl-ra0 10.51.0.1/24 fc00::1/112, default via the node
#   tl-rb: tl-rb0 10.52.0.1/24 fc00::1:1/112, A subnets via the node

set -eu

TR_NS="tl-ra tl-rb"

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

run() {
  if [ -n "$SUDO" ]; then
    "$SUDO" "$@"
  else
    "$@"
  fi
}

# side NS IDX ADDR ADDR6 NODE NODE6 PEER PEER6 -- one host network: host
# in namespace NS on bridge tl-rbrIDX, routes to PEER/PEER6 via the node

side() {
  ns=$1
  idx=$2
  addr=$3
  addr6=$4
  node=$5
  node6=$6
  peer=$7
  peer6=$8

  ip netns list | grep -qw "$ns" || run ip netns add "$ns"
  run ip netns exec "$ns" ip link set lo up

  ip link show "tl-rbr$idx" >/dev/null 2>&1 ||
    run ip link add name "tl-rbr$idx" type bridge
  run sysctl -qw "net.ipv6.conf.tl-rbr$idx.disable_ipv6=1"
  run ip link set "tl-rbr$idx" up

  if ! ip link show "tl-rv$idx" >/dev/null 2>&1; then
    run ip link add "tl-rv$idx" type veth peer name "${ns}0"
    run ip link set "${ns}0" netns "$ns"
  fi

  run sysctl -qw "net.ipv6.conf.tl-rv$idx.disable_ipv6=1"
  run ip link set "tl-rv$idx" master "tl-rbr$idx"
  run ip link set "tl-rv$idx" up

  ip link show "tl-rtap$idx" >/dev/null 2>&1 ||
    run ip tuntap add dev "tl-rtap$idx" mode tap user "$(id -un)"
  run sysctl -qw "net.ipv6.conf.tl-rtap$idx.disable_ipv6=1"
  run ip link set "tl-rtap$idx" master "tl-rbr$idx"
  run ip link set "tl-rtap$idx" up

  # IPv6 may be disabled by default (e.g. in a container); no DAD, so the
  # address is usable at once.

  run ip netns exec "$ns" sysctl -qw "net.ipv6.conf.${ns}0.disable_ipv6=0"
  run ip netns exec "$ns" ip addr replace "$addr" dev "${ns}0"
  run ip netns exec "$ns" ip -6 addr replace "$addr6" dev "${ns}0" nodad
  run ip netns exec "$ns" ip link set "${ns}0" up
  run ip netns exec "$ns" ip route replace "$peer" via "$node"
  run ip netns exec "$ns" ip -6 route replace "$peer6" via "$node6"
}

start() {
  if [ ! -e /dev/net/tun ]; then
    run mkdir -p /dev/net
    run mknod /dev/net/tun c 10 200
  fi

  side tl-ra 0 10.51.0.1/24 fc00::1/112 10.51.0.10 fc00::10 \
    default default
  side tl-rb 1 10.52.0.1/24 fc00::1:1/112 10.52.0.10 fc00::1:10 \
    10.51.0.0/24 fc00::/112
}

stop() {
  for idx in 0 1; do
    run ip link del "tl-rtap$idx" 2>/dev/null || true
    run ip link del "tl-rv$idx" 2>/dev/null || true
    run ip link del "tl-rbr$idx" 2>/dev/null || true
  done

  for ns in $TR_NS; do
    run ip netns del "$ns" 2>/dev/null || true
  done
}

status() {
  for ns in $TR_NS; do
    if ip netns list | grep -qw "$ns"; then
      echo "$ns:"
      ip netns exec "$ns" ip -br addr show "${ns}0" 2>/dev/null || true
    else
      echo "$ns: missing"
    fi
  done

  for idx in 0 1; do
    ip -br link show "tl-rtap$idx" 2>/dev/null || echo "tl-rtap$idx: missing"
  done
}

case "${1:-}" in
  start)  start; status ;;
  stop)   stop ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
