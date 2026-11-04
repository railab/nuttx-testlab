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
"""VFS, tmpfs and FAT behaviour on the node, checked by ``fstl``.

Pipes and FIFOs live in the pseudo file system, tmpfs is mounted at
``/mnt/tmp`` and a FAT volume is made on a RAM disk (``mkrd``,
``mkfatfs``) and mounted at ``/mnt/fat``.  Expectations are quoted from
POSIX.1-2024 (pubs.opengroup.org); where POSIX says nothing (umount),
the Linux interface is the reference.
"""

import pytest
from _fs_common import fields, fstl, fstl_pass, kv, run

TMP = "/mnt/tmp"
FAT = "/mnt/fat"
RAMDISK_MINOR = 7
RAMDISK_SECTORS = 2048  # 1 MiB of 512-byte sectors

FAT_EXTEND_BUG = (
    "fs/fat/fs_fat32util.c fat_dirextend(): the zero-fill of an "
    "ftruncate() extension starts at the open file's current sector "
    "(ff_currentsector), not at the end of the file, so after a seek to "
    "the start the extension overwrites the file's data"
)
FAT_STAT_SIZE_BUG = (
    "fs/fat/fs_fat32.c fat_fstat(): the size of an open file comes from "
    "its directory entry (fat_stat_file), which is written only by "
    "fat_sync(), so fstat() after a write that extended the file reports "
    "the old size (0 for a new file); POSIX fstat(): st_size is the "
    "file size in bytes"
)
FAT_RMDIR_BUG = (
    "fs/fat/fs_fat32dirent.c fat_remove(): a directory whose last cluster "
    "is full of deleted entries (no DIR0_ALLEMPTY entry) is scanned to "
    "the end of its cluster chain and rmdir() fails with ENOSPC instead "
    "of removing the empty directory"
)


def _mounted(path: str, fstype: str) -> bool:
    """Tell whether ``mount`` lists a mount point of a type.

    :param path: mount point
    :param fstype: file system type as printed by ``mount``
    :return: True if mounted
    """
    return f"{path} type {fstype}" in run("mount")


@pytest.fixture(scope="module")
def tmpfs() -> str:
    """Mount tmpfs at ``/mnt/tmp`` once.

    :return: node path of the mount point
    """
    if not kv("CONFIG_FS_TMPFS"):
        pytest.skip("no CONFIG_FS_TMPFS")
    if not _mounted(TMP, "tmpfs"):
        run(f"mount -t tmpfs {TMP}")
    assert _mounted(TMP, "tmpfs"), run("mount")
    return TMP


@pytest.fixture(scope="module")
def fat() -> str:
    """Make a FAT volume on a RAM disk and mount it at ``/mnt/fat`` once.

    :return: node path of the mount point
    """
    for opt in (
        "CONFIG_FS_FAT",
        "CONFIG_BOARDCTL_MKRD",
        "CONFIG_FSUTILS_MKFATFS",
    ):
        if not kv(opt):
            pytest.skip(f"no {opt}")
    if not _mounted(FAT, "vfat"):
        dev = f"/dev/ram{RAMDISK_MINOR}"
        if dev not in run("ls /dev"):
            out = run(f"mkrd -m {RAMDISK_MINOR} -s 512 {RAMDISK_SECTORS}")
            assert "error" not in out.lower(), out
        out = run(f"mkfatfs {dev}")
        assert "error" not in out.lower(), out
        run(f"mount -t vfat {dev} {FAT}")
    assert _mounted(FAT, "vfat"), run("mount")
    return FAT


@pytest.fixture
def tmpdir(request: pytest.FixtureRequest, tmpfs: str) -> str:
    """Return an empty per-test directory on tmpfs.

    :param request: pytest request
    :param tmpfs: tmpfs mount point
    :return: node path
    """
    path = f"{tmpfs}/{request.node.name[:24].replace('[', '_').rstrip(']')}"
    run(f"rm -rf {path}")
    run(f"mkdir {path}")
    return path


@pytest.fixture
def fatdir(request: pytest.FixtureRequest, fat: str) -> str:
    """Return an empty per-test directory on the FAT volume.

    :param request: pytest request
    :param fat: FAT mount point
    :return: node path
    """
    name = request.node.name.split("[")[0].replace("test_fat_", "")[:8]
    path = f"{fat}/{name.upper()}"
    run(f"rm -rf {path}")
    run(f"mkdir {path}")
    return path


# Pipes and FIFOs


@pytest.mark.dep_config("CONFIG_PIPES")
@pytest.mark.parametrize("op", ["seekpipe", "preadpipe", "pwritepipe"])
def test_vfs_pipe_espipe(op: str) -> None:
    """lseek(), pread() and pwrite() on a pipe fail with ESPIPE.

    POSIX lseek(): "[ESPIPE] The fildes argument is associated with a
    pipe, FIFO, or socket."  pread(): "[ESPIPE] fildes is associated
    with a pipe or FIFO."  pwrite(): "[ESPIPE] fildes is associated with
    a pipe or FIFO."
    """
    fstl_pass(f"err {op} - ESPIPE")


@pytest.mark.dep_config("CONFIG_PIPES")
def test_vfs_fifo_espipe() -> None:
    """lseek() on a FIFO fails with ESPIPE (POSIX lseek(), [ESPIPE])."""
    fstl_pass("err seekfifo /tlfifo ESPIPE")
    assert "tlfifo" not in run("ls /")


@pytest.mark.dep_config("CONFIG_PIPES")
@pytest.mark.parametrize("op", ["piperdwrite", "pipewrread"])
def test_vfs_pipe_wrong_end(op: str) -> None:
    """A write to a pipe's read end or a read from its write end fails.

    POSIX pipe(): "fildes[0] for the read end of the pipe, and fildes[1]
    for the write end of the pipe"; write(): "[EBADF] The fildes argument
    is not a valid file descriptor open for writing."  read(): "[EBADF]
    The fildes argument is not a valid file descriptor open for reading."
    """
    fstl_pass(f"err {op} - EBADF")


# Directory opens


@pytest.mark.parametrize(
    "op,expect",
    [
        pytest.param("rdwrdir", "EISDIR", id="rdwr-directory"),
        pytest.param("wrdirflag", "EISDIR", id="wronly-directory"),
        pytest.param("wronly", "EISDIR", id="wronly"),
        pytest.param("tmpfile", "EOPNOTSUPP", id="tmpfile"),
    ],
)
def test_tmpfs_open_dir_write(tmpdir: str, op: str, expect: str) -> None:
    """Opening a directory for writing fails and changes nothing.

    POSIX open(): "[EISDIR] The named file is a directory and oflag
    includes O_WRONLY or O_RDWR, or includes O_CREAT without
    O_DIRECTORY."  O_TMPFILE is a Linux extension: open(2) gives
    EOPNOTSUPP when "the filesystem containing pathname does not support
    O_TMPFILE".
    """
    before = run(f"ls {tmpdir}")
    fstl_pass(f"err {op} {tmpdir} {expect}")
    assert run(f"ls {tmpdir}") == before


# tmpfs


def test_tmpfs_inode_numbers(tmpdir: str) -> None:
    """Every tmpfs file and directory has its own nonzero st_ino.

    POSIX <sys/stat.h>: "The st_ino and st_dev fields taken together
    uniquely identify the file within the system."  readdir(): d_ino
    is "File serial number", the same number.
    """
    for name in ("a", "b", "c"):
        fstl_pass(f"write {tmpdir}/{name} 16 -s {ord(name)}")
    run(f"mkdir {tmpdir}/d")
    inos = {
        name: int(fstl_pass(f"stat {tmpdir}/{name}")["ino"])
        for name in ("a", "b", "c", "d")
    }
    assert 0 not in inos.values(), inos
    assert len(set(inos.values())) == 4, inos
    assert int(fstl_pass(f"stat {tmpdir}")["ino"]) not in inos.values()
    assert fstl_pass(f"ls {tmpdir} -n 4 -q")["n"] == "4"


def test_tmpfs_pread_pwrite(tmpdir: str) -> None:
    """pread/pwrite, lseek and a write past the end on tmpfs.

    POSIX write(): after an lseek past the end "the gap shall be
    filled with zero bytes" (lseek(): "subsequently read in the gap ...
    shall return bytes with value 0 until data is actually written into
    the gap"); pread()/pwrite() do not change the file offset.
    """
    fstl_pass(f"pio {tmpdir}/p.bin")


def test_tmpfs_write_verify(tmpdir: str) -> None:
    """70000 bytes of pattern read back from tmpfs (write(), read())."""
    fstl_pass(f"write {tmpdir}/w.bin 70000 -s 3")
    assert fstl_pass(f"stat {tmpdir}/w.bin")["size"] == "70000"
    fstl_pass(f"verify {tmpdir}/w.bin 70000 -s 3")


def test_tmpfs_truncate(tmpdir: str) -> None:
    """ftruncate(), truncate() and O_TRUNC on tmpfs.

    POSIX ftruncate(): "If the file size is increased, the extended
    area shall appear as if it were zero-filled."
    """
    fstl_pass(f"trunc {tmpdir}/t.bin")


def test_tmpfs_extend_after_seek(tmpdir: str) -> None:
    """ftruncate() extension after a seek to the start keeps the data.

    POSIX ftruncate(): "If the file size is increased, the extended
    area shall appear as if it were zero-filled"; the file offset is
    not changed.
    """
    fstl_pass(f"extend {tmpdir}/e.bin")


def test_tmpfs_append(tmpdir: str) -> None:
    """O_APPEND writes land at the end regardless of the offset.

    POSIX open(): "If set, the file offset shall be set to the end of
    the file prior to each write."
    """
    fstl_pass(f"append {tmpdir}/a.bin")


def test_tmpfs_rmdir_deleted_full(tmpdir: str) -> None:
    """rmdir() of a directory whose entries were all unlinked succeeds.

    POSIX rmdir(): "The rmdir() function shall remove a directory whose
    name is given by path. The directory shall be removed only if it is
    an empty directory."
    """
    fstl_pass(f"fillrm {tmpdir}/full 40")


def test_tmpfs_umount_inside(tmpfs: str, tmpdir: str) -> None:
    """umount() of a path inside the volume fails and keeps it mounted.

    POSIX has no umount; Linux umount(2): "[EINVAL] target is not a
    mount point."
    """
    fstl_pass(f"umount {tmpdir} EINVAL")
    assert _mounted(tmpfs, "tmpfs"), run("mount")
    assert fstl_pass(f"stat {tmpdir}")


@pytest.mark.parametrize(
    "op",
    ["rdwrite", "rdpwrite", "rdfwrite", "wrread", "wrpread", "wrfread"],
)
def test_tmpfs_wrong_access_mode(tmpdir: str, op: str) -> None:
    """A read or write in the wrong access mode fails with EBADF.

    The file is open for reading only and written, or open for writing
    only and read.  POSIX write() and pwrite(): "[EBADF] The fildes
    argument is not a valid file descriptor open for writing."  read()
    and pread(): "[EBADF] The fildes argument is not a valid file
    descriptor open for reading."
    fwrite() and fread() fail as fputc() and fgetc() do: "[EBADF] The
    file descriptor underlying stream is not a valid file descriptor open
    for writing" (for reading); "the error indicator for the stream shall
    be set".  A file open for reading only is not changed.
    """
    if op[2] == "f" and not kv("CONFIG_FILE_STREAM"):
        pytest.skip("no CONFIG_FILE_STREAM")
    fstl_pass(f"write {tmpdir}/m.bin 16 -s 9")
    fstl_pass(f"err {op} {tmpdir}/m.bin EBADF")
    if op.startswith("rd"):
        fstl_pass(f"verify {tmpdir}/m.bin 16 -s 9")


# FAT on a RAM disk


def test_fat_write_verify(fatdir: str) -> None:
    """70000 bytes of pattern read back from FAT (write(), read())."""
    fstl_pass(f"write {fatdir}/W.BIN 70000 -s 5")
    assert fstl_pass(f"stat {fatdir}/W.BIN")["size"] == "70000"
    fstl_pass(f"verify {fatdir}/W.BIN 70000 -s 5")


@pytest.mark.xfail(strict=True, reason=FAT_STAT_SIZE_BUG)
def test_fat_pread_pwrite(fatdir: str) -> None:
    """pread/pwrite, lseek and a write past the end on FAT.

    POSIX lseek(): bytes in a gap "shall return bytes with value 0 until
    data is actually written into the gap".
    """
    fstl_pass(f"pio {fatdir}/P.BIN")


@pytest.mark.xfail(strict=True, reason=FAT_EXTEND_BUG)
def test_fat_extend_after_seek(fatdir: str) -> None:
    """ftruncate() extension after a seek to the start keeps the data.

    POSIX ftruncate(): "If the file size is increased, the extended
    area shall appear as if it were zero-filled"; the data before the
    old end is unchanged and the file offset is not moved.
    """
    fstl_pass(f"extend {fatdir}/E.BIN")


@pytest.mark.xfail(strict=True, reason=FAT_RMDIR_BUG)
def test_fat_rmdir_deleted_full(fat: str, fatdir: str) -> None:
    """rmdir() of a directory whose cluster is full of deleted entries.

    As many 8.3 files as fit in the directory's cluster beside ``.`` and
    ``..`` are created and unlinked again.  POSIX rmdir(): "The
    directory shall be removed only if it is an empty directory."
    """
    bsize = int(fstl_pass(f"statfs {fat}")["bsize"])
    entries = bsize // 32 - 2
    fstl_pass(f"fillrm {fatdir}/FULL {entries}")


def test_fat_rmdir_deleted_partial(fatdir: str) -> None:
    """rmdir() of a directory with a few unlinked entries succeeds.

    POSIX rmdir(): "The directory shall be removed only if it is an
    empty directory."
    """
    fstl_pass(f"fillrm {fatdir}/PART 3")


def test_fat_umount_inside(fat: str, fatdir: str) -> None:
    """umount() of a path inside the FAT volume fails with EINVAL.

    Linux umount(2): "[EINVAL] target is not a mount point."
    """
    fstl_pass(f"umount {fatdir} EINVAL")
    assert _mounted(fat, "vfat"), run("mount")


def test_fat_statfs(fat: str) -> None:
    """statfs() of the FAT volume reports its block size and blocks.

    POSIX fstatvfs(): f_bsize "File system block size", f_blocks "Total
    number of blocks on file system in units of f_frsize".
    """
    got = fields(fstl(f"statfs {fat}"))
    assert int(got["bsize"]) % 512 == 0 and int(got["bsize"]) > 0, got
    assert 0 < int(got["blocks"]) <= RAMDISK_SECTORS, got
    assert int(got["bfree"]) <= int(got["blocks"]), got
