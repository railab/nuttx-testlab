/****************************************************************************
 * apps/ptptl/ptptl_main.c
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
#include <sched.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <sys/socket.h>
#include <sys/wait.h>

#include "netutils/ptpd.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define PTPTL_RUNTIME_DEFAULT  30
#define PTPTL_WAIT_DEFAULT     30
#define PTPTL_BOUND_DEFAULT    50000000  /* ns */
#define PTPTL_SYNCED_POLLS     3
#define PTPTL_TX_RECENT_MS     5000
#define PTPTL_IFACE_DEFAULT    "eth0"
#define PTPTL_CLOCK_DEFAULT    "realtime"

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct ptptl_args_s
{
  int     runtime;   /* Seconds to run */
  int     wait;      /* Client: seconds to reach the bound */
  int64_t bound;     /* Client: |delta| bound in ns */
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

/* Read by the daemon task, so it must outlive ptpd_start() */

static struct ptpd_config_s g_config;

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/****************************************************************************
 * Name: ptptl_daemon
 *
 * Description:
 *   Task entry: ptpd_start() runs the daemon loop until ptpd_stop().
 *
 ****************************************************************************/

static int ptptl_daemon(int argc, FAR char *argv[])
{
  return ptpd_start(&g_config) < 0 ? EXIT_FAILURE : EXIT_SUCCESS;
}

/****************************************************************************
 * Name: ptptl_age_ms
 *
 * Description:
 *   Milliseconds from a CLOCK_MONOTONIC stamp to now, -1 if never set.
 *
 ****************************************************************************/

static long ptptl_age_ms(FAR const struct timespec *now,
                         FAR const struct timespec *ts)
{
  if (ts->tv_sec == 0 && ts->tv_nsec == 0)
    {
      return -1;
    }

  return (now->tv_sec - ts->tv_sec) * 1000 +
         (now->tv_nsec - ts->tv_nsec) / 1000000;
}

/****************************************************************************
 * Name: ptptl_hexid
 *
 * Description:
 *   Format a clock identity as 16 hex digits.
 *
 ****************************************************************************/

static FAR const char *ptptl_hexid(FAR const uint8_t *id, FAR char *buf)
{
  int i;

  for (i = 0; i < 8; i++)
    {
      snprintf(buf + 2 * i, 3, "%02x", id[i]);
    }

  return buf;
}

/****************************************************************************
 * Name: ptptl_print_status
 ****************************************************************************/

static void ptptl_print_status(int t, FAR const struct ptpd_status_s *st)
{
  struct timespec now;
  struct timespec real;
  char gm[17];

  clock_gettime(CLOCK_MONOTONIC, &now);
  clock_gettime(CLOCK_REALTIME, &real);

  printf("ptptl: status t=%d valid=%d gm=%s p1=%d class=%d steps=%d "
         "delta=%" PRId64 " adj=%" PRId64 " drift=%ld path=%ld "
         "ann=%ld sync=%ld dreq=%ld pdreq=%ld txsync=%ld txann=%ld "
         "now=%lld.%09ld\n",
         t, st->clock_source_valid ? 1 : 0,
         ptptl_hexid(st->clock_source_info.gm_id, gm),
         st->clock_source_info.priority1,
         st->clock_source_info.clockclass,
         st->clock_source_info.stepsremoved,
         st->last_delta_ns, st->last_adjtime_ns, st->drift_ppb,
         st->path_delay_ns,
         ptptl_age_ms(&now, &st->last_received_announce),
         ptptl_age_ms(&now, &st->last_received_sync),
         ptptl_age_ms(&now, &st->last_transmitted_delayreq),
         ptptl_age_ms(&now, &st->last_transmitted_pdelayreq),
         ptptl_age_ms(&now, &st->last_transmitted_sync),
         ptptl_age_ms(&now, &st->last_transmitted_announce),
         (long long)real.tv_sec, real.tv_nsec);
}

/****************************************************************************
 * Name: ptptl_usage
 ****************************************************************************/

static void ptptl_usage(void)
{
  printf("usage: ptptl [-c] [-i IFACE] [-d none|e2e|p2p] [-r] [-k CLOCK]"
         " [-t SEC] [-w SEC] [-o NS]\n");
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/****************************************************************************
 * Name: main
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  struct ptptl_args_s args;
  struct ptpd_status_s st;
  struct timespec now;
  bool master_ok = false;
  bool ok;
  int synced = 0;
  int first = -1;
  int status = 0;
  int stop;
  int pid;
  int ret;
  int opt;
  int t;

  memset(&g_config, 0, sizeof(g_config));
  g_config.interface = PTPTL_IFACE_DEFAULT;
  g_config.clock = PTPTL_CLOCK_DEFAULT;
  g_config.delay_mechanism = PTP_DELAY_E2E;
  g_config.bmca = true;
  g_config.af = AF_INET;

  args.runtime = PTPTL_RUNTIME_DEFAULT;
  args.wait = PTPTL_WAIT_DEFAULT;
  args.bound = PTPTL_BOUND_DEFAULT;

  while ((opt = getopt(argc, argv, "ci:d:rk:t:w:o:h")) != -1)
    {
      switch (opt)
        {
          case 'c':
            g_config.client_only = true;
            break;
          case 'i':
            g_config.interface = optarg;
            break;
          case 'd':
            if (strcmp(optarg, "none") == 0)
              {
                g_config.delay_mechanism = PTP_DELAY_NONE;
              }
            else if (strcmp(optarg, "e2e") == 0)
              {
                g_config.delay_mechanism = PTP_DELAY_E2E;
              }
            else if (strcmp(optarg, "p2p") == 0)
              {
                g_config.delay_mechanism = PTP_DELAY_P2P;
              }
            else
              {
                ptptl_usage();
                return EXIT_FAILURE;
              }
            break;
          case 'r':
            g_config.af = AF_PACKET;
            break;
          case 'k':
            g_config.clock = optarg;
            break;
          case 't':
            args.runtime = atoi(optarg);
            break;
          case 'w':
            args.wait = atoi(optarg);
            break;
          case 'o':
            args.bound = strtoll(optarg, NULL, 10);
            break;
          default:
            ptptl_usage();
            return opt == 'h' ? EXIT_SUCCESS : EXIT_FAILURE;
        }
    }

  pid = task_create("ptpd", CONFIG_TESTLAB_PTPTL_PRIORITY,
                    CONFIG_TESTLAB_PTPTL_DAEMON_STACKSIZE,
                    ptptl_daemon, NULL);
  if (pid < 0)
    {
      printf("ptptl: FAIL task_create errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  printf("ptptl: started pid=%d\n", pid);

  for (t = 1; t <= args.runtime; t++)
    {
      sleep(1);

      if (waitpid(pid, &status, WNOHANG) == pid)
        {
          printf("ptptl: FAIL daemon exited status=%d\n",
                 WEXITSTATUS(status));
          return EXIT_FAILURE;
        }

      ret = ptpd_status(pid, &st);
      if (ret < 0)
        {
          printf("ptptl: status t=%d error=%d\n", t, -ret);
          continue;
        }

      ptptl_print_status(t, &st);

      if (g_config.client_only)
        {
          if (st.clock_source_valid &&
              llabs((long long)st.last_delta_ns) <= args.bound)
            {
              if (++synced >= PTPTL_SYNCED_POLLS && first < 0)
                {
                  first = t;
                }
            }
          else
            {
              synced = 0;
            }
        }
      else
        {
          clock_gettime(CLOCK_MONOTONIC, &now);
          master_ok = st.clock_source_valid ||
            (ptptl_age_ms(&now, &st.last_transmitted_sync) >= 0 &&
             ptptl_age_ms(&now, &st.last_transmitted_sync)
               < PTPTL_TX_RECENT_MS &&
             ptptl_age_ms(&now, &st.last_transmitted_announce) >= 0 &&
             ptptl_age_ms(&now, &st.last_transmitted_announce)
               < PTPTL_TX_RECENT_MS);
        }
    }

  stop = ptpd_stop(pid);
  if (stop == 0 && waitpid(pid, &status, 0) != pid)
    {
      stop = -errno;
    }

  if (g_config.client_only)
    {
      ok = first >= 0 && first <= args.wait;
    }
  else
    {
      ok = master_ok;
    }

  printf("ptptl: %s synced=%d first=%d stop=%d exit=%d\n",
         ok && stop == 0 ? "PASS" : "FAIL", first >= 0 ? 1 : 0, first,
         stop, WEXITSTATUS(status));

  return ok && stop == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
