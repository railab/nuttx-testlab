/****************************************************************************
 * apps/cantl/cantl_main.c
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
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <poll.h>
#include <time.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/ioctl.h>

#include <net/if.h>
#include <sys/socket.h>

#include <nuttx/can.h>

#ifdef CONFIG_CAN
#include <nuttx/can/can.h>
#endif

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define CANTL_ID_DEFAULT      0x123
#define CANTL_COUNT_DEFAULT   50
#define CANTL_TIMEOUT_DEFAULT 10
#define CANTL_SEQ_LEN         4
#define CANTL_CHARDEV_RXBUF    (8 * sizeof(struct can_msg_s))

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct cantl_args_s
{
  bool        send;
  bool        canfd;
  FAR char   *endpoint; /* SocketCAN ifname, or a '/'-prefixed chardev path */
  uint32_t    id;
  size_t      count;
  int         gap_ms;
  int         timeout;
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static bool cantl_parse_uint(FAR const char *arg, unsigned long minval,
                              unsigned long maxval, FAR unsigned long *val)
{
  FAR char *end;
  unsigned long parsed;

  if (arg == NULL || arg[0] == '\0')
    {
      return false;
    }

  errno = 0;
  parsed = strtoul(arg, &end, 0);
  if (errno != 0 || *end != '\0' || parsed < minval || parsed > maxval)
    {
      return false;
    }

  *val = parsed;
  return true;
}

static void cantl_put32(FAR uint8_t *buf, uint32_t val)
{
  buf[0] = (uint8_t)(val >> 24);
  buf[1] = (uint8_t)(val >> 16);
  buf[2] = (uint8_t)(val >> 8);
  buf[3] = (uint8_t)val;
}

static uint32_t cantl_get32(FAR const uint8_t *buf)
{
  return ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
         ((uint32_t)buf[2] << 8) | (uint32_t)buf[3];
}

static uint8_t cantl_pattern(uint32_t seq, size_t k)
{
  return (uint8_t)(seq * 31 + 7 + k);
}

static void cantl_fill(FAR uint8_t *data, uint32_t seq, size_t len)
{
  size_t k;

  for (k = 0; k < len; k++)
    {
      data[k] = cantl_pattern(seq, k);
    }
}

static size_t cantl_check(FAR const uint8_t *data, uint32_t seq,
                           size_t len)
{
  size_t err = 0;
  size_t k;

  for (k = 0; k < len; k++)
    {
      if (data[k] != cantl_pattern(seq, k))
        {
          err++;
        }
    }

  return err;
}

static int cantl_elapsed_ms(FAR const struct timespec *start)
{
  struct timespec now;

  clock_gettime(CLOCK_MONOTONIC, &now);
  return (int)((now.tv_sec - start->tv_sec) * 1000 +
               (now.tv_nsec - start->tv_nsec) / 1000000);
}

static bool cantl_is_chardev(FAR const char *endpoint)
{
  return endpoint[0] == '/';
}

/****************************************************************************
 * SocketCAN backend
 ****************************************************************************/

#ifdef CONFIG_NET_CAN

static int cantl_socket(FAR const char *ifname)
{
  struct sockaddr_can addr;
  int sd;

  sd = socket(PF_CAN, SOCK_RAW, CAN_RAW);
  if (sd < 0)
    {
      printf("cantl: socket failed %d\n", errno);
      return -1;
    }

  memset(&addr, 0, sizeof(addr));
  addr.can_family  = AF_CAN;
  addr.can_ifindex = if_nametoindex(ifname);
  if (addr.can_ifindex == 0)
    {
      printf("cantl: unknown interface %s\n", ifname);
      close(sd);
      return -1;
    }

  if (bind(sd, (FAR struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
      printf("cantl: bind failed %d\n", errno);
      close(sd);
      return -1;
    }

  return sd;
}

static int cantl_sock_send(FAR const struct cantl_args_s *args)
{
  struct canfd_frame frame;
  size_t mtu;
  size_t len;
  size_t tx = 0;
  int enable_fd = 1;
  uint32_t seq;
  int sd;

  sd = cantl_socket(args->endpoint);
  if (sd < 0)
    {
      printf("cantl: FAIL tx=0\n");
      return EXIT_FAILURE;
    }

  if (args->canfd)
    {
      if (setsockopt(sd, SOL_CAN_RAW, CAN_RAW_FD_FRAMES, &enable_fd,
                     sizeof(enable_fd)) < 0)
        {
          printf("cantl: CAN_RAW_FD_FRAMES failed %d\n", errno);
          printf("cantl: FAIL tx=0\n");
          close(sd);
          return EXIT_FAILURE;
        }

      mtu = CANFD_MTU;
      len = CANFD_MAX_DLEN;
    }
  else
    {
      mtu = CAN_MTU;
      len = CAN_MAX_DLEN;
    }

  memset(&frame, 0, sizeof(frame));
  frame.can_id = args->id;
  frame.len    = (uint8_t)len;

  for (seq = 0; seq < args->count; seq++)
    {
      cantl_put32(frame.data, seq);
      cantl_fill(frame.data + CANTL_SEQ_LEN, seq, len - CANTL_SEQ_LEN);

      if (write(sd, &frame, mtu) != (ssize_t)mtu)
        {
          printf("cantl: write failed %d\n", errno);
          break;
        }

      tx++;

      if (args->gap_ms > 0)
        {
          usleep(args->gap_ms * 1000);
        }
    }

  close(sd);
  printf("cantl: %s tx=%zu\n",
         tx == args->count ? "PASS" : "FAIL", tx);
  return tx == args->count ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int cantl_sock_recv(FAR const struct cantl_args_s *args)
{
  struct canfd_frame frame;
  struct can_filter filter;
  struct timespec start;
  struct pollfd pfd;
  size_t mtu;
  size_t rx         = 0;
  size_t lost       = 0;
  size_t err        = 0;
  uint32_t expected = 0;
  uint32_t seq;
  ssize_t ret;
  int sd;

  sd = cantl_socket(args->endpoint);
  if (sd < 0)
    {
      printf("cantl: FAIL rx=0 lost=0 err=1\n");
      return EXIT_FAILURE;
    }

  if (args->canfd)
    {
      int enable_fd = 1;

      if (setsockopt(sd, SOL_CAN_RAW, CAN_RAW_FD_FRAMES, &enable_fd,
                     sizeof(enable_fd)) < 0)
        {
          printf("cantl: CAN_RAW_FD_FRAMES failed %d\n", errno);
          printf("cantl: FAIL rx=0 lost=0 err=1\n");
          close(sd);
          return EXIT_FAILURE;
        }

      mtu = CANFD_MTU;
    }
  else
    {
      mtu = CAN_MTU;
    }

  filter.can_id   = args->id;
  filter.can_mask = CAN_SFF_MASK | CAN_EFF_FLAG | CAN_RTR_FLAG;
  if (setsockopt(sd, SOL_CAN_RAW, CAN_RAW_FILTER, &filter,
                 sizeof(filter)) < 0)
    {
      printf("cantl: CAN_RAW_FILTER failed %d\n", errno);
      printf("cantl: FAIL rx=0 lost=0 err=1\n");
      close(sd);
      return EXIT_FAILURE;
    }

  printf("cantl: listening\n");
  fflush(stdout);

  pfd.fd     = sd;
  pfd.events = POLLIN;

  clock_gettime(CLOCK_MONOTONIC, &start);

  while (expected < args->count)
    {
      int remain_ms = args->timeout * 1000 - cantl_elapsed_ms(&start);

      if (remain_ms <= 0 || poll(&pfd, 1, remain_ms) <= 0)
        {
          break;
        }

      ret = read(sd, &frame, sizeof(frame));
      if (ret != (ssize_t)mtu || frame.len < CANTL_SEQ_LEN)
        {
          err++;
          continue;
        }

      seq = cantl_get32(frame.data);
      if (seq < expected)
        {
          err++;
          continue;
        }

      lost     += seq - expected;
      expected  = seq;

      err      += cantl_check(frame.data + CANTL_SEQ_LEN, seq,
                               (size_t)frame.len - CANTL_SEQ_LEN);
      expected++;
      rx++;
    }

  if (expected < args->count)
    {
      lost += args->count - expected;
    }

  close(sd);
  printf("cantl: %s rx=%zu lost=%zu err=%zu\n",
         (lost == 0 && err == 0) ? "PASS" : "FAIL", rx, lost, err);
  return (lost == 0 && err == 0) ? EXIT_SUCCESS : EXIT_FAILURE;
}

#endif /* CONFIG_NET_CAN */

/****************************************************************************
 * CAN character driver backend
 ****************************************************************************/

#ifdef CONFIG_CAN

static int cantl_chardev_open(FAR const char *devpath)
{
  int fd;

  fd = open(devpath, O_RDWR);
  if (fd < 0)
    {
      printf("cantl: open %s failed %d\n", devpath, errno);
    }

  return fd;
}

/* Install an exact-ID hardware filter if the lower-half driver supports
 * CANIOC_ADD_STDFILTER.  Drivers that return -ENOTTY (e.g. the sim
 * character driver, arch/sim/src/sim/sim_canchar.c) have no filtering of
 * their own, so the caller must then filter in software instead.
 */

static bool cantl_chardev_filter_set(int fd, uint32_t id)
{
  struct canioc_stdfilter_s sf;
  int ret;

  memset(&sf, 0, sizeof(sf));
  sf.sf_id1 = (uint16_t)id;
  sf.sf_id2 = CAN_SFF_MASK;
  sf.sf_type = CAN_FILTER_MASK;
  sf.sf_prio = CAN_MSGPRIO_HIGH;

  ret = ioctl(fd, CANIOC_ADD_STDFILTER, (unsigned long)((uintptr_t)&sf));
  if (ret < 0)
    {
      printf("cantl: no chardev hw filter (%d), filtering id in "
             "software\n", errno);
      return true;
    }

  return false;
}

static int cantl_chardev_send(FAR const struct cantl_args_s *args)
{
  struct can_msg_s msg;
  size_t len;
  size_t tx = 0;
  ssize_t msglen;
  uint32_t seq;
  uint8_t dlc;
  int fd;

  fd = cantl_chardev_open(args->endpoint);
  if (fd < 0)
    {
      printf("cantl: FAIL tx=0\n");
      return EXIT_FAILURE;
    }

  len = args->canfd ? CANFD_MAX_DLEN : CAN_MAX_DLEN;

  memset(&msg, 0, sizeof(msg));
  msg.cm_hdr.ch_id  = args->id;
  msg.cm_hdr.ch_rtr = false;

  for (seq = 0; seq < args->count; seq++)
    {
      cantl_put32(msg.cm_data, seq);
      cantl_fill(msg.cm_data + CANTL_SEQ_LEN, seq, len - CANTL_SEQ_LEN);

      dlc = can_bytes2dlc((uint8_t)len);
      msg.cm_hdr.ch_dlc = dlc;
#ifdef CONFIG_CAN_FD
      msg.cm_hdr.ch_edl = args->canfd;
      msg.cm_hdr.ch_brs = args->canfd;
#endif

      msglen = CAN_MSGLEN(can_dlc2bytes(dlc));
      if (write(fd, &msg, msglen) != msglen)
        {
          printf("cantl: write failed %d\n", errno);
          break;
        }

      tx++;

      if (args->gap_ms > 0)
        {
          usleep(args->gap_ms * 1000);
        }
    }

  close(fd);
  printf("cantl: %s tx=%zu\n",
         tx == args->count ? "PASS" : "FAIL", tx);
  return tx == args->count ? EXIT_SUCCESS : EXIT_FAILURE;
}

/* State of one chardev receiver run */

struct cantl_chardev_rx_s
{
  uint32_t expected;
  size_t   rx;
  size_t   lost;
  size_t   err;
};

/* Verify one received message against the cantl frame format */

static void cantl_chardev_check(FAR const struct cantl_args_s *args,
                                FAR const struct can_msg_s *msg,
                                bool swfilter,
                                FAR struct cantl_chardev_rx_s *st)
{
  size_t len;
  uint32_t seq;

  if (swfilter && msg->cm_hdr.ch_id != args->id)
    {
      /* Noise frame on another ID: no hardware filter, drop it. */

      return;
    }

  len = can_dlc2bytes(msg->cm_hdr.ch_dlc);
  if (len < CANTL_SEQ_LEN)
    {
      st->err++;
      return;
    }

  seq = cantl_get32(msg->cm_data);
  if (seq < st->expected)
    {
      st->err++;
      return;
    }

  st->lost     += seq - st->expected;
  st->expected  = seq;
  st->err      += cantl_check(msg->cm_data + CANTL_SEQ_LEN, seq,
                              len - CANTL_SEQ_LEN);
  st->expected++;
  st->rx++;
}

static int cantl_chardev_recv(FAR const struct cantl_args_s *args)
{
  struct cantl_chardev_rx_s st;
  struct can_msg_s msg;
  struct timespec start;
  struct pollfd pfd;
  uint8_t buf[CANTL_CHARDEV_RXBUF];
  bool swfilter;
  size_t off;
  size_t msglen;
  ssize_t ret;
  int fd;

  memset(&st, 0, sizeof(st));

  fd = cantl_chardev_open(args->endpoint);
  if (fd < 0)
    {
      printf("cantl: FAIL rx=0 lost=0 err=1\n");
      return EXIT_FAILURE;
    }

  swfilter = cantl_chardev_filter_set(fd, args->id);

  printf("cantl: listening\n");
  fflush(stdout);

  pfd.fd     = fd;
  pfd.events = POLLIN;

  clock_gettime(CLOCK_MONOTONIC, &start);

  while (st.expected < args->count)
    {
      int remain_ms = args->timeout * 1000 - cantl_elapsed_ms(&start);

      if (remain_ms <= 0 || poll(&pfd, 1, remain_ms) <= 0)
        {
          break;
        }

      /* read() returns as many whole messages as fit in the buffer,
       * packed back to back (message alignment 1).
       */

      ret = read(fd, buf, sizeof(buf));
      if (ret < (ssize_t)CAN_MSGLEN(0))
        {
          st.err++;
          continue;
        }

      for (off = 0; off + CAN_MSGLEN(0) <= (size_t)ret; off += msglen)
        {
          memcpy(&msg, &buf[off], CAN_MSGLEN(0));
          msglen = CAN_MSGLEN(can_dlc2bytes(msg.cm_hdr.ch_dlc));
          if (off + msglen > (size_t)ret)
            {
              st.err++;
              break;
            }

          memcpy(&msg, &buf[off], msglen);
          cantl_chardev_check(args, &msg, swfilter, &st);
        }
    }

  if (st.expected < args->count)
    {
      st.lost += args->count - st.expected;
    }

  close(fd);
  printf("cantl: %s rx=%zu lost=%zu err=%zu\n",
         (st.lost == 0 && st.err == 0) ? "PASS" : "FAIL",
         st.rx, st.lost, st.err);
  return (st.lost == 0 && st.err == 0) ? EXIT_SUCCESS : EXIT_FAILURE;
}

#endif /* CONFIG_CAN */

/****************************************************************************
 * Backend dispatch
 ****************************************************************************/

static int cantl_send(FAR const struct cantl_args_s *args)
{
#ifdef CONFIG_CAN
  if (cantl_is_chardev(args->endpoint))
    {
      return cantl_chardev_send(args);
    }
#endif

#ifdef CONFIG_NET_CAN
  return cantl_sock_send(args);
#else
  printf("cantl: FAIL tx=0\n");
  return EXIT_FAILURE;
#endif
}

static int cantl_recv(FAR const struct cantl_args_s *args)
{
#ifdef CONFIG_CAN
  if (cantl_is_chardev(args->endpoint))
    {
      return cantl_chardev_recv(args);
    }
#endif

#ifdef CONFIG_NET_CAN
  return cantl_sock_recv(args);
#else
  printf("cantl: FAIL rx=0 lost=0 err=1\n");
  return EXIT_FAILURE;
#endif
}

static void cantl_usage(FAR const char *progname)
{
  printf("Usage: %s -s dev [-n count] [-i id] [-f] [-g gap_ms]\n",
         progname);
  printf("       %s -r dev [-n count] [-i id] [-f] [-t sec]\n",
         progname);
  printf("  dev: SocketCAN ifname (e.g. can0), or a CAN character "
         "device path\n");
  printf("       (e.g. /dev/can0) when its name starts with '/'\n");
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  struct cantl_args_s args;
  FAR char *recv_endpoint = NULL;
  int opt;

  memset(&args, 0, sizeof(args));
  args.id      = CANTL_ID_DEFAULT;
  args.count   = CANTL_COUNT_DEFAULT;
  args.timeout = CANTL_TIMEOUT_DEFAULT;

  while ((opt = getopt(argc, argv, "s:r:n:i:fg:t:")) != ERROR)
    {
      unsigned long val;

      switch (opt)
        {
          case 's':
            args.send     = true;
            args.endpoint = optarg;
            break;
          case 'r':
            recv_endpoint = optarg;
            break;
          case 'f':
            args.canfd = true;
            break;
          case 'n':
            if (!cantl_parse_uint(optarg, 1, ULONG_MAX, &val))
              {
                cantl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.count = (size_t)val;
            break;
          case 'i':
            if (!cantl_parse_uint(optarg, 0, CAN_SFF_MASK, &val))
              {
                cantl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.id = (uint32_t)val;
            break;
          case 'g':
            if (!cantl_parse_uint(optarg, 0, INT_MAX, &val))
              {
                cantl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.gap_ms = (int)val;
            break;
          case 't':
            if (!cantl_parse_uint(optarg, 1, INT_MAX, &val))
              {
                cantl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.timeout = (int)val;
            break;
          default:
            cantl_usage(argv[0]);
            return EXIT_FAILURE;
        }
    }

  if (args.send == (recv_endpoint != NULL))
    {
      cantl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  if (!args.send)
    {
      args.endpoint = recv_endpoint;
    }

  return args.send ? cantl_send(&args) : cantl_recv(&args);
}
