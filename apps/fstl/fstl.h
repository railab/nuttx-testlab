/****************************************************************************
 * apps/fstl/fstl.h
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

#ifndef __APPS_FSTL_FSTL_H
#define __APPS_FSTL_FSTL_H

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

/****************************************************************************
 * Public Types
 ****************************************************************************/

/* Options shared by the data checks */

struct fstl_opts_s
{
  uint32_t seed;     /* Pattern seed (-s) */
  size_t   bufsize;  /* I/O buffer size (-b) */
  bool     append;   /* Open with O_APPEND (-a) */
  bool     quiet;    /* No per-entry lines (-q) */
  long     count;    /* Expected entry count, -1: any (-n) */
};

/****************************************************************************
 * Public Function Prototypes
 ****************************************************************************/

/* Pattern helpers (fstl_main.c) */

void fstl_fill(FAR uint8_t *buf, size_t len, uint32_t offset,
               uint32_t seed);
size_t fstl_check(FAR const uint8_t *buf, size_t len, uint32_t offset,
                  uint32_t seed);
FAR const char *fstl_errname(int errcode);
int fstl_errparse(FAR const char *name);

/* Data checks (fstl_io.c) */

int fstl_write(FAR const char *path, off_t size,
               FAR const struct fstl_opts_s *opts);
int fstl_verify(FAR const char *path, off_t size,
                FAR const struct fstl_opts_s *opts);
int fstl_pio(FAR const char *path);
int fstl_trunc(FAR const char *path);
int fstl_append(FAR const char *path);
int fstl_sync(FAR const char *path);
int fstl_conc(FAR const char *dir, int nthreads, off_t size);

/* Name space checks (fstl_dir.c) */

int fstl_ls(FAR const char *dir, FAR const struct fstl_opts_s *opts);
int fstl_stat(FAR const char *path);
int fstl_statfs(FAR const char *path);
int fstl_chmod(FAR const char *path, FAR const char *mode);
int fstl_utime(FAR const char *path, FAR const char *sec);
int fstl_deep(FAR const char *dir, int depth);
int fstl_err(int argc, FAR char *argv[]);
int fstl_path(FAR const char *path);
int fstl_mount(FAR const char *fstype, FAR const char *target,
               FAR const char *data, FAR const char *expect);
int fstl_umount(FAR const char *target, FAR const char *expect);
int fstl_exec(int argc, FAR char *argv[]);

#endif /* __APPS_FSTL_FSTL_H */
