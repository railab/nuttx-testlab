.. SPDX-License-Identifier: Apache-2.0

Hardware sessions
=================

Real boards, one connected at a time, one defconfig per session. No
wiring. Tests: ``hw`` module in :doc:`test-cases`.

Running
-------

Host tools: ``arm-none-eabi-gcc``, ``cmake``, ``ninja``,
``STM32_Programmer_CLI`` from STM32CubeProgrammer on ``PATH``.

.. code-block:: sh

   ./repo_init.sh                  # sources.env, NUTTX_REPO/NUTTX_REF/... overrides
   python3 -m venv .venv
   .venv/bin/pip install -r ntfc/requirements.txt -e ./external/nuttx-ntfc
   tools/hw/probes.sh              # attached ST-LINK/J-Link: serial, tty
   tools/hw/run.sh <board> <board>-hw-rig1 [testpath]

``tools/hw/run.sh <board> <session> [testpath]``:

- Runs ``repo_init.sh`` if ``external/`` is missing and refuses an
  ``external/`` fetched from other sources than the requested ones
  (``external/sources.txt``).
- Looks the session up in ``ntfc/manifest-hw-<board>.yaml`` and renders
  its ``config.yaml.in`` to ``result/hw/<board>/<session>/config.yaml``:
  ``@PORT@`` is ``HW_PORT`` or the only ``/dev/ttyACM*`` (error if none
  or several), ``@PROBE@`` is ``HW_PROBE`` or the USB serial number of
  the console's probe (J-Link: leading zeros dropped).
- Runs ``python -m ntfc test --confpath ... --testpath ... --modules ...
  --flash`` on the host: build, flash, reset, tests. NTFC results go to
  ``result/<timestamp>/``.

Ports and probe serials are never committed.
