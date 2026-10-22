/****************************************************************************
 * apps/sqltl/sqltl_main.c
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
#include <strings.h>

#include "sqltl.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define SQLTL_MAXARGS 4

/* Pattern key of a row id: spreads the id over all four bytes */

#define SQLTL_KEY(id) ((uint32_t)(id) * 0x9e3779b9u)

/****************************************************************************
 * Private Data
 ****************************************************************************/

static uint8_t g_sqltl_buf[SQLTL_MAXLEN];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void sqltl_usage(FAR const char *progname)
{
  fprintf(stderr,
          "Usage: %s write <db> <txns> [-j mode] [-s sync] [-c pages]"
          " [-r rows] [-l len] [-k before|after]\n"
          "       %s verify <db> [-a rows] [-n rows] [-u rows] [-r rows]\n"
          "       %s big <db> <rows> [-j mode] [-s sync] [-c pages]"
          " [-l len] [-x|-w]\n"
          "       %s shrink <db> [-j mode] [-s sync]\n"
          "       %s conc <db> <workers> <txns> [-j mode] [-s sync]"
          " [-d] [-p] [-t sec]\n"
          "       %s handoff <db> [-j mode]\n"
          "       %s visible <db> [-j mode]\n"
          "       %s lock|shm <file>\n"
          "       %s full <db> <filler> [-j mode] [-s sync]\n"
          "       %s multi <dir> [-j mode] [-s sync]\n",
          progname, progname, progname, progname, progname, progname,
          progname, progname, progname, progname);
}

/* Split options from positional arguments.  Options may appear anywhere
 * after the check name.
 */

static int sqltl_parse(int argc, FAR char *argv[],
                       FAR struct sqltl_opts_s *opts,
                       FAR char *args[], FAR int *nargs)
{
  FAR const char *value;
  int i;

  memset(opts, 0, sizeof(*opts));
  opts->rows    = 3;
  opts->exact   = -1;
  opts->most    = -1;
  opts->timeout = 60;
  *nargs        = 0;

  for (i = 2; i < argc; i++)
    {
      if (strcmp(argv[i], "-d") == 0)
        {
          opts->deferred = true;
          continue;
        }

      if (strcmp(argv[i], "-x") == 0)
        {
          opts->rollback = true;
          continue;
        }

      if (strcmp(argv[i], "-w") == 0)
        {
          opts->wait = true;
          continue;
        }

      if (strcmp(argv[i], "-p") == 0)
        {
          opts->procs = true;
          continue;
        }

      if (argv[i][0] == '-' && argv[i][1] != '\0' && argv[i][2] == '\0' &&
          strchr("jscrlantku", argv[i][1]) != NULL)
        {
          if (i + 1 >= argc)
            {
              return -EINVAL;
            }

          value = argv[++i];
          switch (argv[i - 1][1])
            {
              case 'j':
                opts->journal = value;
                break;

              case 's':
                opts->sync = value;
                break;

              case 'c':
                opts->cache = atoi(value);
                break;

              case 'r':
                opts->rows = atoi(value);
                break;

              case 'l':
                opts->len = atoi(value);
                break;

              case 'a':
                opts->acked = strtol(value, NULL, 0);
                break;

              case 'n':
                opts->exact = strtol(value, NULL, 0);
                break;

              case 'u':
                opts->most = strtol(value, NULL, 0);
                break;

              case 'k':
                if (strcmp(value, "before") == 0)
                  {
                    opts->point = SQLTL_POINT_BEFORE;
                  }
                else if (strcmp(value, "after") == 0)
                  {
                    opts->point = SQLTL_POINT_AFTER;
                  }
                else
                  {
                    return -EINVAL;
                  }
                break;

              default:
                opts->timeout = atoi(value);
                break;
            }

          continue;
        }

      if (*nargs >= SQLTL_MAXARGS)
        {
          return -E2BIG;
        }

      args[(*nargs)++] = argv[i];
    }

  if (opts->rows <= 0 || opts->len < 0 || opts->len > SQLTL_MAXLEN ||
      opts->timeout <= 0)
    {
      return -EINVAL;
    }

  return OK;
}

/* Run one statement that returns one text value */

static int sqltl_text(FAR sqlite3 *db, FAR const char *sql,
                      FAR char *buf, size_t size)
{
  FAR sqlite3_stmt *stmt;
  FAR const unsigned char *text;
  int rc;

  rc = sqlite3_prepare_v2(db, sql, -1, &stmt, NULL);
  if (rc != SQLITE_OK)
    {
      return rc;
    }

  rc = sqlite3_step(stmt);
  if (rc == SQLITE_ROW)
    {
      text = sqlite3_column_text(stmt, 0);
      strlcpy(buf, text != NULL ? (FAR const char *)text : "", size);
      rc = SQLITE_OK;
    }
  else if (rc == SQLITE_DONE)
    {
      buf[0] = '\0';
      rc = SQLITE_OK;
    }

  sqlite3_finalize(stmt);
  return rc;
}

/* SQLite error log: the system call and errno behind an error */

static void sqltl_log(FAR void *arg, int rc, FAR const char *msg)
{
  printf("sqltl: log %d %s\n", rc, msg);
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/* Payload length of a row: fixed with -l, else 16 to 6015 bytes, so some
 * rows need overflow pages.
 */

int sqltl_rowlen(FAR const struct sqltl_opts_s *opts, int64_t id)
{
  if (opts->len > 0)
    {
      return opts->len;
    }

  return 16 + (int)(((uint64_t)id * 977u) % 6000u);
}

/* Payload of a row: byte i holds byte (i & 3) of (i & ~3) ^ key(id), so a
 * row read back with another row's payload or a shifted payload is always
 * detected.
 */

void sqltl_fill(FAR uint8_t *buf, int len, int64_t id)
{
  uint32_t key = SQLTL_KEY(id);
  int i;

  for (i = 0; i < len; i++)
    {
      buf[i] = (uint8_t)((((uint32_t)i & ~3u) ^ key) >> ((i & 3) * 8));
    }
}

void sqltl_fail(FAR const char *check, FAR const char *step,
                FAR sqlite3 *db, int rc)
{
  printf("sqltl: FAIL %s step=%s rc=%d errno=%d msg=\"%s\"\n", check, step,
         rc, db != NULL ? sqlite3_system_errno(db) : 0,
         db != NULL ? sqlite3_errmsg(db) : sqlite3_errstr(rc));
}

bool sqltl_busy(int rc)
{
  return (rc & 0xff) == SQLITE_BUSY || (rc & 0xff) == SQLITE_LOCKED;
}

/* Open a database with extended result codes and apply the journal mode,
 * synchronous and cache options.  Returns an SQLite result code; the
 * connection is returned even on failure so that the caller can report
 * the error.
 */

int sqltl_open(FAR const char *path, FAR const struct sqltl_opts_s *opts,
               FAR sqlite3 **db)
{
  char sql[64];
  char mode[16];
  int rc;

  rc = sqlite3_open_v2(path, db, SQLITE_OPEN_READWRITE |
                       SQLITE_OPEN_CREATE, NULL);
  if (*db != NULL)
    {
      sqlite3_extended_result_codes(*db, 1);
      sqlite3_busy_timeout(*db, 5000);
    }

  if (rc == SQLITE_OK && opts->init != NULL)
    {
      rc = sqltl_exec(*db, opts->init);
    }

  if (rc != SQLITE_OK)
    {
      return rc;
    }

  if (opts->journal != NULL)
    {
      snprintf(sql, sizeof(sql), "PRAGMA journal_mode=%s", opts->journal);
      rc = sqltl_text(*db, sql, mode, sizeof(mode));
      if (rc != SQLITE_OK)
        {
          return rc;
        }

      /* The pragma returns the mode in effect: another one means that the
       * requested mode is not available.
       */

      if (strcasecmp(mode, opts->journal) != 0)
        {
          return SQLITE_CANTOPEN;
        }
    }

  if (opts->sync != NULL)
    {
      snprintf(sql, sizeof(sql), "PRAGMA synchronous=%s", opts->sync);
      rc = sqltl_exec(*db, sql);
      if (rc != SQLITE_OK)
        {
          return rc;
        }
    }

  if (opts->cache > 0)
    {
      snprintf(sql, sizeof(sql), "PRAGMA cache_size=%d", opts->cache);
      rc = sqltl_exec(*db, sql);
      if (rc != SQLITE_OK)
        {
          return rc;
        }
    }

  return sqltl_exec(*db, SQLTL_SCHEMA);
}

int sqltl_exec(FAR sqlite3 *db, FAR const char *sql)
{
  return sqlite3_exec(db, sql, NULL, NULL, NULL);
}

/* Run one statement that returns one integer; SQLITE_NOTFOUND if it
 * returns no row.
 */

int sqltl_int(FAR sqlite3 *db, FAR const char *sql, FAR int64_t *value)
{
  FAR sqlite3_stmt *stmt;
  int rc;

  rc = sqlite3_prepare_v2(db, sql, -1, &stmt, NULL);
  if (rc != SQLITE_OK)
    {
      return rc;
    }

  rc = sqlite3_step(stmt);
  if (rc == SQLITE_ROW)
    {
      *value = sqlite3_column_int64(stmt, 0);
      rc = SQLITE_OK;
    }
  else if (rc == SQLITE_DONE)
    {
      rc = SQLITE_NOTFOUND;
    }

  sqlite3_finalize(stmt);
  return rc;
}

/* Insert one row with a prepared "INSERT INTO t VALUES(?, ?, ?)" */

int sqltl_insert(FAR sqlite3_stmt *stmt, FAR uint8_t *buf, int64_t id,
                 int len)
{
  int rc;

  sqltl_fill(buf, len, id);
  sqlite3_bind_int64(stmt, 1, id);
  sqlite3_bind_int(stmt, 2, len);
  sqlite3_bind_blob(stmt, 3, buf, len, SQLITE_STATIC);
  rc = sqlite3_step(stmt);
  sqlite3_reset(stmt);
  return rc == SQLITE_DONE ? SQLITE_OK : rc;
}

/* Check a database: PRAGMA integrity_check returns only "ok", the row ids
 * of t are 1..n without gaps and every payload is intact.  Returns
 * SQLITE_OK, an SQLite error, or SQLITE_CORRUPT with *bad set to the first
 * bad row id (0: integrity_check failed).
 */

int sqltl_check(FAR sqlite3 *db, FAR int64_t *nrows, FAR int64_t *bad)
{
  FAR sqlite3_stmt *stmt;
  FAR const void *data;
  char result[32];
  int64_t id;
  int len;
  int rc;

  *nrows = 0;
  *bad   = 0;

  rc = sqltl_text(db, "PRAGMA integrity_check", result, sizeof(result));
  if (rc != SQLITE_OK)
    {
      return rc;
    }

  if (strcmp(result, "ok") != 0)
    {
      printf("sqltl: integrity %s\n", result);
      return SQLITE_CORRUPT;
    }

  rc = sqlite3_prepare_v2(db, "SELECT id, len, data FROM t ORDER BY id",
                          -1, &stmt, NULL);
  if (rc != SQLITE_OK)
    {
      return rc;
    }

  while ((rc = sqlite3_step(stmt)) == SQLITE_ROW)
    {
      id   = sqlite3_column_int64(stmt, 0);
      len  = sqlite3_column_int(stmt, 1);
      data = sqlite3_column_blob(stmt, 2);

      if (id != *nrows + 1 || len <= 0 || len > SQLTL_MAXLEN ||
          sqlite3_column_bytes(stmt, 2) != len || data == NULL)
        {
          *bad = *nrows + 1;
          rc   = SQLITE_CORRUPT;
          break;
        }

      sqltl_fill(g_sqltl_buf, len, id);
      if (memcmp(data, g_sqltl_buf, len) != 0)
        {
          *bad = id;
          rc   = SQLITE_CORRUPT;
          break;
        }

      (*nrows)++;
    }

  sqlite3_finalize(stmt);
  return rc == SQLITE_DONE ? SQLITE_OK : rc;
}

int main(int argc, FAR char *argv[])
{
  FAR char *args[SQLTL_MAXARGS];
  struct sqltl_opts_s opts;
  FAR const char *check;
  int nargs;
  int ret;

  if (argc < 2 || sqltl_parse(argc, argv, &opts, args, &nargs) < 0)
    {
      sqltl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  sqlite3_config(SQLITE_CONFIG_LOG, sqltl_log, NULL);
  if (sqltl_vfs_register() != SQLITE_OK)
    {
      printf("sqltl: FAIL %s step=vfs\n", argv[1]);
      return EXIT_FAILURE;
    }

  check = argv[1];
  ret   = -EINVAL;

  if (strcmp(check, "write") == 0 && nargs == 2)
    {
      ret = sqltl_write(args[0], strtol(args[1], NULL, 0), &opts);
    }
  else if (strcmp(check, "verify") == 0 && nargs == 1)
    {
      ret = sqltl_verify(args[0], &opts);
    }
  else if (strcmp(check, "big") == 0 && nargs == 2)
    {
      ret = sqltl_big(args[0], strtol(args[1], NULL, 0), &opts);
    }
  else if (strcmp(check, "shrink") == 0 && nargs == 1)
    {
      ret = sqltl_shrink(args[0], &opts);
    }
  else if (strcmp(check, "conc") == 0 && nargs == 3)
    {
      ret = sqltl_conc(args[0], atoi(args[1]), strtol(args[2], NULL, 0),
                       &opts);
    }
  else if (strcmp(check, "concw") == 0 && nargs == 4)
    {
      return sqltl_concw(args[0], atoi(args[1]), strtol(args[2], NULL, 0),
                         strtoll(args[3], NULL, 0), &opts);
    }
  else if (strcmp(check, "handoff") == 0 && nargs == 1)
    {
      ret = sqltl_handoff(args[0], &opts);
    }
  else if (strcmp(check, "visible") == 0 && nargs == 1)
    {
      ret = sqltl_visible(args[0], &opts);
    }
  else if (strcmp(check, "lock") == 0 && nargs == 1)
    {
      ret = sqltl_lock(args[0]);
    }
  else if (strcmp(check, "shm") == 0 && nargs == 1)
    {
      ret = sqltl_shm(args[0]);
    }
  else if (strcmp(check, "shmprobe") == 0 && nargs == 1)
    {
      return sqltl_shmprobe(args[0]);
    }
  else if (strcmp(check, "lockprobe") == 0 && nargs == 4)
    {
      return sqltl_lockprobe(args[0], (off_t)strtol(args[1], NULL, 0),
                             (off_t)strtol(args[2], NULL, 0),
                             (pid_t)strtol(args[3], NULL, 0));
    }
  else if (strcmp(check, "full") == 0 && nargs == 2)
    {
      ret = sqltl_full(args[0], args[1], &opts);
    }
  else if (strcmp(check, "multi") == 0 && nargs == 1)
    {
      ret = sqltl_multi(args[0], &opts);
    }

  if (ret == -EINVAL)
    {
      sqltl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  return ret < 0 ? EXIT_FAILURE : EXIT_SUCCESS;
}
