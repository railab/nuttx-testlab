#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# List attached ST-LINK and J-Link debug probes (read-only, from sysfs).
#
# Usage: tools/hw/probes.sh
#
# Prints one line per probe: type, HW_PROBE serial, console tty(s).

set -eu

found=0

for dev in /sys/bus/usb/devices/*; do
  [ -f "$dev/idVendor" ] || continue

  case "$(cat "$dev/idVendor")" in
    0483) type=st-link ;;
    1366) type=j-link ;;
    *) continue ;;
  esac

  serial=$(cat "$dev/serial" 2>/dev/null || echo "?")
  if [ "$type" = j-link ]; then
    serial=$(echo "$serial" | sed 's/^0*//')
  fi

  ttys=""
  for tty in "$dev"/*/tty/tty*; do
    [ -e "$tty" ] && ttys="$ttys /dev/${tty##*/}"
  done

  echo "$type serial=$serial product=$(cat "$dev/product" 2>/dev/null || echo "?") tty:${ttys:- none}"
  found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
  echo "no ST-LINK or J-Link probe found"
fi
