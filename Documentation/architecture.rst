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
                one defconfig per node (boards/ in this repo),
                "python -m ntfc test ...")
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

   nettl -s [-u] [-p port] [-n count] [-l len] [-t sec] [-w] [-D sec]
            [-L n [-E]] [-S [-R bytes]]
   nettl -c addr [-u] [-p port] [-n count] [-l len] [-t sec] [-k idle [-P]]
            [-r pps] [-d] [-N]

- ``-k idle`` TCP client only: before the transfer, enable keep-alive
  (``idle`` s idle, 1 s interval, 3 probes) and wait idle in
  ``recv()`` for the ``-t`` timeout. Prints ``nettl: idle ok`` on the
  timeout, else ``nettl: recv failed <errno>`` and fails.
- ``-P`` with ``-k``: wait in ``poll(POLLIN)`` instead of ``recv()``;
  on an event prints ``nettl: poll revents 0x<revents> so_error
  <SO_ERROR>`` and fails.
- ``-r pps`` UDP client only: send ``-n`` datagrams at ``pps`` per
  second (``0``: as fast as possible) without waiting for echoes, then
  the end marker; fails only on send errors other than a full queue.
- ``-d`` TCP client only: send and read the echo at the same time on a
  nonblocking socket instead of chunk by chunk.
- ``-N`` TCP client only: only connect, on a nonblocking socket. Prints
  ``nettl: connect returned <ret> errno <errno>`` and, if connect()
  returned ``EINPROGRESS``, ``nettl: connect poll <ret> revents
  0x<revents> so_error <SO_ERROR>`` after ``poll(POLLOUT)`` with the
  ``-t`` timeout; passes if the connection was established.

- ``-w`` TCP server only: after accept(), send ``-n`` bytes of
  pattern data to the client (no echo) then close; does not combine
  with ``-u``.
- ``-D sec`` TCP server or ``-S``: sleep ``sec`` seconds after printing
  the ``listening`` line and before calling accept() or the first recv().
- ``-S`` UDP server only: receive-only sink, no echo. Verifies the
  datagrams until the end marker or the ``-t`` timeout; passes when at
  least one datagram arrived and all were intact.
- ``-R bytes`` with ``-S``: set ``SO_RCVBUF`` (ignored without
  ``CONFIG_NET_RECV_BUFSIZE``, where the receive queue has no limit).
- ``-L n`` UDP server only, ``1`` to ``4``: open ``n`` sockets with
  ``SO_REUSEADDR`` bound to the same port and poll all of them until
  each has received ``-n`` datagrams of ``-l`` bytes each (or the
  ``-t`` timeout elapses); no echo. With ``-w`` or ``-L``, ``-n``/
  ``-l`` are server-side options instead of client-only.
- ``-E`` with ``-L`` only: wait with epoll_wait() instead of poll().

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

``apps/exittl``
~~~~~~~~~~~~~~~

``exittl`` (``CONFIG_TESTLAB_EXITTL``, flat builds with pthreads) checks
that ``exit()`` ends a whole task. It starts a child task (``task_create``)
whose second thread installs a ``SIGTERM`` handler and waits in
``pause()``; the child's main thread then calls ``exit()``. The handler
sets a flag that the parent reads (flat build: shared memory).

- Verdict: ``exittl: PASS pid=<n>`` if the child is gone (``kill(pid, 0)``
  fails with ``ESRCH``) within 5 s and the handler never ran, else
  ``exittl: FAIL pid=<n> alive=<0|1> handler=<0|1>``; exit 0 on PASS.

``apps/cantl``
~~~~~~~~~~~~~~

``cantl`` (``CONFIG_TESTLAB_CANTL``, needs ``NET_CAN``)::

   cantl -s ifname [-n count] [-i id] [-f] [-g gap_ms]
   cantl -r ifname [-n count] [-i id] [-f] [-t sec]

Also builds with ``CAN`` alone (no ``NET_CAN``) to drive the CAN
character driver instead of SocketCAN; pass a device path starting
with ``/`` in place of the ifname, e.g. ``cantl -s /dev/can0 ...``.

- Defaults: 50 frames, ID ``0x123``, classic CAN (``-f``: CAN FD, 64-byte
  payload), receiver idle timeout 10 s.
- Payload: 4-byte big-endian sequence + bytes ``(seq * 31 + 7 + k) & 0xff``.
- The receiver sets an exact ``CAN_RAW_FILTER`` on ``-i`` and prints
  ``cantl: listening`` when ready.
- Verdict: ``cantl: PASS|FAIL tx=<n>`` (sender),
  ``cantl: PASS|FAIL rx=<n> lost=<n> err=<n>`` (receiver); exit 0 on PASS.
- On the character driver backend, the receiver installs the exact ID
  as a ``CANIOC_ADD_STDFILTER`` hardware filter when the lower-half
  driver supports it, else filters in software (e.g. the sim character
  driver, which always returns ``-ENOTTY`` for ``CANIOC_*``).

``apps/canopt``
~~~~~~~~~~~~~~~

``canopt`` (``CONFIG_TESTLAB_CANOPT``, needs ``NET_CAN`` or ``CAN``)::

   canopt <check> <ifname|/dev/canN> [-F id/mask]... [-Z] [-M] [-f] [-q]
          [-m none|us|ns] [-n count] [-g gap_ms] [-l 0|1] [-o 0|1] [-t sec]

- One CAN feature check per run. SocketCAN checks (ifname): ``sockopt``,
  ``optbits``, ``rcvbuf``, ``rcvtimeo``, ``rx``, ``tx``, ``fdrx``,
  ``legacy``, ``ts``, ``loopback``, ``poll``, ``hup``. Character driver
  checks (device path): ``crx``, ``ctx``, ``cioctl``, ``calign``,
  ``cnonblock``, ``coverflow``, ``cfionread``, ``ciflush``, ``crtr``.
  See :doc:`test-cases` for what each one verifies.
- Frame tables (``canopt_table.c``, mirrored in
  ``ntfc/tests/_canopt_common.py``): 18 classic frames (SFF, EFF, RTR,
  DLC 0..8) and 11 CAN FD frames (lengths 0..64, BRS/ESI); payload byte
  ``k`` is ``((id & 0xff) + 7 * k + len) & 0xff``. CAN ID ``0x7fe`` ends a
  host sequence, ``0x7fd`` frames only wake up a blocked reader.
- Checks that need host traffic print ``canopt: ready`` first.
- Verdict: ``canopt: PASS|FAIL <check> ...``; exit 0 on PASS.

``apps/conode``
~~~~~~~~~~~~~~~

``conode`` (``CONFIG_TESTLAB_CONODE``, Lely CANopen with ``CSDO``,
``MASTER``, ``RPDO``, ``TPDO``, ``SYNC``) is a SocketCAN CANopen node::

   conode -i id [-m] [-r id,...] [-s sync_ms] [-b hb_ms] [-c hbc_ms]
          [-d ifname] [slave_id ...]

- Every node: heartbeat ``-b`` ms (default 100), TPDO1 ``0x180 + id``
  on each SYNC with a counter (``0x2100``, +1 per SYNC in OPERATIONAL);
  RPDOs for TPDO1 of the ``-r`` nodes, checked for gaps.
- Master (``-m``): SYNC producer (``-s``, default 100 ms), heartbeat
  consumer for each slave (``-c``, default 500 ms). On a slave's boot-up
  it writes ``0x2000`` = ``boots << 16 | id << 8 | slave`` over SDO,
  reads it back and sends NMT start. ``-r`` defaults to the slaves.
- Objects indexed by node-ID: ``0x2201``/``0x2202``/``0x2203`` PDOs
  received/skipped/not increasing, ``0x2204`` last NMT state,
  ``0x2205``/``0x2206`` heartbeat timeouts/recoveries, ``0x2207``
  boot-ups; ``0x2210`` non-zero clears the PDO counts; ``0x2211``
  frames the socket did not accept; ``0x2001`` is free.
- Console: ``conode: st <id> <state>``, ``conode: hb <id>
  timeout|resolved``, ``conode: boot <id> PASS|FAIL ...``. SIGTERM
  prints ``conode: PASS|FAIL rx=<n> lost=<n> bad=<n> txerr=<n>
  errno=<n>`` and exits.

``apps/fstl``
~~~~~~~~~~~~~

``fstl`` (``CONFIG_TESTLAB_FSTL``) runs one file system check per call
and prints one verdict line ``fstl: PASS|FAIL <check> ...``; exit 0 on
PASS. It uses only POSIX calls, so it works on any file system::

   fstl write|verify <path> <size> [-s seed] [-b bufsize] [-a]
   fstl pio|trunc|append|sync <path>
   fstl conc <dir> <n> <size>
   fstl ls <dir> [-n count] [-q]
   fstl stat|statfs|path <path>
   fstl chmod <path> <octal>
   fstl utime <path> <sec>
   fstl deep <dir> <depth>
   fstl err <op> <path> [path2] <ERRNO[|ERRNO]|OK>
   fstl mount <fstype> <target> <data> <ERRNO|OK>
   fstl umount <target> <ERRNO|OK>
   fstl exec <path|name> <status> [args...]

- Pattern: the 4-byte word at offset ``w`` (multiple of 4) is
  ``w ^ (seed * 0x9e3779b9)``, little-endian, so no two words of a file
  below 4 GiB are equal (``pattern()`` in ``ntfc/tests/_fs_common.py``).
- ``ls`` checks every entry against ``lstat`` (``d_ino == st_ino``,
  non-zero, ``d_type``), duplicates, the end of the directory (``NULL``
  with ``errno`` 0, twice) and ``rewinddir``; it prints
  ``fstl: ent <name> d_ino= st_ino= type= size=`` lines unless ``-q``.
- ``err`` runs one call (``open``, ``wronly``, ``creat``, ``excl``,
  ``opendir``, ``mkdir``, ``rmdir``, ``unlink``, ``stat``, ``rename``,
  ``symlink``, ``readlink``, ``seekneg``) and compares its errno.
- ``conc`` runs ``n`` threads that each write and read back
  ``<dir>/conc<i>.bin`` (seed ``i + 1``); ``exec`` runs a program with
  ``posix_spawn`` (``posix_spawnp`` for a name) and checks its exit
  status.

``apps/elftl``
~~~~~~~~~~~~~~

``elftl`` (``CONFIG_TESTLAB_ELFTL``) is built as a loadable ELF module
(``=m``) and run from a file system::

   elftl args [arg...] | exit <n> | relocs | libc | stack

- ``relocs``: 320 global functions ``elftl_f000`` ... ``elftl_f319``
  called through a table of pointers, entry ``i`` returns ``x + i``.
- ``stack``: prints the stack size (``/proc/self/stack``) and priority
  the loader gave the task (``nx_stacksize``/``nx_priority`` of the
  file: ``TESTLAB_ELFTL_STACKSIZE``, default 24576, and
  ``TESTLAB_ELFTL_PRIORITY``, default 90).
- The same directory builds ``elfsym``, which calls ``getpid``,
  ``gettid`` and ``putchar``.
- Verdict: ``elftl: PASS|FAIL ...`` / ``elfsym: PASS|FAIL ...``.

``apps/tlstl``
~~~~~~~~~~~~~~

``tlstl`` (``CONFIG_TESTLAB_TLSTL``, needs ``CRYPTO_MBEDTLS`` with TLS,
X.509 and PEM parsing) is an Mbed TLS client/server::

   tlstl -c addr -A ca [-H host] [-R] [-p port] [-V 12|13] [-n bytes]
         [-r n] [-N n] [-t sec]
   tlstl -s -C cert -K key [-p port] [-V 12|13] [-n bytes] [-r n]
         [-N n] [-t sec]

- Client: verifies the server against ``-A`` (PEM) and ``-H`` (SNI and
  name check, default ``addr``), echoes ``-n`` bytes (default 65536)
  of the ``nettl`` pattern chunk by chunk, then sends ``close_notify``
  and waits for the peer's. ``-R``: receive only, until the peer ends
  the stream.
- Server: certificate ``-C`` and key ``-K`` (PEM), port ``-p`` (default
  5101), no client authentication; echoes until the peer's
  ``close_notify`` and answers it. Prints ``tlstl: listening on
  <port>`` when ready.
- ``-V`` allows only TLS 1.2 or 1.3 (default both); ``-r`` sessions in
  total (default 1), ``-N`` at a time (threads, default 1, max 8);
  ``-t`` read timeout (default 10 s); ``-n`` with ``-R`` or ``-s``:
  bytes each session must receive (default any).
- A session passes when the handshake succeeds, the data is intact,
  all ``-n`` bytes arrive and the peer ends with ``close_notify``.
  How the peer ended the stream: ``notify``, ``eof`` (TCP FIN without
  ``close_notify``), ``reset`` (``ECONNRESET``), ``timeout``,
  ``error``.
- Verdict: ``tlstl: PASS|FAIL ver=<TLS version> sessions=<ok>/<n>
  tx=<n> rx=<n> end=<end> ret=-0x<mbedtls error> verify=0x<X.509
  flags> errno=<n>``, fields of the first failed session (else the
  last); a failed session also prints ``tlstl: session <i> ...``. Exit
  0 on PASS.

NTFC configs
------------

Each target has one NTFC config per scenario at
``ntfc/configs/<target>/<scenario>/config.yaml`` (``smoke`` below,
``ip-pair`` in `Host network (ip-pair)`_), keyed under ``config:``
(``cwd: './external'``, ``build_dir: './build/<target>/smoke'``) and
``product:`` (one ``core0`` running the built image). Each core's
``defconfig`` points at a defconfig in this repo's ``boards/`` tree
(path relative to ``external/nuttx``, e.g.
``../../boards/sim/sim/sim/configs/smoke``), not at an upstream NuttX
defconfig. ``ntfc/tests/ntfc.yaml`` declares the NTFC module name
``"Testlab"`` with one requirement, ``CONFIG_SYSTEM_NSH: True``.

Defconfigs
----------

``boards/<arch>/<chip>/<board>/configs/<scenario>/defconfig`` mirrors
the upstream NuttX board path and holds the full configuration for that
scenario (an out-of-tree configuration, see NuttX's
``CMakeLists.txt``): ``CONFIG_ARCH_BOARD_CUSTOM=y``,
``CONFIG_ARCH_BOARD_CUSTOM_DIR`` set to the upstream board directory
(e.g. ``"./boards/sim/sim/sim"``, the leading ``./`` keeps the board's
own ``Kconfig`` sourced instead of the empty out-of-tree stub),
``CONFIG_ARCH_BOARD_CUSTOM_DIR_RELPATH=y`` and
``CONFIG_ARCH_BOARD_CUSTOM_NAME``. No board source is copied into this
repo; the upstream board directory under ``external/nuttx`` is used
as-is. ``config.yaml`` has no ``kv:`` overrides: nodes of one scenario
that differ get one defconfig each, e.g. ``ip-pair-node0`` and
``ip-pair-node1`` for the two `Host network (ip-pair)`_ nodes (IPv4,
IPv6 and, on ``sim``, MAC address).

Every flat-build defconfig enables stack overflow detection
(``CONFIG_STACK_COLORATION``, ``CONFIG_DEBUG_ASSERTIONS``,
``CONFIG_STACKCHECK_MARGIN``): a thread that writes into the last
margin bytes of its stack asserts at its next context switch. The
margin (256 on ``rv-virt``, 512 elsewhere) is at least the stack frame
of 99% of the functions in the image (``gcc -fstack-usage``), so a
thread cannot step over it without writing into it.

.. list-table::
   :header-rows: 1

   * - Target
     - defconfig
     - device / exec_path
     - exec_args
   * - ``sim``
     - ``boards/sim/sim/sim/configs/smoke``
     - ``sim``
     - (none; runs as a host process)
   * - ``qemu-armv8a``
     - ``boards/arm64/qemu/qemu-armv8a/configs/smoke``
     - ``qemu`` / ``qemu-system-aarch64``
     - ``-cpu cortex-a53 -nographic -machine
       virt,virtualization=on,gic-version=3 -net none -chardev
       stdio,id=con,mux=on -serial chardev:con -mon
       chardev=con,mode=readline``
   * - ``rv-virt``
     - ``boards/risc-v/qemu-rv/rv-virt/configs/smoke``
     - ``qemu`` / ``qemu-system-riscv32``
     - ``-semihosting -M virt,aclint=on -cpu rv32 -smp 1 -bios none
       -nographic``
   * - ``qemu-intel64``
     - ``boards/x86_64/qemu/qemu-intel64/configs/smoke``
     - ``qemu`` / ``qemu-system-x86_64``
     - ``-m 1G -cpu host -enable-kvm -nographic -serial mon:stdio``

Manifests
---------

``ntfc/manifest-ci-<target>.yaml`` (one per target: ``sim``,
``qemu-armv8a``, ``rv-virt``, ``qemu-intel64``) lists ``options:``
(``fail_fast: false``, ``parallel: false``) and ``sessions:``, each a
``name``, a ``confpath`` (the target's ``config.yaml``) and a
``testpath`` (a test module or directory). Every manifest has three
sessions: ``<target>-smoke`` (``ntfc/tests/smoke``),
``<target>-ip-pair`` (``ntfc/tests/ip``, ``resources: [tl-br0]``) and
``<target>-modbus-rtu`` (``ntfc/tests/modbus``, ``resources:
[ttyTL0]``, see `Host serial line (modbus-rtu)`_). ``sim`` and
``qemu-intel64`` also have ``<target>-can-bus`` and ``<target>-can-char``
(``ntfc/tests/can``, ``resources: [can0]``, see `Host SocketCAN bus
(can-bus)`_). ``qemu-armv8a``, ``rv-virt`` and ``qemu-intel64`` also have
``<target>-v9fs`` and ``qemu-armv8a`` has ``qemu-armv8a-v9fs-knsh``
(``ntfc/tests/fs``, see `Host share (v9fs)`_). ``qemu-armv8a``,
``rv-virt`` and ``qemu-intel64`` also have ``<target>-router``
(``ntfc/tests/router``) and ``<target>-router-hp2`` (its
``test_router_duplex``), both ``resources: [tl-rbr0]``, see
`Routed networks (router)`_. Every manifest also has ``<target>-tls``
(``ntfc/tests/tls``, ``resources: [tl-br0]``), see `TLS (tls)`_.

Host network (ip-pair)
----------------------

``testenv/ip-pair.sh {start|stop|status}`` creates bridge ``tl-br0``
(``10.42.0.1/24`` and ``fc00::1/112``, the host side) and TAPs
``tl-tap0``/``tl-tap1``. Node IPs come from ``CONFIG_NETINIT_IPADDR``
and ``CONFIG_NETINIT_IPv6ADDR_8`` (gateways ``10.42.0.1``, ``fc00::1``).

.. list-table::
   :header-rows: 1

   * - Node
     - IP
     - IPv6
     - MAC
     - Attached via
   * - node0
     - ``10.42.0.10``
     - ``fc00::10``
     - ``52:54:00:2a:00:10``
     - ``tl-tap0``
   * - node1
     - ``10.42.0.11``
     - ``fc00::11``
     - ``52:54:00:2a:00:11``
     - ``tl-tap1``

.. list-table::
   :header-rows: 1

   * - Target
     - defconfig
     - NIC
   * - ``sim``
     - ``boards/sim/sim/sim/configs/ip-pair-node0``, ``-node1``
     - own TAP joined to ``tl-br0`` (``CONFIG_SIM_NET_BRIDGE``)
   * - ``qemu-armv8a``
     - ``boards/arm64/qemu/qemu-armv8a/configs/ip-pair-node0``, ``-node1``
     - ``virtio-net-device``
   * - ``rv-virt``
     - ``boards/risc-v/qemu-rv/rv-virt/configs/ip-pair-node0``, ``-node1``
     - ``virtio-net-device``
   * - ``qemu-intel64``
     - ``boards/x86_64/qemu/qemu-intel64/configs/ip-pair-node0``, ``-node1``
     - ``e1000``

Driver tests check for buffer leaks: the ``net_leak`` fixture
(``_netleak.py``) reads ``/proc/iobinfo`` on every node before a test; the
test ends with ``net_leak.check()``, which fails when a node it used has
fewer IOBs free afterwards (30 s settle time). A node pings the host
before the first read and, while short, before each retry, so a driver
that reclaims sent packets lazily holds the same buffers both times.
With ``CONFIG_NETDEV_STATISTICS`` it also prints the driver RX/TX counter
deltas from ``/proc/net/<dev>``. After a failed test the driver tests
reboot both nodes.

Host SocketCAN bus (can-bus)
-----------------------------

``testenv/can-bus.sh {start|stop|status}`` creates host vcan ``can0``;
all nodes and the host share it as one bus. ``can-bus`` nodes use
SocketCAN (``ifup can0``), ``can-char`` nodes the CAN character driver
(``/dev/can0``). All CAN defconfigs also build the Lely CANopen examples
``coslave`` and ``comaster``; lely-core is downloaded at build time from
``CONFIG_CANUTILS_LELYCANOPEN_URL``, pinned to a commit.
The ``canopen-net`` scenario runs four sim nodes from one defconfig,
``boards/sim/sim/sim/configs/canopen-net`` (``conode``,
``CONFIG_USEC_PER_TICK=1000`` for SYNC periods down to 2 ms); node-ID
and role come from the ``conode`` command line.

.. list-table::
   :header-rows: 1

   * - Target
     - defconfig
     - Joins ``can0`` via
   * - ``sim``
     - ``boards/sim/sim/sim/configs/can-bus``
     - ``CONFIG_SIM_CANDEV_SOCK`` (host ``can0``)
   * - ``qemu-intel64``
     - ``boards/x86_64/qemu/qemu-intel64/configs/can-bus-ctucanfd``
       (node0), ``can-bus-kvaser`` (node1); ``can-char-ctucanfd``,
       ``can-char-kvaser``
     - QEMU ``can-host-socketcan,if=can0``; node0 ``ctucan_pci``
       (``CAN_CTUCANFD``, CAN FD), node1 ``kvaser_pci``
       (``CAN_KVASER``, classic)
   * - ``qemu-armv8a``
     - ``boards/arm64/qemu/qemu-armv8a/configs/can-bus`` (both nodes,
       not in the manifest)
     - as ``qemu-intel64``, ``ctucan_pci`` on both nodes; GICv2
       (``ARM64_GIC_VERSION=2``, ``gic-version=2``) for PCI INTx

The ``qemu-intel64`` CAN and ip-pair nodes run
``-cpu host,+invtsc,vmware-cpuid-freq=on`` with
``ARCH_INTEL64_CORE_FREQ_KHZ=0`` and ``ARCH_INTEL64_TSC_FREQ_VMWARE``:
NuttX reads the TSC frequency from CPUID ``0x40000010`` instead of
assuming one, so its clock keeps real time (CANopen heartbeat and SYNC
periods, ARP request intervals).

Host serial line (modbus-rtu)
-----------------------------

``testenv/modbus-rtu.sh {start|stop|status}`` runs ``socat`` joining
two host pseudoterminals back to back: ``/dev/ttyTL0`` is opened by the
target, ``/dev/ttyTL1`` by the tests.

.. list-table::
   :header-rows: 1

   * - Target
     - Node UART
     - Wiring
   * - ``sim``
     - ``CONFIG_SIM_UART0_NAME="/dev/ttyTL0"``, the same path on the
       node
     - the sim process opens ``/dev/ttyTL0``
   * - ``qemu-armv8a``
     - virtio-serial, ``/dev/ttyS2`` (``ttyS1`` is the PL011 console)
     - ``-device virtio-serial-device -chardev
       serial,id=mb0,path=/dev/ttyTL0 -device virtconsole,chardev=mb0``
   * - ``rv-virt``
     - virtio-serial, ``/dev/ttyS1``
     - as ``qemu-armv8a``
   * - ``qemu-intel64``
     - 16550 COM2 (``0x2f8``, IRQ 3, RX trigger 1 byte), ``/dev/ttyS1``
     - ``-chardev serial,id=mb0,path=/dev/ttyTL0 -serial chardev:mb0``

Defconfigs: ``boards/<arch>/<chip>/<board>/configs/modbus-rtu``. The
line runs without parity: Linux pseudoterminals reject ``PARENB``.

Host share (v9fs)
-----------------

``testenv/v9fs.sh {start|stop|status}`` creates
``build/v9fs/<target>-<scenario>`` for each
``ntfc/configs/<target>/v9fs*``; QEMU (started in the repo root) exports
it with ``-fsdev local,id=tlfs,path=...,security_model=none`` and mount
tag ``tlshare``. ``security_model=none``
keeps host modes and owners as they are, so the tests read them on the
host directly. The ``fs`` tests empty the directory at the start of a
session and copy the build's loadable programs to ``bin/``.

.. list-table::
   :header-rows: 1

   * - Target
     - Device
     - Transport
   * - ``qemu-armv8a``
     - ``virtio-9p-device``
     - virtio-mmio (``DRIVERS_VIRTIO_MMIO``)
   * - ``rv-virt``
     - ``virtio-9p-device``
     - virtio-mmio
   * - ``qemu-intel64``
     - ``virtio-9p-pci``
     - virtio-pci (``DRIVERS_VIRTIO_PCI``, ``PCI_MSIX``)

Defconfigs ``boards/<arch>/<chip>/<board>/configs/v9fs``: ``FS_V9FS``,
``V9FS_VIRTIO_9P``, ``fstl``, ``elftl=m``, ``ELF`` with the generated
system symbol table (``EXECFUNCS_HAVE_SYMTAB``,
``EXECFUNCS_SYSTEM_SYMTAB``), ``NSH_FILE_APPS`` and
``PATH_INITIAL="/mnt/v9fs/bin"``. NSH starts directly
(``INIT_ENTRYPOINT="nsh_main"``). ``qemu-armv8a`` and ``qemu-intel64``
set ``FS_LARGEFILE`` (64-bit ``off_t``); ``rv-virt`` keeps the default
32-bit ``off_t``.

``boards/arm64/qemu/qemu-armv8a/configs/v9fs-knsh`` is a kernel build
(upstream ``knsh``) with no programs in the image: a second 9P device,
tag ``tlbin``, exports the NTFC build directory read-only (NTFC starts
QEMU in that directory for kernel builds, so the paths in its
``config.yaml`` are relative to it), and
``INIT_MOUNT`` mounts it at ``/system`` so ``/system/bin/init`` (NSH),
``fstl`` and ``elftl`` load from it. ``PATH`` is
``/system/bin:/mnt/v9fs/bin``; ``TLS_LOG2_MAXSTACK=15`` allows the
24 KiB ``elftl`` stack.

Routed networks (router)
------------------------

``testenv/router.sh {start|stop|status}`` creates two host networks in
network namespaces, so the host kernel cannot route between them: host A
(``tl-ra``, ``10.51.0.1/24``, ``fc00::1/112``) and host B (``tl-rb``,
``10.52.0.1/24``, ``fc00::1:1/112``). Each namespace reaches a bridge
(``tl-rbr0``, ``tl-rbr1``) over a veth pair; the node's two NICs attach
to the bridges through ``tl-rtap0`` and ``tl-rtap1``. A's default route
and B's routes to ``10.51.0.0/24`` and ``fc00::/112`` point at the node.

.. list-table::
   :header-rows: 1

   * - Side
     - Node IP
     - Node IPv6
     - Node MAC
     - TAP
   * - A
     - ``10.51.0.10``
     - ``fc00::10``
     - ``52:54:00:33:00:10``
     - ``tl-rtap0``
   * - B
     - ``10.52.0.10``
     - ``fc00::1:10``
     - ``52:54:00:33:00:20``
     - ``tl-rtap1``

The tests find each node interface by its MAC and address it from NSH.
Defconfigs ``boards/<arch>/<chip>/<board>/configs/router`` and
``router-hp2`` enable ``NET_IPFORWARD``, ``NET_NAT``, ``NET_IPFILTER``,
``iptables``, ``ip6tables`` and SMP with 4 CPUs; ``router-hp2`` also
runs two high-priority work queue threads (``SCHED_HPNTHREADS=2``), so
the work of both NICs can run at the same time.

.. list-table::
   :header-rows: 1

   * - Target
     - ``router`` NICs (A, B)
     - ``router-hp2`` NICs (A, B)
   * - ``qemu-armv8a``
     - ``e1000`` (PCI, GICv2 with ``ARM64_GICV2M`` MSI), ``virtio-net-device``
     - ``virtio-net-device``, ``virtio-net-device``
   * - ``rv-virt``
     - ``virtio-net-device``, ``virtio-net-device``
     - ``virtio-net-device``, ``virtio-net-device``
   * - ``qemu-intel64``
     - ``e1000``, ``e1000e``
     - ``virtio-net-pci``, ``virtio-net-pci``

The ``qemu-intel64`` ``router`` defconfig also builds ``NET_IGB``:
``-device igb`` in its ``config.yaml`` selects that NIC.

TLS (tls)
---------

One node on ``tl-br0`` with node0's addresses (``tl-tap0``). Defconfigs
``boards/<arch>/<chip>/<board>/configs/tls``: ``CRYPTO_MBEDTLS`` (TLS
1.2 and 1.3, ``MBEDTLS_HAVE_TIME_DATE``), ``MBEDTLS_THREADING_PTHREAD``,
``tlstl``, ``DEV_URANDOM``, NTP and TFTP clients, no RTC (the clock
starts at ``CONFIG_START_YEAR`` until NTP sets it).

.. list-table::
   :header-rows: 1

   * - Target
     - Entropy (``/dev/urandom``)
   * - ``sim``
     - ``DEV_URANDOM_XORSHIFT128``
   * - ``qemu-armv8a``, ``rv-virt``
     - ``virtio-rng-device`` (``DRIVERS_VIRTIO_RNG``)
   * - ``qemu-intel64``
     - RDRAND (``DEV_URANDOM_ARCH``)

``qemu-intel64`` is SMP: the tests pin the shell, and so tlstl, to CPU
0 (``taskset``, ``SYSTEM_TASKSET``), so that ``free`` (run on that CPU)
sees the stacks of exited programs released.

``test_tls_handshake_lossy`` adds a netem qdisc to ``tl-br0`` (host to
node) and redirects the bridge's ingress (node to host) to ``ifb``
device ``tl-ifb0`` with the same netem qdisc.

Docker image and runner
------------------------

``tools/docker/Dockerfile`` builds an ``ubuntu:24.04``-based image with
the build toolchain (``build-essential``, ``cmake``, ``ninja-build``,
``gcc-14``/``g++-14``), QEMU packages (``qemu-system-arm``,
``qemu-system-misc``, ``qemu-system-x86``), ``can-utils``, ``socat``,
``iperf`` (iperf2), ``mosquitto`` and ``mosquitto-clients`` (MQTT broker
and host clients), ``kconfiglib``, ``pymodbus``, ``pyserial``,
``python-can`` and ``canopen`` (pip), and a Linux-kernel
``scripts/config`` fetched as ``kconfig-tweak``; the ARM64 and RISC-V
bare-metal cross-toolchains are downloaded and unpacked under
``/opt``. RISC-V has two: the xPack ``riscv-none-elf`` (as in upstream
CI, the default) and riscv-collab's ``riscv32-unknown-elf`` (built with
``--enable-tls``). ``rv-virt-python`` uses the latter, as CPython keeps
its thread state in ``__thread``: the defconfig sets
``RISCV_TOOLCHAIN_GNU_RV32`` and ``SCHED_THREAD_LOCAL``, and
``config.yaml`` hides the xPack directory from CMake
(``dcmake: CMAKE_IGNORE_PATH``). The entrypoint is ``/usr/local/bin/testlab-entrypoint``
(``entrypoint.sh``), working directory ``/work``.

``tools/docker/run.sh``::

   tools/docker/run.sh [--image IMG] <manifest-name>

It resolves the repo root with ``git rev-parse --show-toplevel``,
creates ``result/`` there, and runs the image (default
``nuttx-testlab``, or ``--image``, or ``TESTLAB_IMAGE`` if set) with
``--rm --init --privileged``, mounting the repo read-only at ``/src``
and ``result/`` at ``/out``, passing through ``NUTTX_REPO``,
``NUTTX_REF``, ``APPS_REPO``, ``APPS_REF``, ``NTFC_REPO``,
``NTFC_REF``, ``TESTLAB_SESSION`` and ``TESTLAB_TESTPATH``, and setting
``TESTLAB_OWNER`` to the invoking host user's ``uid:gid`` so the
container can hand ownership of ``/out`` back at the end.

``entrypoint.sh`` (``testlab-entrypoint <manifest-name>``, e.g.
``ci-sim``, resolving to ``ntfc/manifest-ci-sim.yaml``):

1. ``rsync``s ``/src`` into ``/work`` (excluding ``external``,
   ``build``, ``result`` and ``.venv``).
2. Runs ``repo_init.sh`` (NuttX, nuttx-apps and NTFC sources into
   ``external/``), creates a venv and ``pip install``s
   ``ntfc/requirements.txt`` and ``external/nuttx-ntfc``. Runs
   ``start`` on every ``testenv/*.sh``.
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
  ``apps_ref``. For each target in the matrix it loads the host
  ``can``, ``can_raw`` and ``vcan`` modules with
  ``tools/ci/load-can-modules.sh`` (installing
  ``linux-modules-extra-$(uname -r)`` only if one is missing), pulls
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
