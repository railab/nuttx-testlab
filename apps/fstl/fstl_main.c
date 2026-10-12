/****************************************************************************
 * apps/fstl/fstl_main.c
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

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fstl.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define FSTL_MAXARGS 8

/* Pattern key of a seed: spreads the seed over all four bytes */

#define FSTL_KEY(seed) ((uint32_t)(seed) * 0x9e3779b9u)

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct fstl_errno_s
{
  int              code;
  FAR const char  *name;
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

static const struct fstl_errno_s g_fstl_errnos[] =
{
  { 0,            "OK" },
  { EPERM,        "EPERM" },
  { ENOENT,       "ENOENT" },
  { EIO,          "EIO" },
  { EBADF,        "EBADF" },
  { ENOMEM,       "ENOMEM" },
  { EACCES,       "EACCES" },
  { EBUSY,        "EBUSY" },
  { EEXIST,       "EEXIST" },
  { EXDEV,        "EXDEV" },
  { ENODEV,       "ENODEV" },
  { ENOTDIR,      "ENOTDIR" },
  { EISDIR,       "EISDIR" },
  { EINVAL,       "EINVAL" },
  { ENOTTY,       "ENOTTY" },
  { ENOSPC,       "ENOSPC" },
  { ESPIPE,       "ESPIPE" },
  { EROFS,        "EROFS" },
  { ENAMETOOLONG, "ENAMETOOLONG" },
  { ENOSYS,       "ENOSYS" },
  { ENOTEMPTY,    "ENOTEMPTY" },
  { ELOOP,        "ELOOP" },
  { ENOEXEC,      "ENOEXEC" },
  { EOPNOTSUPP,   "EOPNOTSUPP" },
  { EREMOTEIO,    "EREMOTEIO" },
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void fstl_usage(FAR const char *progname)
{
  fprintf(stderr,
          "Usage: %s <check> [options] args...\n"
          "  write  <path> <size>       write the pattern (-s -b -a)\n"
          "  verify <path> <size>       read the pattern back (-s -b)\n"
          "  pio    <path>              pread/pwrite/lseek\n"
          "  trunc  <path>              ftruncate/truncate/O_TRUNC\n"
          "  append <path>              O_APPEND\n"
          "  sync   <path>              fsync\n"
          "  conc   <dir> <n> <size>    n threads write own files\n"
          "  ls     <dir>               readdir end, d_ino (-n -q)\n"
          "  stat   <path>              print stat, stable st_ino\n"
          "  statfs <path>              print statfs\n"
          "  chmod  <path> <octal>\n"
          "  utime  <path> <sec>        set atime and mtime\n"
          "  deep   <dir> <depth>       nested directories\n"
          "  err    <op> <path> [path2] <ERRNO[|ERRNO]|OK>\n"
          "         op: open wronly creat excl opendir mkdir rmdir\n"
          "         unlink stat rename symlink readlink seekneg\n"
          "  path   <path>              F_GETPATH\n"
          "  mount  <fstype> <target> <data> <ERRNO|OK>\n"
          "  umount <target> <ERRNO|OK>\n"
          "  exec   <path|name> <status> [args...]\n"
          "  -s seed  pattern seed (default 0)\n"
          "  -b size  I/O buffer size (default %d)\n"
          "  -a       open with O_APPEND\n"
          "  -n count expected number of entries\n"
          "  -q       quiet\n",
          progname, CONFIG_TESTLAB_FSTL_BUFSIZE);
}

/* Split options from positional arguments.  Options may appear anywhere
 * after the check name.
 */

static int fstl_parse(int argc, FAR char *argv[],
                      FAR struct fstl_opts_s *opts,
                      FAR char *args[], FAR int *nargs)
{
  int i;

  memset(opts, 0, sizeof(*opts));
  opts->bufsize = CONFIG_TESTLAB_FSTL_BUFSIZE;
  opts->count   = -1;
  *nargs        = 0;

  for (i = 2; i < argc; i++)
    {
      if (strcmp(argv[i], "-a") == 0)
        {
          opts->append = true;
        }
      else if (strcmp(argv[i], "-q") == 0)
        {
          opts->quiet = true;
        }
      else if ((strcmp(argv[i], "-s") == 0 ||
                strcmp(argv[i], "-b") == 0 ||
                strcmp(argv[i], "-n") == 0) && i + 1 < argc)
        {
          long value = strtol(argv[i + 1], NULL, 0);

          switch (argv[i][1])
            {
              case 's':
                opts->seed = (uint32_t)value;
                break;

              case 'b':
                if (value <= 0)
                  {
                    return -EINVAL;
                  }

                opts->bufsize = (size_t)value;
                break;

              default:
                opts->count = value;
                break;
            }

          i++;
        }
      else if (*nargs < FSTL_MAXARGS)
        {
          args[(*nargs)++] = argv[i];
        }
      else
        {
          return -E2BIG;
        }
    }

  return OK;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/* The pattern: the 4-byte word at offset w (w a multiple of 4) holds
 * w ^ FSTL_KEY(seed), little-endian.  Every word of a file below 4 GiB is
 * different, so a misplaced or repeated block is always detected.
 */

void fstl_fill(FAR uint8_t *buf, size_t len, uint32_t offset,
               uint32_t seed)
{
  uint32_t key = FSTL_KEY(seed);
  size_t i;

  for (i = 0; i < len; i++, offset++)
    {
      buf[i] = (uint8_t)(((offset & ~3u) ^ key) >> ((offset & 3u) * 8));
    }
}

/* Return the index of the first byte that differs from the pattern, or
 * len if all match.
 */

size_t fstl_check(FAR const uint8_t *buf, size_t len, uint32_t offset,
                  uint32_t seed)
{
  uint32_t key = FSTL_KEY(seed);
  size_t i;

  for (i = 0; i < len; i++, offset++)
    {
      if (buf[i] !=
          (uint8_t)(((offset & ~3u) ^ key) >> ((offset & 3u) * 8)))
        {
          break;
        }
    }

  return i;
}

FAR const char *fstl_errname(int errcode)
{
  static char unknown[16];
  size_t i;

  for (i = 0; i < sizeof(g_fstl_errnos) / sizeof(g_fstl_errnos[0]); i++)
    {
      if (g_fstl_errnos[i].code == errcode)
        {
          return g_fstl_errnos[i].name;
        }
    }

  snprintf(unknown, sizeof(unknown), "%d", errcode);
  return unknown;
}

/* Return the errno of a name ("OK" is 0), or -1 if it is unknown */

int fstl_errparse(FAR const char *name)
{
  size_t i;

  for (i = 0; i < sizeof(g_fstl_errnos) / sizeof(g_fstl_errnos[0]); i++)
    {
      if (strcmp(g_fstl_errnos[i].name, name) == 0)
        {
          return g_fstl_errnos[i].code;
        }
    }

  return -1;
}

int main(int argc, FAR char *argv[])
{
  FAR char *args[FSTL_MAXARGS];
  struct fstl_opts_s opts;
  FAR const char *check;
  int nargs;
  int ret;

  if (argc < 2)
    {
      fstl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  check = argv[1];

  /* These pass their arguments through unparsed */

  if (strcmp(check, "exec") == 0)
    {
      ret = fstl_exec(argc - 2, &argv[2]);
      return ret < 0 ? EXIT_FAILURE : EXIT_SUCCESS;
    }

  if (strcmp(check, "err") == 0)
    {
      ret = fstl_err(argc - 2, &argv[2]);
      return ret < 0 ? EXIT_FAILURE : EXIT_SUCCESS;
    }

  if (fstl_parse(argc, argv, &opts, args, &nargs) < 0)
    {
      fstl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  ret = -EINVAL;

  if (strcmp(check, "write") == 0 && nargs == 2)
    {
      ret = fstl_write(args[0], (off_t)strtoll(args[1], NULL, 0), &opts);
    }
  else if (strcmp(check, "verify") == 0 && nargs == 2)
    {
      ret = fstl_verify(args[0], (off_t)strtoll(args[1], NULL, 0), &opts);
    }
  else if (strcmp(check, "pio") == 0 && nargs == 1)
    {
      ret = fstl_pio(args[0]);
    }
  else if (strcmp(check, "trunc") == 0 && nargs == 1)
    {
      ret = fstl_trunc(args[0]);
    }
  else if (strcmp(check, "append") == 0 && nargs == 1)
    {
      ret = fstl_append(args[0]);
    }
  else if (strcmp(check, "sync") == 0 && nargs == 1)
    {
      ret = fstl_sync(args[0]);
    }
  else if (strcmp(check, "conc") == 0 && nargs == 3)
    {
      ret = fstl_conc(args[0], atoi(args[1]),
                      (off_t)strtoll(args[2], NULL, 0));
    }
  else if (strcmp(check, "ls") == 0 && nargs == 1)
    {
      ret = fstl_ls(args[0], &opts);
    }
  else if (strcmp(check, "stat") == 0 && nargs == 1)
    {
      ret = fstl_stat(args[0]);
    }
  else if (strcmp(check, "statfs") == 0 && nargs == 1)
    {
      ret = fstl_statfs(args[0]);
    }
  else if (strcmp(check, "chmod") == 0 && nargs == 2)
    {
      ret = fstl_chmod(args[0], args[1]);
    }
  else if (strcmp(check, "utime") == 0 && nargs == 2)
    {
      ret = fstl_utime(args[0], args[1]);
    }
  else if (strcmp(check, "deep") == 0 && nargs == 2)
    {
      ret = fstl_deep(args[0], atoi(args[1]));
    }
  else if (strcmp(check, "path") == 0 && nargs == 1)
    {
      ret = fstl_path(args[0]);
    }
  else if (strcmp(check, "mount") == 0 && nargs == 4)
    {
      ret = fstl_mount(args[0], args[1], args[2], args[3]);
    }
  else if (strcmp(check, "umount") == 0 && nargs == 2)
    {
      ret = fstl_umount(args[0], args[1]);
    }
  else
    {
      fstl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  return ret < 0 ? EXIT_FAILURE : EXIT_SUCCESS;
}
