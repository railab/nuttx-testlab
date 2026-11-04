/****************************************************************************
 * apps/hwtl/hwtl_main.c
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

#include <sys/ioctl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <time.h>

#ifdef CONFIG_TIMER
#  include <nuttx/timers/timer.h>
#endif

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define HWTL_DETAIL_MAX   96
#define HWTL_SIGNO        SIGUSR1

/* Helpers needed only by some subcommands */

#ifdef CONFIG_TIMER
#  define HWTL_HAVE_COUNT 1
#endif

#ifdef HWTL_HAVE_COUNT
#  define HWTL_HAVE_SIGWAIT 1
#endif

/****************************************************************************
 * Private Types
 ****************************************************************************/

/* Result of one check */

enum hwtl_res_e
{
  HWTL_PASS = 0,   /* Check passed */
  HWTL_FAIL,       /* Check ran and failed */
  HWTL_ERROR       /* Bad arguments or device error */
};

/* One subcommand: name, argument count range and check function */

typedef enum hwtl_res_e (*hwtl_check_t)(FAR char **argv, FAR char *detail);

struct hwtl_cmd_s
{
  FAR const char *name;
  FAR const char *usage;
  int             minargs;
  int             maxargs;
  hwtl_check_t    check;
};

/****************************************************************************
 * Private Function Prototypes
 ****************************************************************************/

static enum hwtl_res_e hwtl_timer(FAR char **argv, FAR char *detail);

/****************************************************************************
 * Private Data
 ****************************************************************************/

static const struct hwtl_cmd_s g_hwtl_cmds[] =
{
  {
    "timer", "<dev> <period_us> <ms>", 3, 3, hwtl_timer
  }
};

#define HWTL_NCMDS (sizeof(g_hwtl_cmds) / sizeof(g_hwtl_cmds[0]))

/****************************************************************************
 * Private Functions
 ****************************************************************************/

#ifdef HWTL_HAVE_COUNT
/* Milliseconds elapsed since start */

static long hwtl_elapsed_ms(FAR const struct timespec *start)
{
  struct timespec now;

  clock_gettime(CLOCK_MONOTONIC, &now);
  return (now.tv_sec - start->tv_sec) * 1000 +
         (now.tv_nsec - start->tv_nsec) / 1000000;
}

/* Count HWTL_SIGNO deliveries for ms milliseconds (signal blocked) */

static int hwtl_count_signals(int ms)
{
  struct timespec start;
  struct timespec ts;
  sigset_t set;
  int count = 0;
  long left;

  sigemptyset(&set);
  sigaddset(&set, HWTL_SIGNO);
  clock_gettime(CLOCK_MONOTONIC, &start);

  while ((left = ms - hwtl_elapsed_ms(&start)) > 0)
    {
      ts.tv_sec  = left / 1000;
      ts.tv_nsec = (left % 1000) * 1000000;

      if (sigtimedwait(&set, NULL, &ts) == HWTL_SIGNO)
        {
          count++;
        }
    }

  return count;
}
#endif

#ifdef HWTL_HAVE_SIGWAIT
/* Block HWTL_SIGNO, drop a pending one; restore with hwtl_sig_restore() */

static void hwtl_sig_block(FAR sigset_t *old)
{
  struct timespec ts;
  sigset_t set;

  sigemptyset(&set);
  sigaddset(&set, HWTL_SIGNO);
  sigprocmask(SIG_BLOCK, &set, old);

  ts.tv_sec  = 0;
  ts.tv_nsec = 0;
  sigtimedwait(&set, NULL, &ts);
}

/* Drop late signals (default action would end the task), then restore */

static void hwtl_sig_restore(FAR const sigset_t *old)
{
  struct timespec ts;
  sigset_t set;

  sigemptyset(&set);
  sigaddset(&set, HWTL_SIGNO);

  ts.tv_sec  = 0;
  ts.tv_nsec = 0;
  while (sigtimedwait(&set, NULL, &ts) == HWTL_SIGNO)
    {
    }

  sigprocmask(SIG_SETMASK, old, NULL);
}
#endif

/****************************************************************************
 * Timer devices
 ****************************************************************************/

/* Periodic /dev/timerN: expirations counted against CLOCK_MONOTONIC */

static enum hwtl_res_e hwtl_timer(FAR char **argv, FAR char *detail)
{
#ifdef CONFIG_TIMER
  struct timer_notify_s notify;
  sigset_t old;
  uint32_t period = strtoul(argv[1], NULL, 0);
  int ms = atoi(argv[2]);
  int expect;
  int count;
  int tol;
  int fd;

  fd = open(argv[0], O_RDONLY);
  if (fd < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "open %s errno=%d", argv[0], errno);
      return HWTL_ERROR;
    }

  hwtl_sig_block(&old);

  memset(&notify, 0, sizeof(notify));
  notify.event.sigev_notify = SIGEV_SIGNAL;
  notify.event.sigev_signo  = HWTL_SIGNO;
  notify.pid                = getpid();
  notify.periodic           = true;

  if (ioctl(fd, TCIOC_SETTIMEOUT, (unsigned long)period) < 0 ||
      ioctl(fd, TCIOC_NOTIFICATION, (unsigned long)&notify) < 0 ||
      ioctl(fd, TCIOC_START, 0) < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "%s errno=%d", argv[0], errno);
      close(fd);
      hwtl_sig_restore(&old);
      return HWTL_ERROR;
    }

  count = hwtl_count_signals(ms);
  ioctl(fd, TCIOC_STOP, 0);
  close(fd);
  hwtl_sig_restore(&old);

  /* +-2 % and one period for the window edges */

  expect = (int)((uint64_t)ms * 1000 / period);
  tol    = expect / 50 + 1;

  snprintf(detail, HWTL_DETAIL_MAX, "%s period_us=%" PRIu32 " ms=%d "
           "count=%d expect=%d", argv[0], period, ms, count, expect);
  return (count >= expect - tol && count <= expect + tol) ?
         HWTL_PASS : HWTL_FAIL;
#else
  snprintf(detail, HWTL_DETAIL_MAX, "no CONFIG_TIMER");
  return HWTL_ERROR;
#endif
}

/****************************************************************************
 * Command dispatch
 ****************************************************************************/

static FAR const struct hwtl_cmd_s *hwtl_find(FAR const char *name)
{
  size_t i;

  for (i = 0; i < HWTL_NCMDS; i++)
    {
      if (strcmp(g_hwtl_cmds[i].name, name) == 0)
        {
          return &g_hwtl_cmds[i];
        }
    }

  return NULL;
}

static void hwtl_usage(void)
{
  size_t i;

  printf("Usage:\n");
  for (i = 0; i < HWTL_NCMDS; i++)
    {
      printf("       hwtl %s %s\n", g_hwtl_cmds[i].name,
             g_hwtl_cmds[i].usage);
    }
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  FAR const struct hwtl_cmd_s *cmd;
  FAR char *args[8];
  char detail[HWTL_DETAIL_MAX];
  enum hwtl_res_e res;
  int nargs;
  int i;

  if (argc < 2)
    {
      hwtl_usage();
      return EXIT_FAILURE;
    }

  cmd   = hwtl_find(argv[1]);
  nargs = argc - 2;
  if (cmd == NULL || nargs < cmd->minargs || nargs > cmd->maxargs)
    {
      hwtl_usage();
      return EXIT_FAILURE;
    }

  for (i = 0; i < nargs; i++)
    {
      args[i] = argv[i + 2];
    }

  for (; i < 8; i++)
    {
      args[i] = NULL;
    }

  detail[0] = '\0';
  res = cmd->check(args, detail);

  printf("hwtl: %s %s %s\n", res == HWTL_PASS ? "PASS" : "FAIL", cmd->name,
         detail);
  return res == HWTL_PASS ? EXIT_SUCCESS : EXIT_FAILURE;
}
