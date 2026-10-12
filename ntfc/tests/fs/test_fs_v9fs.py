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

"""virtio 9P (v9fs) file system tests.

QEMU exports the host directory ``build/v9fs/<target>`` (``-fsdev local``,
``security_model=none``) with mount tag ``tlshare``; the node mounts it
at ``/mnt/v9fs``. Each test works in its own directory ``tl/t<hash>``,
created empty on the host, and checks the host side directly. The
loadable programs of the build (``elftl``, ``elfsym``) are copied to
``bin/``, which is the node's ``PATH``.
"""

import hashlib
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Iterator, Optional, Tuple

import pytest
from _fs_common import (
    VERDICT_RE,
    core,
    fields,
    fstl,
    fstl_pass,
    kv,
    pattern,
    run,
    verdict_of,
)

MNT = "/mnt/v9fs"
TAG = "tlshare"
OPTS = f"trans=virtio,tag={TAG}"
V9FS_MAGIC = 0x01021997
ELF_PROGS = ("elftl", "elfsym")

EXCL_BUG = (
    "fs/v9fs/v9fs.c: v9fs_vfs_open() opens an existing file with Tlopen "
    "even for O_CREAT|O_EXCL; the server drops O_CREAT, so the open "
    "succeeds instead of failing with EEXIST"
)
QID_TYPE_BUG = (
    "fs/v9fs/v9fs.c: open, opendir and walk never look at the qid type: "
    "O_WRONLY of a directory and opendir() of a file succeed (POSIX: "
    "EISDIR, ENOTDIR), a file used as a directory gives ENOENT, not "
    "ENOTDIR"
)
SEEK_BUG = (
    "fs/v9fs/v9fs.c: v9fs_vfs_seek() returns a negative resulting offset "
    "as the error code, so lseek(fd, -1, SEEK_SET) fails with errno 1 "
    "(EPERM) instead of EINVAL"
)
APPEND_BUG = (
    "fs/v9fs/v9fs.c: O_APPEND only sets f_pos at open; writes use f_pos, "
    "so after a seek the file offset is wrong (POSIX: each write first "
    "moves the offset to the end of the file)"
)
CHMOD_BUG = (
    "fs/v9fs/client.c: v9fs_client_chstat() sends "
    "v9fs_map_open_flags(st_mode) as the new mode, which drops the 0004, "
    "0010, 0020 and 0040 bits"
)
UTIME_BUG = (
    "fs/v9fs/client.c: v9fs_map_setattr_flags() maps CH_STAT_ATIME/MTIME "
    "to ATIME_SET/MTIME_SET only, without the ATIME/MTIME bits that make "
    "the server change the times"
)
WALK_BUG = (
    "fs/v9fs/client.c: v9fs_client_walk() sends all path elements in one "
    "Twalk; 9P allows at most 16 (MAXWELEM), so paths with more than 16 "
    "elements below the mount fail with EINVAL"
)
SYMTAB_BUG = (
    "libs/libc/libc.csv: getpid, gettid and putchar are libc functions "
    "missing from the system symbol table (EXECFUNCS_SYSTEM_SYMTAB), so "
    "a program that calls them does not load"
)
UNAME_BUG = (
    "fs/v9fs/client.c: v9fs_client_init() copies uname=, aname= and "
    "trans= values into fixed buffers with the option length, a uname "
    "of NAME_MAX characters or more overflows the stack"
)
PROBE_UAF_BUG = (
    "fs/v9fs/virtio_9p.c: virtio_9p_create() frees the driver after a "
    "failed probe (ENODEV) without virtio_unregister_driver(); the freed "
    "driver stays on the virtio bus list and the next unregister writes "
    "into freed memory"
)
PCI_RESET_BUG = (
    "drivers/virtio/virtio-pci.c: virtio_pci_reset_device() calls "
    "set_features(VIRTIO_CONFIG_STATUS_RESET) instead of set_status(), so "
    "the device is never reset; after umount the next probe sets up new "
    "queues on a running device and QEMU stops on a malformed request"
)
KERNEL_UMOUNT_BUG = (
    "cmake/nuttx_add_library.cmake: nuttx_add_external_library(MODE "
    "KERNEL) does not add the kernel definitions, so OpenAMP/libmetal are "
    "built without __KERNEL__ and free virtqueues with the user-heap "
    "free(); umount asserts in mm_free() (DEBUGASSERT(mm_heapmember))"
)
OFFSET_BUG = (
    "fs/v9fs: 9P directory offsets are opaque 64-bit values but v9fs "
    "keeps them in off_t; without CONFIG_FS_LARGEFILE (32-bit off_t) the "
    "offset of the last entry is truncated and the next Treaddir fails, "
    "so readdir() reports an error at the end of the directory"
)


def _share_root() -> Path:
    """Return the host directory QEMU exports as ``tlshare`` (``tlfs``).

    :return: path, a relative path resolved against the QEMU working
     directory (NTFC uses the build directory for kernel builds)
    """
    args = str(core().conf.exec_args)
    found = re.search(r"-fsdev\s+local,id=tlfs,\S*?path=([^,\s]+)", args)
    assert found, f"no -fsdev path in {args!r}"
    cwd = Path(str(core().conf.exec_cwd or "."))
    return (cwd / found.group(1)).resolve()


def _mounted() -> bool:
    """Tell whether the share is mounted on the node.

    :return: True if ``mount`` lists ``/mnt/v9fs`` as v9fs
    """
    return f"{MNT} type v9fs" in run("mount")


def _mount() -> None:
    """Mount the share with the default options."""
    out = run(f"mount -t v9fs -o {OPTS} {MNT}")
    assert "failed" not in out, out
    assert _mounted()


@pytest.fixture(scope="module")
def share() -> Path:
    """Empty the shared host directory and install the test programs.

    :return: host path of the share
    """
    root = _share_root()
    assert root.is_dir(), f"{root} missing (testenv/v9fs.sh start)"
    if _mounted():
        run(f"umount {MNT}")
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    (root / "tl").mkdir()
    (root / "bin").mkdir()
    bindir = Path(str(core().conf.elf_path)).parent / "bin"
    for prog in ELF_PROGS:
        if (bindir / prog).is_file():
            shutil.copy(bindir / prog, root / "bin" / prog)
    return root


@pytest.fixture(autouse=True)
def mounted(share: Path) -> Iterator[None]:
    """Mount the share before each test if it is not mounted.

    :param share: host path of the share
    """
    if not _mounted():
        _mount()
    yield


@pytest.fixture
def work(request: pytest.FixtureRequest, share: Path) -> Tuple[Path, str]:
    """Create an empty work directory for the test on the host.

    The name is short: NAME_MAX of the node is 32.

    :param request: pytest request
    :param share: host path of the share
    :return: host path and node path of the directory
    """
    name = "t" + hashlib.sha1(request.node.nodeid.encode()).hexdigest()[:10]
    host = share / "tl" / name
    if host.exists():
        shutil.rmtree(host)
    host.mkdir()
    return host, f"{MNT}/tl/{name}"


def _skip_remount() -> None:
    """Skip a test that unmounts the share where umount breaks the node."""
    if kv("CONFIG_DRIVERS_VIRTIO_PCI"):
        pytest.skip(PCI_RESET_BUG)
    if kv("CONFIG_BUILD_KERNEL"):
        pytest.skip(KERNEL_UMOUNT_BUG)


def _xfail_readdir_off_t(request: pytest.FixtureRequest) -> None:
    """Expect readdir to fail at the end of a directory with 32-bit off_t.

    :param request: pytest request
    """
    if not kv("CONFIG_FS_LARGEFILE"):
        request.applymarker(pytest.mark.xfail(strict=True, reason=OFFSET_BUG))


def _elf(share: Path, prog: str) -> None:
    """Skip the test if a loadable program was not built.

    :param share: host path of the share
    :param prog: program name
    """
    if not (share / "bin" / prog).is_file():
        pytest.skip(f"{prog} is not built as a loadable module")


def _prog(cmd: str, regex: str, timeout: int = 30) -> str:
    """Run a program from NSH and return its verdict line.

    :param cmd: command line
    :param regex: verdict line pattern
    :param timeout: seconds to wait for the prompt
    :return: verdict line, empty if there is none
    """
    return verdict_of(run(cmd, timeout), regex)


def _ino(path: str) -> int:
    """Return the node's ``st_ino`` of a path (``fstl stat``).

    :param path: node path
    :return: inode number
    """
    return int(fstl_pass(f"stat {path}")["ino"])


# Mount


def test_v9fs_mount_umount(work: Tuple[Path, str]) -> None:
    """Unmount and mount the share; a host file is readable after."""
    _skip_remount()
    host, node = work
    (host / "probe.txt").write_text("mounted again\n")
    out = run(f"umount {MNT}")
    assert "failed" not in out, out
    assert not _mounted()
    _mount()
    assert "mounted again" in run(f"cat {node}/probe.txt")


@pytest.mark.parametrize(
    "opts",
    [
        pytest.param(f"tag={TAG},trans=virtio", id="tag-first"),
        pytest.param(f"trans=virtio,tag={TAG},msize=16384", id="tag-middle"),
        pytest.param(f"trans=virtio,msize=32768,tag={TAG}", id="tag-last"),
        pytest.param(f"trans=virtio,tag={TAG},", id="trailing-comma"),
        pytest.param(
            f"uname=nuttx,trans=virtio,aname=/,tag={TAG}", id="uname-aname"
        ),
    ],
)
def test_v9fs_mount_options(work: Tuple[Path, str], opts: str) -> None:
    """Mount with the tag at any position and extra options."""
    _skip_remount()
    host, node = work
    (host / "probe.txt").write_text(f"opts {opts}\n")
    run(f"umount {MNT}")
    fstl_pass(f"mount v9fs {MNT} {opts} OK")
    assert f"opts {opts}" in run(f"cat {node}/probe.txt")
    fstl_pass(f"write {node}/data.bin 200000 -s 5 -b 65536")
    assert (host / "data.bin").read_bytes() == pattern(0, 200000, 5)


# Data


def test_v9fs_shell_text(work: Tuple[Path, str]) -> None:
    """``echo``/``cat`` text files written on either side."""
    host, node = work
    run(f"echo node text > {node}/n.txt")
    assert (host / "n.txt").read_text() == "node text\n"
    (host / "h.txt").write_text("host text\n")
    assert "host text" in run(f"cat {node}/h.txt")


def test_v9fs_write_small(work: Tuple[Path, str]) -> None:
    """The node writes 1000 pattern bytes; the host reads them."""
    host, node = work
    fstl_pass(f"write {node}/small.bin 1000 -s 1")
    assert (host / "small.bin").read_bytes() == pattern(0, 1000, 1)
    fstl_pass(f"verify {node}/small.bin 1000 -s 1")


def test_v9fs_large_node_to_host(work: Tuple[Path, str]) -> None:
    """The node writes 5 MiB (many msize-limited requests)."""
    host, node = work
    size = 5 * 1024 * 1024
    fstl_pass(f"write {node}/big.bin {size} -s 2 -b 65536", timeout=300)
    assert (host / "big.bin").read_bytes() == pattern(0, size, 2)
    fstl_pass(f"verify {node}/big.bin {size} -s 2 -b 65536", timeout=300)


def test_v9fs_large_host_to_node(work: Tuple[Path, str]) -> None:
    """The host writes 5 MiB plus an odd tail; the node reads it."""
    host, node = work
    size = 5 * 1024 * 1024 + 4099
    (host / "big.bin").write_bytes(pattern(0, size, 3))
    fstl_pass(f"verify {node}/big.bin {size} -s 3 -b 20000", timeout=300)


def test_v9fs_append_reopen(work: Tuple[Path, str]) -> None:
    """Data appended by reopening at the end continues the file."""
    host, node = work
    fstl_pass(f"write {node}/a.bin 3000 -s 4")
    fstl_pass(f"write {node}/a.bin 5000 -s 4 -a")
    assert (host / "a.bin").read_bytes() == pattern(0, 8000, 4)


def test_v9fs_pread_pwrite(work: Tuple[Path, str]) -> None:
    """pread/pwrite keep the offset; lseek; past-end reads; holes."""
    host, node = work
    fstl_pass(f"pio {node}/pio.bin")
    data = (host / "pio.bin").read_bytes()
    assert len(data) == 10004
    assert data[:1000] == pattern(0, 1000)
    assert data[1000:1100] == b"\xa5" * 100
    assert data[4096:8192] == bytes(4096)


@pytest.mark.xfail(strict=True, reason=SEEK_BUG)
def test_v9fs_lseek_negative(work: Tuple[Path, str]) -> None:
    """lseek() to a negative offset fails with EINVAL."""
    host, node = work
    (host / "f").write_bytes(b"x" * 100)
    fstl_pass(f"err seekneg {node}/f EINVAL")


def test_v9fs_truncate(work: Tuple[Path, str]) -> None:
    """Shrink and extend with ftruncate, truncate(), O_TRUNC."""
    host, node = work
    fstl_pass(f"trunc {node}/t.bin")
    assert (host / "t.bin").stat().st_size == 0


@pytest.mark.xfail(strict=True, reason=APPEND_BUG)
def test_v9fs_append(work: Tuple[Path, str]) -> None:
    """O_APPEND writes land at the end and move the offset there."""
    host, node = work
    fstl_pass(f"append {node}/ap.bin")
    assert (host / "ap.bin").read_bytes() == pattern(0, 1500)


def test_v9fs_fsync(work: Tuple[Path, str]) -> None:
    """fsync/fdatasync succeed; the host sees the data."""
    host, node = work
    fstl_pass(f"sync {node}/s.bin")
    assert (host / "s.bin").read_bytes() == pattern(0, 65536)


# Name space


def test_v9fs_mkdir_rmdir(work: Tuple[Path, str]) -> None:
    """``mkdir``/``rmdir`` from NSH show up on the host."""
    host, node = work
    run(f"mkdir {node}/newdir")
    assert (host / "newdir").is_dir()
    run(f"rmdir {node}/newdir")
    assert not (host / "newdir").exists()


def test_v9fs_nested_dirs(work: Tuple[Path, str]) -> None:
    """A file 16 path elements below the mount point (12 nested)."""
    host, node = work
    fstl_pass(f"deep {node}/n 12")
    leaf = host / "n" / Path(*(["d"] * 12)) / "f"
    assert leaf.read_bytes() == pattern(0, 1000, 12)


@pytest.mark.xfail(strict=True, reason=WALK_BUG)
def test_v9fs_deep_path(work: Tuple[Path, str]) -> None:
    """A file 18 path elements below the mount point."""
    host, node = work
    fstl_pass(f"deep {node}/n 14")
    leaf = host / "n" / Path(*(["d"] * 14)) / "f"
    assert leaf.read_bytes() == pattern(0, 1000, 14)


def test_v9fs_rename_file(work: Tuple[Path, str]) -> None:
    """``mv`` of a file keeps its data and inode."""
    host, node = work
    (host / "old.bin").write_bytes(pattern(0, 3000, 6))
    ino = _ino(f"{node}/old.bin")
    run(f"mv {node}/old.bin {node}/new.bin")
    assert not (host / "old.bin").exists()
    assert (host / "new.bin").read_bytes() == pattern(0, 3000, 6)
    assert _ino(f"{node}/new.bin") == ino


def test_v9fs_rename_dir(work: Tuple[Path, str]) -> None:
    """``mv`` of a directory with contents to another directory."""
    host, node = work
    (host / "src" / "sub").mkdir(parents=True)
    (host / "src" / "sub" / "f").write_bytes(pattern(0, 500, 7))
    (host / "dst").mkdir()
    fstl_pass(f"err rename {node}/src {node}/dst/moved OK")
    assert not (host / "src").exists()
    assert (host / "dst/moved/sub/f").read_bytes() == pattern(0, 500, 7)


def test_v9fs_rename_replace(work: Tuple[Path, str]) -> None:
    """rename() onto an existing file replaces it."""
    host, node = work
    (host / "a").write_bytes(b"new contents")
    (host / "b").write_bytes(b"old")
    fstl_pass(f"err rename {node}/a {node}/b OK")
    assert not (host / "a").exists()
    assert (host / "b").read_bytes() == b"new contents"


def test_v9fs_rename_hardlink(work: Tuple[Path, str]) -> None:
    """rename() between two links of one file does nothing (POSIX)."""
    host, node = work
    (host / "a").write_bytes(b"linked")
    os.link(host / "a", host / "b")
    assert _ino(f"{node}/a") == _ino(f"{node}/b")
    fstl_pass(f"err rename {node}/a {node}/b OK")
    assert (host / "a").read_bytes() == b"linked"
    assert (host / "b").read_bytes() == b"linked"


def test_v9fs_unlink(work: Tuple[Path, str]) -> None:
    """``rm`` of a file removes it on the host."""
    host, node = work
    (host / "gone").write_bytes(b"x")
    run(f"rm {node}/gone")
    assert not (host / "gone").exists()


def test_v9fs_readdir_large(
    request: pytest.FixtureRequest, work: Tuple[Path, str]
) -> None:
    """600 entries: all listed once, the end is not an error."""
    _xfail_readdir_off_t(request)
    host, node = work
    for i in range(600):
        (host / f"entry_with_a_long_name_{i:04d}").write_bytes(b"")
    fstl_pass(f"ls -q -n 600 {node}", timeout=180)


def test_v9fs_readdir_entries(
    request: pytest.FixtureRequest, work: Tuple[Path, str]
) -> None:
    """Names, types and d_ino of a mixed directory."""
    _xfail_readdir_off_t(request)
    host, node = work
    (host / "file").write_bytes(b"12345")
    (host / "dir").mkdir()
    (host / "link").symlink_to("file")
    out = run(f"fstl ls -n 3 {node}")
    assert verdict_of(out).startswith("fstl: PASS ls n=3"), out
    ents = {
        name: fields(rest)
        for name, rest in re.findall(r"fstl: ent (\S+) ([^\r\n]*)", out)
    }
    assert set(ents) == {"file", "dir", "link"}
    assert ents["file"]["type"] == "8" and ents["file"]["size"] == "5"
    assert ents["dir"]["type"] == "4"
    assert ents["link"]["type"] == "10"
    inos = {ent["d_ino"] for ent in ents.values()}
    assert len(inos) == 3 and "0" not in inos


def test_v9fs_inode_numbers(work: Tuple[Path, str]) -> None:
    """st_ino: stable, distinct per file, shared by hard links."""
    host, node = work
    for name in ("a", "b", "c"):
        (host / name).write_bytes(name.encode())
    os.link(host / "c", host / "c2")
    inos = {name: _ino(f"{node}/{name}") for name in ("a", "b", "c", "c2")}
    assert len({inos["a"], inos["b"], inos["c"]}) == 3
    assert inos["c"] == inos["c2"]
    assert _ino(f"{node}/a") == inos["a"]
    assert _ino(node) not in inos.values()


def test_v9fs_stat_fields(work: Tuple[Path, str]) -> None:
    """The stat size, mode, times, links and owner match the host."""
    host, node = work
    path = host / "s.bin"
    path.write_bytes(b"y" * 12345)
    path.chmod(0o640)
    os.utime(path, (1500000000, 1600000000))
    st = path.stat()
    got = fstl_pass(f"stat {node}/s.bin")
    assert int(got["size"]) == 12345
    assert int(got["mode"], 8) == st.st_mode
    assert int(got["mtime"]) == 1600000000
    assert int(got["atime"]) == 1500000000
    assert int(got["nlink"]) == st.st_nlink
    assert int(got["uid"]) == st.st_uid and int(got["gid"]) == st.st_gid
    dirst = fstl_pass(f"stat {node}")
    assert stat.S_ISDIR(int(dirst["mode"], 8))


def test_v9fs_statfs(share: Path) -> None:
    """The statfs fields describe the host file system.

    The server may report larger blocks (a multiple of the host block
    size, up to msize) with the total size kept.
    """
    got = fstl_pass(f"statfs {MNT}")
    vfs = os.statvfs(share)
    bsize = int(got["bsize"])
    assert int(got["type"], 16) == V9FS_MAGIC
    assert bsize % vfs.f_bsize == 0
    assert abs(bsize * int(got["blocks"]) - vfs.f_bsize * vfs.f_blocks) < bsize
    assert int(got["namelen"]) == vfs.f_namemax


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("600", id="600"),
        pytest.param(
            "644",
            id="644",
            marks=pytest.mark.xfail(strict=True, reason=CHMOD_BUG),
        ),
        pytest.param(
            "755",
            id="755",
            marks=pytest.mark.xfail(strict=True, reason=CHMOD_BUG),
        ),
    ],
)
def test_v9fs_chmod(work: Tuple[Path, str], mode: str) -> None:
    """chmod() sets the permission bits on the host."""
    host, node = work
    (host / "m").write_bytes(b"")
    fstl_pass(f"chmod {node}/m {mode}")
    assert stat.S_IMODE((host / "m").stat().st_mode) == int(mode, 8)


@pytest.mark.xfail(strict=True, reason=UTIME_BUG)
def test_v9fs_utimens(work: Tuple[Path, str]) -> None:
    """utimens() sets atime and mtime on the host."""
    host, node = work
    (host / "t").write_bytes(b"")
    fstl_pass(f"utime {node}/t 1000000000")
    assert int((host / "t").stat().st_mtime) == 1000000000


def test_v9fs_symlink_unsupported(work: Tuple[Path, str]) -> None:
    """No symlink support: symlink/readlink fail cleanly (ENOSYS)."""
    host, node = work
    (host / "f").write_bytes(b"target")
    (host / "lnk").symlink_to("f")
    fstl_pass(f"err symlink f {node}/new ENOSYS")
    assert not (host / "new").exists()
    fstl_pass(f"err readlink {node}/lnk ENOSYS")
    fstl_pass(f"err open {node}/lnk ELOOP")


ERRNO_CASES = [
    ("open-missing", "open", "nope", None, "ENOENT", None),
    ("stat-missing", "stat", "nope", None, "ENOENT", None),
    ("unlink-missing", "unlink", "nope", None, "ENOENT", None),
    ("rename-missing", "rename", "nope", "z", "ENOENT", None),
    ("creat-no-parent", "creat", "nodir/x", None, "ENOENT", None),
    ("mkdir-exists-dir", "mkdir", "d", None, "EEXIST", None),
    ("mkdir-exists-file", "mkdir", "f", None, "EEXIST", None),
    ("rmdir-not-empty", "rmdir", "d", None, "ENOTEMPTY", None),
    ("rmdir-file", "rmdir", "f", None, "ENOTDIR", None),
    ("rename-dir-to-file", "rename", "d", "f", "ENOTDIR", None),
    ("mkdir-below-file", "mkdir", "f/x", None, "ENOTDIR", None),
    ("unlink-dir", "unlink", "d", None, "EISDIR|EPERM", None),
    ("rename-file-to-dir", "rename", "f", "d", "EISDIR", None),
    ("rename-into-self", "rename", "d", "d/sub", "EINVAL", None),
    ("excl-exists", "excl", "f", None, "EEXIST", EXCL_BUG),
    ("wronly-dir", "wronly", "d", None, "EISDIR", QID_TYPE_BUG),
    ("opendir-file", "opendir", "f", None, "ENOTDIR", QID_TYPE_BUG),
    ("open-below-file", "open", "f/x", None, "ENOTDIR", QID_TYPE_BUG),
]


@pytest.mark.parametrize(
    "op,path,path2,expect",
    [
        pytest.param(
            op,
            path,
            path2,
            expect,
            id=case,
            marks=(
                [pytest.mark.xfail(strict=True, reason=bug)] if bug else []
            ),
        )
        for case, op, path, path2, expect, bug in ERRNO_CASES
    ],
)
def test_v9fs_errno(
    work: Tuple[Path, str],
    op: str,
    path: str,
    path2: Optional[str],
    expect: str,
) -> None:
    """Failing calls return the POSIX errno and change nothing."""
    host, node = work
    (host / "f").write_bytes(b"file")
    (host / "d").mkdir()
    (host / "d" / "x").write_bytes(b"")
    before = sorted(str(p.relative_to(host)) for p in host.rglob("*"))
    second = f" {node}/{path2}" if path2 else ""
    fstl_pass(f"err {op} {node}/{path}{second} {expect}")
    after = sorted(str(p.relative_to(host)) for p in host.rglob("*"))
    assert after == before
    assert (host / "f").read_bytes() == b"file"


def test_v9fs_concurrent_threads(work: Tuple[Path, str]) -> None:
    """Two threads write and read back their own files at once."""
    host, node = work
    size = 1024 * 1024
    fstl_pass(f"conc {node} 2 {size}", timeout=300)
    for i in range(2):
        data = (host / f"conc{i}.bin").read_bytes()
        assert data == pattern(0, size, i + 1)


def test_v9fs_concurrent_tasks(work: Tuple[Path, str]) -> None:
    """Two background tasks write their own files at once."""
    host, node = work
    size = 2 * 1024 * 1024
    out = ""
    for i in (1, 2):
        ret = core().sendCommandReadUntilPattern(
            f"fstl write {node}/task{i}.bin {size} -s {10 + i} &",
            pattern=r"fstl \[\d+:\d+\]",
            timeout=30,
        )
        out += str(ret.output)
    deadline = time.monotonic() + 300
    while len(re.findall(VERDICT_RE, out)) < 2:
        assert time.monotonic() < deadline, out
        ret = core().readUntilPattern(pattern=VERDICT_RE, timeout=60)
        out += str(ret.output)
    assert re.findall(VERDICT_RE, out) == ["PASS", "PASS"], out
    for i in (1, 2):
        data = (host / f"task{i}.bin").read_bytes()
        assert data == pattern(0, size, 10 + i)


def test_v9fs_remount_persistence(work: Tuple[Path, str]) -> None:
    """Files and directories written before umount are there after."""
    _skip_remount()
    host, node = work
    run(f"mkdir {node}/keep")
    fstl_pass(f"write {node}/keep/p.bin 70000 -s 8")
    run(f"umount {MNT}")
    assert (host / "keep" / "p.bin").read_bytes() == pattern(0, 70000, 8)
    _mount()
    fstl_pass(f"verify {node}/keep/p.bin 70000 -s 8")
    assert "p.bin" in run(f"ls {node}/keep")


def test_v9fs_getpath(work: Tuple[Path, str]) -> None:
    """fcntl(F_GETPATH) returns the path of an open file."""
    host, node = work
    (host / "sub").mkdir()
    (host / "sub" / "file.txt").write_bytes(b"")
    fstl_pass(f"path {node}/sub/file.txt")


# Programs run from the share


def test_v9fs_elf_args(share: Path) -> None:
    """A loadable program run by path gets its arguments."""
    _elf(share, "elftl")
    verdict = _prog(f"{MNT}/bin/elftl args one two three", r"elftl: \S+.*")
    assert verdict == "elftl: PASS argc=5 args=one,two,three"


def test_v9fs_elf_exit_status(share: Path) -> None:
    """posix_spawn() of a program returns its exit status."""
    _elf(share, "elftl")
    fstl_pass(f"exec {MNT}/bin/elftl 7 exit 7")
    fstl_pass(f"exec {MNT}/bin/elftl 0 exit 0")


def test_v9fs_elf_path_search(share: Path) -> None:
    """A program in PATH runs by name from NSH and posix_spawnp()."""
    _elf(share, "elftl")
    verdict = _prog("elftl args x", r"elftl: \S+.*")
    assert verdict == "elftl: PASS argc=3 args=x"
    fstl_pass("exec elftl 3 exit 3")


def test_v9fs_elf_twice(share: Path) -> None:
    """The same program file runs twice in a row."""
    _elf(share, "elftl")
    for _ in range(2):
        verdict = _prog(f"{MNT}/bin/elftl libc", r"elftl: \S+.*")
        assert verdict == "elftl: PASS libc"


def test_v9fs_elf_relocations(share: Path) -> None:
    """320 functions called through a table of relocated pointers."""
    _elf(share, "elftl")
    verdict = _prog(f"{MNT}/bin/elftl relocs", r"elftl: \S+.*")
    assert verdict == "elftl: PASS relocs n=320 sum=371040"


def test_v9fs_elf_stack_priority(share: Path) -> None:
    """Stack size and priority come from nx_stacksize/nx_priority."""
    _elf(share, "elftl")
    verdict = _prog(f"{MNT}/bin/elftl stack", r"elftl: \S+.*")
    assert verdict.startswith("elftl: PASS stack"), verdict
    got = fields(verdict)
    want = int(kv("CONFIG_TESTLAB_ELFTL_STACKSIZE"))
    assert want - 256 <= int(got["size"]) <= want
    assert int(got["prio"]) == int(kv("CONFIG_TESTLAB_ELFTL_PRIORITY"))


def test_v9fs_elf_symbols(request: pytest.FixtureRequest, share: Path) -> None:
    """A program that calls getpid(), gettid() and putchar() runs."""
    if not kv("CONFIG_BUILD_KERNEL"):
        request.applymarker(pytest.mark.xfail(strict=True, reason=SYMTAB_BUG))
    _elf(share, "elfsym")
    verdict = _prog(f"{MNT}/bin/elfsym", r"elfsym: \S+.*")
    assert verdict.startswith("elfsym: PASS"), verdict


# Mount errors, last: a failed virtio probe leaves the bus list damaged


def test_v9fs_mount_bad_options() -> None:
    """Mount without a tag, without or with an unknown transport."""
    fstl_pass("mount v9fs /mnt/bad trans=virtio EINVAL")
    fstl_pass(f"mount v9fs /mnt/bad trans=nosuch,tag={TAG} ENOENT")
    fstl_pass(f"mount v9fs /mnt/bad tag={TAG} ENOENT")
    assert _mounted()


@pytest.mark.skip(reason=UNAME_BUG)
def test_v9fs_mount_long_uname(work: Tuple[Path, str]) -> None:
    """A uname= value longer than NAME_MAX is rejected or truncated."""
    host, node = work
    (host / "probe.txt").write_text("long uname\n")
    run(f"umount {MNT}")
    fstl(f"mount v9fs {MNT} {OPTS},uname={'u' * 40} OK|ENAMETOOLONG")
    _mount()
    assert "long uname" in run(f"cat {node}/probe.txt")


@pytest.mark.skip(reason=PROBE_UAF_BUG)
def test_v9fs_mount_busy_tag(work: Tuple[Path, str]) -> None:
    """A second mount of the mounted tag fails; unmount and remount."""
    host, node = work
    (host / "probe.txt").write_text("busy\n")
    fstl_pass(f"mount v9fs /mnt/again {OPTS} ENODEV")
    fstl_pass("mount v9fs /mnt/bad trans=virtio,tag=nosuch ENODEV")
    run(f"umount {MNT}")
    _mount()
    assert "busy" in run(f"cat {node}/probe.txt")


def test_v9fs_mount_wrong_tag(work: Tuple[Path, str]) -> None:
    """A tag no device has fails with ENODEV; the share still mounts."""
    _skip_remount()
    host, node = work
    (host / "probe.txt").write_text("after wrong tag\n")
    run(f"umount {MNT}")
    fstl_pass("mount v9fs /mnt/bad trans=virtio,tag=nosuch ENODEV")
    _mount()
    assert "after wrong tag" in run(f"cat {node}/probe.txt")
    fstl_pass(f"write {node}/after.bin 100000 -s 9")
    assert (host / "after.bin").read_bytes() == pattern(0, 100000, 9)
