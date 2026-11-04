/****************************************************************************
 * apps/exittl/exittl_main.c
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

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <semaphore.h>
#include <signal.h>
#include <time.h>
#include <sys/wait.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define EXITTL_TIMEOUT_MS 5000
#define EXITTL_POLL_MS    10

/* Run times of the two children of a wait check */

#define EXITTL_SHORT_MS   200
#define EXITTL_LONG_MS    600

/* Exit status of the child whose status is kept */

#define EXITTL_STATUS     7

/****************************************************************************
 * Private Data
 ****************************************************************************/

/* The child task shares these with the parent (flat build) */

static volatile sig_atomic_t g_exittl_handler;
static sem_t g_exittl_ready;

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/* SIGTERM handler of the child's second thread: report that it ran */

static void exittl_sigterm(int signo)
{
  g_exittl_handler = 1;
}

/* Child's second thread: catch SIGTERM and wait for signals forever */

static FAR void *exittl_thread(FAR void *arg)
{
  struct sigaction act;

  memset(&act, 0, sizeof(act));
  act.sa_handler = exittl_sigterm;
  sigemptyset(&act.sa_mask);
  sigaction(SIGTERM, &act, NULL);

  sem_post(&g_exittl_ready);

  for (; ; )
    {
      pause();
    }

  return NULL;
}

/* Child task: start the second thread, then exit() from the main thread */

static int exittl_child(int argc, FAR char *argv[])
{
  pthread_t thread;

  if (pthread_create(&thread, NULL, exittl_thread, NULL) != 0)
    {
      exit(EXIT_FAILURE);
    }

  while (sem_wait(&g_exittl_ready) < 0 && errno == EINTR)
    {
    }

  exit(EXIT_SUCCESS);
}

#ifdef CONFIG_SCHED_WAITPID
/* Child task of a wait check: sleep argv[1] ms, exit with argv[2] */

static int exittl_sleeper(int argc, FAR char *argv[])
{
  usleep(atoi(argv[1]) * 1000);
  return atoi(argv[2]);
}

static pid_t exittl_start(int ms, int status)
{
  char msbuf[12];
  char stbuf[12];
  FAR char *args[3];

  snprintf(msbuf, sizeof(msbuf), "%d", ms);
  snprintf(stbuf, sizeof(stbuf), "%d", status);
  args[0] = msbuf;
  args[1] = stbuf;
  args[2] = NULL;

  return task_create("exittl_sleeper", CONFIG_TESTLAB_EXITTL_PRIORITY,
                     CONFIG_TESTLAB_EXITTL_STACKSIZE, exittl_sleeper, args);
}

static void exittl_sigchld(_sa_handler_t handler, int flags)
{
  struct sigaction act;

  memset(&act, 0, sizeof(act));
  act.sa_handler = handler;
  act.sa_flags   = flags;
  sigemptyset(&act.sa_mask);
  sigaction(SIGCHLD, &act, NULL);
}

/* With the child status not kept, waitpid(-1) must block until both
 * children ended, then fail with ECHILD.  Returns true on success.
 */

static bool exittl_nowait(FAR const char *name)
{
  struct timespec start;
  struct timespec end;
  long elapsed;
  int status;
  pid_t ret;
  int err;

  clock_gettime(CLOCK_MONOTONIC, &start);
  if (exittl_start(EXITTL_SHORT_MS, 0) < 0 ||
      exittl_start(EXITTL_LONG_MS, 0) < 0)
    {
      printf("exittl: %s task_create %d\n", name, errno);
      return false;
    }

  do
    {
      ret = waitpid(-1, &status, 0);
      err = errno;
    }
  while (ret < 0 && err == EINTR);

  clock_gettime(CLOCK_MONOTONIC, &end);
  elapsed = (end.tv_sec - start.tv_sec) * 1000 +
            (end.tv_nsec - start.tv_nsec) / 1000000;

  printf("exittl: %s waitpid=%d errno=%d ms=%ld\n", name, (int)ret,
         ret < 0 ? err : 0, elapsed);
  return ret < 0 && err == ECHILD && elapsed >= EXITTL_LONG_MS;
}

/* With the default action the status of a child is kept again */

static bool exittl_keep(void)
{
  int status = 0;
  pid_t pid;
  pid_t ret;

  pid = exittl_start(EXITTL_SHORT_MS, EXITTL_STATUS);
  if (pid < 0)
    {
      printf("exittl: keep task_create %d\n", errno);
      return false;
    }

  do
    {
      ret = waitpid(pid, &status, 0);
    }
  while (ret < 0 && errno == EINTR);

  printf("exittl: keep waitpid=%d pid=%d status=%d\n", (int)ret, (int)pid,
         ret == pid && WIFEXITED(status) ? WEXITSTATUS(status) : -1);
  return ret == pid && WIFEXITED(status) &&
         WEXITSTATUS(status) == EXITTL_STATUS;
}

/* wait: SA_NOCLDWAIT and SIGCHLD set to SIG_IGN, each followed by the
 * default action again
 */

static int exittl_wait(void)
{
  bool pass;

  exittl_sigchld(SIG_DFL, SA_NOCLDWAIT);
  pass = exittl_nowait("nocldwait");
  exittl_sigchld(SIG_DFL, 0);
  pass = exittl_keep() && pass;

#ifdef CONFIG_SIG_DEFAULT
  exittl_sigchld(SIG_IGN, 0);
  pass = exittl_nowait("ignore") && pass;
  exittl_sigchld(SIG_DFL, 0);
  pass = exittl_keep() && pass;
#endif

  printf("exittl: %s wait\n", pass ? "PASS" : "FAIL");
  return pass ? EXIT_SUCCESS : EXIT_FAILURE;
}
#endif /* CONFIG_SCHED_WAITPID */

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  bool alive = true;
  bool handler;
  int waited;
  pid_t pid;

#ifdef CONFIG_SCHED_WAITPID
  if (argc == 2 && strcmp(argv[1], "wait") == 0)
    {
      return exittl_wait();
    }
#endif

  g_exittl_handler = 0;
  sem_init(&g_exittl_ready, 0, 0);

  pid = task_create("exittl_child", CONFIG_TESTLAB_EXITTL_PRIORITY,
                    CONFIG_TESTLAB_EXITTL_STACKSIZE, exittl_child, NULL);
  if (pid < 0)
    {
      printf("exittl: FAIL task_create %d\n", errno);
      return EXIT_FAILURE;
    }

  /* The child task must be gone within the timeout */

  for (waited = 0; waited < EXITTL_TIMEOUT_MS; waited += EXITTL_POLL_MS)
    {
      if (kill(pid, 0) < 0 && errno == ESRCH)
        {
          alive = false;
          break;
        }

      usleep(EXITTL_POLL_MS * 1000);
    }

  /* The SIGTERM handler of the second thread must not have run */

  handler = g_exittl_handler != 0;

  if (alive || handler)
    {
      printf("exittl: FAIL pid=%d alive=%d handler=%d\n",
             (int)pid, alive, handler);
      return EXIT_FAILURE;
    }

  printf("exittl: PASS pid=%d\n", (int)pid);
  return EXIT_SUCCESS;
}
