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

Known skips/xfails
~~~~~~~~~~~~~~~~~~~

None. No test in this module carries a ``skip``, ``skipif`` or
``xfail`` marker; the only conditional marker in use is
``cmd_check("nettl_main")``, which is a build-capability check, not a
known-failure marker.
