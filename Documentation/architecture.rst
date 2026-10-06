.. SPDX-License-Identifier: Apache-2.0

Architecture
============

This page describes what exists in the repository today: how NuttX
sources are fetched, how the in-repo test app is built into NuttX, how
NTFC drives it on each target, and how the Docker runner and CI
workflows tie that together. It does not describe planned scenarios.

Flow overview
-------------

.. code-block:: text

   sources.env / repo_init.sh          (fetch NuttX + nuttx-apps)
             |
             v
   apps/  (symlinked into external/apps/external)
             |
             v
   NTFC build  (ntfc/configs/<target>/<scenario>/config.yaml:
                defconfig + kv overrides, "python -m ntfc test ...")
             |
             v
   target  (sim process, or qemu-system-* under QEMU)
             |
             v
   results / CI  (result/<timestamp>/<session>/report/...,
                  GITHUB_STEP_SUMMARY, uploaded result-<target> artifact)

Sources
-------

``sources.env`` holds the default NuttX and nuttx-apps repositories and
refs:

.. code-block:: text

   NUTTX_REPO="${NUTTX_REPO:-https://github.com/apache/nuttx}"
   NUTTX_REF="${NUTTX_REF:-master}"
   APPS_REPO="${APPS_REPO:-https://github.com/apache/nuttx-apps}"
   APPS_REF="${APPS_REF:-master}"

Each value can be overridden by an environment variable of the same
name. ``repo_init.sh``:

1. Sources ``sources.env``.
2. Refuses to run if ``external/`` already exists, unless ``-f``/
   ``--force`` is given (which removes it first).
3. Shallow-fetches ``NUTTX_REPO``/``NUTTX_REF`` into ``external/nuttx``
   and ``APPS_REPO``/``APPS_REF`` into ``external/apps`` (``git init``,
   ``git remote add origin``, ``git fetch --depth 1``,
   ``git checkout FETCH_HEAD``; a ref may be a branch, a tag, or a
   full 40-character commit SHA).
4. Creates ``apps/`` if missing and symlinks it as
   ``external/apps/external`` so the NuttX build system picks up this
   repository's apps as an out-of-tree app directory.
5. Writes ``external/sources.txt`` with one line per source
   (``<name> <repo> <ref> <resolved-sha>``), used later for the
   step/job summary.

``external/`` is not tracked in git.

Test apps
---------

``apps/`` is a NuttX out-of-tree ``apps`` repository root: ``apps/Makefile``
includes ``$(APPDIR)/Directory.mk`` and ``apps/CMakeLists.txt`` calls
``nuttx_add_subdirectory()`` / ``nuttx_generate_kconfig()``, both with
``MENUDESC = "nuttx-testlab"``. Each test app under it ships its own
``Kconfig``, ``Makefile``, ``CMakeLists.txt`` and ``Make.defs``, matching
upstream ``nuttx-apps`` layout.

``apps/nettl``
~~~~~~~~~~~~~~

``nettl`` (``apps/nettl/nettl_main.c``) is a TCP/UDP data-integrity
echo tool, built when ``CONFIG_TESTLAB_NETTL`` is set (``depends on
NET_TCP || NET_UDP``). Kconfig sub-options: ``TESTLAB_NETTL_PROGNAME``
(program name, default ``nettl``), ``TESTLAB_NETTL_PRIORITY`` (default
``100``), ``TESTLAB_NETTL_STACKSIZE`` (default
``DEFAULT_TASK_STACKSIZE``), ``TESTLAB_NETTL_BUFSIZE`` (I/O buffer
size, default ``512``).

CLI::

   nettl -s [-u] [-p port] [-t sec]
   nettl -c addr [-u] [-p port] [-n count] [-l len] [-t sec]

- ``-s`` runs as server, ``-c addr`` runs as client connecting/sending
  to ``addr``; exactly one of the two must be given.
- ``-u`` selects UDP instead of the default, TCP.
- ``-p port`` listen/connect port, default ``5001``.
- ``-n count`` client only: TCP byte count (default ``65536``) or UDP
  datagram count, ``1`` to ``0xfffffffe`` since the UDP sequence number
  is a ``uint32_t`` and ``0xffffffff`` is the end-of-stream marker
  (default ``100``).
- ``-l len`` client only, UDP only: payload length per datagram in
  bytes, ``1`` to ``CONFIG_TESTLAB_NETTL_BUFSIZE``; default is
  ``min(512, CONFIG_TESTLAB_NETTL_BUFSIZE)``.
- ``-t sec`` socket receive timeout, default ``10``.

TCP/UDP server-only options (added for ``ip`` regression tests, see
:doc:`test-cases`), full CLI::

   nettl -s [-u] [-p port] [-n count] [-l len] [-t sec] [-w] [-D sec] [-L n]
   nettl -c addr [-u] [-p port] [-n count] [-l len] [-t sec]

- ``-w`` TCP server only: after accept(), send ``-n`` bytes of
  pattern data to the client (no echo) then close; does not combine
  with ``-u``.
- ``-D sec`` TCP server only: sleep ``sec`` seconds after printing the
  ``listening`` line and before calling accept().
- ``-L n`` UDP server only, ``1`` to ``4``: open ``n`` sockets with
  ``SO_REUSEADDR`` bound to the same port and poll all of them until
  each has received ``-n`` datagrams of ``-l`` bytes each (or the
  ``-t`` timeout elapses); no echo. With ``-w`` or ``-L``, ``-n``/
  ``-l`` are server-side options instead of client-only.

Protocol: both sides fill/verify a deterministic byte pattern,
``byte(offset) = (offset * 31 + 7) mod 256``, keyed by the byte's
position in the logical stream (TCP) or datagram (UDP).

- TCP: the client sends ``count`` bytes of pattern data in
  ``CONFIG_TESTLAB_NETTL_BUFSIZE``-sized chunks and reads back the
  server's lock-step echo of each chunk, checking the pattern on
  receipt. The server echoes every byte it receives until the client
  closes the connection.
- UDP: each datagram is a 4-byte big-endian sequence number followed
  by ``len`` bytes of pattern data (offset ``seq * len``). The server
  echoes each datagram verbatim to its sender until it receives a
  datagram whose sequence number is ``0xffffffff`` (sent with no
  payload), which ends its loop without a reply. The client sends each
  sequence number and waits for the matching echo, retrying up to 3
  times (and discarding up to 16 stale/duplicate echoes per attempt
  without consuming a retry) before counting it lost; after the last
  sequence number it sends the ``0xffffffff`` end marker 3 times.

Verdict line (always exactly one, to stdout) and process exit code:

.. list-table::
   :header-rows: 1

   * - Mode
     - Verdict line format
     - PASS condition (exit ``EXIT_SUCCESS``/``0``)
   * - TCP server
     - ``nettl: PASS|FAIL rx=<n> err=<n>``
     - no socket/accept error and ``err == 0``
   * - TCP client
     - ``nettl: PASS|FAIL tx=<n> rx=<n> err=<n>``
     - ``err == 0 && rx == count``
   * - UDP server
     - ``nettl: PASS|FAIL rx=<n> err=<n>``
     - no socket error and ``err == 0``
   * - UDP client
     - ``nettl: PASS|FAIL tx=<n> rx=<n> lost=<n> err=<n>``
     - ``lost == 0 && err == 0``
   * - TCP server (``-w``)
     - ``nettl: PASS|FAIL rx=0 err=<n>``
     - no socket/accept error and ``err == 0`` (``err`` counts a
       failed send)
   * - UDP server (``-L n``)
     - ``nettl: PASS|FAIL rx=<n> err=<n>``
     - no socket error and ``err == 0`` (``err`` also counts, per
       listener, any datagram not received by the ``-t`` timeout)

Any FAIL condition, or a socket/connect/bind/accept error, makes the
process exit ``EXIT_FAILURE`` (``1``); a usage error (bad option, bad
numeric argument, both/neither of ``-s``/``-c``, or a UDP ``-l`` out of
range) prints usage and exits ``EXIT_FAILURE`` before printing any
verdict line.

NTFC configs
------------

Each target has one NTFC config per scenario at
``ntfc/configs/<target>/<scenario>/config.yaml`` (``smoke`` below,
``ip-pair`` in `Host network (ip-pair)`_), keyed under ``config:``
(``cwd: './external'``, ``build_dir: './build/<target>/smoke'``, ``kv:``
overrides on top of an upstream ``defconfig``) and ``product:`` (one
``core0`` running the built image). ``ntfc/tests/ntfc.yaml`` declares
the NTFC module name ``"Testlab"`` with one requirement,
``CONFIG_SYSTEM_NSH: True``.

All four targets share the same ``kv`` base
(``CONFIG_DEBUG_SYMBOLS``, ``CONFIG_TESTLAB_NETTL``, ``CONFIG_NET``,
``CONFIG_NET_TCP``, ``CONFIG_NET_UDP``, ``CONFIG_NET_LOOPBACK``,
``CONFIG_NET_SOCKOPTS``, all ``"y"``); the three QEMU targets add
``CONFIG_SCHED_WORKQUEUE``, ``CONFIG_SCHED_HPWORK`` and
``CONFIG_NETDEV_LATEINIT`` (all ``"y"``).

.. list-table::
   :header-rows: 1

   * - Target
     - defconfig
     - device / exec_path
     - exec_args
   * - ``sim``
     - ``boards/sim/sim/sim/configs/nsh``
     - ``sim``
     - (none; runs as a host process)
   * - ``qemu-armv8a``
     - ``boards/arm64/qemu/qemu-armv8a/configs/nsh``
     - ``qemu`` / ``qemu-system-aarch64``
     - ``-cpu cortex-a53 -nographic -machine
       virt,virtualization=on,gic-version=3 -net none -chardev
       stdio,id=con,mux=on -serial chardev:con -mon
       chardev=con,mode=readline``
   * - ``rv-virt``
     - ``boards/risc-v/qemu-rv/rv-virt/configs/nsh``
     - ``qemu`` / ``qemu-system-riscv32``
     - ``-semihosting -M virt,aclint=on -cpu rv32 -smp 1 -bios none
       -nographic``
   * - ``qemu-intel64``
     - ``boards/x86_64/qemu/qemu-intel64/configs/nsh``
     - ``qemu`` / ``qemu-system-x86_64``
     - ``-m 1G -cpu host -enable-kvm -nographic -serial mon:stdio``

Manifests
---------

``ntfc/manifest-ci-<target>.yaml`` (one per target: ``sim``,
``qemu-armv8a``, ``rv-virt``, ``qemu-intel64``) lists ``options:``
(``fail_fast: false``, ``parallel: false``) and ``sessions:``, each a
``name``, a ``confpath`` (the target's ``config.yaml``) and a
``testpath`` (a test module or directory). Every manifest has two
sessions: ``<target>-smoke`` (``ntfc/tests/smoke``) and
``<target>-ip-pair`` (``ntfc/tests/ip``, ``resources: [tl-br0]``).

Host network (ip-pair)
----------------------

``testenv/ip-pair.sh {start|stop|status}`` creates bridge ``tl-br0``
(``10.42.0.1/24``, the host side) and TAPs ``tl-tap0``/``tl-tap1``.
Node IPs come from ``CONFIG_NETINIT_IPADDR`` (gateway ``10.42.0.1``).

.. list-table::
   :header-rows: 1

   * - Node
     - IP
     - MAC
     - Attached via
   * - node0
     - ``10.42.0.10``
     - ``52:54:00:2a:00:10``
     - ``tl-tap0``
   * - node1
     - ``10.42.0.11``
     - ``52:54:00:2a:00:11``
     - ``tl-tap1``

.. list-table::
   :header-rows: 1

   * - Target
     - defconfig
     - NIC
   * - ``sim``
     - ``boards/sim/sim/sim/configs/tcpblaster``
     - own TAP joined to ``tl-br0`` (``CONFIG_SIM_NET_BRIDGE``)
   * - ``qemu-armv8a``
     - ``boards/arm64/qemu/qemu-armv8a/configs/netnsh``
     - ``virtio-net-device``
   * - ``rv-virt``
     - ``boards/risc-v/qemu-rv/rv-virt/configs/netnsh``
     - ``virtio-net-device``
   * - ``qemu-intel64``
     - ``boards/x86_64/qemu/qemu-intel64/configs/jumbo``
     - ``e1000``

Docker image and runner
------------------------

``tools/docker/Dockerfile`` builds an ``ubuntu:24.04``-based image with
the build toolchain (``build-essential``, ``cmake``, ``ninja-build``,
``gcc-14``/``g++-14``), QEMU packages (``qemu-system-arm``,
``qemu-system-misc``, ``qemu-system-x86``), ``can-utils``, ``kconfiglib``
(pip), and a Linux-kernel ``scripts/config`` fetched as
``kconfig-tweak``; the ARM64 and RISC-V bare-metal cross-toolchains are
downloaded and unpacked under ``/opt``. The entrypoint is
``/usr/local/bin/testlab-entrypoint`` (``entrypoint.sh``), working
directory ``/work``.

``tools/docker/run.sh``::

   tools/docker/run.sh [--image IMG] <manifest-name>

It resolves the repo root with ``git rev-parse --show-toplevel``,
creates ``result/`` there, and runs the image (default
``nuttx-testlab``, or ``--image``, or ``TESTLAB_IMAGE`` if set) with
``--rm --init --privileged``, mounting the repo read-only at ``/src``
and ``result/`` at ``/out``, passing through ``NUTTX_REPO``,
``NUTTX_REF``, ``APPS_REPO``, ``APPS_REF``, ``NTFC_PIP_SPEC``,
``TESTLAB_SESSION`` and ``TESTLAB_TESTPATH``, and setting
``TESTLAB_OWNER`` to the invoking host user's ``uid:gid`` so the
container can hand ownership of ``/out`` back at the end.

``entrypoint.sh`` (``testlab-entrypoint <manifest-name>``, e.g.
``ci-sim``, resolving to ``ntfc/manifest-ci-sim.yaml``):

1. ``rsync``s ``/src`` into ``/work`` (excluding ``external``,
   ``build``, ``result`` and ``.venv``).
2. Runs ``repo_init.sh``, creates a venv and ``pip install``s
   ``ntfc/requirements.txt``; if ``NTFC_PIP_SPEC`` is set, force-
   reinstalls it (``--no-deps``) afterward to override the ``ntfc``
   package spec. Runs ``start`` on every ``testenv/*.sh``.
3. If ``TESTLAB_SESSION`` is unset, runs
   ``python -m ntfc test --manifest <manifest>`` (the whole manifest);
   otherwise looks up that session's ``confpath``/``testpath`` in the
   manifest and runs
   ``python -m ntfc test --confpath <confpath> --testpath <testpath>``,
   where ``testpath`` is overridden by ``TESTLAB_TESTPATH`` if that is
   also set (``TESTLAB_TESTPATH`` without ``TESTLAB_SESSION`` is a
   usage error, exit ``2``).
4. On exit (success or failure), runs ``stop`` on every
   ``testenv/*.sh``, copies ``result/`` and
   ``external/sources.txt`` into ``/out``, then ``chown``s ``/out`` to
   ``TESTLAB_OWNER`` if that is set.

The container's own exit status is the NTFC run's exit status.

CI workflows
------------

- ``pr.yml`` (``pull_request``, and ``push`` to ``master``): job
  ``lint`` runs ``checkpatch.sh``/nxstyle on tracked ``apps/*.c``,
  ``apps/*.h`` and ``apps/*CMakeLists.txt`` files, ``shellcheck`` on
  tracked ``*.sh`` files, ``tox -c ntfc/tox.ini``, a Sphinx ``-W``
  docs build, and ``tools/ci/check-docs.py``; job ``changes`` detects
  whether ``tools/docker/**`` changed (``pull_request``: diff against
  the PR base SHA; ``push``: diff against ``github.event.before``,
  treating an all-zero ``before`` -- a branch's first push -- as a
  change); job ``tests`` calls the reusable ``tests.yml`` with
  ``build_image`` set to that result.
- ``tests.yml`` (reusable, ``workflow_call``/``workflow_dispatch``):
  inputs ``targets`` (``all`` or one of ``sim``, ``qemu-armv8a``,
  ``rv-virt``, ``qemu-intel64``), ``session``, ``testpath``, ``image``,
  ``build_image``, ``nuttx_repo``/``nuttx_ref``/``apps_repo``/
  ``apps_ref``. For each target in the matrix it pulls
  ``ghcr.io/<repo>/testlab:latest`` (or ``image``), building
  ``tools/docker`` locally instead if ``build_image`` is set or the
  pull fails; runs
  ``tools/docker/run.sh --image <image> "ci-<target>"`` with
  ``TESTLAB_SESSION``/``TESTLAB_TESTPATH`` set from the ``session``/
  ``testpath`` inputs; writes a job summary with
  ``tools/ci/summary.sh result "<target>"``; and uploads ``result/`` as
  artifact ``result-<target>``.
- ``nightly.yml``: ``schedule`` (``0 1 * * *``, i.e. 01:00 UTC
  daily) and manual ``workflow_dispatch``; calls ``tests.yml`` with no
  inputs (so all targets, upstream ``master``).
- ``docker-image.yml``: on ``push`` to ``master`` when
  ``tools/docker/**`` changes, or manual ``workflow_dispatch``; builds
  ``tools/docker`` and pushes it to
  ``ghcr.io/<repo>/testlab:latest`` and
  ``ghcr.io/<repo>/testlab:sha-<sha>``.

Results and artifacts
----------------------

``entrypoint.sh`` copies the NTFC ``result/`` directory (NTFC's own
output layout, a ``<timestamp>/<session>/report/...`` tree per run)
and ``external/sources.txt`` into ``/out``, which ``run.sh`` mounts
from the host's ``result/`` directory -- so on the host the full path
is ``result/result/<timestamp>/<session>/report/...``.
``tools/ci/summary.sh <result-dir> <title>`` prints a Markdown
heading, a table built from
``sources.txt`` (columns ``source``, ``repo``, ``ref``, ``sha``), and
the contents of every ``report/result_summary.txt`` found directly
under ``<result-dir>/result/*/`` or ``<result-dir>/result/*/*/``,
used by ``tests.yml`` to populate ``GITHUB_STEP_SUMMARY``. The CI
workflow additionally uploads the whole ``result/`` tree as the
``result-<target>`` GitHub Actions artifact (30-day retention).
