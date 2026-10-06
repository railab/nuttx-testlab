#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Run a nuttx-testlab manifest inside the Docker image.
#
# Usage: tools/docker/run.sh [--image IMG] <manifest-name>
#
# Passes NUTTX_REPO/NUTTX_REF/APPS_REPO/APPS_REF, NTFC_PIP_SPEC,
# TESTLAB_SESSION and TESTLAB_TESTPATH through to the container.

set -eu

image="${TESTLAB_IMAGE:-nuttx-testlab}"

if [ "${1:-}" = "--image" ]; then
  image="$2"
  shift 2
fi

name="${1:?manifest name required}"
root="$(git rev-parse --show-toplevel)"
mkdir -p "$root/result"

exec docker run --rm --init --privileged \
  -e NUTTX_REPO -e NUTTX_REF -e APPS_REPO -e APPS_REF -e NTFC_PIP_SPEC -e TESTLAB_SESSION -e TESTLAB_TESTPATH \
  -e "TESTLAB_OWNER=$(id -u):$(id -g)" \
  -v "$root:/src:ro" -v "$root/result:/out" \
  "$image" "$name"
