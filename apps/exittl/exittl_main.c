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

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define EXITTL_TIMEOUT_MS 5000
#define EXITTL_POLL_MS    10

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

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  bool alive = true;
  bool handler;
  int waited;
  pid_t pid;

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
