#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Build every hardware defconfig (boards/*/*/*/configs/hw-*), no runtime.
#
# Usage: tools/ci/build-hw.sh [build-dir]
#
# Fetches sources with repo_init.sh if external/ does not exist. Builds
# go to build-dir (default build/hw-ci), one directory per defconfig.
# A defconfig fails if one of its options is missing from the resulting
# .config (unknown symbol or unmet dependency: kconfig only warns).

set -eu

root="$(cd "$(dirname "$0")/../.." && pwd -P)"
cd "$root"

out="${1:-build/hw-ci}"
mkdir -p "$out"

if [ ! -d external ]; then
  sh repo_init.sh
fi

# check_config DEFCONFIG DOTCONFIG -- every option of the defconfig as is
# in .config, every "# CONFIG_X is not set" still not set

check_config() {
  rc=0
  while IFS= read -r line; do
    case "$line" in
      CONFIG_*=*)
        grep -qxF -- "$line" "$2" || { echo "dropped: $line"; rc=1; }
        ;;
      "# CONFIG_"*" is not set")
        sym="${line#\# }"
        sym="${sym% is not set}"
        if grep -q "^$sym=" "$2"; then
          echo "dropped: $line"
          rc=1
        fi
        ;;
    esac
  done < "$1"
  return "$rc"
}

status=0

for cfg in boards/*/*/*/configs/hw-*/defconfig; do
  dir="${cfg%/defconfig}"
  name=$(echo "${dir#boards/}" | sed 's|/configs/|-|; s|/|-|g')
  log="$out/$name.log"

  # Fresh build directory: CMake keeps an existing .config

  rm -rf "${out:?}/$name"

  if cmake -S external/nuttx -B "$out/$name" -G Ninja \
       -DBOARD_CONFIG="$root/$dir" > "$log" 2>&1 \
     && check_config "$cfg" "$out/$name/.config" >> "$log" 2>&1 \
     && cmake --build "$out/$name" >> "$log" 2>&1; then
    echo "hw-build: PASS $name"
  else
    echo "hw-build: FAIL $name (see $log)"
    tail -n 20 "$log"
    status=1
  fi
done

exit "$status"
