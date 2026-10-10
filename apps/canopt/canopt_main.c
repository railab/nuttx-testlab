/****************************************************************************
 * apps/canopt/canopt_main.c
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
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "canopt.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define CANOPT_COUNT_DEFAULT    10
#define CANOPT_TIMEOUT_DEFAULT  10

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void canopt_usage(FAR const char *progname)
{
  fprintf(stderr,
          "Usage: %s <check> <ifname|/dev/canN> [options]\n"
          "SocketCAN checks: sockopt optbits rcvbuf rcvtimeo rx tx fdrx\n"
          "  legacy ts loopback poll hup\n"
          "Character driver checks: crx ctx cioctl calign cnonblock\n"
          "  coverflow cfionread ciflush crtr\n"
          "  -F id/mask  CAN_RAW_FILTER entry, hex (repeatable)\n"
          "  -Z          empty CAN_RAW_FILTER list\n"
          "  -M          CONFIG_NET_CAN_RAW_FILTER_MAX filters\n"
          "  -f          CAN FD socket\n"
          "  -q          let frames queue before reading\n"
          "  -m mode     timestamp mode: none, us or ns\n"
          "  -n count    frame count\n"
          "  -g ms       host frame gap\n"
          "  -l 0|1      CAN_RAW_LOOPBACK\n"
          "  -o 0|1      CAN_RAW_RECV_OWN_MSGS\n"
          "  -t sec      timeout\n",
          progname);
}

static bool canopt_parse_int(FAR const char *arg, int minval, int maxval,
                             FAR int *val)
{
  FAR char *end;
  long parsed;

  errno = 0;
  parsed = strtol(arg, &end, 0);
  if (errno != 0 || end == arg || *end != '\0' ||
      parsed < minval || parsed > maxval)
    {
      return false;
    }

  *val = (int)parsed;
  return true;
}

static bool canopt_parse_filter(FAR const char *arg,
                                FAR struct canopt_args_s *args)
{
  FAR char *end;
  unsigned long id;
  unsigned long mask;

  if (args->nfilters >= CANOPT_MAX_FILTERS)
    {
      return false;
    }

  errno = 0;
  id = strtoul(arg, &end, 16);
  if (errno != 0 || end == arg || *end != '/')
    {
      return false;
    }

  arg = end + 1;
  mask = strtoul(arg, &end, 16);
  if (errno != 0 || end == arg || *end != '\0')
    {
      return false;
    }

  args->filt_id[args->nfilters]   = (uint32_t)id;
  args->filt_mask[args->nfilters] = (uint32_t)mask;
  args->nfilters++;
  return true;
}

static bool canopt_parse_mode(FAR const char *arg, FAR int *mode)
{
  if (strcmp(arg, "none") == 0)
    {
      *mode = CANOPT_TS_NONE;
    }
  else if (strcmp(arg, "us") == 0)
    {
      *mode = CANOPT_TS_US;
    }
  else if (strcmp(arg, "ns") == 0)
    {
      *mode = CANOPT_TS_NS;
    }
  else
    {
      return false;
    }

  return true;
}

static bool canopt_parse_opts(int argc, FAR char *argv[],
                              FAR struct canopt_args_s *args)
{
  int opt;

  while ((opt = getopt(argc, argv, "F:ZMfqm:n:g:l:o:t:")) != ERROR)
    {
      bool ok = true;

      switch (opt)
        {
          case 'F':
            ok = canopt_parse_filter(optarg, args);
            break;
          case 'Z':
            args->nofilter = true;
            break;
          case 'M':
            args->maxfilter = true;
            break;
          case 'f':
            args->fd = true;
            break;
          case 'q':
            args->queued = true;
            break;
          case 'm':
            ok = canopt_parse_mode(optarg, &args->mode);
            break;
          case 'n':
            ok = canopt_parse_int(optarg, 1, 1000, &args->count);
            break;
          case 'g':
            ok = canopt_parse_int(optarg, 0, 10000, &args->gap_ms);
            break;
          case 'l':
            ok = canopt_parse_int(optarg, 0, 1, &args->loopback);
            break;
          case 'o':
            ok = canopt_parse_int(optarg, 0, 1, &args->recv_own);
            break;
          case 't':
            ok = canopt_parse_int(optarg, 1, 600, &args->timeout);
            break;
          default:
            ok = false;
            break;
        }

      if (!ok)
        {
          return false;
        }
    }

  return optind == argc;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  struct canopt_args_s args;

  if (argc < 3)
    {
      canopt_usage(argv[0]);
      return EXIT_FAILURE;
    }

  memset(&args, 0, sizeof(args));
  args.cmd      = argv[1];
  args.endpoint = argv[2];
  args.mode     = CANOPT_TS_US;
  args.count    = CANOPT_COUNT_DEFAULT;
  args.timeout  = CANOPT_TIMEOUT_DEFAULT;

  /* Options follow the check and endpoint: argv[2] acts as argv[0] */

  optind = 1;
  if (!canopt_parse_opts(argc - 2, argv + 2, &args))
    {
      canopt_usage(argv[0]);
      return EXIT_FAILURE;
    }

  if (args.endpoint[0] == '/')
    {
#ifdef CONFIG_CAN
      return canopt_char_main(&args);
#endif
    }
  else
    {
#ifdef CONFIG_NET_CAN
      return canopt_sock_main(&args);
#endif
    }

  printf("canopt: FAIL %s backend not built\n", args.cmd);
  return EXIT_FAILURE;
}
