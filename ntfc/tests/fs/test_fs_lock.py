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
"""POSIX record locks (``fcntl()`` F_GETLK, F_SETLK, F_SETLKW) on tmpfs.

``fstl lock <case>`` runs one check in one process with one descriptor
per request.  Expectations are quoted from POSIX.1-2024 fcntl()
(pubs.opengroup.org).
"""

import pytest
from _fs_common import fstl_pass, kv, run

pytestmark = [pytest.mark.dep_config("CONFIG_FS_LOCK_BUCKET_SIZE")]

TMP = "/mnt/tmp"

GETLK_OWNER_BUG = (
    "fs/vfs/fs_lock.c file_getlk() never sets the l_pid of the request "
    "(file_setlk() does: request.l_pid = gettid()), so "
    "file_lock_is_conflict() compares the l_pid passed in by the caller "
    "with the lock owner and F_GETLK reports the caller's own lock as "
    "blocking"
)
ACCESS_MODE_BUG = (
    "fs/vfs/fs_lock.c file_setlk() and fs/vfs/fs_fcntl.c F_SETLK/F_SETLKW "
    "never check the lock type against f_oflags: a shared lock on a "
    "descriptor not open for reading and an exclusive lock on one not "
    "open for writing are set instead of failing with EBADF"
)


@pytest.fixture
def tmpdir(request: pytest.FixtureRequest) -> str:
    """Mount tmpfs at ``/mnt/tmp`` and return an empty per-test directory.

    :param request: pytest request
    :return: node path
    """
    if not kv("CONFIG_FS_TMPFS"):
        pytest.skip("no CONFIG_FS_TMPFS")
    if f"{TMP} type tmpfs" not in run("mount"):
        run(f"mount -t tmpfs {TMP}")
    assert f"{TMP} type tmpfs" in run("mount")
    path = f"{TMP}/{request.node.name[:24]}"
    run(f"rm -rf {path}")
    run(f"mkdir {path}")
    return path


@pytest.mark.xfail(strict=True, reason=GETLK_OWNER_BUG)
def test_tmpfs_lock_getlk_own(tmpdir: str) -> None:
    """F_GETLK of the lock owner reports F_UNLCK.

    POSIX fcntl() F_GETLK: "Get any lock which blocks the process-owned
    file lock description"; the own lock of a process does not block it:
    "the previous shared or exclusive lock for each byte in the specified
    region shall be replaced by the new shared or exclusive lock."  "On
    input, the l_pid field shall be ignored for F_GETLK".
    """
    fstl_pass(f"lock getlkown {tmpdir}/l.bin")


@pytest.mark.xfail(strict=True, reason=ACCESS_MODE_BUG)
def test_tmpfs_lock_wrong_access_mode(tmpdir: str) -> None:
    """F_SETLK and F_SETLKW fail with EBADF in the wrong access mode.

    POSIX fcntl(): "A request for a shared lock shall fail if the file
    descriptor is not open for reading.  A request for an exclusive lock
    shall fail if the file descriptor is not open for writing."  [EBADF]:
    the cmd argument is F_SETLK or F_SETLKW, the lock is shared and fildes
    "is not a valid file descriptor open for reading", or exclusive and
    not open for writing.
    """
    fstl_pass(f"lock badf {tmpdir}/l.bin")


def test_tmpfs_lock_einval(tmpdir: str) -> None:
    """An invalid l_type and ranges before the file start fail with EINVAL.

    POSIX fcntl(): "[EINVAL] The cmd argument is ... F_GETLK, F_SETLK,
    F_SETLKW ... and the data pointed to by arg is not valid".  l_type is
    F_RDLCK, F_WRLCK or F_UNLCK (here 99, F_SETLK and F_GETLK); locks
    "shall not extend before the beginning of the file" (l_start -1, and
    l_start 2 with l_len -5: "If l_len is negative, the area affected
    shall start at l_start+l_len").
    """
    fstl_pass(f"lock inval {tmpdir}/l.bin")
