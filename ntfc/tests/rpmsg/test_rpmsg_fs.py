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

"""RPMSG file system (rpmsgfs) tests between two nodes.

Node 1 mounts node 0's ``/tmp/rfs`` (tmpfs) at ``/mnt/rfs``
(``mount -t rpmsgfs -o cpu=<node 0>,fs=/tmp/rfs``); both nodes run
``fstl`` on the same files, node 1 through rpmsgfs, node 0 directly.
Kconfig ``FS_RPMSGFS``: "Use RPMSG file system to mount remote
directories to local. This the method for user to use remote file like
own core." Expectations are quoted from POSIX.1-2024,
https://pubs.opengroup.org/onlinepubs/9799919799/functions/<name>.html.
"""

import itertools
from typing import Iterator, Optional, Tuple

import pytest
from _fs_common import VERDICT_RE, fields
from _rpmsg_common import cpuname, run, verdict

SERVER_DIR = "/tmp/rfs"
CLIENT_DIR = "/mnt/rfs"

_COUNTER = itertools.count()


def _fstl(node: int, args: str, timeout: int = 120) -> str:
    """Run ``fstl`` on a node and return its verdict line.

    :param node: product index
    :param args: check and arguments
    :param timeout: seconds to wait for the verdict
    :return: verdict line
    """
    return verdict(node, f"fstl {args}", VERDICT_RE, timeout)


def _pass(node: int, args: str, timeout: int = 120) -> dict:
    """Run ``fstl`` on a node, require PASS and return its fields.

    :param node: product index
    :param args: check and arguments
    :param timeout: seconds to wait for the verdict
    :return: ``key=value`` fields of the verdict line
    """
    line = _fstl(node, args, timeout)
    assert line.startswith("fstl: PASS"), f"node{node} fstl {args}: {line}"
    return fields(line)


def _nsh(node: int, cmd: str) -> None:
    """Run an NSH command that must not report an error.

    :param node: product index
    :param cmd: command line
    """
    out = run(node, cmd)
    assert "nsh: " not in out, out


@pytest.fixture(scope="module")
def mounted() -> Iterator[None]:
    """Mount node 0's ``/tmp/rfs`` on node 1 for the module."""
    _nsh(0, f"mkdir {SERVER_DIR}")
    _nsh(1, f"mkdir {CLIENT_DIR}")
    _nsh(
        1,
        f"mount -t rpmsgfs -o cpu={cpuname(0)},fs={SERVER_DIR} "
        f"{CLIENT_DIR}",
    )
    assert f"{CLIENT_DIR} type rpmsgfs" in run(1, "mount")
    yield
    _nsh(1, f"umount {CLIENT_DIR}")
    assert f"{CLIENT_DIR} type rpmsgfs" not in run(1, "mount")


@pytest.fixture()
def work(mounted: None) -> Tuple[str, str]:
    """Create a fresh directory for one test on node 0.

    :param mounted: module mount fixture
    :return: the directory on node 0 and on node 1
    """
    name = f"w{next(_COUNTER)}"
    _nsh(0, f"mkdir {SERVER_DIR}/{name}")
    return f"{SERVER_DIR}/{name}", f"{CLIENT_DIR}/{name}"


def test_rpmsgfs_client_to_server(work: Tuple[str, str]) -> None:
    """Data node 1 writes through rpmsgfs is in node 0's file.

    write(): "After a write() to a regular file has successfully
    returned: Any successful read() from each byte position in the file
    that was modified by that write shall return the data specified by
    the write() for that position until such byte positions are again
    modified." 200003 bytes in 4096-byte writes, more than one RPMSG
    buffer each.
    """
    srv, cli = work
    _pass(1, f"write {cli}/big.bin 200003 -s 2 -b 4096")
    _pass(0, f"verify {srv}/big.bin 200003 -s 2 -b 4096")


def test_rpmsgfs_server_to_client(work: Tuple[str, str]) -> None:
    """Data node 0 writes is read by node 1 through rpmsgfs.

    Same write() requirement as above; node 1 reads 150001 bytes in
    20000-byte reads.
    """
    srv, cli = work
    _pass(0, f"write {srv}/big.bin 150001 -s 3 -b 3000")
    _pass(1, f"verify {cli}/big.bin 150001 -s 3 -b 20000")


def test_rpmsgfs_pread_pwrite(work: Tuple[str, str]) -> None:
    """pread()/pwrite() at offsets, lseek() and holes (``fstl pio``)."""
    _, cli = work
    _pass(1, f"pio {cli}/pio.bin")


def test_rpmsgfs_append(work: Tuple[str, str]) -> None:
    """O_APPEND writes land at the end of the file.

    open(): "O_APPEND If set, the file offset shall be set to the end of
    the file prior to each write." Node 0 reads the 1500 bytes back.
    """
    srv, cli = work
    _pass(1, f"append {cli}/ap.bin")
    _pass(0, f"verify {srv}/ap.bin 1500")


def test_rpmsgfs_truncate(work: Tuple[str, str]) -> None:
    """ftruncate(), truncate() and O_TRUNC shrink and extend the file."""
    srv, cli = work
    _pass(1, f"trunc {cli}/t.bin")
    assert _pass(0, f"stat {srv}/t.bin")["size"] == "0"


def test_rpmsgfs_extend_after_seek(work: Tuple[str, str]) -> None:
    """A write past the end leaves a hole that reads as zero bytes."""
    _, cli = work
    _pass(1, f"extend {cli}/e.bin")


def test_rpmsgfs_readv_writev(work: Tuple[str, str]) -> None:
    """readv()/writev() over several buffers (``fstl iov``)."""
    _, cli = work
    _pass(1, f"iov {cli}/iov.bin")


def test_rpmsgfs_fsync(work: Tuple[str, str]) -> None:
    """fsync()/fdatasync() succeed; node 0 has the data."""
    srv, cli = work
    _pass(1, f"sync {cli}/s.bin")
    _pass(0, f"verify {srv}/s.bin 65536")


def test_rpmsgfs_stat_fields(work: Tuple[str, str]) -> None:
    """stat() through rpmsgfs reports node 0's size, mode and times."""
    srv, cli = work
    _pass(0, f"write {srv}/s.bin 12345 -s 5")
    _pass(1, f"utime {cli}/s.bin 1600000000")
    here = _pass(1, f"stat {cli}/s.bin")
    there = _pass(0, f"stat {srv}/s.bin")
    for key in ("size", "mode", "mtime", "atime"):
        assert here[key] == there[key], (key, here, there)
    assert here["size"] == "12345" and here["mtime"] == "1600000000"


def test_rpmsgfs_statfs(work: Tuple[str, str]) -> None:
    """statfs() through rpmsgfs reports node 0's block counts."""
    srv, cli = work
    here = _pass(1, f"statfs {cli}")
    there = _pass(0, f"statfs {srv}")
    for key in ("bsize", "blocks", "files"):
        assert here[key] == there[key], (key, here, there)


def test_rpmsgfs_readdir(work: Tuple[str, str]) -> None:
    """readdir() lists node 0's entries once each (``fstl ls``)."""
    srv, cli = work
    for name in ("a", "b", "c"):
        _pass(0, f"write {srv}/{name} 10")
    _nsh(0, f"mkdir {srv}/d")
    _pass(1, f"ls -n 4 {cli}")


def test_rpmsgfs_rename_unlink(work: Tuple[str, str]) -> None:
    """rename() and unlink() through rpmsgfs change node 0's directory.

    rename(): "The rename() function shall change the name of a file."
    """
    srv, cli = work
    _pass(0, f"write {srv}/old.bin 3000 -s 6")
    _pass(1, f"err rename {cli}/old.bin {cli}/new.bin OK")
    _pass(0, f"err stat {srv}/old.bin ENOENT")
    _pass(0, f"verify {srv}/new.bin 3000 -s 6")
    _pass(1, f"err unlink {cli}/new.bin OK")
    _pass(0, f"err stat {srv}/new.bin ENOENT")


def test_rpmsgfs_nested_dirs(work: Tuple[str, str]) -> None:
    """A file 12 directories below the mount point (``fstl deep``)."""
    srv, cli = work
    _pass(1, f"deep {cli}/n 12")
    _pass(0, f"verify {srv}/n/{'/'.join(['d'] * 12)}/f 1000 -s 12")


def test_rpmsgfs_concurrent_threads(work: Tuple[str, str]) -> None:
    """Four threads write and read back their own files at once."""
    _, cli = work
    _pass(1, f"conc {cli} 4 20000")


ERRNO_CASES = [
    ("open-missing", "open", "nope", None, "ENOENT"),
    ("stat-missing", "stat", "nope", None, "ENOENT"),
    ("unlink-missing", "unlink", "nope", None, "ENOENT"),
    ("rename-missing", "rename", "nope", "z", "ENOENT"),
    ("creat-no-parent", "creat", "nodir/x", None, "ENOENT"),
    ("mkdir-exists-dir", "mkdir", "d", None, "EEXIST"),
    ("mkdir-exists-file", "mkdir", "f", None, "EEXIST"),
    ("rmdir-not-empty", "rmdir", "d", None, "ENOTEMPTY|EEXIST"),
    ("rmdir-file", "rmdir", "f", None, "ENOTDIR"),
    ("rename-dir-to-file", "rename", "d", "f", "ENOTDIR"),
    ("rename-file-to-dir", "rename", "f", "d", "EISDIR"),
    ("rename-into-self", "rename", "d", "d/sub", "EINVAL"),
    ("mkdir-below-file", "mkdir", "f/x", None, "ENOTDIR"),
    ("excl-exists", "excl", "f", None, "EEXIST"),
    ("read-on-wronly", "wrread", "f", None, "EBADF"),
    ("write-on-rdonly", "rdwrite", "f", None, "EBADF"),
]


@pytest.mark.parametrize(
    "op,path,path2,expect",
    [
        pytest.param(op, path, path2, expect, id=case)
        for case, op, path, path2, expect in ERRNO_CASES
    ],
)
def test_rpmsgfs_errno(
    work: Tuple[str, str],
    op: str,
    path: str,
    path2: Optional[str],
    expect: str,
) -> None:
    """A failing call through rpmsgfs returns node 0's POSIX errno.

    open(): "[ENOENT] O_CREAT is not set and a component of path does
    not name an existing file", "[EEXIST] O_CREAT and O_EXCL are set,
    and the named file exists."; mkdir(): "[EEXIST] The named file
    exists."; rmdir(): "[EEXIST] or [ENOTEMPTY] The path argument names
    a directory that is not an empty directory", "[ENOTDIR] A component
    of path names an existing file that is neither a directory nor a
    symbolic link to a directory."; rename(): "[EISDIR] The new argument
    points to a directory and the old argument points to a file that is
    not a directory.", "[ENOTDIR] ... the old argument names a directory
    and the new argument names a non-directory file", "[EINVAL] The old
    pathname names an ancestor directory of the new pathname";
    read()/write(): "[EBADF] The fildes argument is not a valid file
    descriptor open for reading" (writing). Node 0's directory is
    unchanged afterwards.
    """
    srv, cli = work
    _pass(0, f"write {srv}/f 4")
    _nsh(0, f"mkdir {srv}/d")
    _pass(0, f"write {srv}/d/x 1")
    second = f" {cli}/{path2}" if path2 else ""
    _pass(1, f"err {op} {cli}/{path}{second} {expect}")
    _pass(0, f"verify {srv}/f 4")
    _pass(0, f"verify {srv}/d/x 1")
    _pass(1, f"ls -n 2 {cli}")
