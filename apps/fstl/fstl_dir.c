/****************************************************************************
 * apps/fstl/fstl_dir.c
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

#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/wait.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "fstl.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define FSTL_LS_MAX    1024
#define FSTL_DEEP_MAX  64
#define FSTL_DEEP_DATA 1000

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct fstl_ent_s
{
  ino_t ino;
  char  name[NAME_MAX + 1];
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/* Check that the result of an operation matches the expected errno list
 * ("ENOENT", "EISDIR|EPERM" or "OK").  Prints the verdict.
 */

static int fstl_expect(FAR const char *check, FAR const char *op,
                       int errcode, FAR const char *expect)
{
  char list[64];
  FAR char *save;
  FAR char *name;

  strlcpy(list, expect, sizeof(list));
  for (name = strtok_r(list, "|", &save); name != NULL;
       name = strtok_r(NULL, "|", &save))
    {
      if (fstl_errparse(name) == errcode)
        {
          printf("fstl: PASS %s %s errno=%s\n", check, op,
                 fstl_errname(errcode));
          return OK;
        }
    }

  printf("fstl: FAIL %s %s errno=%s want=%s\n", check, op,
         fstl_errname(errcode), expect);
  return -1;
}

static bool fstl_type_ok(unsigned char type, mode_t mode)
{
  switch (type)
    {
      case DT_DIR:
        return S_ISDIR(mode);

      case DT_REG:
        return S_ISREG(mode);

      case DT_LNK:
        return S_ISLNK(mode);

      default:
        return true;
    }
}

/* One pass over a directory.  Returns the number of entries, or -1 after
 * printing a FAIL verdict.
 */

static int fstl_ls_pass(FAR DIR *dirp, FAR const char *dir,
                        FAR struct fstl_ent_s *ents, bool print)
{
  FAR struct dirent *de;
  char path[PATH_MAX];
  struct stat st;
  int count = 0;
  int i;

  for (; ; )
    {
      errno = 0;
      de = readdir(dirp);
      if (de == NULL)
        {
          break;
        }

      if (strcmp(de->d_name, ".") == 0 || strcmp(de->d_name, "..") == 0)
        {
          continue;
        }

      snprintf(path, sizeof(path), "%s/%s", dir, de->d_name);
      if (lstat(path, &st) < 0)
        {
          printf("fstl: FAIL ls stat %s errno=%s\n", de->d_name,
                 fstl_errname(errno));
          return -1;
        }

      if (print)
        {
          printf("fstl: ent %s d_ino=%lu st_ino=%lu type=%u size=%lld\n",
                 de->d_name, (unsigned long)de->d_ino,
                 (unsigned long)st.st_ino, de->d_type,
                 (long long)st.st_size);
        }

      if (de->d_ino != st.st_ino || de->d_ino == 0)
        {
          printf("fstl: FAIL ls ino %s d_ino=%lu st_ino=%lu\n",
                 de->d_name, (unsigned long)de->d_ino,
                 (unsigned long)st.st_ino);
          return -1;
        }

      if (!fstl_type_ok(de->d_type, st.st_mode))
        {
          printf("fstl: FAIL ls type %s d_type=%u mode=%o\n",
                 de->d_name, de->d_type, (unsigned int)st.st_mode);
          return -1;
        }

      for (i = 0; i < count; i++)
        {
          if (strcmp(ents[i].name, de->d_name) == 0 ||
              ents[i].ino == de->d_ino)
            {
              printf("fstl: FAIL ls dup %s %s ino=%lu\n", ents[i].name,
                     de->d_name, (unsigned long)de->d_ino);
              return -1;
            }
        }

      if (count >= FSTL_LS_MAX)
        {
          printf("fstl: FAIL ls more than %d entries\n", FSTL_LS_MAX);
          return -1;
        }

      ents[count].ino = de->d_ino;
      strlcpy(ents[count].name, de->d_name, sizeof(ents[count].name));
      count++;
    }

  /* The end of the directory is a NULL return with errno unchanged */

  if (errno != 0)
    {
      printf("fstl: FAIL ls end n=%d errno=%s\n", count,
             fstl_errname(errno));
      return -1;
    }

  /* Reading again at the end still returns NULL without an error */

  errno = 0;
  if (readdir(dirp) != NULL || errno != 0)
    {
      printf("fstl: FAIL ls past end errno=%s\n", fstl_errname(errno));
      return -1;
    }

  return count;
}

static int fstl_mkdeep(FAR char *path, size_t size, FAR const char *dir,
                       int depth)
{
  int level;

  strlcpy(path, dir, size);
  if (mkdir(path, 0755) < 0 && errno != EEXIST)
    {
      return 1;
    }

  for (level = 1; level <= depth; level++)
    {
      strlcat(path, "/d", size);
      if (mkdir(path, 0755) < 0)
        {
          return level;
        }
    }

  return 0;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/* readdir: entries match lstat (d_ino == st_ino != 0, d_type), no
 * duplicate name or inode, the end is reported as the end, rewinddir
 * gives the same entries.
 */

int fstl_ls(FAR const char *dir, FAR const struct fstl_opts_s *opts)
{
  FAR struct fstl_ent_s *ents;
  FAR DIR *dirp;
  int first;
  int second;

  ents = malloc(sizeof(struct fstl_ent_s) * FSTL_LS_MAX);
  if (ents == NULL)
    {
      printf("fstl: FAIL ls malloc\n");
      return -1;
    }

  dirp = opendir(dir);
  if (dirp == NULL)
    {
      printf("fstl: FAIL ls opendir errno=%s\n", fstl_errname(errno));
      free(ents);
      return -1;
    }

  first = fstl_ls_pass(dirp, dir, ents, !opts->quiet);
  if (first >= 0)
    {
      rewinddir(dirp);
      second = fstl_ls_pass(dirp, dir, ents, false);
      if (second >= 0 && second != first)
        {
          printf("fstl: FAIL ls rewind n=%d then n=%d\n", first, second);
          first = -1;
        }
      else if (second < 0)
        {
          first = -1;
        }
    }

  closedir(dirp);
  free(ents);

  if (first < 0)
    {
      return -1;
    }

  if (opts->count >= 0 && first != opts->count)
    {
      printf("fstl: FAIL ls n=%d want=%ld\n", first, opts->count);
      return -1;
    }

  printf("fstl: PASS ls n=%d\n", first);
  return OK;
}

/* stat fields; stat twice and fstat agree on st_ino */

int fstl_stat(FAR const char *path)
{
  struct stat st1;
  struct stat st2;
  struct stat st3;
  int fd;

  if (stat(path, &st1) < 0)
    {
      printf("fstl: FAIL stat errno=%s\n", fstl_errname(errno));
      return -1;
    }

  if (stat(path, &st2) < 0)
    {
      printf("fstl: FAIL stat again errno=%s\n", fstl_errname(errno));
      return -1;
    }

  st3 = st2;
  fd = open(path, O_RDONLY);
  if (fd >= 0)
    {
      if (fstat(fd, &st3) < 0)
        {
          printf("fstl: FAIL stat fstat errno=%s\n", fstl_errname(errno));
          close(fd);
          return -1;
        }

      close(fd);
    }

  if (st1.st_ino != st2.st_ino || st1.st_ino != st3.st_ino)
    {
      printf("fstl: FAIL stat ino %lu %lu %lu\n",
             (unsigned long)st1.st_ino, (unsigned long)st2.st_ino,
             (unsigned long)st3.st_ino);
      return -1;
    }

  printf("fstl: PASS stat ino=%lu mode=%o size=%lld nlink=%u uid=%u "
         "gid=%u mtime=%lld atime=%lld blksize=%ld blocks=%lld\n",
         (unsigned long)st1.st_ino, (unsigned int)st1.st_mode,
         (long long)st1.st_size, (unsigned int)st1.st_nlink,
         (unsigned int)st1.st_uid, (unsigned int)st1.st_gid,
         (long long)st1.st_mtime, (long long)st1.st_atime,
         (long)st1.st_blksize, (long long)st1.st_blocks);
  return OK;
}

int fstl_statfs(FAR const char *path)
{
  struct statfs sf;

  if (statfs(path, &sf) < 0)
    {
      printf("fstl: FAIL statfs errno=%s\n", fstl_errname(errno));
      return -1;
    }

  printf("fstl: PASS statfs type=0x%lx bsize=%ld blocks=%llu bfree=%llu "
         "bavail=%llu files=%llu ffree=%llu namelen=%ld\n",
         (unsigned long)sf.f_type, (long)sf.f_bsize,
         (unsigned long long)sf.f_blocks, (unsigned long long)sf.f_bfree,
         (unsigned long long)sf.f_bavail, (unsigned long long)sf.f_files,
         (unsigned long long)sf.f_ffree, (long)sf.f_namelen);
  return OK;
}

/* chmod, then stat must report the new permission bits */

int fstl_chmod(FAR const char *path, FAR const char *mode)
{
  mode_t want = (mode_t)strtoul(mode, NULL, 8);
  struct stat st;

  if (chmod(path, want) < 0)
    {
      printf("fstl: FAIL chmod errno=%s\n", fstl_errname(errno));
      return -1;
    }

  if (stat(path, &st) < 0)
    {
      printf("fstl: FAIL chmod stat errno=%s\n", fstl_errname(errno));
      return -1;
    }

  if ((st.st_mode & 07777) != want)
    {
      printf("fstl: FAIL chmod mode=%o want=%o\n",
             (unsigned int)(st.st_mode & 07777), (unsigned int)want);
      return -1;
    }

  printf("fstl: PASS chmod mode=%o\n", (unsigned int)want);
  return OK;
}

/* utimens to <sec>, then stat must report it */

int fstl_utime(FAR const char *path, FAR const char *sec)
{
  struct timespec times[2];
  struct stat st;

  times[0].tv_sec  = (time_t)strtoll(sec, NULL, 0);
  times[0].tv_nsec = 0;
  times[1]         = times[0];

  if (utimens(path, times) < 0)
    {
      printf("fstl: FAIL utime errno=%s\n", fstl_errname(errno));
      return -1;
    }

  if (stat(path, &st) < 0 || st.st_mtime != times[1].tv_sec ||
      st.st_atime != times[0].tv_sec)
    {
      printf("fstl: FAIL utime mtime=%lld atime=%lld\n",
             (long long)st.st_mtime, (long long)st.st_atime);
      return -1;
    }

  printf("fstl: PASS utime mtime=%lld\n", (long long)st.st_mtime);
  return OK;
}

/* <dir>/d/d/.../d (depth levels below <dir>) with a pattern file at the
 * bottom: create, write, read back.
 */

int fstl_deep(FAR const char *dir, int depth)
{
  uint8_t buf[FSTL_DEEP_DATA];
  FAR char *path;
  int level;
  int fd;

  if (depth < 1 || depth > FSTL_DEEP_MAX)
    {
      printf("fstl: FAIL deep depth=%d\n", depth);
      return -1;
    }

  path = malloc(PATH_MAX);
  if (path == NULL)
    {
      printf("fstl: FAIL deep malloc\n");
      return -1;
    }

  level = fstl_mkdeep(path, PATH_MAX, dir, depth);
  if (level > 0)
    {
      printf("fstl: FAIL deep mkdir level=%d errno=%s\n", level,
             fstl_errname(errno));
      goto fail;
    }

  strlcat(path, "/f", PATH_MAX);
  fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
  if (fd < 0)
    {
      printf("fstl: FAIL deep open errno=%s\n", fstl_errname(errno));
      goto fail;
    }

  fstl_fill(buf, sizeof(buf), 0, depth);
  if (write(fd, buf, sizeof(buf)) != sizeof(buf) ||
      pread(fd, buf, sizeof(buf), 0) != sizeof(buf) ||
      fstl_check(buf, sizeof(buf), 0, depth) != sizeof(buf))
    {
      printf("fstl: FAIL deep io errno=%s\n", fstl_errname(errno));
      close(fd);
      goto fail;
    }

  close(fd);
  free(path);
  printf("fstl: PASS deep depth=%d\n", depth);
  return OK;

fail:
  free(path);
  return -1;
}

/* err <op> <path> [path2] <expect>: run one call and check its errno */

int fstl_err(int argc, FAR char *argv[])
{
  FAR const char *op;
  FAR const char *path;
  FAR const char *path2 = NULL;
  FAR const char *expect;
  struct stat st;
#ifdef CONFIG_FS_LINKS
  char buf[PATH_MAX];
#endif
  FAR DIR *dirp;
  int errcode = 0;
  int ret = 0;
  int fd = -1;

  if (argc != 3 && argc != 4)
    {
      printf("fstl: FAIL err usage\n");
      return -1;
    }

  op     = argv[0];
  path   = argv[1];
  expect = argv[argc - 1];
  if (argc == 4)
    {
      path2 = argv[2];
    }

  if (strcmp(op, "open") == 0)
    {
      ret = fd = open(path, O_RDONLY);
    }
  else if (strcmp(op, "wronly") == 0)
    {
      ret = fd = open(path, O_WRONLY);
    }
  else if (strcmp(op, "creat") == 0)
    {
      ret = fd = open(path, O_WRONLY | O_CREAT, 0644);
    }
  else if (strcmp(op, "excl") == 0)
    {
      ret = fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0644);
    }
  else if (strcmp(op, "opendir") == 0)
    {
      dirp = opendir(path);
      ret = dirp == NULL ? -1 : 0;
      if (dirp != NULL)
        {
          closedir(dirp);
        }
    }
  else if (strcmp(op, "mkdir") == 0)
    {
      ret = mkdir(path, 0755);
    }
  else if (strcmp(op, "rmdir") == 0)
    {
      ret = rmdir(path);
    }
  else if (strcmp(op, "unlink") == 0)
    {
      ret = unlink(path);
    }
  else if (strcmp(op, "stat") == 0)
    {
      ret = stat(path, &st);
    }
  else if (strcmp(op, "rename") == 0 && path2 != NULL)
    {
      ret = rename(path, path2);
    }
#ifdef CONFIG_FS_LINKS
  else if (strcmp(op, "symlink") == 0 && path2 != NULL)
    {
      ret = symlink(path, path2);
    }
  else if (strcmp(op, "readlink") == 0)
    {
      ret = readlink(path, buf, sizeof(buf)) < 0 ? -1 : 0;
    }
#endif
  else if (strcmp(op, "seekneg") == 0)
    {
      fd = open(path, O_RDONLY);
      ret = fd < 0 ? -1 : lseek(fd, -1, SEEK_SET) < 0 ? -1 : 0;
    }
  else
    {
      printf("fstl: FAIL err op %s\n", op);
      return -1;
    }

  if (ret < 0)
    {
      errcode = errno;
    }

  if (fd >= 0)
    {
      close(fd);
    }

  return fstl_expect("err", op, errcode, expect);
}

/* F_GETPATH of an open file returns the path it was opened by */

int fstl_path(FAR const char *path)
{
  char buf[PATH_MAX];
  int ret;
  int fd;

  fd = open(path, O_RDONLY);
  if (fd < 0)
    {
      printf("fstl: FAIL path open errno=%s\n", fstl_errname(errno));
      return -1;
    }

  memset(buf, 0, sizeof(buf));
  ret = fcntl(fd, F_GETPATH, buf);
  close(fd);
  if (ret < 0)
    {
      printf("fstl: FAIL path fcntl errno=%s\n", fstl_errname(errno));
      return -1;
    }

  if (strcmp(buf, path) != 0)
    {
      printf("fstl: FAIL path got=%s\n", buf);
      return -1;
    }

  printf("fstl: PASS path %s\n", buf);
  return OK;
}

int fstl_mount(FAR const char *fstype, FAR const char *target,
               FAR const char *data, FAR const char *expect)
{
  int ret = mount(NULL, target, fstype, 0, data);

  return fstl_expect("mount", fstype, ret < 0 ? errno : 0, expect);
}

int fstl_umount(FAR const char *target, FAR const char *expect)
{
  int ret = umount(target);

  return fstl_expect("umount", target, ret < 0 ? errno : 0, expect);
}

/* exec <path|name> <status> [args...]: posix_spawn (posix_spawnp for a
 * name without '/'), wait, check the exit status.
 */

int fstl_exec(int argc, FAR char *argv[])
{
  FAR char *const *args;
  pid_t pid;
  int status;
  int want;
  int ret;

  if (argc < 2)
    {
      printf("fstl: FAIL exec usage\n");
      return -1;
    }

  want = atoi(argv[1]);

  /* argv[1] is replaced by argv[0], so the child gets argv[0] = path */

  argv[1] = argv[0];
  args    = &argv[1];

  if (strchr(argv[0], '/') != NULL)
    {
      ret = posix_spawn(&pid, argv[0], NULL, NULL, args, NULL);
    }
  else
    {
      ret = posix_spawnp(&pid, argv[0], NULL, NULL, args, NULL);
    }

  if (ret != 0)
    {
      printf("fstl: FAIL exec spawn errno=%s\n", fstl_errname(ret));
      return -1;
    }

  if (waitpid(pid, &status, 0) != pid)
    {
      printf("fstl: FAIL exec waitpid errno=%s\n", fstl_errname(errno));
      return -1;
    }

  if (!WIFEXITED(status) || WEXITSTATUS(status) != want)
    {
      printf("fstl: FAIL exec status=0x%x want=%d\n", status, want);
      return -1;
    }

  printf("fstl: PASS exec status=%d\n", want);
  return OK;
}
