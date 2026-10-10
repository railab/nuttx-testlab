#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# testenv/modbus-rtu.sh -- host serial line for the modbus-rtu scenario.
#
# Usage: testenv/modbus-rtu.sh {start|stop|status}
#
# socat joins two host pseudoterminals back to back, like a null-modem
# cable: the target opens TL_MB_NODE (sim: CONFIG_SIM_UART0_NAME, QEMU:
# -chardev serial,path=...), the tests open TL_MB_HOST.

set -eu

TL_MB_NODE="${TL_MB_NODE:-/dev/ttyTL0}"
TL_MB_HOST="${TL_MB_HOST:-/dev/ttyTL1}"
TL_MB_PIDFILE="${TL_MB_PIDFILE:-/tmp/tl-modbus-rtu.pid}"

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

run() {
  if [ -n "$SUDO" ]; then
    "$SUDO" "$@"
  else
    "$@"
  fi
}

running() {
  [ -f "$TL_MB_PIDFILE" ] && kill -0 "$(cat "$TL_MB_PIDFILE")" 2>/dev/null
}

start() {
  running && return 0

  # shellcheck disable=SC2016 # expanded by the inner shell
  run sh -c 'socat pty,link="$1",rawer,perm=0666 \
    pty,link="$2",rawer,perm=0666 </dev/null >/dev/null 2>&1 &
    echo $! > "$3"' sh "$TL_MB_NODE" "$TL_MB_HOST" "$TL_MB_PIDFILE"

  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -e "$TL_MB_NODE" ] && [ -e "$TL_MB_HOST" ] && return 0
    sleep 0.5
  done

  echo "modbus-rtu: socat did not create $TL_MB_NODE/$TL_MB_HOST" >&2
  return 1
}

stop() {
  if running; then
    run kill "$(cat "$TL_MB_PIDFILE")" 2>/dev/null || true
  fi

  run rm -f "$TL_MB_PIDFILE" "$TL_MB_NODE" "$TL_MB_HOST"
}

status() {
  if running; then
    echo "$TL_MB_NODE <-> $TL_MB_HOST"
  else
    echo "modbus-rtu: socat not running"
  fi
}

case "${1:-}" in
  start)  start; status ;;
  stop)   stop ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
