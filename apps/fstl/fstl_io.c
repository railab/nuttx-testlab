/****************************************************************************
 * apps/fstl/fstl_io.c
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

#include <sys/stat.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "fstl.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define FSTL_CONC_MAX  8
#define FSTL_PIO_SIZE  4096
#define FSTL_FILL      0xa5

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct fstl_conc_s
{
  char   path[PATH_MAX];
  off_t  size;
  int    index;
  int    ret;
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/* Print a FAIL verdict for a failed step, errno taken from ret < 0 or from
 * errno.  Always returns -1.
 */

static int fstl_fail(FAR const char *check, FAR const char *step,
                     long long ret)
{
  int errcode = errno;

  printf("fstl: FAIL %s %s ret=%lld errno=%s\n", check, step, ret,
         fstl_errname(ret < 0 && ret != -1 ? (int)-ret : errcode));
  return -1;
}

/* Write len bytes of the pattern at the current position of fd */

static int fstl_putpattern(int fd, FAR uint8_t *buf, size_t bufsize,
                           off_t offset, off_t len, uint32_t seed)
{
  off_t end = offset + len;

  while (offset < end)
    {
      size_t n = end - offset < (off_t)bufsize ?
                 (size_t)(end - offset) : bufsize;
      ssize_t ret;

      fstl_fill(buf, n, (uint32_t)offset, seed);
      ret = write(fd, buf, n);
      if (ret <= 0)
        {
          return ret < 0 ? -errno : -EIO;
        }

      offset += ret;
    }

  return OK;
}

/* Read from fd until EOF and check that it holds len bytes of the
 * pattern starting at offset.  Returns the number of good bytes, or a
 * negated errno.
 */

static off_t fstl_getpattern(int fd, FAR uint8_t *buf, size_t bufsize,
                             off_t offset, off_t len, uint32_t seed)
{
  off_t done = 0;

  for (; ; )
    {
      ssize_t ret = read(fd, buf, bufsize);
      size_t good;

      if (ret < 0)
        {
          return -errno;
        }

      if (ret == 0)
        {
          break;
        }

      if (done + ret > len)
        {
          return done;
        }

      good = fstl_check(buf, ret, (uint32_t)(offset + done), seed);
      done += good;
      if (good != (size_t)ret)
        {
          break;
        }
    }

  return done;
}

static bool fstl_is(FAR const uint8_t *buf, size_t len, uint8_t value)
{
  size_t i;

  for (i = 0; i < len; i++)
    {
      if (buf[i] != value)
        {
          return false;
        }
    }

  return true;
}

static off_t fstl_size(int fd)
{
  struct stat st;

  if (fstat(fd, &st) < 0)
    {
      return -errno;
    }

  return st.st_size;
}

static FAR void *fstl_conc_thread(FAR void *arg)
{
  FAR struct fstl_conc_s *job = arg;
  size_t bufsize = 1000 + job->index * 517;
  FAR uint8_t *buf;
  off_t good;
  int fd;

  buf = malloc(bufsize);
  if (buf == NULL)
    {
      job->ret = -ENOMEM;
      return NULL;
    }

  fd = open(job->path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (fd < 0)
    {
      job->ret = -errno;
      goto out;
    }

  job->ret = fstl_putpattern(fd, buf, bufsize, 0, job->size,
                             job->index + 1);
  close(fd);
  if (job->ret < 0)
    {
      goto out;
    }

  fd = open(job->path, O_RDONLY);
  if (fd < 0)
    {
      job->ret = -errno;
      goto out;
    }

  good = fstl_getpattern(fd, buf, bufsize, 0, job->size, job->index + 1);
  close(fd);
  job->ret = good < 0 ? (int)good : good == job->size ? OK : -EIO;

out:
  free(buf);
  return NULL;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int fstl_write(FAR const char *path, off_t size,
               FAR const struct fstl_opts_s *opts)
{
  FAR uint8_t *buf;
  off_t start = 0;
  int flags;
  int ret;
  int fd;

  flags = O_WRONLY | O_CREAT | (opts->append ? O_APPEND : O_TRUNC);
  fd = open(path, flags, 0644);
  if (fd < 0)
    {
      return fstl_fail("write", "open", fd);
    }

  /* An appended block continues the pattern at the end of the file */

  if (opts->append)
    {
      start = fstl_size(fd);
      if (start < 0)
        {
          close(fd);
          return fstl_fail("write", "fstat", start);
        }
    }

  buf = malloc(opts->bufsize);
  if (buf == NULL)
    {
      close(fd);
      return fstl_fail("write", "malloc", -ENOMEM);
    }

  ret = fstl_putpattern(fd, buf, opts->bufsize, start, size, opts->seed);
  free(buf);
  if (ret < 0)
    {
      close(fd);
      return fstl_fail("write", "write", ret);
    }

  if (close(fd) < 0)
    {
      return fstl_fail("write", "close", -1);
    }

  printf("fstl: PASS write size=%lld\n", (long long)(start + size));
  return OK;
}

int fstl_verify(FAR const char *path, off_t size,
                FAR const struct fstl_opts_s *opts)
{
  FAR uint8_t *buf;
  off_t good;
  off_t st;
  int fd;

  fd = open(path, O_RDONLY);
  if (fd < 0)
    {
      return fstl_fail("verify", "open", fd);
    }

  st = fstl_size(fd);
  if (st != size)
    {
      close(fd);
      printf("fstl: FAIL verify size st_size=%lld want=%lld\n",
             (long long)st, (long long)size);
      return -1;
    }

  buf = malloc(opts->bufsize);
  if (buf == NULL)
    {
      close(fd);
      return fstl_fail("verify", "malloc", -ENOMEM);
    }

  good = fstl_getpattern(fd, buf, opts->bufsize, 0, size, opts->seed);
  free(buf);
  close(fd);

  if (good < 0)
    {
      return fstl_fail("verify", "read", good);
    }

  if (good != size)
    {
      printf("fstl: FAIL verify data good=%lld want=%lld\n",
             (long long)good, (long long)size);
      return -1;
    }

  printf("fstl: PASS verify size=%lld\n", (long long)size);
  return OK;
}

/* Positioned I/O: pread/pwrite leave the file offset alone, lseek
 * SEEK_SET/CUR/END, reads past the end and holes read as zeros.
 */

int fstl_pio(FAR const char *path)
{
  uint8_t buf[100];
  ssize_t n;
  off_t pos;
  int step = 0;
  int fd;

  fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
  if (fd < 0)
    {
      return fstl_fail("pio", "open", fd);
    }

  /* 1: pattern, f_pos at its end */

  step++;
  if (fstl_putpattern(fd, buf, sizeof(buf), 0, FSTL_PIO_SIZE, 0) < 0 ||
      lseek(fd, 0, SEEK_CUR) != FSTL_PIO_SIZE)
    {
      goto fail;
    }

  /* 2: pwrite does not move f_pos */

  step++;
  memset(buf, FSTL_FILL, sizeof(buf));
  if (pwrite(fd, buf, sizeof(buf), 1000) != sizeof(buf) ||
      lseek(fd, 0, SEEK_CUR) != FSTL_PIO_SIZE)
    {
      goto fail;
    }

  /* 3: pread sees it and does not move f_pos */

  step++;
  memset(buf, 0, sizeof(buf));
  if (pread(fd, buf, sizeof(buf), 1000) != sizeof(buf) ||
      !fstl_is(buf, sizeof(buf), FSTL_FILL) ||
      lseek(fd, 0, SEEK_CUR) != FSTL_PIO_SIZE)
    {
      goto fail;
    }

  /* 4: pread across the boundary of the two */

  step++;
  if (pread(fd, buf, 20, 990) != 20 ||
      fstl_check(buf, 10, 990, 0) != 10 ||
      !fstl_is(buf + 10, 10, FSTL_FILL))
    {
      goto fail;
    }

  /* 5: lseek SEEK_END, SEEK_CUR and read */

  step++;
  if (lseek(fd, 0, SEEK_END) != FSTL_PIO_SIZE ||
      lseek(fd, -96, SEEK_END) != FSTL_PIO_SIZE - 96 ||
      lseek(fd, 10, SEEK_CUR) != FSTL_PIO_SIZE - 86 ||
      read(fd, buf, 10) != 10 ||
      fstl_check(buf, 10, FSTL_PIO_SIZE - 86, 0) != 10 ||
      lseek(fd, 0, SEEK_CUR) != FSTL_PIO_SIZE - 76)
    {
      goto fail;
    }

  /* 6: pread past the end returns 0 */

  step++;
  if (pread(fd, buf, 10, FSTL_PIO_SIZE + 1000) != 0)
    {
      goto fail;
    }

  /* 7: write after a seek past the end leaves a hole of zeros */

  step++;
  memset(buf, FSTL_FILL, 4);
  if (lseek(fd, 2 * FSTL_PIO_SIZE, SEEK_SET) != 2 * FSTL_PIO_SIZE ||
      write(fd, buf, 4) != 4 ||
      fstl_size(fd) != 2 * FSTL_PIO_SIZE + 4)
    {
      goto fail;
    }

  for (pos = FSTL_PIO_SIZE; pos < 2 * FSTL_PIO_SIZE; pos += sizeof(buf))
    {
      size_t len = 2 * FSTL_PIO_SIZE - pos < (off_t)sizeof(buf) ?
                   (size_t)(2 * FSTL_PIO_SIZE - pos) : sizeof(buf);

      n = pread(fd, buf, len, pos);
      if (n != (ssize_t)len || !fstl_is(buf, len, 0))
        {
          goto fail;
        }
    }

  /* 8: pwrite past the end extends the file */

  step++;
  memset(buf, FSTL_FILL, 4);
  if (pwrite(fd, buf, 4, 10000) != 4 || fstl_size(fd) != 10004)
    {
      goto fail;
    }

  if (close(fd) < 0)
    {
      return fstl_fail("pio", "close", -1);
    }

  printf("fstl: PASS pio\n");
  return OK;

fail:
  printf("fstl: FAIL pio step=%d errno=%s\n", step, fstl_errname(errno));
  close(fd);
  return -1;
}

/* ftruncate shrinks and extends (zeros), truncate by path, O_TRUNC */

int fstl_trunc(FAR const char *path)
{
  uint8_t buf[256];
  off_t pos;
  int step = 0;
  int fd;

  fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
  if (fd < 0)
    {
      return fstl_fail("trunc", "open", fd);
    }

  /* 1: shrink to 5000, the first 5000 bytes stay */

  step++;
  if (fstl_putpattern(fd, buf, sizeof(buf), 0, 10000, 0) < 0 ||
      ftruncate(fd, 5000) < 0 || fstl_size(fd) != 5000 ||
      lseek(fd, 0, SEEK_SET) != 0 ||
      fstl_getpattern(fd, buf, sizeof(buf), 0, 5000, 0) != 5000)
    {
      goto fail;
    }

  /* 2: extend to 12000, the new part reads as zeros */

  step++;
  if (ftruncate(fd, 12000) < 0 || fstl_size(fd) != 12000)
    {
      goto fail;
    }

  for (pos = 5000; pos < 12000; pos += sizeof(buf))
    {
      size_t len = 12000 - pos < (off_t)sizeof(buf) ?
                   (size_t)(12000 - pos) : sizeof(buf);

      if (pread(fd, buf, len, pos) != (ssize_t)len ||
          !fstl_is(buf, len, 0))
        {
          goto fail;
        }
    }

  close(fd);

  /* 3: truncate() by path */

  step++;
  if (truncate(path, 100) < 0)
    {
      goto fail_closed;
    }

  fd = open(path, O_RDONLY);
  if (fd < 0 || fstl_size(fd) != 100)
    {
      goto fail;
    }

  close(fd);

  /* 4: O_TRUNC empties the file */

  step++;
  fd = open(path, O_WRONLY | O_TRUNC);
  if (fd < 0 || fstl_size(fd) != 0)
    {
      goto fail;
    }

  close(fd);
  printf("fstl: PASS trunc\n");
  return OK;

fail:
  if (fd >= 0)
    {
      close(fd);
    }

fail_closed:
  printf("fstl: FAIL trunc step=%d errno=%s\n", step,
         fstl_errname(errno));
  return -1;
}

/* O_APPEND writes go to the end even after a seek, f_pos follows them */

int fstl_append(FAR const char *path)
{
  uint8_t buf[256];
  int step = 0;
  int fd;

  fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (fd < 0)
    {
      return fstl_fail("append", "open", fd);
    }

  /* 1: 1000 pattern bytes */

  step++;
  if (fstl_putpattern(fd, buf, sizeof(buf), 0, 1000, 0) < 0)
    {
      goto fail;
    }

  close(fd);

  /* 2: append 500 more after seeking to the start */

  step++;
  fd = open(path, O_WRONLY | O_APPEND);
  if (fd < 0 || lseek(fd, 0, SEEK_SET) != 0 ||
      fstl_putpattern(fd, buf, sizeof(buf), 1000, 500, 0) < 0 ||
      lseek(fd, 0, SEEK_CUR) != 1500)
    {
      goto fail;
    }

  close(fd);

  /* 3: the file is 1500 pattern bytes */

  step++;
  fd = open(path, O_RDONLY);
  if (fd < 0 || fstl_size(fd) != 1500 ||
      fstl_getpattern(fd, buf, sizeof(buf), 0, 1500, 0) != 1500)
    {
      goto fail;
    }

  close(fd);
  printf("fstl: PASS append\n");
  return OK;

fail:
  printf("fstl: FAIL append step=%d errno=%s\n", step,
         fstl_errname(errno));
  if (fd >= 0)
    {
      close(fd);
    }

  return -1;
}

/* fsync and fdatasync of a written file and of a read-only descriptor */

int fstl_sync(FAR const char *path)
{
  uint8_t buf[256];
  int step = 0;
  int fd;

  fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
  if (fd < 0)
    {
      return fstl_fail("sync", "open", fd);
    }

  step++;
  if (fstl_putpattern(fd, buf, sizeof(buf), 0, 65536, 0) < 0 ||
      fsync(fd) < 0 || fdatasync(fd) < 0)
    {
      goto fail;
    }

  close(fd);

  step++;
  fd = open(path, O_RDONLY);
  if (fd < 0 || fsync(fd) < 0)
    {
      goto fail;
    }

  close(fd);
  printf("fstl: PASS sync\n");
  return OK;

fail:
  printf("fstl: FAIL sync step=%d errno=%s\n", step, fstl_errname(errno));
  if (fd >= 0)
    {
      close(fd);
    }

  return -1;
}

/* n threads each write and read back <dir>/conc<i>.bin (seed i + 1) at
 * the same time, with different buffer sizes.
 */

int fstl_conc(FAR const char *dir, int nthreads, off_t size)
{
  struct fstl_conc_s jobs[FSTL_CONC_MAX];
  pthread_t threads[FSTL_CONC_MAX];
  pthread_attr_t attr;
  int failed = 0;
  int started;
  int ret;
  int i;

  if (nthreads < 1 || nthreads > FSTL_CONC_MAX)
    {
      return fstl_fail("conc", "args", -EINVAL);
    }

  pthread_attr_init(&attr);
  pthread_attr_setstacksize(&attr, CONFIG_TESTLAB_FSTL_STACKSIZE);

  for (started = 0; started < nthreads; started++)
    {
      FAR struct fstl_conc_s *job = &jobs[started];

      snprintf(job->path, sizeof(job->path), "%s/conc%d.bin", dir,
               started);
      job->size  = size;
      job->index = started;
      job->ret   = -EINVAL;

      ret = pthread_create(&threads[started], &attr, fstl_conc_thread,
                           job);
      if (ret != 0)
        {
          break;
        }
    }

  pthread_attr_destroy(&attr);

  for (i = 0; i < started; i++)
    {
      pthread_join(threads[i], NULL);
      if (jobs[i].ret < 0)
        {
          printf("fstl: conc %s errno=%s\n", jobs[i].path,
                 fstl_errname(-jobs[i].ret));
          failed++;
        }
    }

  if (started != nthreads || failed > 0)
    {
      printf("fstl: FAIL conc n=%d started=%d failed=%d\n", nthreads,
             started, failed);
      return -1;
    }

  printf("fstl: PASS conc n=%d size=%lld\n", nthreads, (long long)size);
  return OK;
}
