/****************************************************************************
 * apps/isotptl/isotptl_main.c
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

#include <sys/socket.h>
#include <sys/time.h>

#include <errno.h>
#include <inttypes.h>
#include <net/if.h>
#include <poll.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <nuttx/can.h>
#include <nuttx/can/isotp.h>
#include <netutils/netlib.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define ISOTP_HOST_ID      0x654
#define ISOTP_NUTTX_ID     0x456
#define ISOTP_FD_HOST_ID   0x655
#define ISOTP_FD_NUTTX_ID  0x457
#define ISOTP_MARK_ID      0x734
#define ISOTP_FD_MARK_ID   0x735
#define ISOTP_READY_ID     0x736
#define ISOTP_BTB_READY_ID 0x738
#define ISOTP_BTB_GO_ID    0x739
#define MAX_TEST_PAYLOAD   8192

#define ISOTP_LONG_LEN    255
#define ISOTP_FD_LONG_LEN 384

/* Concurrent ISO-TP sessions.  ISOTP_SESS_N segmented request/response
 * exchanges run in parallel, each on a distinct rx/tx address pair, to
 * exercise per-socket ISO-TP state-machine isolation.
 */

#define ISOTP_SESS_N        3
#define ISOTP_SESS_RX_BASE  0x460  /* NuttX rx id (host tx) = base + index */
#define ISOTP_SESS_TX_BASE  0x470  /* NuttX tx id (host rx) = base + index */
#define ISOTP_SESS_READY_ID 0x478
#define ISOTP_SESS_GO_ID    0x479

/* Segmented exchanges exercise independent FC and sequence state. */

#define ISOTP_SESS_LEN      255

#define ISOTP_WORKER_STACK  8192

/****************************************************************************
 * Private Data
 ****************************************************************************/

static struct can_frame   g_raw_frame;
static struct canfd_frame g_raw_fd_frame;
static uint8_t            g_rx_buffer[MAX_TEST_PAYLOAD];
static uint8_t            g_tx_buffer[MAX_TEST_PAYLOAD];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static int set_recv_timeout(int fd, long sec)
{
  struct timeval tv;

  tv.tv_sec  = sec;
  tv.tv_usec = 0;

  return setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
}

static void fill_test_payload(uint8_t *buf, size_t len, unsigned int seed)
{
  size_t i;

  for (i = 0; i < len; i++)
    {
      buf[i] = (uint8_t)(seed + i * 31 + len * 7);
    }
}

static int check_payload(const uint8_t *buf, size_t len, unsigned int seed)
{
  size_t i;

  for (i = 0; i < len; i++)
    {
      uint8_t expected = (uint8_t)(seed + i * 31 + len * 7);

      if (buf[i] != expected)
        {
          return -1;
        }
    }

  return 0;
}

static int fill_can_addr(const char *ifname, struct sockaddr_can *addr)
{
  unsigned int ifindex;

#ifdef CONFIG_NETDEV_IFINDEX
  ifindex = if_nametoindex(ifname);
  if (ifindex == 0)
    {
      printf("ERROR: if_nametoindex(%s) failed: %d\n", ifname, errno);
      return -1;
    }
#else
  if (strncmp(ifname, "can", 3) != 0)
    {
      printf("ERROR: invalid CAN interface name %s\n", ifname);
      return -1;
    }

  ifindex = (unsigned int)strtoul(ifname + 3, NULL, 10);
#endif

  memset(addr, 0, sizeof(*addr));
  addr->can_family  = AF_CAN;
  addr->can_ifindex = ifindex;
  return 0;
}

static int bind_can_socket(int fd, const char *ifname,
                           struct sockaddr_can *addr)
{
  if (fill_can_addr(ifname, addr) < 0)
    {
      return -1;
    }

  if (bind(fd, (struct sockaddr *)addr, sizeof(*addr)) < 0)
    {
      printf("ERROR: bind(%s) failed: %d\n", ifname, errno);
      return -1;
    }

  return 0;
}

static int send_raw_frame(int fd, canid_t id, const void *data, uint8_t len)
{
  ssize_t nbytes;

  memset(&g_raw_frame, 0, sizeof(g_raw_frame));
  g_raw_frame.can_id  = id;
  g_raw_frame.can_dlc = len;
  memcpy(g_raw_frame.data, data, len);

  nbytes = write(fd, &g_raw_frame, sizeof(g_raw_frame));
  if (nbytes != sizeof(g_raw_frame))
    {
      printf("ERROR: raw write failed: %zd errno=%d id=%" PRIu32
             " dlc=%u\n",
             nbytes, errno, id, len);
      return -1;
    }

  return 0;
}

static int send_raw_fd_frame(int fd, canid_t id, const void *data,
                             uint8_t len)
{
  ssize_t nbytes;

  memset(&g_raw_fd_frame, 0, sizeof(g_raw_fd_frame));
  g_raw_fd_frame.can_id = id;
  g_raw_fd_frame.len    = len;
  g_raw_fd_frame.flags  = CANFD_FDF;
  memcpy(g_raw_fd_frame.data, data, len);

  nbytes = write(fd, &g_raw_fd_frame, sizeof(g_raw_fd_frame));
  if (nbytes != sizeof(g_raw_fd_frame))
    {
      printf("ERROR: raw fd write failed: %zd errno=%d id=%" PRIu32
             " len=%u\n",
             nbytes, errno, id, len);
      return -1;
    }

  return 0;
}

static int recv_isotp_payload(int fd, unsigned int loop, unsigned int seed,
                              size_t len)
{
  ssize_t nbytes;

  for (; ; )
    {
      nbytes = read(fd, g_rx_buffer, sizeof(g_rx_buffer));
      if (nbytes != (ssize_t)len)
        {
          printf("ERROR: isotp read failed: %zd expected=%zu errno=%d"
                 " loop=%u seed=%u\n",
                 nbytes, len, errno, loop, seed);
          return -1;
        }

      if (check_payload(g_rx_buffer, len, seed) < 0)
        {
          printf("ERROR: unexpected isotp payload len=%zd seed=%u\n",
                 nbytes, seed);
          return -1;
        }

      return 0;
    }
}

static int send_isotp_payload(int fd, const void *data, size_t len)
{
  ssize_t nbytes;

  nbytes = send(fd, data, len, 0);
  if (nbytes != len)
    {
      printf("ERROR: isotp send failed: %zd expected=%zu errno=%d\n",
             nbytes, len, errno);
      return -1;
    }

  return 0;
}

static int enable_isotp_wait_tx_done(int fd)
{
  struct can_isotp_options opts;

  memset(&opts, 0, sizeof(opts));
  opts.flags = CAN_ISOTP_WAIT_TX_DONE;
  if (setsockopt(fd, SOL_CAN_ISOTP, CAN_ISOTP_OPTS, &opts,
                 sizeof(opts)) < 0)
    {
      printf("ERROR: isotp WAIT_TX_DONE setsockopt failed: %d\n", errno);
      return -1;
    }

  return 0;
}

static int check_isotp_wait_tx_done_dontwait(int fd, const void *data,
                                             size_t len)
{
  ssize_t nbytes;

  nbytes = send(fd, data, len, MSG_DONTWAIT);
  if (nbytes >= 0)
    {
      printf("ERROR: isotp WAIT_TX_DONE MSG_DONTWAIT sent %zd bytes\n",
             nbytes);
      return -1;
    }

  if (errno != EAGAIN)
    {
      printf("ERROR: isotp WAIT_TX_DONE MSG_DONTWAIT errno=%d\n", errno);
      return -1;
    }

  return 0;
}

static int wait_isotp_async_ready(int fd, bool canfd, bool wait_tx_done)
{
  ssize_t nbytes;

  nbytes = read(fd, &g_raw_frame, sizeof(g_raw_frame));
  if (nbytes != sizeof(g_raw_frame))
    {
      printf("ERROR: isotp async ready read failed: %zd errno=%d\n",
             nbytes, errno);
      return -1;
    }

  if ((g_raw_frame.can_id & CAN_EFF_MASK) != ISOTP_READY_ID ||
      g_raw_frame.can_dlc != 2 ||
      g_raw_frame.data[0] != (canfd ? 1 : 0) ||
      g_raw_frame.data[1] != (wait_tx_done ? 1 : 0))
    {
      printf("ERROR: unexpected isotp async ready id=%" PRIu32
             " dlc=%u data=%u,%u\n",
             g_raw_frame.can_id, g_raw_frame.can_dlc,
             g_raw_frame.data[0], g_raw_frame.data[1]);
      return -1;
    }

  return 0;
}

static int run_isotp_test(const char *ifname, bool canfd, bool wait_tx_done,
                          unsigned int loops, unsigned int isotp_len)
{
  struct sockaddr_can          addr;
  struct can_isotp_ll_options ll;
  uint16_t                    deflen = canfd ? ISOTP_FD_LONG_LEN :
                                               ISOTP_LONG_LEN;
  uint16_t                    msglen = isotp_len ? (uint16_t)isotp_len :
                                                   deflen;
  struct can_isotp_fc_options fc;
  int                         fd;
  int                         ret = -1;
  unsigned int                i;

  fd = socket(PF_CAN, SOCK_DGRAM, CAN_ISOTP);
  if (fd < 0)
    {
      printf("ERROR: isotp socket failed: %d\n", errno);
      return -1;
    }

  set_recv_timeout(fd, 10);

  if (wait_tx_done && enable_isotp_wait_tx_done(fd) < 0)
    {
      goto out;
    }

  memset(&ll, 0, sizeof(ll));
  ll.mtu   = canfd ? CANFD_MTU : CAN_MTU;
  ll.tx_dl = canfd ? CANFD_MAX_DLEN : CAN_MAX_DLEN;
  if (setsockopt(fd, SOL_CAN_ISOTP, CAN_ISOTP_LL_OPTS, &ll,
                 sizeof(ll)) < 0)
    {
      printf("ERROR: isotp ll opts failed: %d\n", errno);
      goto out;
    }

  memset(&fc, 0, sizeof(fc));
  fc.bs = 8;
  fc.stmin = 1;
  if (setsockopt(fd, SOL_CAN_ISOTP, CAN_ISOTP_RECV_FC, &fc, sizeof(fc)) < 0)
    {
      goto out;
    }

  if (fill_can_addr(ifname, &addr) < 0)
    {
      goto out;
    }

  addr.can_addr.tp.rx_id = canfd ? ISOTP_FD_HOST_ID : ISOTP_HOST_ID;
  addr.can_addr.tp.tx_id = canfd ? ISOTP_FD_NUTTX_ID : ISOTP_NUTTX_ID;
  if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
      printf("ERROR: isotp bind failed: %d\n", errno);
      goto out;
    }

  printf("isotptl: bound\n");
  fflush(stdout);

  if (canfd)
    {
      for (i = 0; i < loops; i++)
        {
          uint16_t rxlen = msglen;
          uint16_t txlen = msglen;

          if (recv_isotp_payload(fd, i, 0x120 + i, rxlen) < 0)
            {
              goto out;
            }

          fill_test_payload(g_tx_buffer, txlen, 0x220 + i);
          if (send_isotp_payload(fd, g_tx_buffer, txlen) < 0)
            {
              goto out;
            }
        }
    }
  else
    {
      for (i = 0; i < loops; i++)
        {
          uint16_t rxlen = msglen;
          uint16_t txlen = msglen;

          if (recv_isotp_payload(fd, i, 0x100 + i, rxlen) < 0)
            {
              goto out;
            }

          fill_test_payload(g_tx_buffer, txlen, 0x200 + i);
          if (send_isotp_payload(fd, g_tx_buffer, txlen) < 0)
            {
              goto out;
            }
        }
    }

  printf("can_socket_test: isotp %s %sWAIT_TX_DONE OK loops=%u\n",
         canfd ? "fd" : "classic", wait_tx_done ? "" : "no ", loops);
  ret = 0;

out:
  close(fd);
  return ret;
}

static int run_isotp_dontwait_test(const char *ifname, bool canfd,
                                   unsigned int isotp_len)
{
  struct sockaddr_can          addr;
  struct can_isotp_ll_options ll;
  size_t                      deflen = canfd ? ISOTP_FD_LONG_LEN :
                                               ISOTP_LONG_LEN;
  size_t                      rxlen;
  size_t                      txlen;
  int                         fd;
  int                         ret = -1;

  fd = socket(PF_CAN, SOCK_DGRAM, CAN_ISOTP);
  if (fd < 0)
    {
      printf("ERROR: isotp dontwait socket failed: %d\n", errno);
      return -1;
    }

  set_recv_timeout(fd, 10);

  if (enable_isotp_wait_tx_done(fd) < 0)
    {
      goto out;
    }

  memset(&ll, 0, sizeof(ll));
  ll.mtu   = canfd ? CANFD_MTU : CAN_MTU;
  ll.tx_dl = canfd ? CANFD_MAX_DLEN : CAN_MAX_DLEN;
  if (setsockopt(fd, SOL_CAN_ISOTP, CAN_ISOTP_LL_OPTS, &ll,
                 sizeof(ll)) < 0)
    {
      printf("ERROR: isotp dontwait ll opts failed: %d\n", errno);
      goto out;
    }

  if (fill_can_addr(ifname, &addr) < 0)
    {
      goto out;
    }

  addr.can_addr.tp.rx_id = canfd ? ISOTP_FD_HOST_ID : ISOTP_HOST_ID;
  addr.can_addr.tp.tx_id = canfd ? ISOTP_FD_NUTTX_ID : ISOTP_NUTTX_ID;
  if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
      printf("ERROR: isotp dontwait bind failed: %d\n", errno);
      goto out;
    }

  printf("isotptl: bound\n");
  fflush(stdout);

  rxlen = isotp_len ? isotp_len : deflen;
  txlen = rxlen;

  if (recv_isotp_payload(fd, 0, canfd ? 0x320 : 0x300, rxlen) < 0)
    {
      goto out;
    }

  fill_test_payload(g_tx_buffer, txlen, canfd ? 0x420 : 0x400);
  if (check_isotp_wait_tx_done_dontwait(fd, g_tx_buffer, txlen) < 0)
    {
      goto out;
    }

  printf("can_socket_test: isotp %s WAIT_TX_DONE MSG_DONTWAIT OK\n",
         canfd ? "fd" : "classic");
  ret = 0;

out:
  close(fd);
  return ret;
}

static int run_isotp_async_test(const char *ifname, bool canfd,
                                bool wait_tx_done)
{
  struct sockaddr_can          addr;
  struct can_isotp_ll_options ll;
  uint8_t                     mark[2];
  size_t                      txlen;
  struct can_filter           filter;
  int                         fd;
  int                         rawfd;
  int                         enable = 1;
  int                         ret = -1;

  fd = socket(PF_CAN, SOCK_DGRAM, CAN_ISOTP);
  if (fd < 0)
    {
      printf("ERROR: isotp async socket failed: %d\n", errno);
      return -1;
    }

  rawfd = socket(PF_CAN, SOCK_RAW, CAN_RAW);
  if (rawfd < 0)
    {
      printf("ERROR: isotp async raw socket failed: %d\n", errno);
      close(fd);
      return -1;
    }

  if (canfd &&
      setsockopt(rawfd, SOL_CAN_RAW, CAN_RAW_FD_FRAMES, &enable,
                 sizeof(enable)) < 0)
    {
      printf("ERROR: isotp async raw fd enable failed: %d\n", errno);
      goto out;
    }

  if (bind_can_socket(rawfd, ifname, &addr) < 0)
    {
      goto out;
    }

  set_recv_timeout(rawfd, 10);

  filter.can_id   = ISOTP_READY_ID;
  filter.can_mask = CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_SFF_MASK;
  if (setsockopt(rawfd, SOL_CAN_RAW, CAN_RAW_FILTER, &filter,
                 sizeof(filter)) < 0)
    {
      printf("ERROR: isotp async raw filter failed: %d\n", errno);
      goto out;
    }

  if (wait_tx_done && enable_isotp_wait_tx_done(fd) < 0)
    {
      goto out;
    }

  memset(&ll, 0, sizeof(ll));
  ll.mtu   = canfd ? CANFD_MTU : CAN_MTU;
  ll.tx_dl = canfd ? CANFD_MAX_DLEN : CAN_MAX_DLEN;
  if (setsockopt(fd, SOL_CAN_ISOTP, CAN_ISOTP_LL_OPTS, &ll,
                 sizeof(ll)) < 0)
    {
      printf("ERROR: isotp async ll opts failed: %d\n", errno);
      goto out;
    }

  if (fill_can_addr(ifname, &addr) < 0)
    {
      goto out;
    }

  addr.can_addr.tp.rx_id = canfd ? ISOTP_FD_HOST_ID : ISOTP_HOST_ID;
  addr.can_addr.tp.tx_id = canfd ? ISOTP_FD_NUTTX_ID : ISOTP_NUTTX_ID;
  if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
      printf("ERROR: isotp async bind failed: %d\n", errno);
      goto out;
    }

  printf("isotptl: bound\n");
  fflush(stdout);

  if (wait_isotp_async_ready(rawfd, canfd, wait_tx_done) < 0)
    {
      goto out;
    }

  txlen = canfd ? ISOTP_FD_LONG_LEN : ISOTP_LONG_LEN;
  fill_test_payload(g_tx_buffer, txlen, canfd ? 0x540 : 0x520);
  if (send_isotp_payload(fd, g_tx_buffer, txlen) < 0)
    {
      goto out;
    }

  mark[0] = canfd ? 1 : 0;
  mark[1] = wait_tx_done ? 1 : 0;
  if (canfd)
    {
      ret = send_raw_fd_frame(rawfd, ISOTP_FD_MARK_ID, mark,
                              sizeof(mark));
    }
  else
    {
      ret = send_raw_frame(rawfd, ISOTP_MARK_ID, mark, sizeof(mark));
    }

  if (ret < 0)
    {
      goto out;
    }

  printf("can_socket_test: isotp %s async %sWAIT_TX_DONE OK\n",
         canfd ? "fd" : "classic", wait_tx_done ? "" : "no ");
  ret = 0;

out:
  close(rawfd);
  close(fd);
  return ret;
}

/****************************************************************************
 * Name: lcg_next
 *
 * Description:
 *   Minimal deterministic PRNG.  The host peer runs the identical recurrence
 *   so both sides agree on the stress payload sequence without negotiating
 *   each frame on the bus.
 *
 ****************************************************************************/

/****************************************************************************
 * Name: open_filtered_raw
 *
 * Description:
 *   Open and bind a RAW CAN socket accepting a single standard id.
 *
 ****************************************************************************/

static int open_filtered_raw(const char *ifname, canid_t id)
{
  struct sockaddr_can addr;
  struct can_filter   filter;
  int                 fd;

  fd = socket(PF_CAN, SOCK_RAW, CAN_RAW);
  if (fd < 0)
    {
      printf("ERROR: raw socket failed: %d\n", errno);
      return -1;
    }

  set_recv_timeout(fd, 10);

  filter.can_id   = id;
  filter.can_mask = CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_SFF_MASK;
  if (setsockopt(fd, SOL_CAN_RAW, CAN_RAW_FILTER, &filter,
                 sizeof(filter)) < 0)
    {
      printf("ERROR: raw filter failed: %d\n", errno);
      close(fd);
      return -1;
    }

  if (bind_can_socket(fd, ifname, &addr) < 0)
    {
      close(fd);
      return -1;
    }

  return fd;
}

/****************************************************************************
 * Name: raw_write_id
 *
 * Description:
 *   Thread-safe helper that writes a single classic frame from a local
 *   buffer (the g_raw_frame global is shared and must not be used from
 *   worker threads).
 *
 ****************************************************************************/

static int raw_write_id(int fd, canid_t id, const void *data, uint8_t len)
{
  struct can_frame frame;
  ssize_t          nbytes;

  memset(&frame, 0, sizeof(frame));
  frame.can_id  = id;
  frame.can_dlc = len;
  if (len > 0)
    {
      memcpy(frame.data, data, len);
    }

  nbytes = write(fd, &frame, sizeof(frame));
  if (nbytes != sizeof(frame))
    {
      printf("ERROR: raw write id=0x%" PRIx32 " failed: %zd errno=%d\n",
             id, nbytes, errno);
      return -1;
    }

  return 0;
}

/****************************************************************************
 * Name: sync_announce_wait
 *
 * Description:
 *   Responder side of the per-phase handshake.  Beacon a ready id until the
 *   host answers with the matching go id.  This guarantees the host does not
 *   start driving traffic until every NuttX socket for the phase is bound.
 *
 ****************************************************************************/

static int sync_announce_wait(const char *ifname, canid_t ready_id,
                              canid_t go_id)
{
  struct can_frame frame;
  int              fd;
  int              ret = -1;
  int              tries;

  fd = open_filtered_raw(ifname, go_id);
  if (fd < 0)
    {
      return -1;
    }

  set_recv_timeout(fd, 1);

  for (tries = 0; tries < 30; tries++)
    {
      uint8_t beacon = 0xa5;
      ssize_t nbytes;

      if (raw_write_id(fd, ready_id, &beacon, 1) < 0)
        {
          break;
        }

      nbytes = read(fd, &frame, sizeof(frame));
      if (nbytes == sizeof(frame) &&
          (frame.can_id & CAN_EFF_MASK) == go_id)
        {
          ret = 0;
          break;
        }
    }

  if (ret < 0)
    {
      printf("ERROR: handshake ready=0x%" PRIx32 " go=0x%" PRIx32
             " timed out\n", ready_id, go_id);
    }

  close(fd);
  return ret;
}

/****************************************************************************
 * Name: run_isotp_backtoback_test
 *
 * Description:
 *   Two back-to-back blocking, fire-and-forget sends on one ISO-TP socket.
 *   The first is multi-frame, so its engine holds the send slot when the
 *   second send starts; a blocking socket must wait and then succeed, not
 *   fail with EAGAIN.
 *
 ****************************************************************************/

static int run_isotp_backtoback_test(const char *ifname, bool canfd)
{
  struct sockaddr_can         addr;
  struct can_isotp_ll_options ll;
  uint16_t                    msglen = canfd ? ISOTP_FD_LONG_LEN :
                                               ISOTP_LONG_LEN;
  ssize_t                     nbytes;
  int                         fd;
  int                         ret = -1;

  fd = socket(PF_CAN, SOCK_DGRAM, CAN_ISOTP);
  if (fd < 0)
    {
      printf("ERROR: isotp b2b socket failed: %d\n", errno);
      return -1;
    }

  set_recv_timeout(fd, 10);

  memset(&ll, 0, sizeof(ll));
  ll.mtu   = canfd ? CANFD_MTU : CAN_MTU;
  ll.tx_dl = canfd ? CANFD_MAX_DLEN : CAN_MAX_DLEN;
  if (setsockopt(fd, SOL_CAN_ISOTP, CAN_ISOTP_LL_OPTS, &ll,
                 sizeof(ll)) < 0)
    {
      printf("ERROR: isotp b2b ll opts failed: %d\n", errno);
      goto out;
    }

  if (fill_can_addr(ifname, &addr) < 0)
    {
      goto out;
    }

  addr.can_addr.tp.rx_id = canfd ? ISOTP_FD_HOST_ID : ISOTP_HOST_ID;
  addr.can_addr.tp.tx_id = canfd ? ISOTP_FD_NUTTX_ID : ISOTP_NUTTX_ID;
  if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
      printf("ERROR: isotp b2b bind failed: %d\n", errno);
      goto out;
    }

  /* Wait until the host peer is ready to receive. */

  if (sync_announce_wait(ifname, ISOTP_BTB_READY_ID, ISOTP_BTB_GO_ID) < 0)
    {
      goto out;
    }

  /* First (multi-frame) send: holds the slot after it returns. */

  fill_test_payload(g_tx_buffer, msglen, canfd ? 0x570 : 0x560);
  nbytes = send(fd, g_tx_buffer, msglen, 0);
  if (nbytes != (ssize_t)msglen)
    {
      printf("ERROR: isotp b2b first send: %zd expected=%u errno=%d\n",
             nbytes, msglen, errno);
      goto out;
    }

  /* Second send: must wait for the slot, not return EAGAIN. */

  fill_test_payload(g_tx_buffer, msglen, canfd ? 0x571 : 0x561);
  nbytes = send(fd, g_tx_buffer, msglen, 0);
  if (nbytes != (ssize_t)msglen)
    {
      printf("ERROR: isotp b2b second send: %zd expected=%u errno=%d%s\n",
             nbytes, msglen, errno,
             (nbytes < 0 && errno == EAGAIN) ?
             " (blocking send must not return EAGAIN)" : "");
      goto out;
    }

  printf("can_socket_test: isotp %s back-to-back OK\n",
         canfd ? "fd" : "classic");
  ret = 0;

out:
  close(fd);
  return ret;
}

/****************************************************************************
 * Name: spawn_thread
 ****************************************************************************/

static int spawn_thread(pthread_t *tid, FAR void *(*entry)(FAR void *),
                        FAR void *arg, size_t stack)
{
  pthread_attr_t attr;
  int            ret;

  pthread_attr_init(&attr);
  pthread_attr_setstacksize(&attr, stack);
  ret = pthread_create(tid, &attr, entry, arg);
  pthread_attr_destroy(&attr);
  return ret;
}

/****************************************************************************
 * Name: isotp_sess_worker / run_isotp_concurrent_test
 *
 * Description:
 *   Concurrent ISO-TP sessions.  ISOTP_SESS_N segmented request/response
 *   exchanges run in parallel on distinct address pairs, exercising
 *   per-socket ISO-TP reassembly and flow-control isolation.
 *
 ****************************************************************************/

struct isotp_sess_ctx
{
  const char  *ifname;
  int          idx;
  unsigned int iters;
  sem_t       *bound;
  int          ok;
};

static FAR void *isotp_sess_worker(FAR void *arg)
{
  FAR struct isotp_sess_ctx  *ctx = arg;
  struct can_isotp_ll_options ll;
  struct sockaddr_can         addr;
  uint8_t                     rxbuf[ISOTP_SESS_LEN];
  uint8_t                     txbuf[ISOTP_SESS_LEN];
  unsigned int                it;
  int                         fd;

  ctx->ok = -1;

  fd = socket(PF_CAN, SOCK_DGRAM, CAN_ISOTP);
  if (fd < 0)
    {
      printf("ERROR: isotp sess %d socket failed: %d\n", ctx->idx, errno);
      sem_post(ctx->bound);
      return NULL;
    }

  set_recv_timeout(fd, 10);

  memset(&ll, 0, sizeof(ll));
  ll.mtu   = CAN_MTU;
  ll.tx_dl = CAN_MAX_DLEN;
  if (setsockopt(fd, SOL_CAN_ISOTP, CAN_ISOTP_LL_OPTS, &ll,
                 sizeof(ll)) < 0)
    {
      printf("ERROR: isotp sess %d ll opts failed: %d\n", ctx->idx, errno);
      goto done;
    }

  if (fill_can_addr(ctx->ifname, &addr) < 0)
    {
      goto done;
    }

  addr.can_addr.tp.rx_id = ISOTP_SESS_RX_BASE + ctx->idx;
  addr.can_addr.tp.tx_id = ISOTP_SESS_TX_BASE + ctx->idx;
  if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
      printf("ERROR: isotp sess %d bind failed: %d\n", ctx->idx, errno);
      goto done;
    }

  sem_post(ctx->bound);

  for (it = 0; it < ctx->iters; it++)
    {
      ssize_t nbytes = read(fd, rxbuf, sizeof(rxbuf));

      if (nbytes != ISOTP_SESS_LEN ||
          check_payload(rxbuf, ISOTP_SESS_LEN,
                        0x600 + ctx->idx * 32 + it) < 0)
        {
          printf("ERROR: isotp sess %d recv it=%u nbytes=%zd\n",
                 ctx->idx, it, nbytes);
          goto done;
        }

      fill_test_payload(txbuf, ISOTP_SESS_LEN, 0x680 + ctx->idx * 32 + it);
      if (send(fd, txbuf, ISOTP_SESS_LEN, 0) != ISOTP_SESS_LEN)
        {
          printf("ERROR: isotp sess %d send it=%u errno=%d\n",
                 ctx->idx, it, errno);
          goto done;
        }
    }

  ctx->ok = 0;
  close(fd);
  return NULL;

done:

  /* Make sure the barrier is released even on the error path so the main
   * thread never blocks forever.
   */

  sem_post(ctx->bound);
  close(fd);
  return NULL;
}

static int run_isotp_concurrent_test(const char *ifname, unsigned int iters)
{
  struct isotp_sess_ctx ctx[ISOTP_SESS_N];
  pthread_t             tid[ISOTP_SESS_N];
  sem_t                 bound;
  int                   started = 0;
  int                   ret = 0;
  int                   i;

  sem_init(&bound, 0, 0);

  for (i = 0; i < ISOTP_SESS_N; i++)
    {
      ctx[i].ifname = ifname;
      ctx[i].idx    = i;
      ctx[i].iters  = iters;
      ctx[i].bound  = &bound;
      ctx[i].ok     = -1;

      if (spawn_thread(&tid[i], isotp_sess_worker, &ctx[i],
                       ISOTP_WORKER_STACK) != 0)
        {
          printf("ERROR: isotp sess pthread_create %d failed: %d\n",
                 i, errno);
          ret = -1;
          break;
        }

      started++;
    }

  for (i = 0; i < started; i++)
    {
      sem_wait(&bound);
    }

  if (ret == 0 && started == ISOTP_SESS_N &&
      sync_announce_wait(ifname, ISOTP_SESS_READY_ID,
                         ISOTP_SESS_GO_ID) < 0)
    {
      ret = -1;
    }

  for (i = 0; i < started; i++)
    {
      pthread_join(tid[i], NULL);
      if (ctx[i].ok < 0)
        {
          ret = -1;
        }
    }

  sem_destroy(&bound);

  if (ret == 0)
    {
      printf("can_socket_test: concurrent isotp OK sessions=%d iters=%u\n",
             ISOTP_SESS_N, iters);
    }

  return ret;
}

/****************************************************************************
 * Name: run_isotp_fault_test
 *
 * Description:
 *   Exercise empty receive, incomplete PDU, sequence failure, and missing
 *   flow control. Each error is followed by a valid transfer on the socket.
 ****************************************************************************/

static int run_isotp_fault_test(FAR const char *ifname, unsigned int phase)
{
  struct sockaddr_can addr;
  struct timeval timeout;
  struct pollfd pfd;
  struct timespec start;
  struct timespec end;
  uint8_t buf[32];
  ssize_t n;
  long elapsed;
  int fd;
  int ret = -1;

  timeout.tv_sec = 0;
  timeout.tv_usec = 250000;
  fd = socket(PF_CAN, SOCK_DGRAM, CAN_ISOTP);
  if (fd < 0)
    {
      return -1;
    }

  if (fill_can_addr(ifname, &addr) < 0 ||
      setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0)
    {
      goto out;
    }

  addr.can_addr.tp.rx_id = 0x680;
  addr.can_addr.tp.tx_id = 0x681;
  if (phase == 4 && enable_isotp_wait_tx_done(fd) < 0)
    {
      goto out;
    }

  if (bind(fd, (FAR struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
      goto out;
    }

  printf("isotptl: bound\n");
  fflush(stdout);
  if (phase == 4)
    {
      memset(buf, 0x5a, sizeof(buf));
      n = send(fd, buf, sizeof(buf), 0);
      if (n >= 0 || errno != ECOMM)
        {
          goto out;
        }

      ret = send(fd, buf, 7, 0) == 7 ? 0 : -1;
      goto out;
    }

  if (phase == 2)
    {
      /* An FF has arrived, but no complete PDU is ready. */

      pfd.fd = fd;
      pfd.events = POLLIN;
      if (poll(&pfd, 1, 100) != 0)
        {
          goto out;
        }

      clock_gettime(CLOCK_MONOTONIC, &start);
      n = recv(fd, buf, sizeof(buf), MSG_DONTWAIT);
      clock_gettime(CLOCK_MONOTONIC, &end);
      elapsed = (end.tv_sec - start.tv_sec) * 1000 +
                (end.tv_nsec - start.tv_nsec) / 1000000;
      if (n >= 0 || errno != EAGAIN || elapsed > 100)
        {
          goto out;
        }
    }

  n = recv(fd, buf, sizeof(buf), 0);
  if (n >= 0 || errno != (phase == 1 ? EAGAIN :
                          phase == 2 ? ECOMM : EILSEQ))
    {
      printf("isotptl: unexpected receive %zd errno=%d\n", n, errno);
      goto out;
    }

  /* Give the host a wire barrier before the recovery PDU. */

  buf[0] = 0xa5;
  if (send(fd, buf, 1, 0) != 1)
    {
      goto out;
    }

  timeout.tv_sec = 2;
  timeout.tv_usec = 0;
  if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0)
    {
      goto out;
    }

  n = recv(fd, buf, sizeof(buf), 0);
  ret = n == 1 && buf[0] == 0x5a ? 0 : -1;

out:
  close(fd);
  return ret;
}

/****************************************************************************
 * Name: run_isotp_api_test
 ****************************************************************************/

static int run_isotp_api_test(FAR const char *ifname)
{
  struct sockaddr_can addr;
  struct sockaddr_can actual;
  struct can_isotp_ll_options ll;
  struct can_isotp_options opts;
  socklen_t addrlen = sizeof(actual);
  int ret = -1;
  int fd;

  fd = socket(PF_CAN, SOCK_DGRAM, CAN_ISOTP);
  if (fd < 0 || fill_can_addr(ifname, &addr) < 0)
    {
      return -1;
    }

  memset(&ll, 0, sizeof(ll));
  ll.mtu = CAN_MTU;
  ll.tx_dl = 7;
  if (setsockopt(fd, SOL_CAN_ISOTP, CAN_ISOTP_LL_OPTS,
                 &ll, sizeof(ll)) == 0 || errno != EINVAL)
    {
      goto out;
    }

  memset(&opts, 0, sizeof(opts));
  opts.flags = CAN_ISOTP_EXTEND_ADDR;
  if (setsockopt(fd, SOL_CAN_ISOTP, CAN_ISOTP_OPTS,
                 &opts, sizeof(opts)) == 0 || errno != EINVAL)
    {
      goto out;
    }

  addr.can_addr.tp.rx_id = 0x682;
  addr.can_addr.tp.tx_id = 0x683;
  if (bind(fd, (FAR struct sockaddr *)&addr, 1) == 0 || errno != EINVAL ||
      bind(fd, (FAR struct sockaddr *)&addr, sizeof(addr)) < 0 ||
      bind(fd, (FAR struct sockaddr *)&addr, sizeof(addr)) == 0 ||
      errno != EINVAL)
    {
      goto out;
    }

  if (getsockname(fd, (FAR struct sockaddr *)&actual, &addrlen) < 0 ||
      addrlen != sizeof(actual) || actual.can_family != AF_CAN ||
      actual.can_ifindex != addr.can_ifindex ||
      actual.can_addr.tp.rx_id != 0x682 || actual.can_addr.tp.tx_id != 0x683)
    {
      goto out;
    }

  ret = 0;

out:
  close(fd);
  return ret;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  FAR const char *mode;
  FAR const char *ifname;
  unsigned int loops;
  bool canfd;
  bool waittx;
  int ret = -EINVAL;

  if (argc != 6)
    {
      printf("isotptl: FAIL usage: mode ifname loops fd waittx\n");
      return EXIT_FAILURE;
    }

  mode = argv[1];
  ifname = argv[2];
  loops = strtoul(argv[3], NULL, 0);
  canfd = strcmp(argv[4], "1") == 0;
  waittx = strcmp(argv[5], "1") == 0;
  if (loops == 0 || loops > 100 || netlib_ifup(ifname) < 0)
    {
      printf("isotptl: FAIL setup\n");
      return EXIT_FAILURE;
    }

  printf("isotptl: start mode=%s\n", mode);
  fflush(stdout);

  if (strcmp(mode, "exchange") == 0)
    {
      ret = run_isotp_test(ifname, canfd, waittx, loops, 0);
    }
  else if (strcmp(mode, "large") == 0)
    {
#ifdef CONFIG_LIBC_CAN_ISOTP_LONG_PDU
      ret = run_isotp_test(ifname, canfd, waittx, 1, 8192);
#else
      ret = run_isotp_test(ifname, canfd, waittx, 1, 4095);
#endif
    }
  else if (strcmp(mode, "api") == 0)
    {
      ret = run_isotp_api_test(ifname);
    }
  else if (strcmp(mode, "fault") == 0)
    {
      ret = run_isotp_fault_test(ifname, loops);
    }
  else if (strcmp(mode, "dontwait") == 0)
    {
      ret = run_isotp_dontwait_test(ifname, canfd, 0);
    }
  else if (strcmp(mode, "async") == 0)
    {
      ret = run_isotp_async_test(ifname, canfd, waittx);
    }
  else if (strcmp(mode, "backtoback") == 0)
    {
      ret = run_isotp_backtoback_test(ifname, canfd);
    }
  else if (strcmp(mode, "concurrent") == 0)
    {
      ret = run_isotp_concurrent_test(ifname, loops);
    }

  printf("isotptl: %s mode=%s\n", ret == 0 ? "PASS" : "FAIL", mode);
  return ret == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
