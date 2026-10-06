# nuttx-testlab

Runtime tests for [Apache NuttX](https://github.com/apache/nuttx) master
that upstream CI does not run. NuttX is built and booted on sim and QEMU
targets and driven with [NTFC](https://github.com/apache/nuttx-ntfc).
Target-side test apps (C, NuttX style) live in `apps/`, NTFC tests
(Python) in `ntfc/`.

Architecture and the exact list of test cases: [`Documentation/`](Documentation/).

## Why

Upstream NuttX CI boots only a few single-node images and leaves most
runtime features untested. This repo runs nightly against NuttX master
the scenarios that are too heavy or host-dependent for upstream CI: many
emulated nodes per test, NuttX talking to Linux, real devices (NICs, block
devices, virtio), root-only host setup.

## Quick start

```sh
./repo_init.sh
python -m venv .venv && . .venv/bin/activate
pip install -r ntfc/requirements.txt
python -m ntfc test --manifest ntfc/manifest-ci-sim.yaml
```

Manifests: `ntfc/manifest-ci-{sim,qemu-armv8a,rv-virt,qemu-intel64}.yaml`.

## Testing a fork

`repo_init.sh` reads `sources.env`; every value can be overridden with an
environment variable of the same name (or the matching input of the
`tests` workflow):

| Variable     | Default                                |
|--------------|----------------------------------------|
| `NUTTX_REPO` | `https://github.com/apache/nuttx`      |
| `NUTTX_REF`  | `master`                               |
| `APPS_REPO`  | `https://github.com/apache/nuttx-apps` |
| `APPS_REF`   | `master`                               |

A ref can be a branch, a tag, or a full 40-character commit SHA:

```sh
NUTTX_REPO=https://github.com/me/nuttx NUTTX_REF=my-branch ./repo_init.sh --force
```

## Running in Docker

```sh
docker build -t nuttx-testlab tools/docker
tools/docker/run.sh ci-sim
TESTLAB_SESSION=sim-smoke TESTLAB_TESTPATH=ntfc/tests/smoke/test_smoke.py \
  tools/docker/run.sh ci-sim
```

`tools/docker/run.sh [--image IMG] <manifest-name>` runs
`ntfc/manifest-<manifest-name>.yaml` in the container; results go to
`result/`.

## CI

- `pr.yml` — every pull request and push to `master`: lint (checkpatch,
  shellcheck, tox, Sphinx `-W`, docs check) and all tests on all targets
  against NuttX master.
- `nightly.yml` — all tests on all targets against NuttX master, daily
  at 01:00 UTC.
- `tests.yml` — reusable; also started manually from
  Actions → Tests → Run workflow with `targets` (all or one), optional
  `session` and `testpath`, and `nuttx_repo`/`nuttx_ref`/`apps_repo`/
  `apps_ref`.
- `docker-image.yml` — publishes `ghcr.io/railab/nuttx-testlab/testlab`
  when `tools/docker/**` changes on `master`. Test jobs pull this image;
  a PR that changes `tools/docker/**` builds its own.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`AGENTS.md`](AGENTS.md).
