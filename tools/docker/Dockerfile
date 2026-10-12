# SPDX-License-Identifier: Apache-2.0
#
# nuttx-testlab Docker image.
#
# Host package list is derived from railab/dawn's
# tools/docker/ubuntu-container.Dockerfile (Dawn-specific bits dropped:
# DAWN_* env, Dawn install scripts, clang-format/llvm.sh, cppcheck).
# Cross toolchain recipes (versions/URLs) are taken from the upstream
# Apache NuttX CI image build, apache/nuttx tools/ci/docker/linux/Dockerfile
# (nuttx-toolchain-arm64 / nuttx-toolchain-riscv stages).

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG ARM64_TC=13.2.Rel1
ARG RISCV_TC=14.3.0-1
ARG RISCV_TLS_TC=2026.08.27
ARG LINUX_TAG=v6.12

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bison \
        build-essential \
        ca-certificates \
        can-utils \
        cmake \
        curl \
        flex \
        g++-14 \
        gcc-14 \
        genromfs \
        git \
        gperf \
        iperf \
        iproute2 \
        iputils-ping \
        unzip \
        kmod \
        make \
        mosquitto \
        mosquitto-clients \
        ninja-build \
        pkg-config \
        python-is-python3 \
        python3 \
        python3-pip \
        python3-venv \
        qemu-system-arm \
        qemu-system-misc \
        qemu-system-x86 \
        rsync \
        socat \
        xxd \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# ARM64 toolchain: version/URL per apache/nuttx
# tools/ci/docker/linux/Dockerfile (nuttx-toolchain-arm64 stage).
RUN curl -fsSL "https://developer.arm.com/-/media/Files/downloads/gnu/${ARM64_TC}/binrel/arm-gnu-toolchain-${ARM64_TC}-x86_64-aarch64-none-elf.tar.xz" \
      | tar -xJ -C /opt \
    && ln -s /opt/arm-gnu-toolchain-${ARM64_TC}-x86_64-aarch64-none-elf /opt/aarch64-none-elf

# RISC-V toolchain: version/URL per apache/nuttx
# tools/ci/docker/linux/Dockerfile (nuttx-toolchain-riscv stage). NuttX's
# arch/risc-v/src/cmake/Toolchain.cmake prefers riscv-none-elf-gcc when it
# is on PATH over a riscv64-unknown-elf multilib, which is missing libm.a
# for the rv32ima target used by rv-virt:nsh.
RUN curl -fsSL "https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v${RISCV_TC}/xpack-riscv-none-elf-gcc-${RISCV_TC}-linux-x64.tar.gz" \
      | tar -xz -C /opt \
    && ln -s /opt/xpack-riscv-none-elf-gcc-${RISCV_TC} /opt/riscv-none-elf

# RISC-V toolchain with native TLS (riscv-collab/riscv-gnu-toolchain,
# riscv32-unknown-elf, newlib, rv32gc/ilp32d, --enable-tls). The xPack one
# is built with --disable-tls, so __thread becomes libgcc's single-thread
# emutls: one instance shared by all threads. Used by scenarios that need
# __thread (CONFIG_SCHED_THREAD_LOCAL); unused tools are dropped and the
# host binaries stripped to keep the image small.
RUN curl -fsSL "https://github.com/riscv-collab/riscv-gnu-toolchain/releases/download/${RISCV_TLS_TC}/riscv32-elf-ubuntu-24.04-gcc.tar.xz" \
      | tar -xJ -C /opt \
    && mv /opt/riscv /opt/riscv32-elf \
    && cd /opt/riscv32-elf \
    && rm -rf share bin/qemu-* bin/*-gdb* bin/*-gstack bin/*-lto-dump \
       bin/dejagnu bin/runtest \
    && strip --strip-unneeded bin/* libexec/gcc/*/*/cc1* \
       libexec/gcc/*/*/lto1 libexec/gcc/*/*/collect2 \
       libexec/gcc/*/*/lto-wrapper

# scripts/config from the Linux kernel, used as kconfig-tweak by NTFC's
# builder to apply each config.yaml's kv overrides. raw.githubusercontent.com
# is occasionally rate-limited, so fall back to the GitHub contents API.
RUN (curl -fsSL --retry 3 "https://raw.githubusercontent.com/torvalds/linux/${LINUX_TAG}/scripts/config" \
      -o /usr/local/bin/kconfig-tweak \
     || curl -fsSL -H "Accept: application/vnd.github.raw" \
      "https://api.github.com/repos/torvalds/linux/contents/scripts/config?ref=${LINUX_TAG}" \
      -o /usr/local/bin/kconfig-tweak) \
    && chmod +x /usr/local/bin/kconfig-tweak

ENV PATH="/opt/aarch64-none-elf/bin:/opt/riscv-none-elf/bin:/opt/riscv32-elf/bin:${PATH}"
ENV CC=gcc-14 CXX=g++-14

RUN pip install --break-system-packages --no-cache-dir kconfiglib pymodbus==3.15.0 \
    pyserial python-can==4.6.1 canopen==2.4.1

COPY entrypoint.sh /usr/local/bin/testlab-entrypoint
RUN chmod +x /usr/local/bin/testlab-entrypoint
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/testlab-entrypoint"]
