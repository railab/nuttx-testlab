.. SPDX-License-Identifier: Apache-2.0

nuttx-testlab
=============

``nuttx-testlab`` builds Apache NuttX and runs runtime tests against it
on the ``sim`` target and under QEMU (``qemu-armv8a``, ``rv-virt``,
``qemu-intel64``), driven by `NTFC <https://github.com/apache/nuttx-ntfc>`_.

This documentation describes what is in the repository: the build and
test architecture, and the test cases that exist today. It does not
describe plans, roadmaps or unimplemented scenarios.

.. toctree::
   :maxdepth: 2

   architecture
   test-cases
