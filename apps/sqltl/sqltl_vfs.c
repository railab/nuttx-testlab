/****************************************************************************
 * apps/sqltl/sqltl_vfs.c
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

/* The default VFS with a hook at the commit point of a transaction: the
 * deletion (journal_mode DELETE) or truncation to zero (TRUNCATE) of the
 * rollback journal, or the sync of the WAL file after a commit frame.
 * Once armed, the hook stops the task just before or just after the
 * commit point and waits to be powered off.
 *
 * The SQLite VFS objects (sqlite3_vfs, sqlite3_io_methods, sqlite3_file)
 * are copied to and from structures of this file with the same layout,
 * which SQLite keeps stable for each version (3 here).
 */

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>

#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "sqltl.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

/* WAL file layout: a 32 byte header, then frames of a 24 byte header and
 * a page.  The frame header of a commit frame holds the database size in
 * pages (big-endian) at offset 4, the other frames hold zero.
 */

#define SQLTL_WAL_HDR   32
#define SQLTL_FRAME_HDR 24

/* The default VFS gives files without locks (journals, WAL) other methods
 * than the database file.
 */

#define SQLTL_NMETHODS  4

/* Compile time check of a layout copy */

#define SQLTL_SAME_SIZE(a, b) \
  typedef char sqltl_size_##a[sizeof(struct a) == sizeof(b) ? 1 : -1]

/****************************************************************************
 * Private Types
 ****************************************************************************/

typedef CODE void (*sqltl_fn_t)(void);

/* sqlite3_io_methods, version 3 */

struct sqltl_io_s
{
  int version;
  CODE int (*close)(FAR sqlite3_file *file);
  CODE int (*read)(FAR sqlite3_file *file, FAR void *buf, int amt,
                   sqlite3_int64 off);
  CODE int (*write)(FAR sqlite3_file *file, FAR const void *buf, int amt,
                    sqlite3_int64 off);
  CODE int (*truncate)(FAR sqlite3_file *file, sqlite3_int64 size);
  CODE int (*sync)(FAR sqlite3_file *file, int flags);
  sqltl_fn_t rest[13];
};

/* sqlite3_vfs, version 3 */

struct sqltl_vfs_s
{
  int version;
  int osfilesize;
  int maxpathname;
  FAR sqlite3_vfs *next;
  FAR const char *name;
  FAR void *appdata;
  CODE int (*open)(FAR sqlite3_vfs *vfs, sqlite3_filename name,
                   FAR sqlite3_file *file, int flags, FAR int *outflags);
  CODE int (*delete)(FAR sqlite3_vfs *vfs, FAR const char *name,
                     int syncdir);
  sqltl_fn_t rest[14];
};

SQLTL_SAME_SIZE(sqltl_io_s, sqlite3_io_methods);
SQLTL_SAME_SIZE(sqltl_vfs_s, sqlite3_vfs);

/* What is stored behind the file object of the default VFS */

struct sqltl_vfile_s
{
  FAR const struct sqltl_io_s *orig;   /* Methods of the default VFS */
  int                          flags;  /* Open flags */
  bool                         commit; /* WAL: commit frame not synced */
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

static sqlite3_vfs g_sqltl_vfs;
static struct sqltl_vfs_s g_sqltl_real;
static FAR sqlite3_vfs *g_sqltl_realvfs;

static sqlite3_io_methods g_sqltl_io[SQLTL_NMETHODS];
static struct sqltl_io_s g_sqltl_orig[SQLTL_NMETHODS];
static FAR const void *g_sqltl_origptr[SQLTL_NMETHODS];

static pthread_mutex_t g_sqltl_lock = PTHREAD_MUTEX_INITIALIZER;
static volatile int g_sqltl_point;

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static FAR struct sqltl_vfile_s *sqltl_vfile(FAR sqlite3_file *file)
{
  return (FAR struct sqltl_vfile_s *)
         ((FAR char *)file + g_sqltl_real.osfilesize);
}

/* Stop here if the hook is armed for this side of the commit point */

static void sqltl_point(int when)
{
  if (g_sqltl_point == when)
    {
      printf("sqltl: wait point=%s\n",
             when == SQLTL_POINT_BEFORE ? "before" : "after");
      fflush(stdout);
      for (; ; )
        {
          sleep(1);
        }
    }
}

static bool sqltl_journal(FAR const char *name)
{
  size_t len = name != NULL ? strlen(name) : 0;

  return len > 8 && strcmp(name + len - 8, "-journal") == 0;
}

static int sqltl_vfs_write(FAR sqlite3_file *file, FAR const void *buf,
                           int amt, sqlite3_int64 off)
{
  FAR struct sqltl_vfile_s *v = sqltl_vfile(file);
  FAR const uint8_t *hdr = buf;

  if ((v->flags & SQLITE_OPEN_WAL) != 0 && amt == SQLTL_FRAME_HDR &&
      off >= SQLTL_WAL_HDR && (hdr[4] | hdr[5] | hdr[6] | hdr[7]) != 0)
    {
      v->commit = true;
    }

  return v->orig->write(file, buf, amt, off);
}

static int sqltl_vfs_truncate(FAR sqlite3_file *file, sqlite3_int64 size)
{
  FAR struct sqltl_vfile_s *v = sqltl_vfile(file);
  bool point = size == 0 && (v->flags & SQLITE_OPEN_MAIN_JOURNAL) != 0;
  int rc;

  if (point)
    {
      sqltl_point(SQLTL_POINT_BEFORE);
    }

  rc = v->orig->truncate(file, size);
  if (point && rc == SQLITE_OK)
    {
      sqltl_point(SQLTL_POINT_AFTER);
    }

  return rc;
}

static int sqltl_vfs_sync(FAR sqlite3_file *file, int flags)
{
  FAR struct sqltl_vfile_s *v = sqltl_vfile(file);
  bool point = v->commit;
  int rc;

  v->commit = false;
  if (point)
    {
      sqltl_point(SQLTL_POINT_BEFORE);
    }

  rc = v->orig->sync(file, flags);
  if (point && rc == SQLITE_OK)
    {
      sqltl_point(SQLTL_POINT_AFTER);
    }

  return rc;
}

/* Give an opened file a copy of its methods with the hooks */

static void sqltl_vfs_hook(FAR sqlite3_file *file, int flags)
{
  FAR struct sqltl_vfile_s *v = sqltl_vfile(file);
  FAR const void *methods;
  FAR const void *hooked;
  struct sqltl_io_s io;
  int i;

  memcpy(&methods, file, sizeof(methods));

  pthread_mutex_lock(&g_sqltl_lock);
  for (i = 0; i < SQLTL_NMETHODS; i++)
    {
      if (g_sqltl_origptr[i] == NULL)
        {
          g_sqltl_origptr[i] = methods;
          memcpy(&g_sqltl_orig[i], methods, sizeof(g_sqltl_orig[i]));
          memcpy(&io, methods, sizeof(io));
          io.write    = sqltl_vfs_write;
          io.truncate = sqltl_vfs_truncate;
          io.sync     = sqltl_vfs_sync;
          memcpy(&g_sqltl_io[i], &io, sizeof(io));
        }

      if (g_sqltl_origptr[i] == methods)
        {
          v->orig   = &g_sqltl_orig[i];
          v->flags  = flags;
          v->commit = false;
          hooked    = &g_sqltl_io[i];
          memcpy(file, &hooked, sizeof(hooked));
          break;
        }
    }

  /* More method tables than expected: no hooks for this file */

  pthread_mutex_unlock(&g_sqltl_lock);
}

static int sqltl_vfs_open(FAR sqlite3_vfs *vfs, sqlite3_filename name,
                          FAR sqlite3_file *file, int flags,
                          FAR int *outflags)
{
  FAR const void *methods;
  int rc;

  rc = g_sqltl_real.open(g_sqltl_realvfs, name, file, flags, outflags);
  memcpy(&methods, file, sizeof(methods));
  if (rc == SQLITE_OK && methods != NULL)
    {
      sqltl_vfs_hook(file, flags);
    }

  return rc;
}

static int sqltl_vfs_delete(FAR sqlite3_vfs *vfs, FAR const char *name,
                            int syncdir)
{
  bool point = sqltl_journal(name);
  int rc;

  if (point)
    {
      sqltl_point(SQLTL_POINT_BEFORE);
    }

  rc = g_sqltl_real.delete(g_sqltl_realvfs, name, syncdir);
  if (point && rc == SQLITE_OK)
    {
      sqltl_point(SQLTL_POINT_AFTER);
    }

  return rc;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/* Make the hooked VFS the default one */

int sqltl_vfs_register(void)
{
  FAR sqlite3_vfs *vfs = sqlite3_vfs_find(NULL);
  struct sqltl_vfs_s hooked;

  if (vfs == NULL)
    {
      return SQLITE_ERROR;
    }

  if (vfs == &g_sqltl_vfs)
    {
      return SQLITE_OK;
    }

  g_sqltl_realvfs = vfs;
  memcpy(&g_sqltl_real, vfs, sizeof(g_sqltl_real));

  memcpy(&hooked, vfs, sizeof(hooked));
  hooked.name       = "sqltl";
  hooked.next       = NULL;
  hooked.osfilesize = g_sqltl_real.osfilesize +
                      sizeof(struct sqltl_vfile_s);
  hooked.open       = sqltl_vfs_open;
  hooked.delete     = sqltl_vfs_delete;
  memcpy(&g_sqltl_vfs, &hooked, sizeof(hooked));

  return sqlite3_vfs_register(&g_sqltl_vfs, 1);
}

/* Arm the hook: the next commit point stops the task */

void sqltl_vfs_arm(int point)
{
  g_sqltl_point = point;
}
