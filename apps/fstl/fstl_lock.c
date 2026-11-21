/****************************************************************************
 * apps/fstl/fstl_lock.c
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Licensed under the Apache License, Version 2.0 (the "License"); you
 * may not use this file except in compliance with the License.  You
 * may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
 * WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
 * License for the specific language governing permissions and limitations
 * under the License.
 *
 ****************************************************************************/

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>

#include <sys/param.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "fstl.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define FSTL_LK_SIZE      32    /* File size of the lock checks */

/****************************************************************************
 * Private Types
 ****************************************************************************/

/* A request of fcntl() and the errno it must fail with */

struct fstl_lk_req_s
{
  int             cmd;
  int             oflags;
  int             type;
  off_t           start;
  off_t           len;
  int             want;
  FAR const char *step;
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

/* badf: lock type against the access mode */

static const struct fstl_lk_req_s g_fstl_lk_badf[] =
{
  { F_SETLK,  O_RDONLY, F_WRLCK, 0, 0, EBADF, "rdonly-wrlck" },
  { F_SETLK,  O_WRONLY, F_RDLCK, 0, 0, EBADF, "wronly-rdlck" },
  { F_SETLKW, O_RDONLY, F_WRLCK, 0, 0, EBADF, "rdonly-wrlckw" },
  { F_SETLKW, O_WRONLY, F_RDLCK, 0, 0, EBADF, "wronly-rdlckw" },
};

/* inval: invalid l_type and ranges before the file start */

static const struct fstl_lk_req_s g_fstl_lk_inval[] =
{
  { F_SETLK, O_RDWR, 99,      0,  1,  EINVAL, "setlk-type" },
  { F_GETLK, O_RDWR, 99,      0,  1,  EINVAL, "getlk-type" },
  { F_SETLK, O_RDWR, F_WRLCK, -1, 1,  EINVAL, "setlk-before" },
  { F_SETLK, O_RDWR, F_WRLCK, 2,  -5, EINVAL, "setlk-neglen" },
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static char fstl_lk_char(int type)
{
  return type == F_RDLCK ? 'R' : type == F_WRLCK ? 'W' :
         type == F_UNLCK ? 'U' : '?';
}

/* fcntl() with a lock of [start, start + len) from SEEK_SET; returns 0
 * or the errno.
 */

static int fstl_lk_cmd(int fd, int cmd, int type, off_t start, off_t len)
{
  struct flock fl;

  memset(&fl, 0, sizeof(fl));
  fl.l_type   = type;
  fl.l_whence = SEEK_SET;
  fl.l_start  = start;
  fl.l_len    = len;
  return fcntl(fd, cmd, &fl) < 0 ? errno : 0;
}

/* Create the file with FSTL_LK_SIZE bytes and return it open O_RDWR */

static int fstl_lk_open(FAR const char *check, FAR const char *path)
{
  uint8_t buf[FSTL_LK_SIZE];
  int fd;

  fstl_fill(buf, sizeof(buf), 0, 0);
  fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
  if (fd < 0 || write(fd, buf, sizeof(buf)) != sizeof(buf))
    {
      printf("fstl: FAIL lock %s open errno=%s\n", check,
             fstl_errname(errno));
      if (fd >= 0)
        {
          close(fd);
        }

      return -1;
    }

  return fd;
}

/* getlkown: the own lock of a process does not block it, F_GETLK of the
 * owner reports F_UNLCK.
 */

static int fstl_lk_getlkown(int fd)
{
  struct flock fl;
  int ret;

  if ((ret = fstl_lk_cmd(fd, F_SETLK, F_WRLCK, 4, 4)) != 0)
    {
      printf("fstl: FAIL lock getlkown wrlck errno=%s\n",
             fstl_errname(ret));
      return -1;
    }

  memset(&fl, 0, sizeof(fl));
  fl.l_type   = F_WRLCK;
  fl.l_whence = SEEK_SET;
  if (fcntl(fd, F_GETLK, &fl) < 0 || fl.l_type != F_UNLCK)
    {
      printf("fstl: FAIL lock getlkown type=%c start=%lld len=%lld "
             "pid=%d\n", fstl_lk_char(fl.l_type), (long long)fl.l_start,
             (long long)fl.l_len, (int)fl.l_pid);
      return -1;
    }

  return OK;
}

/* Run requests that must fail with their errno, each on a descriptor of
 * the file open with its access mode.
 */

static int fstl_lk_reqs(FAR const char *check, FAR const char *path,
                        FAR const struct fstl_lk_req_s *req, int nreq)
{
  int ret;
  int fd;
  int i;

  for (i = 0; i < nreq; i++, req++)
    {
      fd = open(path, req->oflags);
      if (fd < 0)
        {
          printf("fstl: FAIL lock %s open errno=%s\n", check,
                 fstl_errname(errno));
          return -1;
        }

      ret = fstl_lk_cmd(fd, req->cmd, req->type, req->start, req->len);
      close(fd);
      if (ret != req->want)
        {
          printf("fstl: FAIL lock %s %s errno=%s want=%s\n", check,
                 req->step, fstl_errname(ret), fstl_errname(req->want));
          return -1;
        }
    }

  return OK;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/* lock <case> <path>: one POSIX record lock check */

int fstl_lock(FAR const char *check, FAR const char *path)
{
  int ret;
  int fd;

  fd = fstl_lk_open(check, path);
  if (fd < 0)
    {
      return -1;
    }

  if (strcmp(check, "getlkown") == 0)
    {
      ret = fstl_lk_getlkown(fd);
    }
  else if (strcmp(check, "badf") == 0)
    {
      ret = fstl_lk_reqs(check, path, g_fstl_lk_badf,
                         (int)nitems(g_fstl_lk_badf));
    }
  else if (strcmp(check, "inval") == 0)
    {
      ret = fstl_lk_reqs(check, path, g_fstl_lk_inval,
                         (int)nitems(g_fstl_lk_inval));
    }
  else
    {
      printf("fstl: FAIL lock %s unknown\n", check);
      ret = -1;
    }

  close(fd);
  unlink(path);
  if (ret == OK)
    {
      printf("fstl: PASS lock %s\n", check);
    }

  return ret;
}
