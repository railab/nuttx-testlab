/****************************************************************************
 * apps/sqltl/sqltl_txn.c
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
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "sqltl.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define SQLTL_INSERT "INSERT INTO t VALUES(?, ?, ?)"

/* big prints a progress line every this many rows */

#define SQLTL_PROGRESS 32

/* shrink: rows of SQLTL_SHRINK_LEN bytes, the second half is deleted */

#define SQLTL_SHRINK_ROWS 60
#define SQLTL_SHRINK_LEN  3000

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/* write <db> <txns>: commit txns transactions of -r rows each, continuing
 * after the last row id, and print "sqltl: commit <id>" after each COMMIT
 * returned.  A crash test kills the node at any point: every row reported
 * must survive, and only whole transactions may be present.
 */

int sqltl_write(FAR const char *path, long txns,
                FAR const struct sqltl_opts_s *opts)
{
  FAR sqlite3_stmt *stmt = NULL;
  FAR sqlite3 *db = NULL;
  FAR const char *step;
  FAR uint8_t *buf;
  int64_t last = 0;
  int64_t id;
  long n;
  int ret = -1;
  int rc;
  int r;

  if (txns <= 0)
    {
      return -EINVAL;
    }

  buf = malloc(SQLTL_MAXLEN);
  if (buf == NULL)
    {
      printf("sqltl: FAIL write step=malloc\n");
      return -1;
    }

  step = "open";
  rc   = sqltl_open(path, opts, &db);
  if (rc == SQLITE_OK)
    {
      step = "resume";
      rc   = sqltl_int(db, "SELECT coalesce(max(id), 0) FROM t", &last);
    }

  if (rc == SQLITE_OK)
    {
      step = "prepare";
      rc   = sqlite3_prepare_v2(db, SQLTL_INSERT, -1, &stmt, NULL);
    }

  if (rc != SQLITE_OK)
    {
      sqltl_fail("write", step, db, rc);
      goto out;
    }

  if (last % opts->rows != 0)
    {
      printf("sqltl: FAIL write step=resume rows=%" PRId64 " partial\n",
             last);
      goto out;
    }

  for (n = 0; n < txns; n++)
    {
      step = "begin";
      rc   = sqltl_exec(db, "BEGIN");
      for (r = 0; r < opts->rows && rc == SQLITE_OK; r++)
        {
          id   = last + 1 + r;
          step = "insert";
          rc   = sqltl_insert(stmt, buf, id, sqltl_rowlen(opts, id));
        }

      if (rc == SQLITE_OK)
        {
          /* -k: the last COMMIT stops at its commit point */

          if (n == txns - 1)
            {
              sqltl_vfs_arm(opts->point);
            }

          step = "commit";
          rc   = sqltl_exec(db, "COMMIT");
        }

      if (rc != SQLITE_OK)
        {
          sqltl_fail("write", step, db, rc);
          goto out;
        }

      last += opts->rows;
      printf("sqltl: commit %" PRId64 "\n", last);
      fflush(stdout);
    }

  if (opts->point != SQLTL_POINT_NONE)
    {
      printf("sqltl: FAIL write step=point rows=%" PRId64 " missed\n",
             last);
      goto out;
    }

  printf("sqltl: PASS write rows=%" PRId64 " txns=%ld\n", last, txns);
  ret = 0;

out:
  sqlite3_finalize(stmt);
  sqlite3_close(db);
  free(buf);
  return ret;
}

/* verify <db>: integrity_check is "ok", rows 1..n without gaps and intact,
 * n >= -a, n a multiple of -r (whole transactions only), n == -n and
 * n <= -u if given.
 */

int sqltl_verify(FAR const char *path, FAR const struct sqltl_opts_s *opts)
{
  FAR sqlite3 *db = NULL;
  int64_t nrows = 0;
  int64_t bad = 0;
  int ret = -1;
  int rc;

  rc = sqltl_open(path, opts, &db);
  if (rc != SQLITE_OK)
    {
      sqltl_fail("verify", "open", db, rc);
      goto out;
    }

  rc = sqltl_check(db, &nrows, &bad);
  if (rc == SQLITE_CORRUPT)
    {
      printf("sqltl: FAIL verify rows=%" PRId64 " bad=%" PRId64 "\n",
             nrows, bad);
      goto out;
    }

  if (rc != SQLITE_OK)
    {
      sqltl_fail("verify", "check", db, rc);
      goto out;
    }

  if (nrows < opts->acked || nrows % opts->rows != 0 ||
      (opts->exact >= 0 && nrows != opts->exact) ||
      (opts->most >= 0 && nrows > opts->most))
    {
      printf("sqltl: FAIL verify rows=%" PRId64 " acked=%ld exact=%ld"
             " most=%ld\n", nrows, opts->acked, opts->exact, opts->most);
      goto out;
    }

  printf("sqltl: PASS verify rows=%" PRId64 "\n", nrows);
  ret = 0;

out:
  sqlite3_close(db);
  return ret;
}

/* big <db> <rows>: one transaction of rows rows, larger than the page
 * cache (-c) so that SQLite spills dirty pages into the database file
 * before the commit.  Prints "sqltl: row <id>" while inserting, so that a
 * crash test can kill the node inside the transaction.  With -x it rolls
 * the transaction back and checks that the database is as before; with -w
 * it prints "sqltl: wait" before the COMMIT and waits to be killed.
 */

int sqltl_big(FAR const char *path, long rows,
              FAR const struct sqltl_opts_s *opts)
{
  FAR sqlite3_stmt *stmt = NULL;
  FAR sqlite3 *db = NULL;
  FAR const char *step;
  FAR uint8_t *buf;
  int64_t last = 0;
  int64_t nrows = 0;
  int64_t bad = 0;
  int64_t id;
  int spill = 0;
  int high;
  int ret = -1;
  int rc;

  if (rows <= 0)
    {
      return -EINVAL;
    }

  buf = malloc(SQLTL_MAXLEN);
  if (buf == NULL)
    {
      printf("sqltl: FAIL big step=malloc\n");
      return -1;
    }

  step = "open";
  rc   = sqltl_open(path, opts, &db);
  if (rc == SQLITE_OK)
    {
      step = "resume";
      rc   = sqltl_int(db, "SELECT coalesce(max(id), 0) FROM t", &last);
    }

  if (rc == SQLITE_OK)
    {
      step = "prepare";
      rc   = sqlite3_prepare_v2(db, SQLTL_INSERT, -1, &stmt, NULL);
    }

  if (rc == SQLITE_OK)
    {
      step = "begin";
      rc   = sqltl_exec(db, "BEGIN");
    }

  for (id = last + 1; id <= last + rows && rc == SQLITE_OK; id++)
    {
      step = "insert";
      rc   = sqltl_insert(stmt, buf, id, sqltl_rowlen(opts, id));
      if (rc == SQLITE_OK && (id - last) % SQLTL_PROGRESS == 0)
        {
          printf("sqltl: row %" PRId64 "\n", id);
          fflush(stdout);
        }
    }

  if (rc == SQLITE_OK)
    {
      sqlite3_db_status(db, SQLITE_DBSTATUS_CACHE_SPILL, &spill, &high, 0);
      if (opts->wait)
        {
          /* Stay inside the transaction until the node is powered off */

          printf("sqltl: wait spill=%d\n", spill);
          fflush(stdout);
          for (; ; )
            {
              sleep(1);
            }
        }

      step = opts->rollback ? "rollback" : "commit";
      rc   = sqltl_exec(db, opts->rollback ? "ROLLBACK" : "COMMIT");
    }

  if (rc == SQLITE_OK && opts->rollback)
    {
      /* The database must be as before the transaction */

      step = "check";
      rc   = sqltl_check(db, &nrows, &bad);
      if (rc == SQLITE_OK && nrows != last)
        {
          rc = SQLITE_CORRUPT;
        }
    }

  if (rc != SQLITE_OK)
    {
      printf("sqltl: FAIL big step=%s rc=%d errno=%d rows=%" PRId64
             " bad=%" PRId64 " spill=%d\n", step, rc,
             sqlite3_system_errno(db), nrows, bad, spill);
      goto out;
    }

  if (!opts->rollback)
    {
      last += rows;
      printf("sqltl: commit %" PRId64 "\n", last);
    }

  printf("sqltl: PASS big rows=%" PRId64 " spill=%d\n", last, spill);
  ret = 0;

out:
  sqlite3_finalize(stmt);
  sqlite3_close(db);
  free(buf);
  return ret;
}

/* Insert rows first..last in one transaction */

static int sqltl_rows(FAR sqlite3 *db, FAR sqlite3_stmt *stmt,
                      FAR uint8_t *buf, int64_t first, int64_t last)
{
  int64_t id;
  int rc;

  rc = sqltl_exec(db, "BEGIN");
  for (id = first; id <= last && rc == SQLITE_OK; id++)
    {
      rc = sqltl_insert(stmt, buf, id, SQLTL_SHRINK_LEN);
    }

  if (rc == SQLITE_OK)
    {
      rc = sqltl_exec(db, "COMMIT");
    }
  else if (!sqlite3_get_autocommit(db))
    {
      sqltl_exec(db, "ROLLBACK");
    }

  return rc;
}

/* shrink <db>: the database file shrinks and grows again.  With
 * auto_vacuum FULL, the commit that deletes the second half of the rows
 * truncates the file; then VACUUM truncates it.  Each time the rows are
 * inserted again; all intact after reopening.
 */

int sqltl_shrink(FAR const char *path, FAR const struct sqltl_opts_s *opts)
{
  static const char *const g_shrink[2] =
  {
    "DELETE FROM t WHERE id > 30; PRAGMA wal_checkpoint(TRUNCATE)",
    "DELETE FROM t WHERE id > 30; VACUUM; PRAGMA wal_checkpoint(TRUNCATE)"
  };

  struct sqltl_opts_s o;
  FAR sqlite3_stmt *stmt = NULL;
  FAR sqlite3 *db = NULL;
  FAR const char *step;
  FAR uint8_t *buf;
  struct stat st;
  off_t size[3];
  int64_t nrows = 0;
  int64_t bad = 0;
  int ret = -1;
  int rc;
  int i;

  buf = malloc(SQLTL_MAXLEN);
  if (buf == NULL)
    {
      printf("sqltl: FAIL shrink step=malloc\n");
      return -1;
    }

  memcpy(&o, opts, sizeof(o));
  o.init = "PRAGMA auto_vacuum=FULL; PRAGMA temp_store=MEMORY";

  step = "open";
  rc   = sqltl_open(path, &o, &db);
  if (rc == SQLITE_OK)
    {
      step = "prepare";
      rc   = sqlite3_prepare_v2(db, SQLTL_INSERT, -1, &stmt, NULL);
    }

  if (rc == SQLITE_OK)
    {
      step = "fill";
      rc   = sqltl_rows(db, stmt, buf, 1, SQLTL_SHRINK_ROWS);
    }

  for (i = 0; i < 2 && rc == SQLITE_OK; i++)
    {
      /* In WAL mode the database file changes only at a checkpoint */

      step = "checkpoint";
      rc   = sqltl_exec(db, "PRAGMA wal_checkpoint(TRUNCATE)");
      if (rc != SQLITE_OK)
        {
          break;
        }

      size[0] = stat(path, &st) == 0 ? st.st_size : -1;
      step    = i == 0 ? "delete" : "vacuum";
      rc      = sqltl_exec(db, g_shrink[i]);
      size[1] = stat(path, &st) == 0 ? st.st_size : -1;
      if (rc == SQLITE_OK && size[1] >= size[0])
        {
          printf("sqltl: FAIL shrink step=%s size=%ld/%ld\n", step,
                 (long)size[0], (long)size[1]);
          goto out;
        }

      if (rc == SQLITE_OK)
        {
          step = "grow";
          rc   = sqltl_rows(db, stmt, buf, 31, SQLTL_SHRINK_ROWS);
        }
    }

  if (rc == SQLITE_OK)
    {
      sqlite3_finalize(stmt);
      stmt = NULL;
      sqlite3_close(db);
      db   = NULL;
      step = "reopen";
      rc   = sqltl_open(path, opts, &db);
    }

  if (rc == SQLITE_OK)
    {
      step = "check";
      rc   = sqltl_check(db, &nrows, &bad);
    }

  if (rc != SQLITE_OK || nrows != SQLTL_SHRINK_ROWS)
    {
      printf("sqltl: FAIL shrink step=%s rc=%d errno=%d rows=%" PRId64
             " bad=%" PRId64 "\n", step, rc,
             db != NULL ? sqlite3_system_errno(db) : 0, nrows, bad);
      goto out;
    }

  size[2] = stat(path, &st) == 0 ? st.st_size : -1;
  printf("sqltl: PASS shrink rows=%" PRId64 " size=%ld\n", nrows,
         (long)size[2]);
  ret = 0;

out:
  sqlite3_finalize(stmt);
  sqlite3_close(db);
  free(buf);
  return ret;
}
