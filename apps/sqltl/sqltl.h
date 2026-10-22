/****************************************************************************
 * apps/sqltl/sqltl.h
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

#ifndef __APPS_SQLTL_SQLTL_H
#define __APPS_SQLTL_SQLTL_H

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>

#include <stdbool.h>
#include <stdint.h>
#include <sys/types.h>

#include <sqlite3.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

/* Table of the data checks: row id, payload length, payload.  Ids start
 * at 1 and have no gaps; the payload is sqltl_fill(id, len).
 */

#define SQLTL_SCHEMA \
  "CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, " \
  "len INTEGER NOT NULL, data BLOB NOT NULL)"

#define SQLTL_MAXLEN 8192

/* Commit point hook of the VFS (-k) */

#define SQLTL_POINT_NONE   0
#define SQLTL_POINT_BEFORE 1
#define SQLTL_POINT_AFTER  2

/****************************************************************************
 * Public Types
 ****************************************************************************/

/* Options shared by the checks */

struct sqltl_opts_s
{
  FAR const char *init;     /* SQL run first after open, NULL: none */
  FAR const char *journal;  /* PRAGMA journal_mode (-j), NULL: keep */
  FAR const char *sync;     /* PRAGMA synchronous (-s), NULL: default */
  int             cache;    /* PRAGMA cache_size in pages (-c), 0: default */
  int             rows;     /* Rows per transaction (-r) */
  int             len;      /* Payload length (-l), 0: varies by row id */
  long            acked;    /* verify: rows reported committed (-a) */
  long            exact;    /* verify: exact row count, -1: any (-n) */
  long            most;     /* verify: most rows, -1: any (-u) */
  int             point;    /* write: stop at the last commit point (-k) */
  bool            procs;    /* conc: tasks instead of threads (-p) */
  int             timeout;  /* conc: give up after this many s (-t) */
  bool            deferred; /* conc: BEGIN DEFERRED, not IMMEDIATE (-d) */
  bool            rollback; /* big: ROLLBACK instead of COMMIT (-x) */
  bool            wait;     /* big: wait to be killed before COMMIT (-w) */
};

/****************************************************************************
 * Public Function Prototypes
 ****************************************************************************/

/* Helpers (sqltl_main.c) */

int sqltl_rowlen(FAR const struct sqltl_opts_s *opts, int64_t id);
void sqltl_fill(FAR uint8_t *buf, int len, int64_t id);
int sqltl_open(FAR const char *path, FAR const struct sqltl_opts_s *opts,
               FAR sqlite3 **db);
int sqltl_exec(FAR sqlite3 *db, FAR const char *sql);
int sqltl_int(FAR sqlite3 *db, FAR const char *sql, FAR int64_t *value);
int sqltl_insert(FAR sqlite3_stmt *stmt, FAR uint8_t *buf, int64_t id,
                 int len);
int sqltl_check(FAR sqlite3 *db, FAR int64_t *nrows, FAR int64_t *bad);
void sqltl_fail(FAR const char *check, FAR const char *step,
                FAR sqlite3 *db, int rc);
bool sqltl_busy(int rc);

/* Commit point hook (sqltl_vfs.c) */

int sqltl_vfs_register(void);
void sqltl_vfs_arm(int point);

/* Transaction checks (sqltl_txn.c) */

int sqltl_write(FAR const char *path, long txns,
                FAR const struct sqltl_opts_s *opts);
int sqltl_verify(FAR const char *path, FAR const struct sqltl_opts_s *opts);
int sqltl_big(FAR const char *path, long rows,
              FAR const struct sqltl_opts_s *opts);
int sqltl_shrink(FAR const char *path, FAR const struct sqltl_opts_s *opts);

/* Concurrency and lock checks (sqltl_conc.c) */

int sqltl_conc(FAR const char *path, int nthreads, long txns,
               FAR const struct sqltl_opts_s *opts);
int sqltl_concw(FAR const char *path, int id, long txns, int64_t start,
                FAR const struct sqltl_opts_s *opts);
int sqltl_handoff(FAR const char *path,
                  FAR const struct sqltl_opts_s *opts);
int sqltl_visible(FAR const char *path, FAR const struct sqltl_opts_s *opts);
int sqltl_lock(FAR const char *path);
int sqltl_lockprobe(FAR const char *path, off_t start, off_t len,
                    pid_t pid);
int sqltl_shm(FAR const char *path);
int sqltl_shmprobe(FAR const char *path);

/* Disk full and several databases (sqltl_full.c) */

int sqltl_full(FAR const char *path, FAR const char *filler,
               FAR const struct sqltl_opts_s *opts);
int sqltl_multi(FAR const char *dir, FAR const struct sqltl_opts_s *opts);

#endif /* __APPS_SQLTL_SQLTL_H */
