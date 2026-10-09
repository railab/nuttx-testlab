.. SPDX-License-Identifier: Apache-2.0

Test cases
==========

This page lists every NTFC test module and test function that exists
under ``ntfc/tests/`` today, what each one does and its exact PASS
criteria. Shared helpers live in ``ntfc/tests/_*.py``;
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

``ip`` IPv6
-----------

Source: ``ntfc/tests/ip/test_ip_v6.py``. The ``ip`` module tests over
IPv6 (host ``fc00::1``, nodes ``fc00::10``/``fc00::11``). Same sessions
as the ``ip`` module.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_ping6_host``
     - Each node: ``ping6 -c 3 fc00::1``, 0% loss.
   * - ``test_ping6_node_to_node``
     - node0: ``ping6 -c 3 fc00::11``, 0% loss.
   * - ``test_nettl6_node_to_node``
     - node0 client to node1 server over IPv6: ``nettl: PASS`` (TCP
       262144 bytes, UDP 200 datagrams).
   * - ``test_host_tcp6_to_node``
     - Host TCP client to ``nettl -s -6`` on each node: 131072 bytes
       echoed intact.
   * - ``test_host_udp6_to_node``
     - Host UDP client to ``nettl -s -6 -u`` on each node: 100 datagrams
       echoed intact.

``ip`` fragmentation
--------------------

Source: ``ntfc/tests/ip/test_ip_frag.py``. Datagrams larger than the
1500-byte MTU, IPv4 and IPv6 variants. Same sessions as the ``ip``
module.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_ping_fragmented_host``
     - node0: ``ping``/``ping6 -s 4000 -c 3`` to the host, 0% loss.
   * - ``test_udp_fragmented_node_to_node``
     - node0 client to node1 server, 50 datagrams of 6000 bytes:
       ``nettl: PASS``.
   * - ``test_host_udp_fragmented_to_node``
     - Host sends 50 datagrams of 6000 bytes to node0: all echoed intact.
   * - ``test_frag_reorder_and_stale``
     - Host injects raw IPv4 fragments to node0: a datagram with reversed
       fragments, then one after 32 incomplete datagrams, then one after
       the reassembly timeout; each is echoed intact.

``ip`` DHCP
-----------

Source: ``ntfc/tests/ip/test_ip_dhcp.py``. node1 runs ``renew eth0``
against a DHCP server and gets its static address back after each
test. Same sessions as the ``ip`` module.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_dhcpc_from_host``
     - A host DHCP server leases ``10.42.0.150``: the server acknowledges
       the request, node1 has the address and pings the host.
   * - ``test_dhcpd_node_to_node``
     - ``dhcpd_start eth0`` on node0 leases ``10.42.0.100``: node1 has the
       address and pings node0.

``ip`` multicast
----------------

Source: ``ntfc/tests/ip/test_ip_mcast.py``. IPv4 (IGMP, group
``239.42.0.1``) and IPv6 (MLD, group ``ff12::42``) variants. The host
bridge is the querier (General Query every 5 s, membership timeout
15 s); its group table (``bridge mdb``) shows the node reports. Same
sessions as the ``ip`` module.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_mcast_host_to_node``
     - node0 ``nettl -s -u -g`` joins the group (bridge shows the
       membership); 50 host datagrams to the group echoed intact.
   * - ``test_mcast_node_to_node``
     - node1 joins the group; node0 client to the group:
       ``nettl: PASS``.
   * - ``test_mcast_membership_lifetime``
     - node0's membership is still in the bridge table 20 s after the
       join, and is gone within 10 s after its socket closes.

``ip`` TCP
----------

Source: ``ntfc/tests/ip/test_ip_tcp.py``. TCP connection handling on
node0 against the host. Same sessions as the ``ip`` module.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_tcp_concurrent_host_to_node``
     - ``nettl -s -C 8``: 8 host connections, opened one after another
       (each once the previous one echoed its first 1 KiB), 32 KiB each,
       echoed interleaved and intact.
   * - ``test_tcp_half_close``
     - 64 KiB sent, then ``shutdown(SHUT_WR)``: all data echoed, then
       EOF from the node.
   * - ``test_tcp_churn_node_client``
     - 3x ``CONFIG_NET_TCP_PREALLOC_CONNS`` sequential node client
       connections to a host echo server (node closes first, so each
       socket ends in TIME_WAIT): all pass. Runs second to last in the
       session (``@pytest.mark.run(order=-2)``).
   * - ``test_tcp_churn_node_server``
     - 3x ``CONFIG_NET_TCP_PREALLOC_CONNS`` sequential host connections
       to node servers (host closes first): all pass.
   * - ``test_tcp_backlog_overflow``
     - 8 host connection attempts while the server waits 5 s before
       ``accept()``; afterwards the server churn above still passes.
   * - ``test_tcp_rst_mid_transfer``
     - A host RST during a transfer ends the node server with a
       verdict; a new server then echoes 16 KiB intact.
   * - ``test_tcp_keepalive_live_peer``
     - A node client with keep-alive (1 s idle, 1 s interval, 3 probes)
       idles 6 s against a host server, then echoes 1 KiB.
   * - ``test_tcp_keepalive_dead_peer``
     - Same client while the host stops answering (blackhole route to
       the node): ``recv()`` fails with ``ETIMEDOUT`` within 10 s.

``ip`` link changes
-------------------

Source: ``ntfc/tests/ip/test_ip_link.py``. Interface and link changes on
node1; its link, bridge port and static address are restored after
each test. Same sessions as the ``ip`` module.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_link_ifdown_ifup``
     - After ``ifdown eth0`` the host gets no ping replies; after
       ``ifup eth0`` node1 pings the host, answers the host and echoes
       16 KiB over TCP.
   * - ``test_link_host_port_flap``
     - node1's host bridge port is set down (no ping replies) and up
       again: node1 pings the host, answers the host and echoes 16 KiB
       over TCP.
   * - ``test_link_readdress``
     - ``ifconfig eth0 10.42.0.21``: node1 pings the host and answers
       on the new address, not on the old one, and echoes 16 KiB over
       TCP on the new address.

``ip`` Modbus TCP
-----------------

Source: ``ntfc/tests/ip/test_ip_modbus.py``. nxmodbus on the nodes:
``nxmbserver`` as slave (port 502), ``nxmbclient`` as master; the host
peer is pymodbus (master, or slave on port 1502). Same sessions as the
``ip`` module.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_modbus_slave_read``
     - A host master reads holding and input registers, coils and
       discrete inputs of node0 with the values of the nxmbserver
       register map.
   * - ``test_modbus_slave_write``
     - Single and multiple register and coil writes read back the
       written values.
   * - ``test_modbus_slave_exception``
     - Raw requests get the exception response: illegal data address
       (read at or across the end of the map), illegal data value
       (quantity 0 or 126, FC05 value not ``0x0000``/``0xFF00``).
   * - ``test_modbus_master_read``
     - node0 reads all four tables of a host slave and prints the
       slave's values.
   * - ``test_modbus_master_write``
     - ``write-holding``, ``write-holdings`` and ``write-coil`` of node0
       reach the host slave with the same function code, address and
       values, and read back.
   * - ``test_modbus_master_write_coils``
     - ``write-coils`` with 6 values sets each listed coil on the host
       slave (one FC15 request with the same values).
   * - ``test_modbus_master_exception``
     - A host slave exception response fails the read; without a slave
       the client reports the refused connection.
   * - ``test_modbus_master_to_node_slave``
     - node0 writes registers and coils on a node1 slave and reads them
       back; untouched registers keep the register map values.

``ip`` iperf
------------

Source: ``ntfc/tests/ip/test_ip_iperf.py``. ``iperf`` (``netutils/iperf``)
on the nodes, iperf2 on the host. Each run lasts 5 s and is measured by
the receiving node's server, or by the node client when the host
receives. Same sessions as the ``ip`` module.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_iperf_tcp_node_to_host``
     - node0 client to a host server: throughput at or above the floor.
   * - ``test_iperf_tcp_host_to_node``
     - Host client to a node0 server: throughput at or above the floor.
   * - ``test_iperf_udp_host_to_node``
     - Host client sends 1024-byte datagrams at 10 Mbit/s for 3 s to a
       node0 server: the node receives at least 95% of the bytes sent.
   * - ``test_iperf_tcp_node_to_node``
     - node0 client to a node1 server: throughput at or above the floor.

TCP floors in Mbit/s (``FLOORS`` in the test module):

.. list-table::
   :header-rows: 1

   * - Target
     - node to host
     - host to node
     - node to node
   * - sim
     - 80
     - 100
     - 2
   * - qemu-armv8a
     - 60
     - 80
     - 60
   * - rv-virt
     - 50
     - 70
     - 50
   * - qemu-intel64
     - 70
     - 60
     - 60

``ip`` MQTT
-----------

Source: ``ntfc/tests/ip/test_ip_mqtt.py``. MQTT clients on the nodes
against a mosquitto broker on the host bridge (port 1883, anonymous, no
persistence); the host peers are ``mosquitto_sub``/``mosquitto_pub``.
Paho MQTT C: ``mqtt_pub``, ``mqtt_sub``; MQTT-C: ``mqttc_pub``,
``mqttc_sub``. Before each test both nodes must have 4 free TCP
connections (``/proc/net/tcp``); the test waits up to 130 s for closed
connections to leave TIME_WAIT. Same sessions as the ``ip`` module.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_mqtt_pub``
     - node0 ``mqtt_pub`` at QoS 0, 1 and 2: a host subscriber receives
       exactly the payload at that QoS, not retained.
   * - ``test_mqtt_pub_retained``
     - ``mqtt_pub -r``: the broker gets a retained PUBLISH and a host
       subscriber started afterwards receives it as retained.
   * - ``test_mqtt_pub_not_retained``
     - ``mqtt_pub`` without ``-r``, run after a ``-r`` run, publishes
       with the retain flag clear.
   * - ``test_mqtt_sub``
     - node0 ``mqtt_sub`` prints a retained message on subscribe, then
       host messages at QoS 0, 1 and 2; on SIGINT it sends DISCONNECT.
   * - ``test_mqtt_node_to_node``
     - node0 ``mqtt_pub`` messages at QoS 0, 1 and 2 are printed by
       node1 ``mqtt_sub``.
   * - ``test_mqttc_pub``
     - ``mqttc_pub -n 2`` at QoS 0 and 1: a host subscriber receives
       both messages at that QoS.
   * - ``test_mqttc_sub_reconnect``
     - node0 ``mqttc_sub`` prints a host message, resubscribes after a
       broker restart and prints the next one, and exits on ``q``.
   * - ``test_mqtt_no_broker``
     - With the broker stopped, ``mqtt_pub`` reports ``Connect failed``
       and then exits on SIGINT. Runs last.

``ip`` services
---------------

Source: ``ntfc/tests/ip/test_ip_services.py``. Node 0 against servers
and clients on the host (``ntfc/tests/_host_services.py``, standard
ports, needs root). Same sessions as the ``ip`` module. Files go to a
tmpfs at ``/tl`` and are checked with ``md5``.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_wget_from_host``
     - ``wget`` of a 65659-byte file from a host HTTP server; node
       ``md5`` matches.
   * - ``test_tftp_get_put``
     - TFTP ``get`` of a 20000-byte file from the host (node ``md5``
       matches), then ``put`` back; the host receives identical data.
   * - ``test_ntpc_from_host``
     - ``ntpcstart`` against a host SNTP server reporting 2030-01-01:
       ``date`` shows 2030 within 60 s.
   * - ``test_ntpc_stop_no_server``
     - With no NTP server on the host, ``ntpcstop`` returns to the prompt
       within 3 s of a running ``ntpcstart``.
   * - ``test_nslookup_from_host``
     - ``nslookup peer.testlab`` through a host DNS server prints
       ``Addr: 10.42.0.99``.
   * - ``test_host_telnet_to_node``
     - Host telnet session to the node's ``telnetd`` runs ``echo``; the
       output line comes back ending with CR LF.
   * - ``test_host_ftp_to_node``
     - Host ``ftplib`` STOR then RETR of a 40000-byte file on the node's
       ``ftpd_start`` server: identical data, node ``md5`` matches.

``can`` module
---------------

Source: ``ntfc/tests/can/test_can_bus.py``. Two nodes and the host
share one SocketCAN bus, vcan ``can0`` (see :doc:`architecture`).
Sessions: ``sim-can-bus`` (SocketCAN, ``can0``) and ``sim-can-char``
(CAN character driver, ``/dev/can0``).

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

``modbus`` module
-----------------

Source: ``ntfc/tests/modbus/test_modbus_rtu.py``. Modbus RTU between
one node and pymodbus on the host over a serial line (see
:doc:`architecture`), host end ``/dev/ttyTL1``. nxmodbus:
``nxmbserver`` slave (unit 1) and ``nxmbclient`` master at 19200 baud,
register maps as in `ip Modbus TCP`_. Sessions: ``sim-modbus-rtu``,
``qemu-armv8a-modbus-rtu``, ``rv-virt-modbus-rtu``,
``qemu-intel64-modbus-rtu``.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_modbus_rtu_master_read``
     - The node reads all four tables of a host slave and prints the
       slave's values.
   * - ``test_modbus_rtu_master_write``
     - ``write-holding``, ``write-holdings`` and ``write-coil`` reach
       the host slave with the same function code, address and values,
       and read back.
   * - ``test_modbus_rtu_master_write_coils``
     - ``write-coils`` with 6 values sets each listed coil on the host
       slave (one FC15 request with the same values).
   * - ``test_modbus_rtu_master_exception``
     - A host slave exception response fails the read; with no slave
       on the line the read times out (``-110``).
   * - ``test_modbus_rtu_slave_read``
     - A host master reads holding and input registers, coils and
       discrete inputs with the values of the nxmbserver register map.
   * - ``test_modbus_rtu_slave_write``
     - FC05, FC06, FC15, FC16 and FC23 writes read back the written
       values.
   * - ``test_modbus_rtu_slave_diag``
     - Diagnostics Return Query Data (FC08/0) echoes the request.
   * - ``test_modbus_rtu_slave_id``
     - Report Server ID (FC17) returns byte count 2, unit 1, run
       indicator ``0xFF``.
   * - ``test_modbus_rtu_slave_exception``
     - Raw requests get the exception response, as
       ``test_modbus_slave_exception``.
   * - ``test_modbus_rtu_slave_framing``
     - No reply to a frame with a bad CRC or for unit 2; a broadcast
       (unit 0) write gets no reply but is applied.
   * - ``test_modbus_rtu_slave_restart``
     - ``nxmbserver`` started a second time in the same boot serves
       requests.

``python`` module
-----------------

Source: ``ntfc/tests/python/test_python.py``. CPython
(``apps/interpreters/python``) on one node with QEMU user networking;
the host is ``10.0.2.2`` for the node. Sessions ``rv-virt-python`` and
``qemu-intel64-python``.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_python_version``
     - ``python -c`` prints the major version ``3``.
   * - ``test_python_stdlib``
     - ``hashlib``, ``zlib``, ``base64``, ``struct``, ``math``, ``json``
       and ``re`` give the same results as on the host.
   * - ``test_python_exception``
     - ``1/0`` prints a ``ZeroDivisionError`` traceback; NSH answers
       afterwards.
   * - ``test_python_files``
     - A 1000-line file in ``/tmp`` is written, read back, listed and
       removed.
   * - ``test_python_script``
     - A script written with ``echo`` computes ``fib(90)`` and a sum.
   * - ``test_python_threads``
     - 4 threads increment a counter 1000 times each under a lock (4000);
       ``time.sleep(0.5)`` takes 0.45 to 1.5 s.
   * - ``test_python_thread_stack``
     - A thread serializes a 100-level nested list with ``json.dumps``
       (C recursion).
   * - ``test_python_socket_host``
     - A Python TCP client echoes 64 KiB with a host server intact.
