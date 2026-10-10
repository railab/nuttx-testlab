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

``test_nsh_max_args``
  ``hello`` with as many argv entries as NSH accepts
  (``CONFIG_NSH_MAXARGUMENTS``, raised to 11 by ``nshlib/nsh.h`` with
  networking and ``ifconfig``). PASS: ``Hello, World`` printed, no
  ``too many arguments`` error.

``test_nsh_too_many_args``
  ``hello`` with one argv entry more. PASS: NSH prints
  ``nsh: hello: too many arguments`` and ``hello`` does not run.
  Skipped if the command line does not fit in ``CONFIG_LINE_MAX``.

``test_exit_ends_all_threads``
  Runs ``exittl`` (``apps/exittl``): a child task whose second thread
  catches ``SIGTERM`` and waits in ``pause()`` calls ``exit()`` from
  its main thread. PASS: ``exittl: PASS`` (the child is gone within
  5 s and the ``SIGTERM`` handler did not run).

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
   * - ``test_udp_kill_poll_close``
     - A UDP server blocked in poll() and killed (``kill -9``, and
       ``kill -15``) leaves ``/proc/net/udp`` within 5 s; a restarted
       server on the same port receives all 3 host datagrams. Needs
       ``CONFIG_SIG_DEFAULT``.
   * - ``test_kill_blocked_call_close``
     - A ``nettl`` server killed (``kill -9``) while blocked in an OS
       call releases its socket. ``recvfrom`` (``nettl -s -u``) and
       ``epoll_wait`` (``nettl -s -u -L 1 -E``): the port leaves
       ``/proc/net/udp`` within 5 s, the node survives 3 host
       datagrams sent to it, a restarted server echoes 3 datagrams.
       ``accept`` (``nettl -s``): a host connect is refused within
       5 s, a restarted server echoes 4 KiB. ``recv`` (``nettl -s``
       with a host connection): the host sees the connection closed
       within 5 s, a server on another port echoes 4 KiB. Needs
       ``CONFIG_SIG_DEFAULT``.
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
   * - ``test_tcp_keepalive_dead_peer_poll``
     - Same, client waiting in ``poll(POLLIN)`` (``nettl -P``):
       ``POLLERR`` and ``POLLHUP`` within 10 s, then
       ``getsockopt(SO_ERROR)`` returns ``ETIMEDOUT``.

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
Sessions: ``sim-can-bus``, ``qemu-intel64-can-bus`` (SocketCAN,
``can0``), ``sim-can-char`` and ``qemu-intel64-can-char`` (CAN
character driver, ``/dev/can0``). Nodes and the host send 1 ms apart
unless stated otherwise.

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
   * - ``test_can_node_burst_to_host``
     - Node 0 sends 200 frames back to back; the host decodes all 200
       intact (0 lost, 0 corrupt).

``can`` socket options
----------------------

Source: ``ntfc/tests/can/test_can_sockopt.py``. Node 0 runs one
``canopt`` check (``apps/canopt``) on ``can0``; the host plays the bus
side. The frame tables (classic: SFF, EFF, RTR, DLC 0..8; CAN FD: all
lengths 0..64 with BRS/ESI) are in ``ntfc/tests/_canopt_common.py``.
Sessions: ``sim-can-bus``, ``qemu-intel64-can-bus``.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_can_sockopt_roundtrip``
     - ``CAN_RAW_FILTER`` (default catch-all, up to
       ``NET_CAN_RAW_FILTER_MAX``, empty list), ``CAN_RAW_ERR_FILTER``,
       ``CAN_RAW_LOOPBACK``, ``CAN_RAW_RECV_OWN_MSGS``,
       ``CAN_RAW_FD_FRAMES`` and ``SO_TIMESTAMP`` read back what was set;
       too many filters and bad lengths give ``EINVAL``, unknown options
       ``ENOPROTOOPT``.
   * - ``test_can_sockopt_no_alias``
     - Setting ``SO_TIMESTAMPNS`` leaves ``CAN_RAW_FD_FRAMES`` at 0 and
       vice versa.
   * - ``test_can_so_rcvbuf``
     - ``SO_RCVBUF`` at level ``SOL_SOCKET`` is accepted and read back.
   * - ``test_can_so_rcvtimeo``
     - A read on an idle socket with a 300 ms ``SO_RCVTIMEO`` fails with
       ``EAGAIN`` after 200..3000 ms.
   * - ``test_can_raw_filter``
     - Variants ``default``, ``empty``, ``sff-any-format``,
       ``sff-exact``, ``multi``, ``inverted`` (``CAN_INV_FILTER``),
       ``eff-exact``, ``rtr``, ``max``: the node receives exactly the
       classic-table frames the filter list matches under SocketCAN
       rules, in order and intact.
   * - ``test_can_loopback``
     - Variants ``off``, ``on``, ``recv-own``: a frame sent on socket A
       reaches socket B on the same node only with
       ``CAN_RAW_LOOPBACK``, A itself only with
       ``CAN_RAW_RECV_OWN_MSGS`` too; the host always receives it.
   * - ``test_can_nonblock_poll``
     - ``EAGAIN`` on an empty socket (``O_NONBLOCK``, ``MSG_DONTWAIT``),
       ``poll()`` idle timeout and ``POLLOUT``, a nonblocking write the
       host receives, ``select()`` and ``poll()`` wake-ups for two host
       frames.
   * - ``test_can_poll_hup``
     - ``poll()`` returns ``POLLHUP`` on ``ifdown can0``.
   * - ``test_can_stats``
     - ``/proc/net/stat`` CAN ``Received`` grows by the 5 host frames,
       ``Sent`` by the frames ``canopt tx`` sent.

``can`` frame types
-------------------

Source: ``ntfc/tests/can/test_can_frames.py``. Same setup as `can socket
options`_.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_can_tx_frame_types``
     - The host receives both tables from the node intact (ID, flags,
       length, BRS/ESI, payload); a ``CANFD_MTU`` write on a classic
       socket fails with ``EINVAL``.
   * - ``test_can_fd_rx``
     - A ``CAN_RAW_FD_FRAMES`` socket receives both tables: CAN FD
       frames as ``CANFD_MTU`` with length and BRS/ESI intact, classic
       frames as ``CAN_MTU``.
   * - ``test_can_fd_to_classic_socket``
     - Variants ``blocking`` (reader waits in ``read()``) and ``queued``
       (frames queue before the first read): a classic socket on a bus
       carrying both tables reads only the 18 classic frames, each
       ``CAN_MTU`` bytes.

``can`` timestamps
------------------

Source: ``ntfc/tests/can/test_can_timestamp.py``. The host sends 10
frames 20 ms apart to ``canopt ts``, which lets the first ones queue
before it reads. Sessions: ``sim-can-bus``, ``qemu-intel64-can-bus``.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_can_rx_timestamp``
     - Variants ``us`` (``SO_TIMESTAMP``) and ``ns``
       (``SO_TIMESTAMPNS``): one control message per frame,
       ``CLOCK_REALTIME`` between check start and read, monotonic,
       spread over at least half the host's 180 ms send span.
   * - ``test_can_rx_no_timestamp``
     - Variants ``classic`` and ``fd`` socket: without a timestamp
       option ``recvmsg()`` returns no control message.

``can`` character driver
------------------------

Source: ``ntfc/tests/can/test_can_char.py``. ``canopt`` on
``/dev/can0`` of node 0; skipped on SocketCAN nodes. Sessions:
``sim-can-char``, ``qemu-intel64-can-char``.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_can_char_rx_frame_types``
     - ``read()`` returns both tables intact: ID, ``ch_extid``,
       ``ch_rtr``, DLC (CAN FD DLC 9..15 = 12..64 bytes),
       ``ch_edl``/``ch_brs``/``ch_esi``, payload.
   * - ``test_can_char_tx_frame_types``
     - ``write()`` of both tables; the host receives every frame intact.
   * - ``test_can_char_ioctl``
     - ``CANIOC_GET/SET_MSGALIGN`` and ``FIONWRITE`` round trip;
       ``CANIOC_GET_BITTIMING`` and ``CANIOC_ADD/DEL_STDFILTER`` work or
       return ``ENOTTY``.
   * - ``test_can_char_msgalign``
     - 6 queued messages: alignment 1 returns 3 packed messages into a
       3-message buffer, 0 exactly one, 16 two messages padded to 16
       bytes each.
   * - ``test_can_char_nonblock_poll``
     - ``O_NONBLOCK``: ``EAGAIN`` when empty, ``poll()`` idle timeout and
       ``POLLOUT``, a nonblocking write the host receives, ``POLLIN`` for
       a host frame, then ``EAGAIN``.
   * - ``test_can_char_rx_overflow``
     - ``CONFIG_CAN_RXFIFOSIZE + 16`` unread host frames: one
       ``CAN_ERROR5_RXOVERFLOW`` error message, then
       ``CONFIG_CAN_RXFIFOSIZE - 1`` frames.
   * - ``test_can_char_fionread``
     - ``FIONREAD`` returns 5 (``int``) with 5 messages queued.
   * - ``test_can_char_iflush``
     - ``CANIOC_IFLUSH`` returns 0; the next nonblocking read returns
       ``EAGAIN``.
   * - ``test_can_char_rtr_request``
     - ``CANIOC_RTR`` returns the 8-byte reply a host responder sends to
       the remote request.

``can`` CANopen
----------------

Source: ``ntfc/tests/can/test_canopen.py`` (helpers in
``ntfc/tests/_canopen_common.py``). The nuttx-apps Lely CANopen
examples against python-canopen on the host: ``coslave`` (node-ID 2,
heartbeat 50 ms, TPDO1 ``0x182`` on every SYNC with a counter
incremented every 100 ms) and ``comaster`` (node-ID 1, heartbeat
50 ms, SYNC every 100 ms; reads ``0x1000`` of node 2, starts it and
prints each RPDO1 counter). Both exit on NMT reset node. Same sessions
as the ``can`` module.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_canopen_slave_boot``
     - The first frame is the boot-up message; then PRE-OPERATIONAL
       heartbeats, mean period 40-62.5 ms, no gap of 100 ms.
   * - ``test_canopen_slave_nmt``
     - Start, stop, pre-operational (to node 2 and to all nodes) give
       heartbeat states ``0x05``, ``0x04``, ``0x7F``; a command for
       node 3 is ignored; reset communication sends boot-up, then
       ``0x7F``.
   * - ``test_canopen_slave_frame_burst``
     - Back-to-back NMT start and stop leave the slave STOPPED (3
       times).
   * - ``test_canopen_slave_sdo_read``
     - Expedited uploads of ``0x1000``, ``0x1005``, ``0x1017``,
       ``0x1018``, ``0x1800``, ``0x1A00`` and ``0x1F80`` return the
       example's object dictionary values.
   * - ``test_canopen_slave_sdo_write``
     - Expedited, segmented and block downloads to ``0x2000`` read back
       (block upload included).
   * - ``test_canopen_slave_sdo_abort``
     - Abort codes: write to read-only ``0x2001`` ``0x06010002``,
       object ``0x3000`` ``0x06020000``, sub-index ``0x1018:9``
       ``0x06090011``, 2 bytes to a ``UNSIGNED32`` ``0x06070013``.
   * - ``test_canopen_slave_stopped``
     - A STOPPED slave answers neither SDO nor SYNC; after
       pre-operational it answers SDO again.
   * - ``test_canopen_slave_heartbeat_time``
     - Writing ``0x1017`` = 200 gives a 180-220 ms mean heartbeat
       period; 0 stops the heartbeat; 50 restarts it.
   * - ``test_canopen_slave_tpdo``
     - No TPDO on SYNC in PRE-OPERATIONAL; in OPERATIONAL 10 SYNCs give
       10 4-byte TPDOs; the counter never decreases and grows.
   * - ``test_canopen_slave_tpdo_config``
     - TPDO1 disabled, transmission type 2 and COB-ID ``0x190`` over SDO:
       10 SYNCs give 5 TPDOs on ``0x190``, none on ``0x182``.
   * - ``test_canopen_slave_time``
     - A TIME message for 2030-01-01 12:00 sets the node clock: ``date``
       shows ``Jan 01 12:00:xx 2030``.
   * - ``test_canopen_slave_reset_node``
     - NMT reset node stops the heartbeat and ends ``coslave``.
   * - ``test_canopen_master_boot``
     - With a host slave, ``comaster`` sends an SDO upload of
       ``0x1000``, prints the host value, starts the host slave (state
       OPERATIONAL), sends boot-up then ``0x05`` heartbeats and SYNC
       with a 90-110 ms mean period.
   * - ``test_canopen_master_rpdo``
     - PDOs ``0x182`` from the host with 7, 1000 and ``0xDEADBEEF`` are
       printed by ``comaster`` in order.
   * - ``test_canopen_master_sdo_abort``
     - A host slave without ``0x1000``: ``comaster`` prints abort code
       ``0x06020000`` and still starts the network.
   * - ``test_canopen_node_to_node``
     - ``comaster`` on node 0, ``coslave`` on node 1: device type
       ``0x00000000`` read, node 1 OPERATIONAL, at least 5 increasing
       counter values printed from its TPDOs.
   * - ``test_canopen_classic_frames``
     - Every frame ``coslave`` sends during NMT start, 5 SYNCs and an
       SDO upload is a classic CAN frame.

``canopen`` network
-------------------

Source: ``ntfc/tests/canopen/test_canopen_net.py`` (helpers in
``ntfc/tests/_canopen_net.py``). ``conode`` master node-ID 1 (node 0),
slaves 2-4 (nodes 1-3), python-canopen node 5 on the host; every node
receives TPDO1 of all others. SYNC 100 ms, heartbeat 100 ms, master
heartbeat consumer 500 ms. Every PDO check also requires no transmit
error on any node. Session: ``sim-canopen-net``.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Test
     - PASS criterion
   * - ``test_canopen_net_boot``
     - The master sees nodes 2-5 OPERATIONAL, each booted at least once,
       no heartbeat timeout; all heartbeats report ``0x05``.
   * - ``test_canopen_net_master_sdo``
     - ``0x2000`` of nodes 2-5 holds the value the master wrote at their
       last boot.
   * - ``test_canopen_net_heartbeat``
     - Nodes 1-4 send ``0x05`` heartbeats, mean period 80-125 ms, no gap
       of 200 ms.
   * - ``test_canopen_net_pdo``
     - Over 20 SYNCs every node receives TPDO1 of every other node, no
       counter skipped or repeated; one TPDO per SYNC per node.
   * - ``test_canopen_net_host_sdo_concurrent``
     - Parallel host SDO to nodes 1-4, 30 rounds each: expedited and
       segmented write of ``0x2001`` read back, ``0x1018:1`` read.
   * - ``test_canopen_net_nmt_broadcast``
     - NMT stop and pre-operational to all: nodes 2-5 report ``0x04`` and
       ``0x7F`` (heartbeat and master view), send no TPDO; the master
       stays OPERATIONAL with SYNC and TPDO. Start to all: PDOs resume
       without gaps.
   * - ``test_canopen_net_hb_timeout``
     - ``conode`` on node 4 stopped: the master counts one heartbeat
       timeout within 1.2 s. Restarted: one recovery, one boot-up,
       ``0x2000`` rewritten, OPERATIONAL, PDOs without gaps.
   * - ``test_canopen_net_duplicate_bootup``
     - A host boot-up for running node 3: the master boots it again
       (``0x2000`` rewritten, NMT start); node 3 stays OPERATIONAL and
       its PDOs continue without gaps.
   * - ``test_canopen_net_duplicate_bootup_state``
     - After a duplicate boot-up the master's state of node 3 returns to
       OPERATIONAL.
   * - ``test_canopen_net_bus_load``
     - SYNC 2 ms for 5 s (``0x1006`` over SDO): mean SYNC period at most
       2.5 ms, every receiver got at least 95% of the PDOs of each node
       without gaps, no heartbeat timeout.
   * - ``test_canopen_net_master_restart``
     - The stopped master prints ``conode: PASS``; restarted, it resets
       all nodes, sees one boot-up of each and boots all to
       OPERATIONAL.
   * - ``test_canopen_net_kill_restart``
     - ``conode`` on node 4 killed with SIGKILL and restarted is booted
       to OPERATIONAL and answers SDO.

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
