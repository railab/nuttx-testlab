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

Sessions
--------

Defconfig ``hw-rig1``: upstream ``nsh`` + ``hwtl``, ``SIG_DEFAULT``,
``NSH_MAXARGUMENTS=32``, ``LINE_MAX=256`` and the board devices below,
registered by the STM32 board common code
(``BOARD_STM32_COMMON_TIMn_TIMER``/``_ONESHOT``,
``BOARD_STM32_COMMON_LPTIMn_TIMER``/``_ONESHOT``), and ``/dev/random``
from ``STM32_RNG`` where listed. Session names are
``<board>-hw-rig1``. All boards are flashed with ``STM32_Programmer_CLI
-c port=SWD sn=<probe> mode=UR -d nuttx.bin 0x08000000 -v -rst``.

nucleo-g071rb
-------------

Console USART2 (PA2/PA3, ST-LINK VCP). Timers: LPTIM1 ``/dev/lptimer1``.
Oneshots: LPTIM2 ``/dev/lponeshot2``.

nucleo-c092rc
-------------

Console USART2 (PA2/PA3, ST-LINK VCP). Timers: TIM2 ``/dev/timer2``,
TIM3 ``/dev/timer3``. Oneshots: TIM14 ``/dev/oneshot14``, TIM16
``/dev/oneshot16``.

nucleo-c562re
-------------

Console USART2 (PA2/PA3, ST-LINK VCP). Timers: TIM2 ``/dev/timer2``,
TIM7 ``/dev/timer7``, LPTIM1 ``/dev/lptimer1``. Oneshots: TIM6
``/dev/oneshot6``.

nucleo-f091rc
-------------

Console USART2 (PA2/PA3, ST-LINK VCP). Timers: TIM2 ``/dev/timer2``,
TIM7 ``/dev/timer7``. Oneshots: TIM6 ``/dev/oneshot6``, TIM14
``/dev/oneshot14``.

nucleo-f446re
-------------

Console USART2 (PA2/PA3, ST-LINK VCP). Timers: TIM2 ``/dev/timer2``,
TIM7 ``/dev/timer7``. Oneshots: TIM6 ``/dev/oneshot6``, TIM5
``/dev/oneshot5``. ``/dev/oneshot5`` is 32-bit: no half-maximum delay
entry.

nucleo-f722ze
-------------

Console USART3 (PD8/PD9, ST-LINK VCP). Timers: LPTIM1 ``/dev/lptimer1``.

nucleo-f767zi
-------------

Console USART3 (PD8/PD9, ST-LINK VCP). Timers: TIM2 ``/dev/timer2``,
TIM5 ``/dev/timer5``, TIM6 ``/dev/timer6``, LPTIM1 ``/dev/lptimer1``.
Oneshots: TIM7 ``/dev/oneshot7``.

nucleo-l552ze
-------------

NUCLEO-L552ZE-Q. Console LPUART1 (PG7/PG8, ST-LINK VCP). Timers: TIM2
``/dev/timer2``, LPTIM1 ``/dev/lptimer1``, LPTIM3 ``/dev/lptimer3``.
Oneshots: TIM6 ``/dev/oneshot6``, LPTIM2 ``/dev/lponeshot2``.

nucleo-u083rc
-------------

Console USART2 (PA2/PA3, ST-LINK VCP). Timers: TIM2 ``/dev/timer2``,
LPTIM2 ``/dev/lptimer2``. Oneshots: TIM6 ``/dev/oneshot6``, LPTIM3
``/dev/lponeshot3``.

nucleo-u3c5zi-q
---------------

Console USART1 (PA9/PA10, ST-LINK VCP). Timers: LPTIM1
``/dev/lptimer1``, LPTIM3 ``/dev/lptimer3``. Oneshots: LPTIM2
``/dev/lponeshot2``, LPTIM4 ``/dev/lponeshot4``.

nucleo-l073rz
-------------

Console USART2 (PA2/PA3, ST-LINK VCP). Timers: LPTIM1 ``/dev/lptimer1``.

nucleo-l476rg
-------------

Console USART2 (PA2/PA3, ST-LINK VCP). Timers: TIM2 ``/dev/timer2``,
LPTIM1 ``/dev/lptimer1``. Oneshots: TIM6 ``/dev/oneshot6``, LPTIM2
``/dev/lponeshot2``.

nucleo-f303ze
-------------

Console USART3 (PD8/PD9, ST-LINK VCP). Timers: TIM2 ``/dev/timer2``,
TIM7 ``/dev/timer7``. Oneshots: TIM6 ``/dev/oneshot6``.

nucleo-c071rb
-------------

Console USART2 (PA2/PA3, ST-LINK VCP). Timers: TIM2 ``/dev/timer2``,
TIM3 ``/dev/timer3``. Oneshots: TIM14 ``/dev/oneshot14``, TIM16
``/dev/oneshot16``.

nucleo-h533re
-------------

Console USART2 (PA2/PA3, ST-LINK VCP). Timers: TIM2 ``/dev/timer2``,
TIM7 ``/dev/timer7``, LPTIM1 ``/dev/lptimer1``. Oneshots: TIM6
``/dev/oneshot6``, LPTIM2 ``/dev/lponeshot2``. RNG ``/dev/random``.

nucleo-h743zi
-------------

Console USART3 (PD8/PD9, ST-LINK VCP). Timers: TIM2 ``/dev/timer2``,
TIM7 ``/dev/timer7``, LPTIM1 ``/dev/lptimer1``, LPTIM3
``/dev/lptimer3``, LPTIM5 ``/dev/lptimer5``. Oneshots: TIM6
``/dev/oneshot6``, LPTIM2 ``/dev/lponeshot2``, LPTIM4
``/dev/lponeshot4``. RNG ``/dev/random``.
