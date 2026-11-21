############################################################################
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations
# under the License.
#
############################################################################

"""SQLite integration tests (``sqltl``, ``apps/sqltl``).

The databases live on file systems that survive a QEMU restart: the v9fs
host share ``/mnt/v9fs`` (host ``build/sqlite/<target>/share``), FAT on
the 128 MiB virtio-blk disk ``/mnt/fat`` and FAT on the 8 MiB disk
``/mnt/full``.  A power-off is SIGKILL to QEMU: no shutdown, no sync.
The commits the node reported before it are read from the QEMU console
log (``-chardev ...,logfile=``), which QEMU writes as the node prints.
"""

import hashlib
import os
import random
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Dict, Iterator

import pytest
from _fs_common import core, fields, kv, run, verdict_of

pytestmark = pytest.mark.cmd_check("sqltl_main")

VERDICT_RE = r"sqltl: (PASS|FAIL) [^\r\n]*"
LINE_END = r"\r*\n"
V9FS = "/mnt/v9fs"
FAT = "/mnt/fat"
FULL = "/mnt/full"
MOUNTS = {"v9fs": V9FS, "fat": FAT}
DISKS = ("/dev/virtblk0", "/dev/virtblk1")
SMALL_DISK = 8 * 1024 * 1024

LOCK_BUG = (
    "fs/vfs/fs_lock.c: record locks belong to the thread (l_pid = "
    "gettid()) and the open file, not to the process: a second descriptor "
    "or another thread of the process conflicts with the process lock "
    "instead of replacing it, close() keeps the locks taken through the "
    "other descriptors and F_GETLK reports the thread ID; a SQLite "
    "connection then cannot commit after another connection of the "
    "process has read the database (SQLITE_BUSY until the reader closes)"
)
FAT_INO_BUG = (
    "fs/fat/fs_fat32.c: fat_stat_common() never sets st_ino, so all files "
    "of a FAT volume have the same st_dev and st_ino; the SQLite unix VFS "
    "takes them for one file: a transaction over attached databases gets "
    "SQLITE_BUSY and a new connection reuses the descriptor of another "
    "database file"
)
FAT_ENOSPC_BUG = (
    "fs/fat/fs_fat32.c: fat_get_sectors() returns -EIO when "
    "fat_extendchain() finds no free cluster, so write() on a full volume "
    "fails with EIO instead of ENOSPC and SQLite reports "
    "SQLITE_IOERR_WRITE instead of SQLITE_FULL"
)
FAT_FSTAT_BUG = (
    "fs/fat/fs_fat32.c: fat_fstat() reports the size in the directory "
    "entry, which only fsync() and close() update, not the size of the "
    "open file; with synchronous=OFF SQLite reads stale database and "
    "journal sizes: the database looks malformed and a ROLLBACK after a "
    "cache spill leaves it corrupt"
)

FAT_SHRINK_BUG = (
    "fs/fat: a shrinking ftruncate() breaks the file: fat_truncate() "
    "(fs_fat32.c) reads the start cluster through a directory entry "
    "pointer after fat_dirshrink() has loaded a FAT sector into the "
    "buffer, and fat_dirshrink() (fs_fat32util.c) marks the new last "
    "cluster free instead of end of chain; later I/O fails with EINVAL or "
    "EIO and the cluster can go to another file. SQLite truncates the "
    "database when it rolls back a transaction that grew it"
)
FAT_CACHE_BUG = (
    "fs/fat: every descriptor has its own sector cache (ff_buffer) and a "
    "write through one descriptor neither updates nor invalidates the "
    "caches of the others, so they read stale data; another SQLite "
    "connection reads a stale change counter and keeps stale pages"
)
RAMMAP_BUG = (
    "fs/mmap/fs_mmap.c: mmap(MAP_SHARED) of a file whose file system has "
    "no mmap falls back to rammap(), a private copy of the file per "
    "mapping: stores through one mapping are not seen through the others; "
    "the SQLite WAL index (-shm) is then not shared between processes"
)
VIRTIO_BLK_BUG = (
    "drivers/virtio/virtio-blk.c: virtio_blk_rdwr() hands a user buffer "
    "to the device as one descriptor, but in a kernel build its pages are "
    "not physically adjacent: the device writes past the first page into "
    "other memory (FAT reads into SQLite buffers crash the node)"
)
MUNMAP_BUG = (
    "fs/mmap/fs_rammap.c: unmap_rammap() refuses (ENOSYS) a munmap() that "
    "does not reach the end of the mapping instead of removing the whole "
    "pages it covers; with a page size above 32 KiB SQLite unmaps its "
    "WAL index with a shorter length, so the mapping and the -shm file "
    "stay until the task exits and a reopen finds the volume full"
)
WAL_CACHE_SKIP = (
    f"{FAT_CACHE_BUG}; in WAL mode a connection now and then reads a stale "
    "WAL sector rewritten after a checkpoint (SQLITE_NOTADB), too rarely "
    "for an expected failure: test_sqlite_visible covers the bug"
)
FLAT_TASKS = (
    "flat build: the tasks share the SQLite library globals (unix VFS "
    "inode, lock and shared-memory state, which hold descriptors) but "
    "not their descriptor tables"
)
FAT_ROOT_BUG = (
    "fs/fat/fs_fat32dirent.c: for a long name created in the FAT12/16 "
    "root directory the directory index is computed from "
    "fat_cluster2sector(0) (-EINVAL), so fat_open() of the creator and "
    "later opens get separate open-file objects with separate sizes"
)

# Verdicts of the known bugs above: anything else is a test failure

LOCK_SIGNATURE = r"sqltl: FAIL lock fd=[1-9]"
HANDOFF_SIGNATURE = r"sqltl: FAIL handoff same=write/5 thread=write/5 check=0"
INO_SIGNATURE = r"sqltl: FAIL multi attach=5 .* ino=same"
ENOSPC_SIGNATURE = r"sqltl: FAIL full rows=\d+ fill=5 rc=778 errno=5"
FSTAT_SIGNATURE = r"sqltl: FAIL \w+ .*rc=11 "
# SQLITE_CORRUPT or an SQLITE_IOERR code: the broken chain fails a read,
# a write or a sync, depending on the file

SHRINK_SIGNATURE = (
    r"sqltl: FAIL (verify step=open|shrink step=\w+) "
    r"rc=(11|10|266|522|778|1034|1546) "
)
CACHE_SIGNATURE = (
    r"sqltl: FAIL (conc w=\d+ step=insert rc=1555|conc rows=\d+ counter=|"
    r"visible round)"
)
REOPEN_SIGNATURE = r"sqltl: FAIL full step=reopen rc=4874 "
SHM_SIGNATURE = r"sqltl: FAIL shm peer=1 seen=0"
TASKS_SIGNATURE = r"sqltl: FAIL conc (rows=\d+ counter=|w=\d+ step=task)"
ROOT_SIGNATURE = (
    r"sqltl: FAIL visible "
    r"(round|step=recount rc=522 |step=open rc=14 errno=2 )"
)


class KnownBugError(Exception):
    """A verdict matched the signature of a known upstream bug."""


_DISK: Dict[str, str] = {}


def sqltl(args: str, timeout: int = 120) -> str:
    """Run ``sqltl`` and return its verdict line.

    :param args: check name and arguments
    :param timeout: seconds to wait for the verdict
    :return: verdict line, empty if there is none
    """
    ret = core().sendCommandReadUntilPattern(
        f"sqltl {args}", pattern=VERDICT_RE + LINE_END, timeout=timeout
    )
    return verdict_of(str(ret.output), VERDICT_RE)


def sqltl_pass(
    args: str, timeout: int = 120, known: str = ""
) -> Dict[str, str]:
    """Run ``sqltl``, require a PASS verdict and return its fields.

    :param args: check name and arguments
    :param timeout: seconds to wait for the verdict
    :param known: signature regex of a known bug: a matching verdict
     raises ``KnownBugError`` instead of failing the assertion
    :return: ``key=value`` fields of the verdict line
    """
    verdict = sqltl(args, timeout)
    if known and re.search(known, verdict):
        raise KnownBugError(verdict)
    assert verdict.startswith("sqltl: PASS"), f"sqltl {args}: {verdict!r}"
    return fields(verdict)


def _xfail(request: pytest.FixtureRequest, applies: bool, reason: str) -> None:
    """Expect the test to hit a known bug, by its verdict signature.

    :param request: pytest request
    :param applies: whether the bug applies to this parameter set
    :param reason: bug description
    """
    if applies:
        request.applymarker(
            pytest.mark.xfail(strict=True, reason=reason, raises=KnownBugError)
        )


def _big_pagesize() -> bool:
    """Tell whether sysconf(_SC_PAGESIZE) is larger than a WAL index region.

    Without ``CONFIG_MM_PGSIZE`` it is ``CONFIG_PTHREAD_STACK_MIN``.

    :return: True if SQLite maps the WAL index more than 32 KiB at a time
    """
    if kv("CONFIG_MM_PGSIZE"):
        return False
    return int(str(kv("CONFIG_PTHREAD_STACK_MIN") or 0)) > 32768


def _qemu_arg(name: str) -> Path:
    """Return a host path given to QEMU, resolved like QEMU does.

    :param name: regex of the option prefix, e.g. ``logfile=``
    :return: path relative to the QEMU working directory, resolved
    """
    args = str(core().conf.exec_args)
    found = re.search(name + r"([^,\s]+)", args)
    assert found, f"no {name} in {args!r}"
    cwd = Path(str(core().conf.exec_cwd or "."))
    return (cwd / found.group(1)).resolve()


def _mounted(mnt: str) -> bool:
    """Tell whether a mount point is mounted on the node.

    :param mnt: mount point
    :return: True if ``mount`` lists it
    """
    return f"{mnt} type" in run("mount")


def _df_bytes(mnt: str) -> int:
    """Return the size of a mounted volume from ``df``.

    :param mnt: mount point
    :return: block size times blocks
    """
    for line in run("df").splitlines():
        cols = line.split()
        if len(cols) >= 5 and cols[-1] == mnt and cols[0].isdigit():
            return int(cols[0]) * int(cols[1])
    raise AssertionError(f"{mnt} not in df")


def _format() -> None:
    """Format both disks and find out which one is the small one."""
    for i, dev in enumerate(DISKS):
        out = run(f"mkfatfs {dev}", timeout=120)
        assert "failed" not in out, out
        probe = f"/mnt/probe{i}"
        out = run(f"mount -t vfat {dev} {probe}")
        assert "failed" not in out, out
        size = _df_bytes(probe)
        run(f"umount {probe}")
        _DISK[FULL if size <= SMALL_DISK else FAT] = dev
    assert set(_DISK) == {FAT, FULL}, _DISK


def _mount_all() -> None:
    """Mount the share and both disks where they are not mounted."""
    if not _mounted(V9FS):
        out = run(f"mount -t v9fs -o trans=virtio,tag=tlshare {V9FS}")
        assert "failed" not in out, out
    for mnt, dev in _DISK.items():
        if not _mounted(mnt):
            out = run(f"mount -t vfat {dev} {mnt}")
            assert "failed" not in out, out


def _log_size() -> int:
    """Return the current size of the QEMU console log.

    :return: bytes
    """
    return _qemu_arg("logfile=").stat().st_size


def _power_off(start: int) -> int:
    """Kill QEMU and return the last commit reported since ``start``.

    :param start: console log offset where the reported commits begin
    :return: highest ``sqltl: commit <n>`` after the offset, 0 if none
    """
    device = core().device
    os.kill(device.pid, signal.SIGKILL)
    deadline = time.time() + 10
    while not device.notalive and time.time() < deadline:
        time.sleep(0.05)
    assert device.notalive, "QEMU still running after SIGKILL"
    with open(_qemu_arg("logfile="), "rb") as log:
        log.seek(start)
        text = log.read().decode(errors="replace")
    commits = [int(n) for n in re.findall(r"sqltl: commit (\d+)\r*\n", text)]
    return max(commits, default=0)


def _boot() -> None:
    """Start the node again and mount its file systems."""
    assert core().reboot(), "reboot failed"
    _mount_all()


def _start(cmd: str, pattern: str, timeout: int = 300) -> None:
    """Start a long ``sqltl`` run and wait until it prints a line.

    :param cmd: ``sqltl`` arguments
    :param pattern: regex of the line to wait for
    :param timeout: seconds to wait
    """
    ret = core().sendCommandReadUntilPattern(
        f"sqltl {cmd}",
        pattern=f"{pattern}|{VERDICT_RE}{LINE_END}",
        timeout=timeout,
    )
    output = str(ret.output)
    assert re.search(pattern, output), f"sqltl {cmd}: {verdict_of(output)!r}"


@pytest.fixture(scope="module")
def media() -> None:
    """Empty the host share and format both disks."""
    share = _qemu_arg(r"-fsdev\s+local,id=tlfs,\S*?path=")
    assert share.is_dir(), f"{share} missing (testenv/sqlite.sh start)"
    if _mounted(V9FS):
        run(f"umount {V9FS}")
    for child in share.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    _DISK.clear()
    if not kv("CONFIG_BUILD_KERNEL"):
        _format()


def _on_fat(request: pytest.FixtureRequest) -> bool:
    """Tell whether a test uses a FAT volume.

    :param request: pytest request
    :return: True for the FAT parameters and for ``test_sqlite_full``
    """
    callspec = getattr(request.node, "callspec", None)
    params = callspec.params if callspec is not None else {}
    where = str(params.get("fs", params.get("where", "")))
    full = request.node.originalname == "test_sqlite_full"
    return where.startswith("fat") or full


@pytest.fixture(autouse=True)
def mounted(media: None, request: pytest.FixtureRequest) -> Iterator[None]:
    """Mount everything before a test, reboot after a failed one.

    :param media: module setup
    :param request: pytest request
    """
    if _on_fat(request) and kv("CONFIG_BUILD_KERNEL"):
        pytest.skip(VIRTIO_BLK_BUG)
    _mount_all()
    yield
    report = getattr(request.node, "rep_call", None)
    if report is not None and (report.failed or hasattr(report, "wasxfail")):
        _boot()


def _workdir(request: pytest.FixtureRequest, mnt: str) -> str:
    """Create an empty directory for the test on a node file system.

    The name is short: NAME_MAX of the node is 32.

    :param request: pytest request
    :param mnt: mount point
    :return: node path
    """
    name = "s" + hashlib.sha1(request.node.nodeid.encode()).hexdigest()[:10]
    path = f"{mnt}/{name}"
    out = run(f"mkdir {path}")
    assert "failed" not in out, out
    return path


FS_PARAMS = [pytest.param("v9fs", id="v9fs"), pytest.param("fat", id="fat")]

# Crash safety

# Power-off points of the crash rounds: at a random time between
# transactions reported committed, or with the last transaction stopped
# just before or just after its commit point

CRASH_POINTS = ("random", "before", "random", "after", "random")


def _crash_round(db: str, opts: str, rows: int, point: str, seed: str) -> str:
    """Run one crash round and return the ``verify`` arguments for it.

    :param db: database path on the node
    :param opts: ``-j`` and ``-s`` options
    :param rows: rows in the database before the round
    :param point: entry of ``CRASH_POINTS``
    :param seed: random seed of the round
    :return: ``verify`` arguments that the database must satisfy
    """
    rng = random.Random(seed)
    start = _log_size()
    if point == "random":
        stop = rows + 3 * rng.randint(4, 40)
        _start(f"write {db} 100000 {opts}", rf"sqltl: commit {stop}\r*\n")
        time.sleep(rng.uniform(0, 0.2))
        return f"-a {_power_off(start)}"

    # Rows larger than a page: the transaction always grows the file

    _start(f"write {db} 1 {opts} -l 6000 -k {point}", r"sqltl: wait point")
    _power_off(start)
    if point == "after":
        return f"-n {rows + 3}"
    if "-j wal" in opts:
        return f"-a {rows} -u {rows + 3}"
    return f"-n {rows}"


@pytest.mark.parametrize("fs", FS_PARAMS)
@pytest.mark.parametrize(
    "journal,sync",
    [
        pytest.param("delete", "extra", id="delete"),
        pytest.param("truncate", "full", id="truncate"),
        pytest.param("wal", "full", id="wal"),
    ],
)
def test_sqlite_crash(
    request: pytest.FixtureRequest, fs: str, journal: str, sync: str
) -> None:
    """Power off during transactions; committed ones survive, others not.

    SQLite, "SQLite is Transactional": "All changes within a single
    transaction in SQLite either occur completely or not at all, even if
    the act of writing the change out to the disk is interrupted by a
    program crash, an operating system crash, or a power failure."
    POSIX fsync(): "all data for the open file descriptor named by
    fildes is to be transferred to the storage device".

    Five rounds, see ``CRASH_POINTS``; after each boot integrity_check
    "ok", whole transactions only, every row reported committed present
    and intact, a transaction stopped before its commit point absent (WAL:
    absent or present), one stopped after it present.
    """
    bug = fs == "fat" and journal != "wal"
    _xfail(request, bug, FAT_SHRINK_BUG)
    db = f"{_workdir(request, MOUNTS[fs])}/crash.db"
    opts = f"-j {journal} -s {sync}"
    rows = 0
    for i, point in enumerate(CRASH_POINTS):
        seed = f"{request.node.nodeid}-{i}"
        check = _crash_round(db, opts, rows, point, seed)
        _boot()
        verdict = sqltl_pass(
            f"verify {db} {check} -r 3",
            known=SHRINK_SIGNATURE if bug else "",
        )
        rows = int(verdict["rows"])


@pytest.mark.parametrize("fs", FS_PARAMS)
def test_sqlite_large(request: pytest.FixtureRequest, fs: str) -> None:
    """A database far larger than the page cache survives power-offs.

    SQLite, "SQLite is Transactional": "All changes within a single
    transaction in SQLite either occur completely or not at all, even if
    the act of writing the change out to the disk is interrupted by a
    program crash, an operating system crash, or a power failure."

    8 MiB in 256 transactions with a 16-page cache, power off, check; a
    512-row transaction that spilled pages, power off before its COMMIT:
    rolled back after boot; the same transaction committed, power off:
    present.
    """
    db = f"{_workdir(request, MOUNTS[fs])}/large.db"
    opts = "-r 8 -l 4000 -c 16"
    sqltl_pass(f"write {db} 256 {opts}", timeout=900)
    _power_off(_log_size())
    _boot()
    sqltl_pass(f"verify {db} -n 2048 {opts}", timeout=600)

    start = _log_size()
    _start(f"big {db} 512 {opts} -w", r"sqltl: wait spill=[1-9]", timeout=900)
    assert _power_off(start) == 0
    _boot()
    sqltl_pass(f"verify {db} -n 2048 {opts}", timeout=600)

    start = _log_size()
    verdict = sqltl_pass(f"big {db} 512 {opts}", timeout=900)
    assert int(verdict["spill"]) > 0
    assert _power_off(start) == 2560
    _boot()
    sqltl_pass(f"verify {db} -n 2560 {opts}", timeout=600)


@pytest.mark.parametrize("fs", FS_PARAMS)
@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_sqlite_shrink(
    request: pytest.FixtureRequest, fs: str, journal: str
) -> None:
    """The database file shrinks and grows again, data intact.

    SQLite PRAGMA auto_vacuum: "When the auto-vacuum mode is 1 or
    'full', ... the database file is truncated to remove the freelist
    pages at every transaction commit."  POSIX ftruncate(): "If the size
    of the file previously exceeded length, the extra data shall no
    longer be available to reads on the file."

    auto_vacuum FULL: a commit deleting half of the rows truncates the
    file, the rows are inserted again; the same with VACUUM.
    """
    _xfail(request, fs == "fat", FAT_SHRINK_BUG)
    known = SHRINK_SIGNATURE if fs == "fat" else ""
    db = f"{_workdir(request, MOUNTS[fs])}/shrink.db"
    sqltl_pass(f"shrink {db} -j {journal}", timeout=300, known=known)


@pytest.mark.parametrize("fs", FS_PARAMS)
@pytest.mark.parametrize("sync", ["full", "off"])
def test_sqlite_rollback(
    request: pytest.FixtureRequest, fs: str, sync: str
) -> None:
    """ROLLBACK of a transaction that spilled pages restores the database.

    SQLite, "SQLite is Transactional": "All changes within a single
    transaction in SQLite either occur completely or not at all".  POSIX
    stat: st_size "For regular files, the file size in bytes."

    Holds at any synchronous level: only crash safety depends on it.
    """
    bug = fs == "fat" and sync == "off"
    _xfail(request, bug, f"{FAT_FSTAT_BUG}; then {FAT_SHRINK_BUG}")
    known = FSTAT_SIGNATURE if bug else ""
    db = f"{_workdir(request, MOUNTS[fs])}/rollback.db"
    sqltl_pass(f"write {db} 10 -s {sync}", known=known)
    verdict = sqltl_pass(
        f"big {db} 300 -l 3000 -c 16 -s {sync} -x", timeout=300, known=known
    )
    assert int(verdict["spill"]) > 0
    sqltl_pass(f"verify {db} -n 30", known=known)


# Concurrency and locks


@pytest.mark.parametrize("fs", FS_PARAMS)
@pytest.mark.parametrize("journal", ["delete", "wal"])
@pytest.mark.parametrize("begin", ["immediate", "deferred"])
@pytest.mark.parametrize("workers", ["threads", "tasks"])
def test_sqlite_conc(
    request: pytest.FixtureRequest,
    fs: str,
    journal: str,
    begin: str,
    workers: str,
) -> None:
    """8 connections, 50 read-modify-write transactions each.

    SQLite, "Isolation In SQLite": "all transactions in SQLite show
    'serializable' isolation."  Serialized read-modify-write
    transactions lose no update.

    Threads of one task or tasks of their own. SQLITE_BUSY is retried;
    no lost update, every row once, integrity_check "ok".
    """
    known = ""
    flags = "-d" if begin == "deferred" else ""
    if fs == "fat" and journal == "wal":
        pytest.skip(WAL_CACHE_SKIP)
    if fs == "fat" and journal == "delete":
        _xfail(request, True, f"{FAT_CACHE_BUG}; {LOCK_BUG}")
        known = CACHE_SIGNATURE
    if workers == "tasks":
        if not kv("CONFIG_BUILD_KERNEL"):
            pytest.skip(FLAT_TASKS)
        if journal == "wal":
            _xfail(request, True, RAMMAP_BUG)
        known = TASKS_SIGNATURE if known or journal == "wal" else ""
        flags += " -p"
    db = f"{_workdir(request, MOUNTS[fs])}/conc.db"
    sqltl_pass(
        f"conc {db} 8 50 -j {journal} {flags} -t 240",
        timeout=600,
        known=known,
    )


@pytest.mark.parametrize(
    "where",
    [
        pytest.param("v9fs", id="v9fs"),
        pytest.param("fat", id="fat"),
        pytest.param("fat-root", id="fat-root"),
    ],
)
@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_sqlite_visible(
    request: pytest.FixtureRequest, where: str, journal: str
) -> None:
    """A connection sees the rows another connection committed.

    SQLite, "Isolation In SQLite": "SQLite provides isolation between
    operations in separate database connections"; "Partial changes by
    the writer that have not been committed are invisible to the
    reader", so committed ones are seen by the next read transaction.
    POSIX write(): "Any successful read() from each byte position in the
    file that was modified by that write shall return the data specified
    by the write() for that position".

    Five times: A counts, B inserts a row, A counts again and reads the
    row; in WAL mode five more times with B restarting the WAL first.
    ``fat-root``: the database in the root directory of the volume.
    """
    known = ""
    if where == "fat-root":
        reason = FAT_ROOT_BUG
        if journal == "delete":
            reason = f"{FAT_ROOT_BUG}; {FAT_CACHE_BUG}"
        _xfail(request, True, reason)
        known = ROOT_SIGNATURE
        db = f"{FAT}/vis-{journal}.db"
    else:
        bug = where == "fat" and journal == "delete"
        _xfail(request, bug, FAT_CACHE_BUG)
        known = CACHE_SIGNATURE if bug else ""
        db = f"{_workdir(request, MOUNTS[where])}/visible.db"
    sqltl_pass(f"visible {db} -j {journal}", known=known)


@pytest.mark.parametrize("fs", FS_PARAMS)
@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_sqlite_handoff(
    request: pytest.FixtureRequest, fs: str, journal: str
) -> None:
    """A connection commits once the other readers of the process are done.

    SQLite BEGIN: "An attempt to execute COMMIT might also result in an
    SQLITE_BUSY return code if another thread or process has an open
    read connection"; once the readers are done the COMMIT succeeds.
    POSIX fcntl(): "the previous shared or exclusive lock for each byte
    in the specified region shall be replaced by the new shared or
    exclusive lock."

    Reader and writer connection in one thread, then the writer in other
    threads.
    """
    bug = journal == "delete"
    _xfail(request, bug, LOCK_BUG)
    db = f"{_workdir(request, MOUNTS[fs])}/handoff.db"
    known = HANDOFF_SIGNATURE if bug else ""
    sqltl_pass(f"handoff {db} -j {journal}", known=known)


@pytest.mark.parametrize("fs", FS_PARAMS)
def test_sqlite_fcntl_lock(request: pytest.FixtureRequest, fs: str) -> None:
    """POSIX record locks belong to the process, as the SQLite VFS needs.

    POSIX fcntl(): F_SETLK on bytes with "previously existing
    process-owned locks ... shall be replaced by the new shared or
    exclusive lock"; F_GETLK l_pid is the "Process ID of the process
    that holds the blocking lock"; "All process-owned locks associated
    with a file for a given process shall be removed when any file
    descriptor for that file is closed by that process (even if via a
    different open file description)".

    A second descriptor and another thread replace the process lock,
    another process sees it with the process ID and cannot take it, and
    closing one descriptor releases all of them.
    """
    _xfail(request, True, LOCK_BUG)
    path = f"{_workdir(request, MOUNTS[fs])}/lock.bin"
    sqltl_pass(f"lock {path}", known=LOCK_SIGNATURE)


@pytest.mark.parametrize("fs", FS_PARAMS)
def test_sqlite_shm_map(request: pytest.FixtureRequest, fs: str) -> None:
    """A shared file mapping is shared between tasks, as the WAL index needs.

    POSIX mmap(): "If MAP_SHARED is specified, write references shall
    change the underlying object."

    A byte stored through the mapping of one task is seen through the
    mapping of another task and the other way round.
    """
    _xfail(request, True, RAMMAP_BUG)
    path = f"{_workdir(request, MOUNTS[fs])}/shm.bin"
    sqltl_pass(f"shm {path}", known=SHM_SIGNATURE)


@pytest.mark.parametrize("fs", FS_PARAMS)
def test_sqlite_multi(request: pytest.FixtureRequest, fs: str) -> None:
    """Several databases at once stay separate files.

    SQLite ATTACH: "Transactions involving multiple attached databases
    are atomic".  POSIX <sys/stat.h>: "A file identity is uniquely
    determined by the combination of st_dev and st_ino."

    One transaction over two attached databases commits; a connection
    opened while another one reads keeps to its own file.
    """
    _xfail(request, fs == "fat", FAT_INO_BUG)
    known = INO_SIGNATURE if fs == "fat" else ""
    sqltl_pass(f"multi {_workdir(request, MOUNTS[fs])}", known=known)


# Disk full


@pytest.mark.parametrize("journal", ["delete", "wal"])
def test_sqlite_full(request: pytest.FixtureRequest, journal: str) -> None:
    """Transactions on a full volume fail with SQLITE_FULL, data intact.

    SQLite result codes: "The SQLITE_FULL result code indicates that a
    write could not complete because the disk is full."  POSIX write():
    "[ENOSPC] There was no free space remaining on the device containing
    the file."  POSIX munmap(): "shall remove any mappings for those
    entire pages containing any part of the address space of the process
    starting at addr and continuing for len bytes."

    On a freshly formatted volume, with 0, 1, 4 and 16 free blocks; the
    database recovers once space is freed, also after reopening.
    """
    known = ENOSPC_SIGNATURE
    reason = FAT_ENOSPC_BUG
    if journal == "wal" and _big_pagesize():
        known = REOPEN_SIGNATURE
        reason = f"{FAT_ENOSPC_BUG}; {MUNMAP_BUG}"
    _xfail(request, True, reason)
    run(f"umount {FULL}")
    out = run(f"mkfatfs {_DISK[FULL]}", timeout=120)
    assert "failed" not in out, out
    _mount_all()
    path = _workdir(request, FULL)
    sqltl_pass(
        f"full {path}/full.db {path}/filler -j {journal}",
        timeout=600,
        known=known,
    )
