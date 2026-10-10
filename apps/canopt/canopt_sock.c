/****************************************************************************
 * apps/canopt/canopt_sock.c
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
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>

#include <net/if.h>
#include <nuttx/can.h>

#include "canopt.h"

#ifdef CONFIG_NET_CAN

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define CANOPT_EXACT_MASK  (CAN_EFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG)
#define CANOPT_LOOP_ID     0x555
#define CANOPT_POLL_TX_ID  0x5a0
#define CANOPT_POLL_RX1_ID 0x5a1
#define CANOPT_POLL_RX2_ID 0x5a2
#define CANOPT_RCVBUF      2048
#define CANOPT_RCVTIMEO_MS 300

#ifndef CONFIG_NET_CAN_RAW_FILTER_MAX
#  define CONFIG_NET_CAN_RAW_FILTER_MAX 1
#endif

/****************************************************************************
 * Private Types
 ****************************************************************************/

/* Receive statistics of one check */

struct canopt_rxstat_s
{
  int  classic;     /* Classic table frames received intact */
  int  fd;          /* CAN FD table frames received intact */
  int  err;         /* Unknown, corrupt or wrongly sized frames */
  bool marker;      /* End marker seen */
  char ids[CANOPT_IDS_LEN];
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static int canopt_fail(FAR const char *cmd, FAR const char *what)
{
  printf("canopt: FAIL %s %s errno=%d\n", cmd, what, errno);
  return EXIT_FAILURE;
}

static int canopt_sock_open(FAR const char *ifname, bool fd)
{
  struct sockaddr_can addr;
  int enable = 1;
  int sd;

  sd = socket(PF_CAN, SOCK_RAW, CAN_RAW);
  if (sd < 0)
    {
      return -1;
    }

  if (fd && setsockopt(sd, SOL_CAN_RAW, CAN_RAW_FD_FRAMES, &enable,
                       sizeof(enable)) < 0)
    {
      close(sd);
      return -1;
    }

  memset(&addr, 0, sizeof(addr));
  addr.can_family  = AF_CAN;
  addr.can_ifindex = if_nametoindex(ifname);
  if (addr.can_ifindex == 0 ||
      bind(sd, (FAR struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
      close(sd);
      return -1;
    }

  return sd;
}

static int canopt_setint(int sd, int level, int option, int val)
{
  return setsockopt(sd, level, option, &val, sizeof(val));
}

static int canopt_getint(int sd, int level, int option, FAR int *val)
{
  socklen_t len = sizeof(*val);

  *val = -1;
  if (getsockopt(sd, level, option, val, &len) < 0)
    {
      return -1;
    }

  return len == sizeof(*val) ? 0 : -1;
}

/* Set an int option and read it back */

static bool canopt_roundtrip(int sd, int level, int option, int val)
{
  int got;

  return canopt_setint(sd, level, option, val) == 0 &&
         canopt_getint(sd, level, option, &got) == 0 && got == val;
}

/* Check one received frame against the frame tables */

static void canopt_rx_check(FAR const struct canfd_frame *frame,
                            ssize_t nbytes,
                            FAR struct canopt_rxstat_s *st)
{
  FAR const struct canopt_frame_s *entry;
  ssize_t mtu;

  if (frame->can_id == CANOPT_MARKER_ID || frame->can_id == CANOPT_KICK_ID)
    {
      return;
    }

  canopt_ids_add(st->ids, frame->can_id);

  entry = canopt_find(frame->can_id);
  if (entry == NULL)
    {
      st->err++;
      return;
    }

  mtu = entry->fd ? CANFD_MTU : CAN_MTU;
  if (nbytes != mtu || frame->len != entry->len ||
      !canopt_data_ok(frame->data, frame->can_id, frame->len) ||
      (entry->fd &&
       (frame->flags & (CANFD_BRS | CANFD_ESI)) != entry->flags))
    {
      printf("canopt: bad frame id=%" PRIx32 " size=%zd len=%u "
             "flags=%x\n", frame->can_id, nbytes, frame->len,
             frame->flags);
      st->err++;
      return;
    }

  if (entry->fd)
    {
      st->fd++;
    }
  else
    {
      st->classic++;
    }
}

/* Install the -F / -Z / -M receive filters */

static int canopt_set_filters(int sd, FAR struct canopt_args_s *args)
{
  struct can_filter filters[CONFIG_NET_CAN_RAW_FILTER_MAX];
  int count = 0;
  int i;

  if (args->maxfilter)
    {
      /* All slots but the last one match unused IDs, the last one
       * matches table frame 0x105 only.
       */

      for (i = 0; i < CONFIG_NET_CAN_RAW_FILTER_MAX - 1; i++)
        {
          filters[i].can_id   = 0x600 + i;
          filters[i].can_mask = CANOPT_EXACT_MASK;
        }

      filters[i].can_id   = 0x105;
      filters[i].can_mask = CANOPT_EXACT_MASK;
      count = CONFIG_NET_CAN_RAW_FILTER_MAX;
    }
  else if (args->nfilters > 0)
    {
      if (args->nfilters > CONFIG_NET_CAN_RAW_FILTER_MAX)
        {
          errno = EINVAL;
          return -1;
        }

      for (i = 0; i < args->nfilters; i++)
        {
          filters[i].can_id   = args->filt_id[i];
          filters[i].can_mask = args->filt_mask[i];
        }

      count = args->nfilters;
    }
  else if (!args->nofilter)
    {
      return 0;
    }

  return setsockopt(sd, SOL_CAN_RAW, CAN_RAW_FILTER, filters,
                    count * sizeof(struct can_filter));
}

/****************************************************************************
 * Name: canopt_sockopt
 *
 * Description:
 *   setsockopt()/getsockopt() round trips and argument validation.
 *
 ****************************************************************************/

#ifdef CONFIG_NET_CANPROTO_OPTIONS
static int canopt_sockopt(FAR struct canopt_args_s *args)
{
  static const int boolopts[] =
  {
    CAN_RAW_LOOPBACK, CAN_RAW_RECV_OWN_MSGS,
#ifdef CONFIG_NET_CAN_CANFD
    CAN_RAW_FD_FRAMES,
#endif
  };

  struct can_filter set[CONFIG_NET_CAN_RAW_FILTER_MAX + 1];
  struct can_filter get[CONFIG_NET_CAN_RAW_FILTER_MAX];
  FAR const char *cmd = args->cmd;
  socklen_t len;
  int checks = 0;
  int val;
  int sd;
  int i;

  sd = canopt_sock_open(args->endpoint, false);
  if (sd < 0)
    {
      return canopt_fail(cmd, "open");
    }

  /* Default filter list: a single catch-all filter */

  len = sizeof(get);
  memset(get, 0xff, sizeof(get));
  if (getsockopt(sd, SOL_CAN_RAW, CAN_RAW_FILTER, get, &len) < 0 ||
      len != sizeof(struct can_filter) || get[0].can_id != 0 ||
      get[0].can_mask != 0)
    {
      return canopt_fail(cmd, "filter-default");
    }

  checks++;

  /* Maximum filter count round trip */

  for (i = 0; i <= CONFIG_NET_CAN_RAW_FILTER_MAX; i++)
    {
      set[i].can_id   = (i & 1) ? (CAN_EFF_FLAG | (0x10000 + i)) : i;
      set[i].can_mask = (i & 1) ? CANOPT_EXACT_MASK : CAN_SFF_MASK;
    }

  len = sizeof(get);
  if (setsockopt(sd, SOL_CAN_RAW, CAN_RAW_FILTER, set,
                 CONFIG_NET_CAN_RAW_FILTER_MAX *
                 sizeof(struct can_filter)) < 0 ||
      getsockopt(sd, SOL_CAN_RAW, CAN_RAW_FILTER, get, &len) < 0 ||
      len != sizeof(get) || memcmp(set, get, sizeof(get)) != 0)
    {
      return canopt_fail(cmd, "filter-max");
    }

  checks++;

  /* Too many filters and a truncated filter are rejected */

  if (setsockopt(sd, SOL_CAN_RAW, CAN_RAW_FILTER, set, sizeof(set)) == 0 ||
      errno != EINVAL)
    {
      return canopt_fail(cmd, "filter-overflow");
    }

  checks++;

  if (setsockopt(sd, SOL_CAN_RAW, CAN_RAW_FILTER, set,
                 sizeof(struct can_filter) + 1) == 0 || errno != EINVAL)
    {
      return canopt_fail(cmd, "filter-badlen");
    }

  checks++;

  /* Empty filter list */

  len = sizeof(get);
  if (setsockopt(sd, SOL_CAN_RAW, CAN_RAW_FILTER, set, 0) < 0 ||
      getsockopt(sd, SOL_CAN_RAW, CAN_RAW_FILTER, get, &len) < 0 ||
      len != 0)
    {
      return canopt_fail(cmd, "filter-empty");
    }

  checks++;

#ifdef CONFIG_NET_CAN_ERRORS
  {
    can_err_mask_t mask = CAN_ERR_TX_TIMEOUT | CAN_ERR_BUSOFF |
                          CAN_ERR_CRTL;
    can_err_mask_t got  = 0;

    len = sizeof(got);
    if (setsockopt(sd, SOL_CAN_RAW, CAN_RAW_ERR_FILTER, &mask,
                   sizeof(mask)) < 0 ||
        getsockopt(sd, SOL_CAN_RAW, CAN_RAW_ERR_FILTER, &got, &len) < 0 ||
        len != sizeof(got) || got != mask)
      {
        return canopt_fail(cmd, "err-filter");
      }

    if (setsockopt(sd, SOL_CAN_RAW, CAN_RAW_ERR_FILTER, &mask, 2) == 0 ||
        errno != EINVAL)
      {
        return canopt_fail(cmd, "err-filter-badlen");
      }

    checks += 2;
  }
#endif

  /* Boolean options */

  for (i = 0; i < (int)(sizeof(boolopts) / sizeof(boolopts[0])); i++)
    {
      if (!canopt_roundtrip(sd, SOL_CAN_RAW, boolopts[i], 1) ||
          !canopt_roundtrip(sd, SOL_CAN_RAW, boolopts[i], 0))
        {
          printf("canopt: option %d\n", boolopts[i]);
          return canopt_fail(cmd, "bool-option");
        }

      val = 1;
      if (setsockopt(sd, SOL_CAN_RAW, boolopts[i], &val, 2) == 0 ||
          errno != EINVAL)
        {
          printf("canopt: option %d\n", boolopts[i]);
          return canopt_fail(cmd, "bool-option-badlen");
        }

      checks += 2;
    }

#ifdef CONFIG_NET_TIMESTAMP
  if (!canopt_roundtrip(sd, SOL_SOCKET, SO_TIMESTAMP, 1) ||
      !canopt_roundtrip(sd, SOL_SOCKET, SO_TIMESTAMP, 0))
    {
      return canopt_fail(cmd, "so-timestamp");
    }

  checks++;
#endif

  /* Unknown options and levels */

  val = 1;
  if (setsockopt(sd, SOL_CAN_RAW, 99, &val, sizeof(val)) == 0 ||
      errno != ENOPROTOOPT ||
      canopt_getint(sd, SOL_CAN_RAW, 99, &val) == 0 ||
      errno != ENOPROTOOPT)
    {
      return canopt_fail(cmd, "unknown-option");
    }

  checks++;

  close(sd);
  printf("canopt: PASS sockopt checks=%d\n", checks);
  return EXIT_SUCCESS;
}

/****************************************************************************
 * Name: canopt_optbits
 *
 * Description:
 *   SOL_SOCKET and SOL_CAN_RAW options must not alias each other.
 *
 ****************************************************************************/

static int canopt_optbits(FAR struct canopt_args_s *args)
{
  int fdval = -1;
  int nsval = -1;
  int sd;

  sd = canopt_sock_open(args->endpoint, false);
  if (sd < 0)
    {
      return canopt_fail(args->cmd, "open");
    }

#if defined(CONFIG_NET_TIMESTAMP) && defined(CONFIG_NET_CAN_CANFD)
  if (canopt_setint(sd, SOL_SOCKET, SO_TIMESTAMPNS, 1) < 0 ||
      canopt_getint(sd, SOL_CAN_RAW, CAN_RAW_FD_FRAMES, &fdval) < 0)
    {
      return canopt_fail(args->cmd, "timestampns");
    }

  canopt_setint(sd, SOL_SOCKET, SO_TIMESTAMPNS, 0);

  if (canopt_setint(sd, SOL_CAN_RAW, CAN_RAW_FD_FRAMES, 1) < 0 ||
      canopt_getint(sd, SOL_SOCKET, SO_TIMESTAMPNS, &nsval) < 0)
    {
      return canopt_fail(args->cmd, "fd-frames");
    }
#else
  fdval = 0;
  nsval = 0;
#endif

  close(sd);
  printf("canopt: %s optbits fd_after_tsns=%d tsns_after_fd=%d\n",
         fdval == 0 && nsval == 0 ? "PASS" : "FAIL", fdval, nsval);
  return fdval == 0 && nsval == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
#endif /* CONFIG_NET_CANPROTO_OPTIONS */

/****************************************************************************
 * Name: canopt_rcvbuf
 *
 * Description:
 *   SO_RCVBUF round trip at the standard SOL_SOCKET level.
 *
 ****************************************************************************/

static int canopt_rcvbuf(FAR struct canopt_args_s *args)
{
  int ret;
  int val = -1;
  int sd;

  sd = canopt_sock_open(args->endpoint, false);
  if (sd < 0)
    {
      return canopt_fail(args->cmd, "open");
    }

  ret = canopt_setint(sd, SOL_SOCKET, SO_RCVBUF, CANOPT_RCVBUF);
  if (ret < 0)
    {
      return canopt_fail(args->cmd, "set");
    }

  if (canopt_getint(sd, SOL_SOCKET, SO_RCVBUF, &val) < 0 ||
      val < CANOPT_RCVBUF)
    {
      return canopt_fail(args->cmd, "get");
    }

  close(sd);
  printf("canopt: PASS rcvbuf val=%d\n", val);
  return EXIT_SUCCESS;
}

/****************************************************************************
 * Name: canopt_rcvtimeo
 *
 * Description:
 *   A blocking read on an idle bus returns EAGAIN after SO_RCVTIMEO.
 *
 ****************************************************************************/

static int canopt_rcvtimeo(FAR struct canopt_args_s *args)
{
  struct canfd_frame frame;
  struct timespec start;
  struct timeval tv;
  ssize_t ret;
  int elapsed;
  int err;
  int sd;

  sd = canopt_sock_open(args->endpoint, false);
  if (sd < 0)
    {
      return canopt_fail(args->cmd, "open");
    }

  tv.tv_sec  = 0;
  tv.tv_usec = CANOPT_RCVTIMEO_MS * 1000;
  if (setsockopt(sd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv)) < 0)
    {
      return canopt_fail(args->cmd, "set");
    }

  canopt_ready();
  clock_gettime(CLOCK_MONOTONIC, &start);
  ret     = read(sd, &frame, sizeof(frame));
  err     = errno;
  elapsed = canopt_elapsed_ms(&start);
  close(sd);

  if (ret < 0 && err == EAGAIN && elapsed >= CANOPT_RCVTIMEO_MS - 100 &&
      elapsed < 3000)
    {
      printf("canopt: PASS rcvtimeo elapsed_ms=%d\n", elapsed);
      return EXIT_SUCCESS;
    }

  printf("canopt: FAIL rcvtimeo ret=%zd errno=%d elapsed_ms=%d\n",
         ret, ret < 0 ? err : 0, elapsed);
  return EXIT_FAILURE;
}

/****************************************************************************
 * Name: canopt_rx
 *
 * Description:
 *   Receive host frames through the -F/-Z/-M filters until the host's end
 *   marker, which arrives on a second socket that filters for it alone.
 *
 ****************************************************************************/

static int canopt_rx(FAR struct canopt_args_s *args)
{
  struct canopt_rxstat_s st;
  struct canfd_frame frame;
  struct can_filter marker;
  struct timespec start;
  struct pollfd pfd[2];
  ssize_t nbytes;
  int sa;
  int sb;

  memset(&st, 0, sizeof(st));

  sa = canopt_sock_open(args->endpoint, false);
  sb = canopt_sock_open(args->endpoint, false);
  if (sa < 0 || sb < 0)
    {
      return canopt_fail(args->cmd, "open");
    }

  if (canopt_set_filters(sa, args) < 0)
    {
      return canopt_fail(args->cmd, "filter");
    }

  marker.can_id   = CANOPT_MARKER_ID;
  marker.can_mask = CANOPT_EXACT_MASK;
  if (setsockopt(sb, SOL_CAN_RAW, CAN_RAW_FILTER, &marker,
                 sizeof(marker)) < 0)
    {
      return canopt_fail(args->cmd, "marker-filter");
    }

  canopt_ready();
  clock_gettime(CLOCK_MONOTONIC, &start);

  pfd[0].fd     = sa;
  pfd[0].events = POLLIN;
  pfd[1].fd     = sb;
  pfd[1].events = POLLIN;

  while (!st.marker)
    {
      pfd[0].revents = 0;
      pfd[1].revents = 0;
      if (poll(pfd, 2, canopt_remain_ms(&start, args->timeout)) <= 0)
        {
          break;
        }

      if ((pfd[0].revents & POLLIN) != 0)
        {
          nbytes = read(sa, &frame, sizeof(frame));
          canopt_rx_check(&frame, nbytes, &st);
          continue;
        }

      if ((pfd[1].revents & POLLIN) != 0)
        {
          read(sb, &frame, sizeof(frame));
          st.marker = true;

          /* Everything sent before the marker is queued on sa by now */

          while ((nbytes = recv(sa, &frame, sizeof(frame),
                                MSG_DONTWAIT)) > 0)
            {
              canopt_rx_check(&frame, nbytes, &st);
            }

          if (errno != EAGAIN)
            {
              st.err++;
            }
        }
    }

  close(sa);
  close(sb);

  printf("canopt: %s rx n=%d err=%d ids=%s\n",
         st.marker && st.err == 0 ? "PASS" : "FAIL",
         st.classic + st.fd, st.err, st.ids[0] ? st.ids : "-");
  return st.marker && st.err == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}

/****************************************************************************
 * Name: canopt_tx
 *
 * Description:
 *   Send the classic table on a classic socket, check that a CAN FD sized
 *   write is refused there, then send the CAN FD table on a CAN FD socket.
 *
 ****************************************************************************/

static int canopt_tx(FAR struct canopt_args_s *args)
{
  FAR const struct canopt_frame_s *entry;
  struct canfd_frame frame;
  int classic = 0;
  int fd = 0;
  size_t i;
  int sd;

  sd = canopt_sock_open(args->endpoint, false);
  if (sd < 0)
    {
      return canopt_fail(args->cmd, "open");
    }

  for (i = 0; i < g_canopt_nclassic; i++)
    {
      entry = &g_canopt_classic[i];
      memset(&frame, 0, sizeof(frame));
      frame.can_id = entry->id;
      frame.len    = entry->len;
      if ((entry->id & CAN_RTR_FLAG) == 0)
        {
          canopt_fill(frame.data, entry->id, entry->len);
        }

      if (write(sd, &frame, CAN_MTU) != CAN_MTU)
        {
          return canopt_fail(args->cmd, "write-classic");
        }

      classic++;
    }

  memset(&frame, 0, sizeof(frame));
  frame.can_id = CANOPT_KICK_ID;
  if (write(sd, &frame, CANFD_MTU) >= 0 || errno != EINVAL)
    {
      return canopt_fail(args->cmd, "fd-on-classic-socket");
    }

#ifdef CONFIG_NET_CAN_CANFD
  if (canopt_setint(sd, SOL_CAN_RAW, CAN_RAW_FD_FRAMES, 1) < 0)
    {
      return canopt_fail(args->cmd, "fd-frames");
    }

  for (i = 0; i < g_canopt_nfd; i++)
    {
      entry = &g_canopt_fdframes[i];
      memset(&frame, 0, sizeof(frame));
      frame.can_id = entry->id;
      frame.len    = entry->len;
      frame.flags  = entry->flags;
      canopt_fill(frame.data, entry->id, entry->len);

      if (write(sd, &frame, CANFD_MTU) != CANFD_MTU)
        {
          return canopt_fail(args->cmd, "write-fd");
        }

      fd++;
    }
#endif

  close(sd);
  printf("canopt: PASS tx classic=%d fd=%d\n", classic, fd);
  return EXIT_SUCCESS;
}

/****************************************************************************
 * Name: canopt_fdrx
 *
 * Description:
 *   A CAN FD socket receives both tables: CAN FD frames as CANFD_MTU with
 *   BRS/ESI preserved, classic frames as CAN_MTU.
 *
 ****************************************************************************/

static int canopt_fdrx(FAR struct canopt_args_s *args)
{
  struct canopt_rxstat_s st;
  struct canfd_frame frame;
  struct timespec start;
  struct pollfd pfd;
  ssize_t nbytes;
  bool pass;
  int sd;

  memset(&st, 0, sizeof(st));

  sd = canopt_sock_open(args->endpoint, true);
  if (sd < 0)
    {
      return canopt_fail(args->cmd, "open");
    }

  canopt_ready();
  clock_gettime(CLOCK_MONOTONIC, &start);

  pfd.fd     = sd;
  pfd.events = POLLIN;

  while (!st.marker &&
         poll(&pfd, 1, canopt_remain_ms(&start, args->timeout)) > 0)
    {
      nbytes = read(sd, &frame, sizeof(frame));
      if (nbytes == CAN_MTU && frame.can_id == CANOPT_MARKER_ID)
        {
          st.marker = true;
          continue;
        }

      canopt_rx_check(&frame, nbytes, &st);
    }

  close(sd);

  pass = st.marker && st.err == 0 && st.classic == (int)g_canopt_nclassic &&
         st.fd == (int)g_canopt_nfd;
  printf("canopt: %s fdrx classic=%d fd=%d err=%d\n",
         pass ? "PASS" : "FAIL", st.classic, st.fd, st.err);
  return pass ? EXIT_SUCCESS : EXIT_FAILURE;
}

/****************************************************************************
 * Name: canopt_legacy
 *
 * Description:
 *   A classic (non CAN FD) socket on a bus carrying both tables must see
 *   only the classic frames, each read returning exactly CAN_MTU.  With
 *   -q the frames are left to queue on the socket before the first read,
 *   otherwise the reader is blocked in read() when each frame arrives.
 *
 ****************************************************************************/

static int canopt_legacy(FAR struct canopt_args_s *args)
{
  FAR const struct canopt_frame_s *entry;
  struct canfd_frame frame;
  struct timespec start;
  ssize_t nbytes;
  bool marker = false;
  int classic = 0;
  int fdseen = 0;
  int bad = 0;
  bool pass;
  int sd;

  sd = canopt_sock_open(args->endpoint, false);
  if (sd < 0)
    {
      return canopt_fail(args->cmd, "open");
    }

  canopt_ready();

  if (args->queued)
    {
      usleep(1000 * 1000);
    }

  clock_gettime(CLOCK_MONOTONIC, &start);

  while (!marker && canopt_remain_ms(&start, args->timeout) > 0)
    {
      nbytes = read(sd, &frame, sizeof(frame));
      if (nbytes != CAN_MTU)
        {
          printf("canopt: read returned %zd\n", nbytes);
          bad++;
          continue;
        }

      if (frame.can_id == CANOPT_MARKER_ID)
        {
          marker = true;
          continue;
        }

      if (frame.can_id == CANOPT_KICK_ID)
        {
          continue;
        }

      entry = canopt_find(frame.can_id);
      if (entry != NULL && entry->fd)
        {
          fdseen++;
        }
      else if (entry != NULL && frame.len == entry->len &&
               canopt_data_ok(frame.data, frame.can_id, frame.len))
        {
          classic++;
        }
      else
        {
          bad++;
        }
    }

  close(sd);

  pass = marker && classic == (int)g_canopt_nclassic && fdseen == 0 &&
         bad == 0;
  printf("canopt: %s legacy classic=%d fd=%d bad=%d\n",
         pass ? "PASS" : "FAIL", classic, fdseen, bad);
  return pass ? EXIT_SUCCESS : EXIT_FAILURE;
}

/****************************************************************************
 * Name: canopt_ts
 *
 * Description:
 *   SO_TIMESTAMP / SO_TIMESTAMPNS receive timestamps through recvmsg()
 *   control messages: exactly one per frame of the requested type (none
 *   with -m none), CLOCK_REALTIME based, monotonic and spread like the
 *   host's -g frame gap.  Part of the frames queue before the first read.
 *
 ****************************************************************************/

static int64_t canopt_ts_us(FAR const struct timespec *ts)
{
  return (int64_t)ts->tv_sec * 1000000 + ts->tv_nsec / 1000;
}

static int canopt_ts_get(FAR struct msghdr *msg, int mode,
                         FAR struct timespec *ts)
{
  FAR struct cmsghdr *cmsg;
  int found = 0;
  int other = 0;

  for (cmsg = CMSG_FIRSTHDR(msg); cmsg != NULL;
       cmsg = CMSG_NXTHDR(msg, cmsg))
    {
      if (mode == CANOPT_TS_US && cmsg->cmsg_level == SOL_SOCKET &&
          cmsg->cmsg_type == SO_TIMESTAMP &&
          cmsg->cmsg_len == CMSG_LEN(sizeof(struct timeval)))
        {
          FAR struct timeval *tv = (FAR struct timeval *)CMSG_DATA(cmsg);

          ts->tv_sec  = tv->tv_sec;
          ts->tv_nsec = tv->tv_usec * 1000;
          found++;
        }
      else if (mode == CANOPT_TS_NS && cmsg->cmsg_level == SOL_SOCKET &&
               cmsg->cmsg_type == SO_TIMESTAMPNS &&
               cmsg->cmsg_len == CMSG_LEN(sizeof(struct timespec)))
        {
          memcpy(ts, CMSG_DATA(cmsg), sizeof(*ts));
          found++;
        }
      else
        {
          printf("canopt: cmsg level=%d type=%d\n", cmsg->cmsg_level,
                 cmsg->cmsg_type);
          other++;
        }
    }

  return mode == CANOPT_TS_NONE ? (other == 0 ? 0 : -1) :
         (found == 1 && other == 0 ? 0 : -1);
}

static int canopt_ts(FAR struct canopt_args_s *args)
{
  union
  {
    struct cmsghdr hdr;
    uint8_t        buf[2 * CMSG_SPACE(sizeof(struct timespec))];
  } control;

  struct canfd_frame frame;
  struct timespec start;
  struct timespec t0;
  struct timespec ts;
  struct timespec now;
  struct pollfd pfd;
  struct msghdr msg;
  struct iovec iov;
  int64_t first = 0;
  int64_t prev = 0;
  int64_t span = 0;
  int rx = 0;
  int bad = 0;
  bool pass;
  int sd;

  sd = canopt_sock_open(args->endpoint, args->fd);
  if (sd < 0)
    {
      return canopt_fail(args->cmd, "open");
    }

#ifdef CONFIG_NET_TIMESTAMP
  if (args->mode != CANOPT_TS_NONE)
    {
      int opt;

      opt = args->mode == CANOPT_TS_US ? SO_TIMESTAMP : SO_TIMESTAMPNS;
      if (canopt_setint(sd, SOL_SOCKET, opt, 1) < 0)
        {
          return canopt_fail(args->cmd, "set");
        }
    }
#else
  if (args->mode != CANOPT_TS_NONE)
    {
      errno = ENOSYS;
      return canopt_fail(args->cmd, "set");
    }
#endif

  clock_gettime(CLOCK_REALTIME, &t0);
  canopt_ready();

  /* Let the first frames queue on the socket (read-ahead path), the rest
   * arrive while the reader waits.
   */

  usleep(5 * args->gap_ms * 1000);
  clock_gettime(CLOCK_MONOTONIC, &start);

  pfd.fd     = sd;
  pfd.events = POLLIN;

  while (rx < args->count &&
         poll(&pfd, 1, canopt_remain_ms(&start, args->timeout)) > 0)
    {
      iov.iov_base       = &frame;
      iov.iov_len        = sizeof(frame);
      memset(&msg, 0, sizeof(msg));
      msg.msg_iov        = &iov;
      msg.msg_iovlen     = 1;
      msg.msg_control    = &control;
      msg.msg_controllen = sizeof(control);

      if (recvmsg(sd, &msg, 0) != CAN_MTU)
        {
          bad++;
          continue;
        }

      clock_gettime(CLOCK_REALTIME, &now);
      rx++;

      if (canopt_ts_get(&msg, args->mode, &ts) < 0)
        {
          bad++;
          continue;
        }

      if (args->mode == CANOPT_TS_NONE)
        {
          continue;
        }

      /* Plausible: after the start, not in the future, monotonic */

      if (canopt_ts_us(&ts) < canopt_ts_us(&t0) - 1000000 ||
          canopt_ts_us(&ts) > canopt_ts_us(&now) ||
          canopt_ts_us(&ts) < prev)
        {
          printf("canopt: bad ts %lld.%09ld\n", (long long)ts.tv_sec,
                 ts.tv_nsec);
          bad++;
        }

      if (first == 0)
        {
          first = canopt_ts_us(&ts);
        }

      prev = canopt_ts_us(&ts);
    }

  close(sd);

  /* Frames were sent gap_ms apart: the stamps must reflect arrival, not
   * the read time.
   */

  span = prev - first;
  pass = rx == args->count && bad == 0 &&
         (args->mode == CANOPT_TS_NONE ||
          span >= (int64_t)(args->count - 1) * args->gap_ms * 1000 / 2);
  printf("canopt: %s ts rx=%d bad=%d span_ms=%lld\n",
         pass ? "PASS" : "FAIL", rx, bad, (long long)(span / 1000));
  return pass ? EXIT_SUCCESS : EXIT_FAILURE;
}

/****************************************************************************
 * Name: canopt_loopback
 *
 * Description:
 *   Local loopback semantics: a frame sent on socket A reaches socket B on
 *   the same node only with CAN_RAW_LOOPBACK, and A itself only with
 *   CAN_RAW_LOOPBACK and CAN_RAW_RECV_OWN_MSGS.
 *
 ****************************************************************************/

static int canopt_loopback(FAR struct canopt_args_s *args)
{
  struct canfd_frame frame;
  struct timespec start;
  struct pollfd pfd[2];
  int expect_peer;
  int expect_own;
  int peer = 0;
  int own = 0;
  bool pass;
  int sa;
  int sb;
  int i;

  sa = canopt_sock_open(args->endpoint, false);
  sb = canopt_sock_open(args->endpoint, false);
  if (sa < 0 || sb < 0)
    {
      return canopt_fail(args->cmd, "open");
    }

  if (canopt_setint(sa, SOL_CAN_RAW, CAN_RAW_LOOPBACK,
                    args->loopback) < 0 ||
      canopt_setint(sa, SOL_CAN_RAW, CAN_RAW_RECV_OWN_MSGS,
                    args->recv_own) < 0)
    {
      return canopt_fail(args->cmd, "set");
    }

  memset(&frame, 0, sizeof(frame));
  frame.can_id = CANOPT_LOOP_ID;
  frame.len    = 8;
  canopt_fill(frame.data, frame.can_id, frame.len);
  if (write(sa, &frame, CAN_MTU) != CAN_MTU)
    {
      return canopt_fail(args->cmd, "write");
    }

  pfd[0].fd     = sa;
  pfd[0].events = POLLIN;
  pfd[1].fd     = sb;
  pfd[1].events = POLLIN;
  clock_gettime(CLOCK_MONOTONIC, &start);

  while (poll(pfd, 2, 500 - canopt_elapsed_ms(&start)) > 0)
    {
      for (i = 0; i < 2; i++)
        {
          if ((pfd[i].revents & POLLIN) != 0 &&
              read(pfd[i].fd, &frame, sizeof(frame)) == CAN_MTU &&
              frame.can_id == CANOPT_LOOP_ID)
            {
              if (i == 0)
                {
                  own++;
                }
              else
                {
                  peer++;
                }
            }
        }

      if (canopt_elapsed_ms(&start) >= 500)
        {
          break;
        }
    }

  close(sa);
  close(sb);

  expect_peer = args->loopback;
  expect_own  = args->loopback && args->recv_own;
  pass = peer == expect_peer && own == expect_own;
  printf("canopt: %s loopback peer=%d own=%d\n", pass ? "PASS" : "FAIL",
         peer, own);
  return pass ? EXIT_SUCCESS : EXIT_FAILURE;
}

/****************************************************************************
 * Name: canopt_poll
 *
 * Description:
 *   Nonblocking I/O and readiness: EAGAIN on an empty socket (O_NONBLOCK
 *   and MSG_DONTWAIT), poll() timeout and POLLOUT, a nonblocking write,
 *   then select() and poll() wake-ups for two host frames.
 *
 ****************************************************************************/

static bool canopt_read_id(int sd, uint32_t id)
{
  struct canfd_frame frame;

  return read(sd, &frame, sizeof(frame)) == CAN_MTU && frame.can_id == id &&
         frame.len == 8 && canopt_data_ok(frame.data, id, 8);
}

static int canopt_poll(FAR struct canopt_args_s *args)
{
  FAR const char *cmd = args->cmd;
  struct canfd_frame frame;
  struct timeval tv;
  struct pollfd pfd;
  fd_set rfds;
  int sd;
  int sb;

  sd = canopt_sock_open(args->endpoint, false);
  sb = canopt_sock_open(args->endpoint, false);
  if (sd < 0 || sb < 0 ||
      fcntl(sd, F_SETFL, fcntl(sd, F_GETFL) | O_NONBLOCK) < 0)
    {
      return canopt_fail(cmd, "open");
    }

  if (read(sd, &frame, sizeof(frame)) >= 0 || errno != EAGAIN)
    {
      return canopt_fail(cmd, "nonblock-read");
    }

  if (recv(sb, &frame, sizeof(frame), MSG_DONTWAIT) >= 0 ||
      errno != EAGAIN)
    {
      return canopt_fail(cmd, "dontwait-recv");
    }

  pfd.fd      = sd;
  pfd.events  = POLLIN;
  pfd.revents = 0;
  if (poll(&pfd, 1, 100) != 0)
    {
      return canopt_fail(cmd, "poll-idle");
    }

  pfd.events  = POLLOUT;
  pfd.revents = 0;
  if (poll(&pfd, 1, 0) != 1 || (pfd.revents & POLLOUT) == 0)
    {
      return canopt_fail(cmd, "pollout");
    }

  memset(&frame, 0, sizeof(frame));
  frame.can_id = CANOPT_POLL_TX_ID;
  frame.len    = 8;
  canopt_fill(frame.data, frame.can_id, frame.len);
  if (write(sd, &frame, CAN_MTU) != CAN_MTU)
    {
      return canopt_fail(cmd, "nonblock-write");
    }

  canopt_ready();

  FD_ZERO(&rfds);
  FD_SET(sd, &rfds);
  tv.tv_sec  = args->timeout;
  tv.tv_usec = 0;
  if (select(sd + 1, &rfds, NULL, NULL, &tv) != 1 || !FD_ISSET(sd, &rfds))
    {
      return canopt_fail(cmd, "select");
    }

  if (!canopt_read_id(sd, CANOPT_POLL_RX1_ID))
    {
      return canopt_fail(cmd, "read-1");
    }

  pfd.events  = POLLIN;
  pfd.revents = 0;
  if (poll(&pfd, 1, args->timeout * 1000) != 1 ||
      (pfd.revents & POLLIN) == 0)
    {
      return canopt_fail(cmd, "pollin");
    }

  if (!canopt_read_id(sd, CANOPT_POLL_RX2_ID))
    {
      return canopt_fail(cmd, "read-2");
    }

  if (read(sd, &frame, sizeof(frame)) >= 0 || errno != EAGAIN)
    {
      return canopt_fail(cmd, "drained-read");
    }

  close(sd);
  close(sb);
  printf("canopt: PASS poll\n");
  return EXIT_SUCCESS;
}

/****************************************************************************
 * Name: canopt_hup
 *
 * Description:
 *   poll() reports POLLHUP when the interface is taken down.
 *
 ****************************************************************************/

static int canopt_hup(FAR struct canopt_args_s *args)
{
  struct pollfd pfd;
  int ret;
  int sd;

  sd = canopt_sock_open(args->endpoint, false);
  if (sd < 0)
    {
      return canopt_fail(args->cmd, "open");
    }

  pfd.fd      = sd;
  pfd.events  = POLLIN;
  pfd.revents = 0;

  canopt_ready();
  ret = poll(&pfd, 1, args->timeout * 1000);
  close(sd);

  if (ret == 1 && (pfd.revents & POLLHUP) != 0)
    {
      printf("canopt: PASS hup revents=0x%x\n", pfd.revents);
      return EXIT_SUCCESS;
    }

  printf("canopt: FAIL hup ret=%d revents=0x%x\n", ret, pfd.revents);
  return EXIT_FAILURE;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int canopt_sock_main(FAR struct canopt_args_s *args)
{
  FAR const char *cmd = args->cmd;

#ifdef CONFIG_NET_CANPROTO_OPTIONS
  if (strcmp(cmd, "sockopt") == 0)
    {
      return canopt_sockopt(args);
    }
  else if (strcmp(cmd, "optbits") == 0)
    {
      return canopt_optbits(args);
    }
#endif

  if (strcmp(cmd, "loopback") == 0)
    {
      return canopt_loopback(args);
    }
  else if (strcmp(cmd, "rcvbuf") == 0)
    {
      return canopt_rcvbuf(args);
    }
  else if (strcmp(cmd, "rcvtimeo") == 0)
    {
      return canopt_rcvtimeo(args);
    }
  else if (strcmp(cmd, "rx") == 0)
    {
      return canopt_rx(args);
    }
  else if (strcmp(cmd, "tx") == 0)
    {
      return canopt_tx(args);
    }
  else if (strcmp(cmd, "fdrx") == 0)
    {
      return canopt_fdrx(args);
    }
  else if (strcmp(cmd, "legacy") == 0)
    {
      return canopt_legacy(args);
    }
  else if (strcmp(cmd, "ts") == 0)
    {
      return canopt_ts(args);
    }
  else if (strcmp(cmd, "poll") == 0)
    {
      return canopt_poll(args);
    }
  else if (strcmp(cmd, "hup") == 0)
    {
      return canopt_hup(args);
    }

  printf("canopt: FAIL %s unknown SocketCAN check\n", cmd);
  return EXIT_FAILURE;
}

#endif /* CONFIG_NET_CAN */
