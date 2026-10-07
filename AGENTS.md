# AGENTS.md

Rules for working in this repo. Humans: start with
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Layout

| Path            | Contents                                                          |
|-----------------|-------------------------------------------------------------------|
| `apps/`         | In-repo C test apps (NuttX coding standard, CMake + Kconfig)      |
| `boards/`       | Out-of-tree NuttX defconfigs, mirroring upstream board paths      |
| `ntfc/`         | NTFC Python test suites, configs and manifests                    |
| `testenv/`      | Host-side network setup scripts for multi-node scenarios          |
| `Documentation/`| Architecture and test-case documentation (Sphinx, RST)            |
| `tools/docker/` | Docker image and runner used for local/CI reproduction            |
| `.github/`      | PR checks (lint + tests), nightly, manual tests, Docker image publish |
| `external/`     | NuttX + nuttx-apps sources, fetched by `repo_init.sh` (not in git) |

## Rules

- Never create a `docs/` directory. Never add temporary or WIP files
  (plans, specs, notes, agent scratch) to this repo.
- C code: NuttX coding standard, must pass
  `external/nuttx/tools/checkpatch.sh -f <files>` (nxstyle); Apache-2.0 SPDX
  header in NuttX banner format. Apps ship `Kconfig`, `CMakeLists.txt`,
  `Makefile`, `Make.defs` like upstream `nuttx-apps`.
- C test apps print a single machine-parsable verdict line
  (`<app>: PASS ...` / `<app>: FAIL ...`) so NTFC tests stay thin.
- Python: NTFC style — black + isort (`profile = "black"`, line length 79,
  target py310); flake8 with bugbear, builtins, docstrings, pep8-naming,
  `max-complexity = 10`, `ignore = E203 W503`; Sphinx docstrings
  (`:param x:`, `:return:`); type hints on all functions; Apache SPDX `#`
  banner header on every `.py`.
- Shell: POSIX `sh`, `set -eu`, must pass `shellcheck`.
- Default sources: `https://github.com/apache/nuttx` @ `master`,
  `https://github.com/apache/nuttx-apps` @ `master`; every value overridable
  by environment variable (`NUTTX_REPO`, `NUTTX_REF`, `APPS_REPO`,
  `APPS_REF`) and by the matching `workflow_dispatch` input.
- Defconfigs live in this repo under
  `boards/<arch>/<chip>/<board>/configs/<scenario>/defconfig`, mirroring
  the upstream NuttX board path (out-of-tree configuration:
  `CONFIG_ARCH_BOARD_CUSTOM=y`, `CONFIG_ARCH_BOARD_CUSTOM_DIR` set to
  the upstream board directory, e.g. `"./boards/sim/sim/sim"`). Do not
  copy board source code into this repo. NTFC `kv` overrides are only
  for values that must differ between products of the same scenario
  (e.g. `CONFIG_NETINIT_IPADDR`); everything else belongs in the
  defconfig.
- NTFC config paths: `cwd: './external'`, `build_dir: './build/...'`
  (relative to the repo root); `defconfig` is relative to
  `external/nuttx`, e.g. `'../../boards/sim/sim/sim/configs/smoke'`.
- Do not commit unless explicitly asked.

## Commit messages

Same format as Apache NuttX (`CONTRIBUTING.md` there):

1. Topic: functional area prefix, `:`, short self-explanatory summary,
   `.` (e.g. `ntfc/ip: Add UDP integrity test between two nodes.`).
2. Blank line.
3. Description of what changed, how and why (short sentences or bullet
   points).
4. Blank line.
5. `Assisted-by: AGENT_NAME:MODEL_VERSION [TOOL1] [TOOL2]` — mandatory on
   every commit created or assisted by an LLM/AI tool
   (e.g. `Assisted-by: Claude:claude-opus-5-5`).
6. `Signed-off-by: Name <email>` (`git commit -s`), below `Assisted-by`.
   AI agents add it only when the human author asks them to commit
   (`git commit -s`, the author's own identity); never on their own.

```
apps/nettl: Add TCP/UDP data integrity test tool.

Client/server echo tool that verifies a deterministic byte pattern
and prints a single PASS/FAIL verdict line for NTFC tests.

Assisted-by: Claude:claude-opus-5-5
Signed-off-by: AuthorName <Valid@EmailAddress>
```

## Adding a scenario

1. C test app under `apps/<name>/` if the scenario needs new target-side
   behavior.
2. Defconfig for each target:
   `boards/<arch>/<chip>/<board>/configs/<scenario>/defconfig`
   (`CONFIG_ARCH_BOARD_CUSTOM=y` pointing at the upstream board; see
   `Defconfigs` in `Documentation/architecture.rst`).
3. NTFC config for each target:
   `ntfc/configs/<target>/<scenario>/config.yaml`, `defconfig` pointing
   at the new defconfig, `kv` only for values that must differ between
   products.
4. Test module `ntfc/tests/<area>/test_*.py`, using shared helpers
   (`ntfc/tests/_*.py`) and the `cmd_check` marker where appropriate.
5. Sessions named `<target>-<scenario>` in `ntfc/manifest-ci-<target>.yaml`.
6. Document it: every new `test_*` function and every new manifest
   session name must be added to `Documentation/test-cases.rst` (as a
   RST ``literal``, exact name). `tools/ci/check-docs.py` fails the
   build if a test or session is undocumented, or if the docs name one
   that does not exist.

## Commands

```sh
./repo_init.sh                                         # fetch sources
tox -c ntfc/tox.ini                                    # Python lint (ntfc/ and tools/ci/)
external/nuttx/tools/checkpatch.sh -f apps/*/*.c       # C style
shellcheck $(git ls-files '*.sh')                     # shell style
python -m ntfc test --manifest ntfc/manifest-ci-sim.yaml
tools/docker/run.sh ci-sim                             # same, in Docker
pip install -r Documentation/requirements.txt          # docs
sphinx-build -W -b html Documentation /tmp/testlab-docs # build docs
python tools/ci/check-docs.py                          # check docs are current
```
