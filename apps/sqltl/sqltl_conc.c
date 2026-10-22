/****************************************************************************
 * apps/sqltl/sqltl_conc.c
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
#include <pthread.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "sqltl.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define SQLTL_MAXTHREADS 8

/* conc: the workers start their transactions together, this long after
 * the first one was started.
 */

#define SQLTL_GATE_THREADS 200
#define SQLTL_GATE_TASKS   2000

#define SQLTL_CONC_SCHEMA \
  "CREATE TABLE IF NOT EXISTS c(w INTEGER, seq INTEGER, " \
  "PRIMARY KEY(w, seq));" \
  "CREATE TABLE IF NOT EXISTS ctr(k INTEGER PRIMARY KEY, " \
  "v INTEGER NOT NULL);" \
  "DELETE FROM c;" \
  "INSERT OR REPLACE INTO ctr VALUES(1, 0)"

/* shm: size of the mapping and the bytes stored through it */

#define SQLTL_SHM_SIZE      4096
#define SQLTL_SHM_MINE      0x5a
#define SQLTL_SHM_PEER      0xa5

/* lockprobe exit codes */

#define SQLTL_PROBE_OK      0 /* As expected */
#define SQLTL_PROBE_UNLOCK  1 /* Not locked, a lock was expected */
#define SQLTL_PROBE_PID     2 /* Locked by another pid */
#define SQLTL_PROBE_SHARED  3 /* Our F_SETLK succeeded: not exclusive */
#define SQLTL_PROBE_LOCKED  4 /* Locked, no lock was expected */
#define SQLTL_PROBE_ERROR   5 /* A call failed */

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct sqltl_worker_s
{
  pthread_t                     thread;
  FAR const char               *path;
  FAR const struct sqltl_opts_s *opts;
  time_t                        deadline;
  int64_t                       start;  /* CLOCK_MONOTONIC ms to start */
  int                           id;
  long                          txns;
  long                          done;   /* Transactions committed */
  long                          busy;   /* SQLITE_BUSY/LOCKED retries */
  int                           rc;     /* First other error */
  int                           sys;    /* Its errno */
  FAR const char               *step;   /* Where it happened */
};

struct sqltl_exec_s
{
  FAR sqlite3    *db;
  FAR const char *sql;
  int             rc;
};

struct sqltl_locker_s
{
  int   fd;
  off_t start;
  off_t len;
  int   err;
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/* One transaction: read the counter, insert (w, seq), write counter + 1.
 * The counter is written from the value read, so two transactions that
 * overlap without exclusion lose an update.
 */

static int sqltl_txn(FAR struct sqltl_worker_s *w, FAR sqlite3 *db,
                     FAR sqlite3_stmt *sel, FAR sqlite3_stmt *ins,
                     FAR sqlite3_stmt *upd, long seq)
{
  int64_t v = 0;
  int rc;

  w->step = "begin";
  rc = sqltl_exec(db, w->opts->deferred ? "BEGIN" : "BEGIN IMMEDIATE");
  if (rc == SQLITE_OK)
    {
      w->step = "select";
      rc = sqlite3_step(sel);
      v  = sqlite3_column_int64(sel, 0);
      rc = rc == SQLITE_ROW ? SQLITE_OK : rc;
      sqlite3_reset(sel);
    }

  if (rc == SQLITE_OK)
    {
      w->step = "insert";
      sqlite3_bind_int(ins, 1, w->id);
      sqlite3_bind_int64(ins, 2, seq);
      rc = sqlite3_step(ins);
      rc = rc == SQLITE_DONE ? SQLITE_OK : rc;
      sqlite3_reset(ins);
    }

  if (rc == SQLITE_OK)
    {
      w->step = "update";
      sqlite3_bind_int64(upd, 1, v + 1);
      rc = sqlite3_step(upd);
      rc = rc == SQLITE_DONE ? SQLITE_OK : rc;
      sqlite3_reset(upd);
    }

  if (rc == SQLITE_OK)
    {
      w->step = "commit";
      rc = sqltl_exec(db, "COMMIT");
    }

  if (rc != SQLITE_OK && !sqlite3_get_autocommit(db))
    {
      sqltl_exec(db, "ROLLBACK");
    }

  return rc;
}

/* Milliseconds of CLOCK_MONOTONIC */

static int64_t sqltl_now_ms(void)
{
  struct timespec ts;

  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* Back off for 1 to 4 ms before a retry */

static void sqltl_backoff(FAR unsigned int *seed)
{
  *seed = *seed * 1103515245u + 12345u;
  usleep(1000 + (*seed >> 16) % 3000);
}

/* Prepare the statements of a worker; none are left on failure */

static int sqltl_worker_prepare(FAR sqlite3 *db, FAR sqlite3_stmt **sel,
                                FAR sqlite3_stmt **ins,
                                FAR sqlite3_stmt **upd)
{
  int rc;

  rc = sqlite3_prepare_v2(db, "SELECT v FROM ctr WHERE k = 1", -1, sel,
                          NULL);
  if (rc == SQLITE_OK)
    {
      rc = sqlite3_prepare_v2(db, "INSERT INTO c VALUES(?, ?)", -1, ins,
                              NULL);
    }

  if (rc == SQLITE_OK)
    {
      rc = sqlite3_prepare_v2(db, "UPDATE ctr SET v = ? WHERE k = 1", -1,
                              upd, NULL);
    }

  if (rc != SQLITE_OK)
    {
      sqlite3_finalize(*sel);
      sqlite3_finalize(*ins);
      sqlite3_finalize(*upd);
      *sel = NULL;
      *ins = NULL;
      *upd = NULL;
    }

  return rc;
}

static FAR void *sqltl_worker(FAR void *arg)
{
  FAR struct sqltl_worker_s *w = arg;
  FAR sqlite3_stmt *sel = NULL;
  FAR sqlite3_stmt *ins = NULL;
  FAR sqlite3_stmt *upd = NULL;
  FAR sqlite3 *db = NULL;
  char sql[48];
  unsigned int seed = (unsigned int)w->id * 2654435761u;
  long seq = 1;
  int rc;

  w->step = "open";
  rc = sqlite3_open_v2(w->path, &db, SQLITE_OPEN_READWRITE, NULL);
  if (rc == SQLITE_OK)
    {
      sqlite3_extended_result_codes(db, 1);
      sqlite3_busy_timeout(db, 100);
      if (w->opts->sync != NULL)
        {
          snprintf(sql, sizeof(sql), "PRAGMA synchronous=%s",
                   w->opts->sync);
          rc = sqltl_exec(db, sql);
        }
    }

  /* Preparing reads the schema, which another connection may have
   * locked: SQLITE_BUSY is retried like in a transaction.
   */

  while (rc == SQLITE_OK && upd == NULL)
    {
      w->step = "prepare";
      rc = sqltl_worker_prepare(db, &sel, &ins, &upd);
      if (sqltl_busy(rc) && time(NULL) < w->deadline)
        {
          w->busy++;
          sqltl_backoff(&seed);
          rc = SQLITE_OK;
        }
    }

  /* Wait for the other workers */

  while (rc == SQLITE_OK && sqltl_now_ms() < w->start)
    {
      usleep(1000);
    }

  while (rc == SQLITE_OK && seq <= w->txns)
    {
      rc = sqltl_txn(w, db, sel, ins, upd, seq);
      if (rc == SQLITE_OK)
        {
          w->done = seq++;
        }
      else if (sqltl_busy(rc) && time(NULL) < w->deadline)
        {
          w->busy++;
          sqltl_backoff(&seed);
          rc = SQLITE_OK;
        }
    }

  if (rc != SQLITE_OK)
    {
      w->rc  = rc;
      w->sys = db != NULL ? sqlite3_system_errno(db) : 0;
    }

  sqlite3_finalize(sel);
  sqlite3_finalize(ins);
  sqlite3_finalize(upd);
  sqlite3_close(db);
  return NULL;
}

/* Check the result of conc: no error, every (w, seq) once, the counter
 * equal to the number of transactions, integrity_check "ok".
 */

static int sqltl_conc_check(FAR const char *path, int nthreads, long txns,
                            FAR const struct sqltl_opts_s *opts)
{
  FAR sqlite3 *db = NULL;
  FAR const char *step;
  char sql[192];
  int64_t nrows = 0;
  int64_t bad = 0;
  int64_t count = 0;
  int64_t ctr = 0;
  int64_t full = 0;
  int ret = -1;
  int rc;

  step = "open";
  rc   = sqltl_open(path, opts, &db);
  if (rc == SQLITE_OK)
    {
      step = "check";
      rc   = sqltl_check(db, &nrows, &bad);
    }

  if (rc == SQLITE_OK)
    {
      step = "count";
      rc   = sqltl_int(db, "SELECT count(*) FROM c", &count);
    }

  if (rc == SQLITE_OK)
    {
      step = "counter";
      rc   = sqltl_int(db, "SELECT v FROM ctr WHERE k = 1", &ctr);
    }

  if (rc == SQLITE_OK)
    {
      snprintf(sql, sizeof(sql),
               "SELECT count(*) FROM (SELECT w FROM c GROUP BY w "
               "HAVING count(*) = %ld AND min(seq) = 1 AND "
               "max(seq) = %ld)", txns, txns);
      step = "writers";
      rc   = sqltl_int(db, sql, &full);
    }

  if (rc != SQLITE_OK)
    {
      sqltl_fail("conc", step, db, rc);
    }
  else if (count != (int64_t)nthreads * txns ||
           ctr != (int64_t)nthreads * txns || full != nthreads)
    {
      printf("sqltl: FAIL conc rows=%" PRId64 " counter=%" PRId64
             " writers=%" PRId64 "\n", count, ctr, full);
    }
  else
    {
      ret = 0;
    }

  sqlite3_close(db);
  return ret;
}

static FAR void *sqltl_exec_thread(FAR void *arg)
{
  FAR struct sqltl_exec_s *e = arg;

  e->rc = sqltl_exec(e->db, e->sql);
  return NULL;
}

/* Run SQL on a connection, in a new thread of this process if thread */

static int sqltl_exec_in(FAR sqlite3 *db, FAR const char *sql, bool thread)
{
  struct sqltl_exec_s e;
  pthread_attr_t attr;
  pthread_t tid;
  int ret;

  if (!thread)
    {
      return sqltl_exec(db, sql);
    }

  e.db  = db;
  e.sql = sql;
  e.rc  = SQLITE_ERROR;

  pthread_attr_init(&attr);
  pthread_attr_setstacksize(&attr, CONFIG_TESTLAB_SQLTL_STACKSIZE);
  ret = pthread_create(&tid, &attr, sqltl_exec_thread, &e);
  pthread_attr_destroy(&attr);
  if (ret != 0)
    {
      return SQLITE_ERROR;
    }

  pthread_join(tid, NULL);
  return e.rc;
}

/* One handoff run: reader R and writer W connections of this process.
 * R reads, W joins the read, R finishes, then W must be able to write:
 * no other connection holds any lock.  W runs in other threads if thread.
 */

static int sqltl_handoff_run(FAR const char *path,
                             FAR const struct sqltl_opts_s *opts,
                             bool thread, FAR const char **step)
{
  FAR sqlite3 *r = NULL;
  FAR sqlite3 *w = NULL;
  int rc;

  *step = "open";
  rc    = sqltl_open(path, opts, &r);
  if (rc == SQLITE_OK)
    {
      rc = sqltl_open(path, opts, &w);
    }

  if (rc == SQLITE_OK)
    {
      sqlite3_busy_timeout(w, 2000);
      *step = "read";
      rc    = sqltl_exec(r, "BEGIN; SELECT count(*) FROM h");
    }

  if (rc == SQLITE_OK)
    {
      *step = "share";
      rc    = sqltl_exec_in(w, "BEGIN; SELECT count(*) FROM h", thread);
    }

  if (rc == SQLITE_OK)
    {
      *step = "done";
      rc    = sqltl_exec(r, "COMMIT");
    }

  if (rc == SQLITE_OK)
    {
      *step = "write";
      rc    = sqltl_exec_in(w, "INSERT INTO h VALUES(1); COMMIT", thread);
    }

  if (w != NULL && !sqlite3_get_autocommit(w))
    {
      sqltl_exec(w, "ROLLBACK");
    }

  if (r != NULL && !sqlite3_get_autocommit(r))
    {
      sqltl_exec(r, "ROLLBACK");
    }

  sqlite3_close(w);
  sqlite3_close(r);
  return rc;
}

/* F_SETLK of [start, start + len) on fd, 0 or the errno */

static int sqltl_setlk(int fd, short type, off_t start, off_t len)
{
  struct flock fl;

  memset(&fl, 0, sizeof(fl));
  fl.l_type   = type;
  fl.l_whence = SEEK_SET;
  fl.l_start  = start;
  fl.l_len    = len;
  return fcntl(fd, F_SETLK, &fl) < 0 ? errno : 0;
}

static FAR void *sqltl_locker(FAR void *arg)
{
  FAR struct sqltl_locker_s *l = arg;

  l->err = sqltl_setlk(l->fd, F_WRLCK, l->start, l->len);
  return NULL;
}

/* Take an exclusive lock from another thread of this process */

static int sqltl_thread_lock(int fd, off_t start, off_t len)
{
  struct sqltl_locker_s l;
  pthread_t thread;
  int ret;

  l.fd    = fd;
  l.start = start;
  l.len   = len;
  l.err   = 0;

  ret = pthread_create(&thread, NULL, sqltl_locker, &l);
  if (ret != 0)
    {
      return ret;
    }

  pthread_join(thread, NULL);
  return l.err;
}

/* Run sqltl with argv[1..] as another process, return its exit code */

static int sqltl_child(FAR char *argv[])
{
  pid_t child;
  int status;

  argv[0] = CONFIG_TESTLAB_SQLTL_PROGNAME;
  if (posix_spawnp(&child, argv[0], NULL, NULL, argv, NULL) != 0 ||
      waitpid(child, &status, 0) != child || !WIFEXITED(status))
    {
      return SQLTL_PROBE_ERROR;
    }

  return WEXITSTATUS(status);
}

/* Run "sqltl lockprobe" as another process and return its exit code */

static int sqltl_probe(FAR const char *path, off_t start, off_t len,
                       pid_t pid)
{
  char sstart[16];
  char slen[16];
  char spid[16];
  FAR char *argv[7];

  snprintf(sstart, sizeof(sstart), "%ld", (long)start);
  snprintf(slen, sizeof(slen), "%ld", (long)len);
  snprintf(spid, sizeof(spid), "%d", (int)pid);

  argv[1] = "lockprobe";
  argv[2] = (FAR char *)path;
  argv[3] = sstart;
  argv[4] = slen;
  argv[5] = spid;
  argv[6] = NULL;
  return sqltl_child(argv);
}

/* Start worker w as "sqltl concw" in a task of its own */

static int sqltl_spawn(FAR struct sqltl_worker_s *w, FAR pid_t *pid)
{
  char sid[12];
  char stxns[24];
  char sstart[24];
  char stimeout[24];
  FAR char *argv[12];
  int argc = 0;

  snprintf(sid, sizeof(sid), "%d", w->id);
  snprintf(stxns, sizeof(stxns), "%ld", w->txns);
  snprintf(sstart, sizeof(sstart), "%" PRId64, w->start);
  snprintf(stimeout, sizeof(stimeout), "%ld",
           (long)(w->deadline - time(NULL)) + 1);

  argv[argc++] = CONFIG_TESTLAB_SQLTL_PROGNAME;
  argv[argc++] = "concw";
  argv[argc++] = (FAR char *)w->path;
  argv[argc++] = sid;
  argv[argc++] = stxns;
  argv[argc++] = sstart;
  argv[argc++] = "-t";
  argv[argc++] = stimeout;
  if (w->opts->sync != NULL)
    {
      argv[argc++] = "-s";
      argv[argc++] = (FAR char *)w->opts->sync;
    }

  if (w->opts->deferred)
    {
      argv[argc++] = "-d";
    }

  argv[argc] = NULL;
  return posix_spawnp(pid, argv[0], NULL, NULL, argv, NULL);
}

/* Run the workers as tasks; their result is the exit status */

static int sqltl_conc_tasks(FAR struct sqltl_worker_s *workers,
                            int nworkers)
{
  pid_t pids[SQLTL_MAXTHREADS];
  int created;
  int status;
  int ret = 0;
  int i;

  for (created = 0; created < nworkers; created++)
    {
      ret = sqltl_spawn(&workers[created], &pids[created]);
      if (ret != 0)
        {
          break;
        }
    }

  for (i = 0; i < created; i++)
    {
      if (waitpid(pids[i], &status, 0) != pids[i] || !WIFEXITED(status) ||
          WEXITSTATUS(status) != 0)
        {
          workers[i].rc   = SQLITE_ERROR;
          workers[i].step = "task";
        }
    }

  return created < nworkers ? ret : 0;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/* conc <db> <workers> <txns>: workers connections, threads of this task or
 * tasks of their own with -p, commit txns read-modify-write transactions
 * each, retrying on SQLITE_BUSY until -t seconds have passed.
 */

int sqltl_conc(FAR const char *path, int nthreads, long txns,
               FAR const struct sqltl_opts_s *opts)
{
  struct sqltl_worker_s workers[SQLTL_MAXTHREADS];
  FAR struct sqltl_worker_s *w;
  pthread_attr_t attr;
  FAR sqlite3 *db = NULL;
  time_t deadline;
  int64_t start;
  long busy = 0;
  int created;
  int ret = 0;
  int rc;
  int i;

  if (nthreads <= 0 || nthreads > SQLTL_MAXTHREADS || txns <= 0)
    {
      return -EINVAL;
    }

  rc = sqltl_open(path, opts, &db);
  if (rc == SQLITE_OK)
    {
      rc = sqltl_exec(db, SQLTL_CONC_SCHEMA);
    }

  if (rc != SQLITE_OK)
    {
      sqltl_fail("conc", "setup", db, rc);
      sqlite3_close(db);
      return -1;
    }

  sqlite3_close(db);

  memset(workers, 0, sizeof(workers));
  deadline = time(NULL) + opts->timeout;
  start    = sqltl_now_ms() +
             (opts->procs ? SQLTL_GATE_TASKS : SQLTL_GATE_THREADS);

  pthread_attr_init(&attr);
  pthread_attr_setstacksize(&attr, CONFIG_TESTLAB_SQLTL_STACKSIZE);

  for (i = 0; i < nthreads; i++)
    {
      w           = &workers[i];
      w->path     = path;
      w->opts     = opts;
      w->deadline = deadline;
      w->start    = start;
      w->id       = i + 1;
      w->txns     = txns;
    }

  if (opts->procs)
    {
      ret = sqltl_conc_tasks(workers, nthreads);
      created = ret == 0 ? nthreads : 0;
    }
  else
    {
      for (created = 0; created < nthreads; created++)
        {
          w   = &workers[created];
          ret = pthread_create(&w->thread, &attr, sqltl_worker, w);
          if (ret != 0)
            {
              break;
            }
        }

      for (i = 0; i < created; i++)
        {
          pthread_join(workers[i].thread, NULL);
        }
    }

  pthread_attr_destroy(&attr);

  if (created < nthreads)
    {
      printf("sqltl: FAIL conc step=%s errno=%d\n",
             opts->procs ? "posix_spawn" : "pthread_create", ret);
      return -1;
    }

  for (i = 0; i < nthreads; i++)
    {
      busy += workers[i].busy;
    }

  for (i = 0; i < nthreads; i++)
    {
      w = &workers[i];
      if (w->rc != SQLITE_OK)
        {
          printf("sqltl: FAIL conc w=%d step=%s rc=%d errno=%d done=%ld"
                 " busy=%ld\n", w->id, w->step, w->rc, w->sys, w->done,
                 busy);
          return -1;
        }
    }

  if (sqltl_conc_check(path, nthreads, txns, opts) < 0)
    {
      return -1;
    }

  printf("sqltl: PASS conc threads=%d txns=%ld busy=%ld\n", nthreads,
         txns, busy);
  return 0;
}

/* concw <db> <id> <txns> <start>: worker id of conc -p in this task,
 * starting at CLOCK_MONOTONIC ms start.  The result goes to the console,
 * the verdict is the exit status.
 */

int sqltl_concw(FAR const char *path, int id, long txns, int64_t start,
                FAR const struct sqltl_opts_s *opts)
{
  struct sqltl_worker_s w;

  memset(&w, 0, sizeof(w));
  w.path     = path;
  w.opts     = opts;
  w.deadline = time(NULL) + opts->timeout;
  w.start    = start;
  w.id       = id;
  w.txns     = txns;

  sqltl_worker(&w);
  if (w.rc != SQLITE_OK)
    {
      printf("sqltl: worker %d step=%s rc=%d errno=%d done=%ld busy=%ld\n",
             id, w.step, w.rc, w.sys, w.done, w.busy);
      return EXIT_FAILURE;
    }

  return EXIT_SUCCESS;
}

/* handoff <db>: a writer connection must be able to commit once the
 * reader connections of the process are done, with both in one thread
 * and with the writer in other threads.
 */

int sqltl_handoff(FAR const char *path, FAR const struct sqltl_opts_s *opts)
{
  FAR const char *step[2];
  FAR sqlite3 *db = NULL;
  int64_t nrows = 0;
  int64_t bad = 0;
  int64_t count = 0;
  int rc[2];
  int ret;
  int i;

  ret = sqltl_open(path, opts, &db);
  if (ret == SQLITE_OK)
    {
      ret = sqltl_exec(db, "CREATE TABLE IF NOT EXISTS h(x INTEGER);"
                           "DELETE FROM h");
    }

  sqlite3_close(db);
  db = NULL;
  if (ret != SQLITE_OK)
    {
      sqltl_fail("handoff", "setup", NULL, ret);
      return -1;
    }

  for (i = 0; i < 2; i++)
    {
      rc[i] = sqltl_handoff_run(path, opts, i == 1, &step[i]);
    }

  ret = sqltl_open(path, opts, &db);
  if (ret == SQLITE_OK)
    {
      ret = sqltl_check(db, &nrows, &bad);
    }

  if (ret == SQLITE_OK)
    {
      ret = sqltl_int(db, "SELECT count(*) FROM h", &count);
    }

  sqlite3_close(db);

  if (rc[0] != SQLITE_OK || rc[1] != SQLITE_OK || ret != SQLITE_OK ||
      count != 2)
    {
      printf("sqltl: FAIL handoff same=%s/%d thread=%s/%d check=%d"
             " rows=%" PRId64 "\n", step[0], rc[0], step[1], rc[1], ret,
             count);
      return -1;
    }

  printf("sqltl: PASS handoff\n");
  return 0;
}

/* visible <db>: a connection sees what another connection of the process
 * committed.  Five times: A counts the rows, B inserts one, A counts again
 * and reads the new row.  In WAL mode five more times with B restarting
 * the WAL first, so that its frames overwrite the ones A has read.
 */

int sqltl_visible(FAR const char *path, FAR const struct sqltl_opts_s *opts)
{
  FAR sqlite3_stmt *ins = NULL;
  FAR sqlite3_stmt *sel = NULL;
  FAR const void *data;
  FAR sqlite3 *a = NULL;
  FAR sqlite3 *b = NULL;
  FAR const char *step;
  uint8_t buf[256];
  int64_t before = 0;
  int64_t after = 0;
  bool wal;
  int ret = -1;
  int rc;
  int i;

  wal  = opts->journal != NULL && strcasecmp(opts->journal, "wal") == 0;
  step = "open";
  rc   = sqltl_open(path, opts, &a);
  if (rc == SQLITE_OK)
    {
      rc = sqltl_open(path, opts, &b);
    }

  if (rc == SQLITE_OK)
    {
      step = "prepare";
      rc   = sqlite3_prepare_v2(b, "INSERT INTO t VALUES(?, ?, ?)", -1,
                                &ins, NULL);
    }

  if (rc == SQLITE_OK)
    {
      rc = sqlite3_prepare_v2(a, "SELECT data FROM t WHERE id = ?", -1,
                              &sel, NULL);
    }

  for (i = 0; i < (wal ? 10 : 5) && rc == SQLITE_OK; i++)
    {
      step = "count";
      rc   = sqltl_int(a, "SELECT count(*) FROM t", &before);
      if (rc == SQLITE_OK && i >= 5)
        {
          step = "restart";
          rc   = sqltl_exec(b, "PRAGMA wal_checkpoint(RESTART)");
        }

      if (rc == SQLITE_OK)
        {
          step = "insert";
          rc   = sqltl_insert(ins, buf, before + 1, sizeof(buf));
        }

      if (rc == SQLITE_OK)
        {
          step = "recount";
          rc   = sqltl_int(a, "SELECT count(*) FROM t", &after);
        }

      if (rc == SQLITE_OK && after != before + 1)
        {
          printf("sqltl: FAIL visible round=%d before=%" PRId64
                 " after=%" PRId64 "\n", i, before, after);
          goto out;
        }

      if (rc == SQLITE_OK)
        {
          step = "read";
          sqlite3_bind_int64(sel, 1, after);
          rc   = sqlite3_step(sel);
          data = sqlite3_column_blob(sel, 0);
          sqltl_fill(buf, sizeof(buf), after);
          if (rc == SQLITE_ROW && (data == NULL ||
              sqlite3_column_bytes(sel, 0) != sizeof(buf) ||
              memcmp(data, buf, sizeof(buf)) != 0))
            {
              printf("sqltl: FAIL visible round=%d row=%" PRId64 " bad\n",
                     i, after);
              sqlite3_reset(sel);
              goto out;
            }

          rc = rc == SQLITE_ROW ? SQLITE_OK : rc;
          sqlite3_reset(sel);
        }
    }

  if (rc != SQLITE_OK)
    {
      sqltl_fail("visible", step, a, rc);
      goto out;
    }

  printf("sqltl: PASS visible rows=%" PRId64 "\n", after);
  ret = 0;

out:
  sqlite3_finalize(ins);
  sqlite3_finalize(sel);
  sqlite3_close(b);
  sqlite3_close(a);
  return ret;
}

/* lock <file>: the POSIX record lock semantics that the SQLite unix VFS
 * relies on.  Locks belong to the process: a second descriptor or another
 * thread of the process changes its own lock instead of conflicting with
 * it, another process sees the lock with the process ID and cannot take
 * it, and closing any descriptor of the file releases all the process
 * locks on it.
 */

int sqltl_lock(FAR const char *path)
{
  char data[24];
  int fd1;
  int fd2;
  int efd;
  int ethr;
  int spid;
  int speer;
  int sclose;
  int ret;

  memset(data, 'L', sizeof(data));
  fd1 = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
  if (fd1 < 0 || write(fd1, data, sizeof(data)) != sizeof(data))
    {
      printf("sqltl: FAIL lock step=open errno=%d\n", errno);
      if (fd1 >= 0)
        {
          close(fd1);
        }

      return -1;
    }

  fd2 = open(path, O_RDWR);
  ret = sqltl_setlk(fd1, F_RDLCK, 0, 8);
  if (fd2 < 0 || ret != 0)
    {
      printf("sqltl: FAIL lock step=rdlck errno=%d\n",
             fd2 < 0 ? errno : ret);
      close(fd1);
      if (fd2 >= 0)
        {
          close(fd2);
        }

      return -1;
    }

  /* The shared lock of fd1 becomes exclusive through fd2 */

  efd = sqltl_setlk(fd2, F_WRLCK, 0, 8);

  /* Another thread replaces it again, then locks [16, 24) */

  ethr = sqltl_thread_lock(fd1, 0, 8);
  ret  = sqltl_thread_lock(fd1, 16, 8);
  if (ret != 0)
    {
      printf("sqltl: FAIL lock step=thread errno=%d\n", ret);
      close(fd1);
      close(fd2);
      return -1;
    }

  /* Another process sees both locks held by this process */

  spid  = sqltl_probe(path, 16, 8, getpid());
  speer = sqltl_probe(path, 0, 8, getpid());

  /* Closing fd2 drops every lock of the process on the file */

  close(fd2);
  sclose = sqltl_probe(path, 0, 24, 0);
  close(fd1);
  unlink(path);

  if (efd != 0 || ethr != 0 || spid != SQLTL_PROBE_OK ||
      speer != SQLTL_PROBE_OK || sclose != SQLTL_PROBE_OK)
    {
      printf("sqltl: FAIL lock fd=%d thread=%d pid=%d peer=%d close=%d\n",
             efd, ethr, spid, speer, sclose);
      return -1;
    }

  printf("sqltl: PASS lock\n");
  return 0;
}

/* shm <file>: the shared file mapping that the SQLite WAL index relies on.
 * This process maps the file shared and stores a byte, another process
 * maps it and must see the byte, stores one itself and this process must
 * see that one.
 */

int sqltl_shm(FAR const char *path)
{
  FAR volatile uint8_t *map;
  FAR char *argv[4];
  uint8_t page[SQLTL_SHM_SIZE];
  int peer;
  int fd;

  memset(page, 0, sizeof(page));
  fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
  if (fd < 0 || write(fd, page, sizeof(page)) != sizeof(page))
    {
      printf("sqltl: FAIL shm step=open errno=%d\n", errno);
      if (fd >= 0)
        {
          close(fd);
        }

      return -1;
    }

  map = mmap(NULL, sizeof(page), PROT_READ | PROT_WRITE, MAP_SHARED, fd,
             0);
  if (map == MAP_FAILED)
    {
      printf("sqltl: FAIL shm step=mmap errno=%d\n", errno);
      close(fd);
      return -1;
    }

  map[0] = SQLTL_SHM_MINE;

  argv[1] = "shmprobe";
  argv[2] = (FAR char *)path;
  argv[3] = NULL;
  peer = sqltl_child(argv);

  if (peer != SQLTL_PROBE_OK || map[1] != SQLTL_SHM_PEER)
    {
      printf("sqltl: FAIL shm peer=%d seen=%d\n", peer, map[1]);
      munmap((FAR void *)map, sizeof(page));
      close(fd);
      return -1;
    }

  munmap((FAR void *)map, sizeof(page));
  close(fd);
  unlink(path);
  printf("sqltl: PASS shm\n");
  return 0;
}

/* shmprobe <file>: run by shm as another process.  Returns
 * SQLTL_PROBE_UNLOCK if the byte of shm is not seen through the mapping.
 */

int sqltl_shmprobe(FAR const char *path)
{
  FAR volatile uint8_t *map;
  int ret = SQLTL_PROBE_OK;
  int fd;

  fd = open(path, O_RDWR);
  if (fd < 0)
    {
      return SQLTL_PROBE_ERROR;
    }

  map = mmap(NULL, SQLTL_SHM_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
             0);
  if (map == MAP_FAILED)
    {
      close(fd);
      return SQLTL_PROBE_ERROR;
    }

  if (map[0] != SQLTL_SHM_MINE)
    {
      ret = SQLTL_PROBE_UNLOCK;
    }

  map[1] = SQLTL_SHM_PEER;
  munmap((FAR void *)map, SQLTL_SHM_SIZE);
  close(fd);
  return ret;
}

/* lockprobe <file> <start> <len> <pid>: run by lock as another process.
 * pid > 0: [start, start + len) must be locked by pid and F_SETLK of an
 * exclusive lock must fail with EAGAIN or EACCES.  pid 0: it must not be
 * locked.  Returns the SQLTL_PROBE_* exit code, prints nothing.
 */

int sqltl_lockprobe(FAR const char *path, off_t start, off_t len,
                    pid_t pid)
{
  struct flock fl;
  int ret = SQLTL_PROBE_OK;
  int err;
  int fd;

  fd = open(path, O_RDWR);
  if (fd < 0)
    {
      return SQLTL_PROBE_ERROR;
    }

  memset(&fl, 0, sizeof(fl));
  fl.l_type   = F_WRLCK;
  fl.l_whence = SEEK_SET;
  fl.l_start  = start;
  fl.l_len    = len;

  if (fcntl(fd, F_GETLK, &fl) < 0)
    {
      ret = SQLTL_PROBE_ERROR;
    }
  else if (pid == 0)
    {
      ret = fl.l_type == F_UNLCK ? SQLTL_PROBE_OK : SQLTL_PROBE_LOCKED;
    }
  else if (fl.l_type == F_UNLCK)
    {
      ret = SQLTL_PROBE_UNLOCK;
    }
  else if (fl.l_pid != pid)
    {
      ret = SQLTL_PROBE_PID;
    }
  else
    {
      err = sqltl_setlk(fd, F_WRLCK, start, len);
      if (err == 0)
        {
          ret = SQLTL_PROBE_SHARED;
        }
      else if (err != EAGAIN && err != EACCES)
        {
          ret = SQLTL_PROBE_ERROR;
        }
    }

  close(fd);
  return ret;
}
