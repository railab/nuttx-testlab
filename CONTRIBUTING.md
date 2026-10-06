# Contributing

Contributions are welcome: new test scenarios, new targets, fixes.

1. Open an issue or a pull request against `master`.
2. Follow the rules and layout in [`AGENTS.md`](AGENTS.md) — they apply to
   humans and agents alike (NuttX style for C, NTFC style for Python,
   POSIX sh for scripts).
3. Run the lint commands from `AGENTS.md` and the affected test manifests
   locally (or in Docker) before submitting.
4. If a test exposes a NuttX bug, report it upstream at
   <https://github.com/apache/nuttx/issues> and link the issue in the test's
   `xfail` reason. Never delete a test to make CI green.
