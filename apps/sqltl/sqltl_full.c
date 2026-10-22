/****************************************************************************
 * apps/sqltl/sqltl_full.c
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
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <unistd.h>

#include "sqltl.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define SQLTL_INSERT    "INSERT INTO t VALUES(?, ?, ?)"

/* full: transactions committed before the disk is filled, and the size of
 * the transaction that must not fit (rows of SQLTL_FULL_LEN bytes).
 */

#define SQLTL_FULL_BASE 10
#define SQLTL_FULL_ROWS 512
#define SQLTL_FULL_LEN  4000

/* full: free space left for the transaction that must not fit, in file
 * system blocks.
 */

#define SQLTL_FULL_STEPS 4

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct sqltl_full_s
{
  FAR sqlite3        *db;
  FAR sqlite3_stmt   *stmt;
  FAR uint8_t        *buf;
  FAR const char     *dir;    /* Directory of the filler files */
  int64_t             last;   /* Rows committed */
  int                 nfill;  /* Filler files f0 .. f<nfill - 1> */
  size_t              bsize;  /* File system block size */
  int                 fill;   /* errno of a full file system, not ENOSPC */
  int                 rc;     /* First result other than SQLITE_FULL */
  int                 sys;    /* Its errno */
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

static const int g_sqltl_slack[SQLTL_FULL_STEPS] =
{
  0, 1, 4, 16
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/* Commit one transaction of rows rows after the last row */

static int sqltl_commit(FAR sqlite3 *db, FAR sqlite3_stmt *stmt,
                        FAR uint8_t *buf, FAR int64_t *last,
                        FAR const struct sqltl_opts_s *opts)
{
  int64_t id;
  int rc;

  rc = sqltl_exec(db, "BEGIN");
  for (id = *last + 1; id <= *last + opts->rows && rc == SQLITE_OK; id++)
    {
      rc = sqltl_insert(stmt, buf, id, sqltl_rowlen(opts, id));
    }

  if (rc == SQLITE_OK)
    {
      rc = sqltl_exec(db, "COMMIT");
    }

  if (rc == SQLITE_OK)
    {
      *last += opts->rows;
    }
  else if (!sqlite3_get_autocommit(db))
    {
      sqltl_exec(db, "ROLLBACK");
    }

  return rc;
}

/* Fill the file system with files of one block until open() or write()
 * fails.  The error must be ENOSPC (a short write also means full).
 */

static void sqltl_fill_disk(FAR struct sqltl_full_s *f)
{
  char path[PATH_MAX];
  ssize_t n;
  int err;
  int fd;

  memset(f->buf, 0xf5, f->bsize);
  for (; ; )
    {
      snprintf(path, sizeof(path), "%s/f%d", f->dir, f->nfill);
      fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
      if (fd < 0)
        {
          err = errno;
          break;
        }

      n   = write(fd, f->buf, f->bsize);
      err = n < 0 ? errno : ENOSPC;
      close(fd);
      f->nfill++;
      if (n != (ssize_t)f->bsize)
        {
          break;
        }
    }

  if (err != ENOSPC && f->fill == 0)
    {
      f->fill = err;
    }
}

/* Remove the last n filler files: n blocks free */

static int sqltl_free_blocks(FAR struct sqltl_full_s *f, int n)
{
  char path[PATH_MAX];

  for (; n > 0 && f->nfill > 0; n--)
    {
      snprintf(path, sizeof(path), "%s/f%d", f->dir, --f->nfill);
      if (unlink(path) < 0)
        {
          return errno;
        }
    }

  return 0;
}

/* With slack blocks free: a transaction larger than that must fail with
 * SQLITE_FULL and leave the committed rows intact.
 */

static int sqltl_full_step(FAR struct sqltl_full_s *f, int slack)
{
  FAR const char *step;
  int64_t nrows = 0;
  int64_t bad = 0;
  int64_t id;
  int rc;

  rc = sqltl_free_blocks(f, slack);
  if (rc != 0)
    {
      printf("sqltl: FAIL full step=unlink slack=%d errno=%d\n", slack, rc);
      return -1;
    }

  step = "begin";
  rc   = sqltl_exec(f->db, "BEGIN");
  for (id = f->last + 1;
       id <= f->last + SQLTL_FULL_ROWS && rc == SQLITE_OK; id++)
    {
      step = "insert";
      rc   = sqltl_insert(f->stmt, f->buf, id, SQLTL_FULL_LEN);
    }

  if (rc == SQLITE_OK)
    {
      step = "commit";
      rc   = sqltl_exec(f->db, "COMMIT");
    }

  if (rc == SQLITE_OK)
    {
      printf("sqltl: FAIL full step=commit slack=%d fits\n", slack);
      return -1;
    }

  /* The error must be SQLITE_FULL; with another one still check that the
   * database is intact.
   */

  if ((rc & 0xff) != SQLITE_FULL && f->rc == SQLITE_OK)
    {
      f->rc  = rc;
      f->sys = sqlite3_system_errno(f->db);
      printf("sqltl: full step=%s slack=%d rc=%d errno=%d\n", step, slack,
             rc, f->sys);
    }

  /* SQLite may have rolled back already; else roll back now */

  if (!sqlite3_get_autocommit(f->db))
    {
      rc = sqltl_exec(f->db, "ROLLBACK");
      if (rc != SQLITE_OK)
        {
          sqltl_fail("full", "rollback", f->db, rc);
          return -1;
        }
    }

  rc = sqltl_check(f->db, &nrows, &bad);
  if (rc != SQLITE_OK || nrows != f->last)
    {
      printf("sqltl: FAIL full step=check slack=%d rc=%d rows=%" PRId64
             " bad=%" PRId64 "\n", slack, rc, nrows, bad);
      return -1;
    }

  /* The rollback may have released space again */

  sqltl_fill_disk(f);
  return 0;
}

/* Open a database of the multi check and return its ident name */

static int sqltl_ident(FAR const char *path, FAR char *name,
                       FAR const struct sqltl_opts_s *opts,
                       FAR sqlite3 **db)
{
  FAR sqlite3_stmt *stmt;
  FAR const unsigned char *text;
  int rc;

  name[0] = '\0';
  rc = sqltl_open(path, opts, db);
  if (rc != SQLITE_OK)
    {
      return rc;
    }

  rc = sqlite3_prepare_v2(*db, "SELECT name FROM ident", -1, &stmt, NULL);
  if (rc != SQLITE_OK)
    {
      return rc;
    }

  rc = sqlite3_step(stmt);
  if (rc == SQLITE_ROW)
    {
      text = sqlite3_column_text(stmt, 0);
      strlcpy(name, text != NULL ? (FAR const char *)text : "", 8);
      rc = SQLITE_OK;
    }
  else if (rc == SQLITE_DONE)
    {
      rc = SQLITE_OK;
    }

  sqlite3_finalize(stmt);
  return rc;
}

/* Insert row 1 into table t of a schema ("main" or an attached one) */

static int sqltl_row1(FAR sqlite3 *db, FAR const char *schema,
                      FAR uint8_t *buf)
{
  FAR sqlite3_stmt *stmt;
  char sql[48];
  int rc;

  snprintf(sql, sizeof(sql), "INSERT INTO %s.t VALUES(?, ?, ?)", schema);
  rc = sqlite3_prepare_v2(db, sql, -1, &stmt, NULL);
  if (rc == SQLITE_OK)
    {
      rc = sqltl_insert(stmt, buf, 1, 100);
      sqlite3_finalize(stmt);
    }

  return rc;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/* full <db> <filler>: commit some rows, fill the file system with files of
 * one block in the directory filler, then for 0, 1, 4 and 16 free blocks
 * run a transaction too large for the free space: it must fail with
 * SQLITE_FULL and leave the database intact.  After the filler files are
 * removed a transaction commits again, also after reopening the database.
 */

int sqltl_full(FAR const char *path, FAR const char *filler,
               FAR const struct sqltl_opts_s *opts)
{
  struct sqltl_full_s f;
  struct statfs fs;
  FAR const char *step;
  int64_t nrows = 0;
  int64_t bad = 0;
  int ret = -1;
  int rc;
  int i;

  memset(&f, 0, sizeof(f));
  f.dir = filler;
  f.buf = malloc(SQLTL_MAXLEN);
  if (f.buf == NULL)
    {
      printf("sqltl: FAIL full step=malloc\n");
      return -1;
    }

  if (mkdir(filler, 0755) < 0 || statfs(filler, &fs) < 0 ||
      fs.f_bsize <= 0 || fs.f_bsize > SQLTL_MAXLEN)
    {
      printf("sqltl: FAIL full step=filler errno=%d\n", errno);
      free(f.buf);
      return -1;
    }

  f.bsize = fs.f_bsize;

  step = "open";
  rc   = sqltl_open(path, opts, &f.db);
  if (rc == SQLITE_OK)
    {
      step = "prepare";
      rc   = sqlite3_prepare_v2(f.db, SQLTL_INSERT, -1, &f.stmt, NULL);
    }

  for (i = 0; i < SQLTL_FULL_BASE && rc == SQLITE_OK; i++)
    {
      step = "base";
      rc   = sqltl_commit(f.db, f.stmt, f.buf, &f.last, opts);
    }

  if (rc != SQLITE_OK)
    {
      sqltl_fail("full", step, f.db, rc);
      goto out;
    }

  sqltl_fill_disk(&f);
  for (i = 0; i < SQLTL_FULL_STEPS; i++)
    {
      if (sqltl_full_step(&f, g_sqltl_slack[i]) < 0)
        {
          goto out;
        }
    }

  /* The space is back: transactions commit again */

  rc = sqltl_free_blocks(&f, f.nfill);
  if (rc != 0)
    {
      printf("sqltl: FAIL full step=unlink errno=%d\n", rc);
      goto out;
    }

  rc = sqltl_commit(f.db, f.stmt, f.buf, &f.last, opts);
  if (rc != SQLITE_OK)
    {
      sqltl_fail("full", "recover", f.db, rc);
      goto out;
    }

  sqlite3_finalize(f.stmt);
  f.stmt = NULL;
  sqlite3_close(f.db);
  f.db = NULL;

  rc = sqltl_open(path, opts, &f.db);
  if (rc == SQLITE_OK)
    {
      rc = sqltl_check(f.db, &nrows, &bad);
    }

  if (rc != SQLITE_OK || nrows != f.last)
    {
      printf("sqltl: FAIL full step=reopen rc=%d rows=%" PRId64
             " bad=%" PRId64 "\n", rc, nrows, bad);
      goto out;
    }

  if (f.fill != 0 || f.rc != SQLITE_OK)
    {
      printf("sqltl: FAIL full rows=%" PRId64 " fill=%d rc=%d errno=%d\n",
             f.last, f.fill, f.rc, f.sys);
      goto out;
    }

  printf("sqltl: PASS full rows=%" PRId64 " bsize=%ld\n", f.last,
         (long)f.bsize);
  ret = 0;

out:
  sqltl_free_blocks(&f, f.nfill);
  rmdir(filler);
  sqlite3_finalize(f.stmt);
  sqlite3_close(f.db);
  free(f.buf);
  return ret;
}

/* multi <dir>: three databases a, b and c in dir.
 * 1. One transaction writes a.db and an attached b.db.
 * 2. While a connection has a read transaction open on a.db, another one
 *    opens and closes b.db and a third one opens c.db: it must read and
 *    write c.db.
 * Each database must hold its own name and one row afterwards.  The
 * SQLite unix VFS identifies files by st_dev and st_ino, which POSIX
 * requires to identify a file uniquely.
 */

int sqltl_multi(FAR const char *dir, FAR const struct sqltl_opts_s *opts)
{
  static const char names[3] = "abc";
  char path[3][PATH_MAX];
  char sql[PATH_MAX + 32];
  char name[8];
  FAR sqlite3 *db[3] =
    {
      NULL, NULL, NULL
    };

  struct stat st[2];
  FAR uint8_t *buf;
  int64_t rows[3];
  int64_t nrows;
  int64_t bad;
  int rattach;
  int rreuse = SQLITE_OK;
  char reuse[8] = "";
  int files = 0;
  bool same;
  int ret = -1;
  int rc;
  int i;

  buf = malloc(SQLTL_MAXLEN);
  if (buf == NULL)
    {
      printf("sqltl: FAIL multi step=malloc\n");
      return -1;
    }

  for (i = 0; i < 3; i++)
    {
      snprintf(path[i], PATH_MAX, "%s/%c.db", dir, names[i]);
      unlink(path[i]);
      snprintf(sql, sizeof(sql),
               "CREATE TABLE ident(name TEXT);"
               "INSERT INTO ident VALUES('%c')", names[i]);
      rc = sqltl_open(path[i], opts, &db[0]);
      if (rc == SQLITE_OK)
        {
          rc = sqltl_exec(db[0], sql);
        }

      if (rc != SQLITE_OK)
        {
          sqltl_fail("multi", "setup", db[0], rc);
          sqlite3_close(db[0]);
          goto out;
        }

      sqlite3_close(db[0]);
      db[0] = NULL;
    }

  same = stat(path[0], &st[0]) == 0 && stat(path[1], &st[1]) == 0 &&
         st[0].st_dev == st[1].st_dev && st[0].st_ino == st[1].st_ino;

  /* 1. One transaction over a.db and the attached b.db */

  rc = sqltl_open(path[0], opts, &db[0]);
  if (rc == SQLITE_OK)
    {
      snprintf(sql, sizeof(sql), "ATTACH '%s' AS b", path[1]);
      rc = sqltl_exec(db[0], sql);
    }

  if (rc == SQLITE_OK)
    {
      rc = sqltl_exec(db[0], "BEGIN");
    }

  if (rc == SQLITE_OK)
    {
      rc = sqltl_row1(db[0], "main", buf);
    }

  if (rc == SQLITE_OK)
    {
      rc = sqltl_row1(db[0], "b", buf);
    }

  if (rc == SQLITE_OK)
    {
      rc = sqltl_exec(db[0], "COMMIT");
    }

  rattach = rc;
  if (!sqlite3_get_autocommit(db[0]))
    {
      sqltl_exec(db[0], "ROLLBACK");
    }

  sqlite3_close(db[0]);
  db[0] = NULL;

  /* 2. Read transaction on a.db; open and close b.db; open c.db */

  rc = sqltl_open(path[0], opts, &db[0]);
  if (rc == SQLITE_OK)
    {
      rc = sqltl_exec(db[0], "BEGIN; SELECT count(*) FROM t");
    }

  if (rc == SQLITE_OK)
    {
      rc = sqltl_ident(path[1], name, opts, &db[1]);
      sqlite3_close(db[1]);
      db[1] = NULL;
    }

  if (rc == SQLITE_OK)
    {
      rc = sqltl_ident(path[2], reuse, opts, &db[2]);
    }

  if (rc == SQLITE_OK)
    {
      rc = sqltl_row1(db[2], "main", buf);
    }

  rreuse = rc;
  sqlite3_close(db[2]);
  db[2] = NULL;
  sqltl_exec(db[0], "COMMIT");
  sqlite3_close(db[0]);
  db[0] = NULL;

  /* Every database holds its own name and only its own intact rows: one
   * in a.db and b.db if the attached transaction committed, one in c.db.
   */

  for (i = 0; i < 3; i++)
    {
      nrows = -1;
      rc = sqltl_ident(path[i], name, opts, &db[0]);
      if (rc == SQLITE_OK)
        {
          rc = sqltl_check(db[0], &nrows, &bad);
        }

      rows[i] = rc == SQLITE_OK ? nrows : -1;
      if (rc == SQLITE_OK && name[0] == names[i] &&
          nrows == (i == 2 || rattach == SQLITE_OK ? 1 : 0))
        {
          files++;
        }

      sqlite3_close(db[0]);
      db[0] = NULL;
    }

  if (rattach != SQLITE_OK || rreuse != SQLITE_OK ||
      strcmp(reuse, "c") != 0 || files != 3)
    {
      printf("sqltl: FAIL multi attach=%d reuse=%d ident=%s rows=%" PRId64
             ",%" PRId64 ",%" PRId64 " ino=%s\n", rattach, rreuse, reuse,
             rows[0], rows[1], rows[2], same ? "same" : "distinct");
      goto out;
    }

  printf("sqltl: PASS multi ino=%s\n", same ? "same" : "distinct");
  ret = 0;

out:
  free(buf);
  return ret;
}
