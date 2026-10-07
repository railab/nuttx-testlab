.. SPDX-License-Identifier: Apache-2.0

Test cases
==========

This page lists every NTFC test module and test function that exists
under ``ntfc/tests/`` today, what each one does and its exact PASS
criteria. Shared helpers live in ``ntfc/tests/_net_common.py``;
``ntfc/tests/conftest.py`` only adjusts ``sys.path`` so test modules
can import them.

``smoke`` module
-----------------

Source: ``ntfc/tests/smoke/test_smoke.py``.

Topology: a single node (``pytest.product``, core 0); no second node
and no host network setup are involved. ``test_nettl_loopback_tcp``
and ``test_nettl_loopback_udp`` run the ``nettl`` client and server on
that same node, talking over ``127.0.0.1``.

Targets, build and manifest sessions:

.. list-table::
   :header-rows: 1

   * - Target
     - NTFC config
     - Manifest session
     - Manifest
   * - sim
     - ``ntfc/configs/sim/smoke/config.yaml``
     - ``sim-smoke``
     - ``ntfc/manifest-ci-sim.yaml``
   * - qemu-armv8a
     - ``ntfc/configs/qemu-armv8a/smoke/config.yaml``
     - ``qemu-armv8a-smoke``
     - ``ntfc/manifest-ci-qemu-armv8a.yaml``
   * - rv-virt
     - ``ntfc/configs/rv-virt/smoke/config.yaml``
     - ``rv-virt-smoke``
     - ``ntfc/manifest-ci-rv-virt.yaml``
   * - qemu-intel64
     - ``ntfc/configs/qemu-intel64/smoke/config.yaml``
     - ``qemu-intel64-smoke``
     - ``ntfc/manifest-ci-qemu-intel64.yaml``

Each target runs this module exactly once; all four sessions run
``testpath: ntfc/tests/smoke``.

Test functions
~~~~~~~~~~~~~~

``test_uname``
  Sends ``uname -a`` to the node's NSH prompt
  (``pytest.product.sendCommand``, 10 s timeout) and asserts that the
  command returns ``0`` after the shell's output matched the pattern
  ``NuttX``. PASS: the ``sendCommand`` call returns ``0`` (i.e. NSH
  responded and the string ``NuttX`` appeared in its output within the
  timeout).

``test_nettl_loopback_tcp``
  Marked ``@pytest.mark.cmd_check("nettl_main")`` (skipped if the
  built ELF has no ``nettl_main`` symbol). Starts
  ``nettl -s -p 5100 -t 30 &`` as a background NSH command on node 0
  via ``nettl_server()``, waits for its ``listening`` output, then
  runs ``nettl -c 127.0.0.1 -p 5100 -n 8192`` via ``nettl_client()``
  and captures its verdict line (120 s timeout). PASS: the captured
  verdict line is exactly ``nettl: PASS tx=8192 rx=8192 err=0``.

``test_nettl_loopback_udp``
  Marked ``@pytest.mark.cmd_check("nettl_main")``. Same pattern as
  ``test_nettl_loopback_tcp`` but with ``-u``: starts
  ``nettl -s -u -p 5101 -t 30 &``, then runs
  ``nettl -c 127.0.0.1 -u -p 5101 -n 50``. PASS: the captured verdict
  line is exactly ``nettl: PASS tx=50 rx=50 lost=0 err=0``.

``ip`` module
-------------

Source: ``ntfc/tests/ip/test_ip_pair.py``. Two nodes and the host on
bridge ``tl-br0`` (see :doc:`architecture`). Sessions:
``sim-ip-pair``, ``qemu-armv8a-ip-pair``, ``rv-virt-ip-pair``,
``qemu-intel64-ip-pair``.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_ping_host``
     - Each node: ``ping -c 3 10.42.0.1``, 0% loss.
   * - ``test_ping_node_to_node``
     - node0: ``ping -c 3 10.42.0.11``, 0% loss.
   * - ``test_nettl_node_to_node``
     - node0 client to node1 server: ``nettl: PASS`` (TCP 262144 bytes,
       UDP 200 datagrams).
   * - ``test_host_tcp_to_node``
     - Host client to ``nettl -s`` on each node: 131072 bytes echoed
       intact.
   * - ``test_host_udp_to_node``
     - Host client to ``nettl -s -u`` on each node: 100 datagrams echoed
       intact.

``ip`` regression tests
------------------------

Source: ``ntfc/tests/ip/test_ip_regress.py``. Same topology and
sessions as the ``ip`` module above (``sim-ip-pair``,
``qemu-armv8a-ip-pair``, ``rv-virt-ip-pair``, ``qemu-intel64-ip-pair``).

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_tcp_rst_after_handshake``
     - A peer RST right after the TCP handshake does not hang the
       server's blocking send(): a verdict line (PASS or FAIL) is
       printed within 15 s.
   * - ``test_udp_reuseaddr_broadcast``
     - Two ``SO_REUSEADDR`` UDP listeners on one port both receive all
       20 broadcast datagrams intact: ``nettl: PASS rx=40 err=0``.
   * - ``test_arp_expiry_traffic``
     - 300 paced UDP echoes (~30 s, over 2x the default
       ``CONFIG_NET_ARP_MAXAGE``) across an ARP entry's expiry: zero
       datagrams lost after up to 3 retries each, all intact.
   * - ``test_tcp_long_transfer``
     - 4 MiB host-to-node TCP echo, 10 s per-operation stall
       watchdog: all bytes echoed intact.
   * - ``test_tcp_kill_listener_leak``
     - After killing (``kill -9``) ``CONFIG_NET_TCP_PREALLOC_CONNS +
       1`` TCP listeners each blocked in accept(), a probe client
       still allocates a socket and is refused by the host
       (``connect failed 111``). Runs last in the session
       (``@pytest.mark.run(order=-1)``): it poisons the node.

``can`` module
---------------

Source: ``ntfc/tests/can/test_can_bus.py``. Two nodes and the host
share one SocketCAN bus, vcan ``can0`` (see :doc:`architecture`).
Sessions: ``sim-can-bus``.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_can_node_to_node``
     - Classic and CAN FD variants: node 0 ``cantl -s`` to node 1's
       filtered ``cantl -r``. PASS: node 0 reports
       ``cantl: PASS tx=30`` and node 1 reports
       ``cantl: PASS rx=30 lost=0 err=0``.
   * - ``test_can_host_to_nodes``
     - The host sends 20 frames; both nodes' receivers report
       ``cantl: PASS rx=20 lost=0 err=0``.
   * - ``test_can_node_to_host``
     - Node 0 sends 20 frames to a host socket bound before the send
       starts; the host decodes all 20 intact (0 lost, 0 corrupt).
   * - ``test_can_filter``
     - A node 1 receiver filtered on CAN ID ``0x123`` still reports
       ``cantl: PASS rx=20 lost=0 err=0`` while the host sends
       unrelated ``0x456`` frames before, during and after node 0's
       ``0x123`` transmission.
