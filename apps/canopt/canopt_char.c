/****************************************************************************
 * apps/canopt/canopt_char.c
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
#include <sys/ioctl.h>
#include <sys/param.h>

#include <nuttx/can.h>
#include <nuttx/can/can.h>

#include "canopt.h"

#ifdef CONFIG_CAN

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define CANOPT_CHAR_TX_ID   0x5b0
#define CANOPT_CHAR_RX_ID   0x5b1
#define CANOPT_RTR_ID       0x3a0
#define CANOPT_ALIGN        16
#define CANOPT_MSG8         CAN_MSGLEN(8)
#define CANOPT_RXBUF        (8 * sizeof(struct can_msg_s))

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct canopt_crx_s
{
  int  classic;     /* Classic table messages received intact */
  int  fd;          /* CAN FD table messages received intact */
  int  err;         /* Unknown or corrupt messages */
  bool marker;      /* End marker seen */
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static int canopt_cfail(FAR const char *cmd, FAR const char *what)
{
  printf("canopt: FAIL %s %s errno=%d\n", cmd, what, errno);
  return EXIT_FAILURE;
}

/* Full CAN ID (with EFF/RTR flags) of a received message */

static uint32_t canopt_msg_id(FAR const struct can_msg_s *msg)
{
  uint32_t id = msg->cm_hdr.ch_id;

#ifdef CONFIG_CAN_EXTID
  if (msg->cm_hdr.ch_extid)
    {
      id |= CAN_EFF_FLAG;
    }
#endif

  if (msg->cm_hdr.ch_rtr)
    {
      id |= CAN_RTR_FLAG;
    }

  return id;
}

static bool canopt_msg_fd(FAR const struct can_msg_s *msg)
{
#ifdef CONFIG_CAN_FD
  return msg->cm_hdr.ch_edl;
#else
  return false;
#endif
}

static uint8_t canopt_msg_flags(FAR const struct can_msg_s *msg)
{
  uint8_t flags = 0;

#ifdef CONFIG_CAN_FD
  if (msg->cm_hdr.ch_brs)
    {
      flags |= CANFD_BRS;
    }

  if (msg->cm_hdr.ch_esi)
    {
      flags |= CANFD_ESI;
    }
#endif

  return flags;
}

/* Build a message from a frame table entry; returns its write length */

static size_t canopt_msg_build(FAR struct can_msg_s *msg,
                               FAR const struct canopt_frame_s *entry)
{
  bool rtr = (entry->id & CAN_RTR_FLAG) != 0;

  memset(msg, 0, sizeof(*msg));
  msg->cm_hdr.ch_id  = entry->id & CAN_EFF_MASK;
  msg->cm_hdr.ch_dlc = can_bytes2dlc(entry->len);
  msg->cm_hdr.ch_rtr = rtr;
#ifdef CONFIG_CAN_EXTID
  msg->cm_hdr.ch_extid = (entry->id & CAN_EFF_FLAG) != 0;
#endif
#ifdef CONFIG_CAN_FD
  msg->cm_hdr.ch_edl = entry->fd;
  msg->cm_hdr.ch_brs = (entry->flags & CANFD_BRS) != 0;
  msg->cm_hdr.ch_esi = (entry->flags & CANFD_ESI) != 0;
#endif

  if (rtr)
    {
      return CAN_MSGLEN(0);
    }

  canopt_fill(msg->cm_data, entry->id, entry->len);
  return CAN_MSGLEN(entry->len);
}

/* Check one received message against the frame tables */

static void canopt_crx_check(FAR const struct can_msg_s *msg,
                             FAR struct canopt_crx_s *st)
{
  FAR const struct canopt_frame_s *entry;
  uint32_t id = canopt_msg_id(msg);
  uint8_t len = can_dlc2bytes(msg->cm_hdr.ch_dlc);

  if (id == CANOPT_MARKER_ID)
    {
      st->marker = true;
      return;
    }

  if (id == CANOPT_KICK_ID)
    {
      return;
    }

  entry = canopt_find(id);
  if (entry == NULL || len != entry->len ||
      canopt_msg_fd(msg) != entry->fd ||
      canopt_msg_flags(msg) != entry->flags ||
      !canopt_data_ok(msg->cm_data, id, len))
    {
      printf("canopt: bad msg id=%" PRIx32 " dlc=%u fd=%d flags=%x\n",
             id, msg->cm_hdr.ch_dlc, canopt_msg_fd(msg),
             canopt_msg_flags(msg));
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

/* Walk the messages packed into one read() buffer (msgalign 1) */

static int canopt_crx_parse(FAR const uint8_t *buf, ssize_t nbytes,
                            FAR struct canopt_crx_s *st)
{
  struct can_msg_s msg;
  ssize_t off = 0;
  int nmsgs = 0;
  size_t len;

  while (off + (ssize_t)CAN_MSGLEN(0) <= nbytes)
    {
      memcpy(&msg.cm_hdr, buf + off, sizeof(msg.cm_hdr));
      len = CAN_MSGLEN(can_dlc2bytes(msg.cm_hdr.ch_dlc));
      if (off + (ssize_t)len > nbytes)
        {
          st->err++;
          break;
        }

      memcpy(&msg, buf + off, len);
      canopt_crx_check(&msg, st);
      off += len;
      nmsgs++;
    }

  return nmsgs;
}

/****************************************************************************
 * Name: canopt_crx
 *
 * Description:
 *   Receive both frame tables from the host and check ID, EFF, RTR, DLC
 *   (incl. CAN FD DLC 9..15), EDL/BRS/ESI and payload of every message.
 *
 ****************************************************************************/

static int canopt_crx(FAR struct canopt_args_s *args)
{
  uint8_t buf[CANOPT_RXBUF];
  struct canopt_crx_s st;
  struct timespec start;
  struct pollfd pfd;
  ssize_t nbytes;
  int nfd = 0;
  bool pass;
  int fd;

  memset(&st, 0, sizeof(st));

  fd = open(args->endpoint, O_RDWR);
  if (fd < 0)
    {
      return canopt_cfail(args->cmd, "open");
    }

  canopt_ready();
  clock_gettime(CLOCK_MONOTONIC, &start);

  pfd.fd     = fd;
  pfd.events = POLLIN;

  while (!st.marker &&
         poll(&pfd, 1, canopt_remain_ms(&start, args->timeout)) > 0)
    {
      nbytes = read(fd, buf, sizeof(buf));
      if (nbytes < 0)
        {
          st.err++;
          break;
        }

      canopt_crx_parse(buf, nbytes, &st);
    }

  close(fd);

#ifdef CONFIG_CAN_FD
  nfd = (int)g_canopt_nfd;
#endif

  pass = st.marker && st.err == 0 && st.classic == (int)g_canopt_nclassic &&
         st.fd == nfd;
  printf("canopt: %s crx classic=%d fd=%d err=%d\n",
         pass ? "PASS" : "FAIL", st.classic, st.fd, st.err);
  return pass ? EXIT_SUCCESS : EXIT_FAILURE;
}

/****************************************************************************
 * Name: canopt_ctx
 *
 * Description:
 *   Send both frame tables, one message per write().
 *
 ****************************************************************************/

static int canopt_ctx(FAR struct canopt_args_s *args)
{
  struct can_msg_s msg;
  int classic = 0;
  int nfd = 0;
  ssize_t len;
  size_t i;
  int fd;

  fd = open(args->endpoint, O_RDWR);
  if (fd < 0)
    {
      return canopt_cfail(args->cmd, "open");
    }

  for (i = 0; i < g_canopt_nclassic; i++)
    {
      len = canopt_msg_build(&msg, &g_canopt_classic[i]);
      if (write(fd, &msg, len) != len)
        {
          return canopt_cfail(args->cmd, "write-classic");
        }

      classic++;
    }

#ifdef CONFIG_CAN_FD
  for (i = 0; i < g_canopt_nfd; i++)
    {
      len = canopt_msg_build(&msg, &g_canopt_fdframes[i]);
      if (write(fd, &msg, len) != len)
        {
          return canopt_cfail(args->cmd, "write-fd");
        }

      nfd++;
    }
#endif

  close(fd);
  printf("canopt: PASS ctx classic=%d fd=%d\n", classic, nfd);
  return EXIT_SUCCESS;
}

/****************************************************************************
 * Name: canopt_cioctl
 *
 * Description:
 *   Upper-half ioctls (CANIOC_GET/SET_MSGALIGN, FIONWRITE) round trip;
 *   lower-half ioctls either work or report ENOTTY.
 *
 ****************************************************************************/

static bool canopt_lower_ok(int ret)
{
  return ret >= 0 || errno == ENOTTY;
}

static int canopt_cioctl(FAR struct canopt_args_s *args)
{
  FAR const char *cmd = args->cmd;
  struct canioc_bittiming_s bt;
  struct canioc_stdfilter_s sf;
  unsigned int align;
  int checks = 0;
  int pending;
  int ret;
  int fd;

  fd = open(args->endpoint, O_RDWR);
  if (fd < 0)
    {
      return canopt_cfail(cmd, "open");
    }

  align = 99;
  if (ioctl(fd, CANIOC_GET_MSGALIGN, (unsigned long)&align) < 0 ||
      align != 1)
    {
      return canopt_cfail(cmd, "msgalign-default");
    }

  checks++;

  align = 0;
  if (ioctl(fd, CANIOC_SET_MSGALIGN, (unsigned long)&align) < 0)
    {
      return canopt_cfail(cmd, "msgalign-set");
    }

  align = 99;
  if (ioctl(fd, CANIOC_GET_MSGALIGN, (unsigned long)&align) < 0 ||
      align != 0)
    {
      return canopt_cfail(cmd, "msgalign-get");
    }

  checks++;

  pending = -1;
  if (ioctl(fd, FIONWRITE, (unsigned long)&pending) < 0 || pending != 0)
    {
      return canopt_cfail(cmd, "fionwrite");
    }

  checks++;

  memset(&bt, 0, sizeof(bt));
  ret = ioctl(fd, CANIOC_GET_BITTIMING, (unsigned long)&bt);
  if (!canopt_lower_ok(ret) || (ret >= 0 && bt.bt_baud == 0))
    {
      return canopt_cfail(cmd, "get-bittiming");
    }

  checks++;

  memset(&sf, 0, sizeof(sf));
  sf.sf_id1  = 0x123;
  sf.sf_id2  = CAN_SFF_MASK;
  sf.sf_type = CAN_FILTER_MASK;
  sf.sf_prio = CAN_MSGPRIO_HIGH;
  ret = ioctl(fd, CANIOC_ADD_STDFILTER, (unsigned long)&sf);
  if (!canopt_lower_ok(ret) ||
      (ret >= 0 && !canopt_lower_ok(ioctl(fd, CANIOC_DEL_STDFILTER, ret))))
    {
      return canopt_cfail(cmd, "stdfilter");
    }

  checks++;

  close(fd);
  printf("canopt: PASS cioctl checks=%d\n", checks);
  return EXIT_SUCCESS;
}

/****************************************************************************
 * Name: canopt_calign
 *
 * Description:
 *   CANIOC_SET_MSGALIGN effect on read(): with the default alignment 1 a
 *   read returns as many packed messages as fit, with 0 exactly one, with
 *   16 each message is padded to 16 bytes.  The host sends 6 classic
 *   8-byte frames before the first read.
 *
 ****************************************************************************/

static int canopt_calign(FAR struct canopt_args_s *args)
{
  uint8_t buf[CANOPT_RXBUF];
  unsigned int align;
  ssize_t n1;
  ssize_t n2;
  ssize_t n3;
  ssize_t padded;
  bool pass;
  int fd;

  fd = open(args->endpoint, O_RDWR);
  if (fd < 0)
    {
      return canopt_cfail(args->cmd, "open");
    }

  canopt_ready();
  usleep(1000 * 1000);

  /* Alignment 1: three messages fit exactly */

  n1 = read(fd, buf, 3 * CANOPT_MSG8);

  align = 0;
  ioctl(fd, CANIOC_SET_MSGALIGN, (unsigned long)&align);
  n2 = read(fd, buf, sizeof(buf));

  align = CANOPT_ALIGN;
  ioctl(fd, CANIOC_SET_MSGALIGN, (unsigned long)&align);
  n3 = read(fd, buf, sizeof(buf));

  close(fd);

  padded = (CANOPT_MSG8 + CANOPT_ALIGN - 1) / CANOPT_ALIGN * CANOPT_ALIGN;
  pass = n1 == 3 * CANOPT_MSG8 && n2 == CANOPT_MSG8 && n3 == 2 * padded;
  printf("canopt: %s calign n1=%zd n2=%zd n3=%zd msglen=%zu\n",
         pass ? "PASS" : "FAIL", n1, n2, n3, (size_t)CANOPT_MSG8);
  return pass ? EXIT_SUCCESS : EXIT_FAILURE;
}

/****************************************************************************
 * Name: canopt_cnonblock
 *
 * Description:
 *   O_NONBLOCK read returns EAGAIN when empty, poll() times out on POLLIN
 *   and reports POLLOUT, a nonblocking write is sent, then poll() wakes up
 *   for a host frame.
 *
 ****************************************************************************/

static int canopt_cnonblock(FAR struct canopt_args_s *args)
{
  FAR const char *cmd = args->cmd;
  struct can_msg_s msg;
  struct pollfd pfd;
  ssize_t nbytes;
  int fd;

  fd = open(args->endpoint, O_RDWR | O_NONBLOCK);
  if (fd < 0)
    {
      return canopt_cfail(cmd, "open");
    }

  if (read(fd, &msg, sizeof(msg)) >= 0 || errno != EAGAIN)
    {
      return canopt_cfail(cmd, "nonblock-read");
    }

  pfd.fd      = fd;
  pfd.events  = POLLIN;
  pfd.revents = 0;
  if (poll(&pfd, 1, 100) != 0)
    {
      return canopt_cfail(cmd, "poll-idle");
    }

  pfd.events  = POLLOUT;
  pfd.revents = 0;
  if (poll(&pfd, 1, 0) != 1 || (pfd.revents & POLLOUT) == 0)
    {
      return canopt_cfail(cmd, "pollout");
    }

  memset(&msg, 0, sizeof(msg));
  msg.cm_hdr.ch_id  = CANOPT_CHAR_TX_ID;
  msg.cm_hdr.ch_dlc = 8;
  canopt_fill(msg.cm_data, CANOPT_CHAR_TX_ID, 8);
  if (write(fd, &msg, CANOPT_MSG8) != CANOPT_MSG8)
    {
      return canopt_cfail(cmd, "nonblock-write");
    }

  canopt_ready();

  pfd.events  = POLLIN;
  pfd.revents = 0;
  if (poll(&pfd, 1, args->timeout * 1000) != 1 ||
      (pfd.revents & POLLIN) == 0)
    {
      return canopt_cfail(cmd, "pollin");
    }

  nbytes = read(fd, &msg, sizeof(msg));
  if (nbytes != CANOPT_MSG8 || msg.cm_hdr.ch_id != CANOPT_CHAR_RX_ID ||
      !canopt_data_ok(msg.cm_data, CANOPT_CHAR_RX_ID, 8))
    {
      return canopt_cfail(cmd, "read");
    }

  if (read(fd, &msg, sizeof(msg)) >= 0 || errno != EAGAIN)
    {
      return canopt_cfail(cmd, "drained-read");
    }

  close(fd);
  printf("canopt: PASS cnonblock\n");
  return EXIT_SUCCESS;
}

/****************************************************************************
 * Name: canopt_coverflow
 *
 * Description:
 *   RX FIFO overflow: the host floods the bus while nobody reads; the
 *   reader gets one CAN_ERROR_INTERNAL / CAN_ERROR5_RXOVERFLOW error
 *   message, then the CONFIG_CAN_RXFIFOSIZE - 1 queued messages.
 *
 ****************************************************************************/

#ifdef CONFIG_CAN_ERRORS
static int canopt_coverflow(FAR struct canopt_args_s *args)
{
  uint8_t buf[CANOPT_RXBUF];
  struct can_msg_s msg;
  ssize_t nbytes;
  ssize_t off;
  int errmsgs = 0;
  int other = 0;
  int rx = 0;
  bool pass;
  int fd;

  fd = open(args->endpoint, O_RDWR | O_NONBLOCK);
  if (fd < 0)
    {
      return canopt_cfail(args->cmd, "open");
    }

  canopt_ready();
  usleep(1500 * 1000);

  while ((nbytes = read(fd, buf, sizeof(buf))) > 0)
    {
      off = 0;
      while (off + (ssize_t)CAN_MSGLEN(0) <= nbytes)
        {
          memcpy(&msg, buf + off, MIN(sizeof(msg), (size_t)(nbytes - off)));
          off += CAN_MSGLEN(can_dlc2bytes(msg.cm_hdr.ch_dlc));

          if (msg.cm_hdr.ch_error)
            {
              if ((msg.cm_hdr.ch_id & CAN_ERROR_INTERNAL) != 0 &&
                  (msg.cm_data[5] & CAN_ERROR5_RXOVERFLOW) != 0)
                {
                  errmsgs++;
                }
              else
                {
                  other++;
                }
            }
          else
            {
              rx++;
            }
        }
    }

  close(fd);

  pass = errmsgs == 1 && other == 0 && rx == CONFIG_CAN_RXFIFOSIZE - 1;
  printf("canopt: %s coverflow errmsgs=%d other=%d rx=%d\n",
         pass ? "PASS" : "FAIL", errmsgs, other, rx);
  return pass ? EXIT_SUCCESS : EXIT_FAILURE;
}
#endif

/****************************************************************************
 * Name: canopt_cfionread
 *
 * Description:
 *   FIONREAD reports the number of queued messages as an int.
 *
 ****************************************************************************/

static int canopt_cfionread(FAR struct canopt_args_s *args)
{
  int queued;
  int fd;

  fd = open(args->endpoint, O_RDWR);
  if (fd < 0)
    {
      return canopt_cfail(args->cmd, "open");
    }

  canopt_ready();
  usleep(1000 * 1000);

  queued = -1;
  if (ioctl(fd, FIONREAD, (unsigned long)&queued) < 0)
    {
      return canopt_cfail(args->cmd, "ioctl");
    }

  close(fd);
  printf("canopt: %s cfionread queued=%d expected=%d\n",
         queued == args->count ? "PASS" : "FAIL", queued, args->count);
  return queued == args->count ? EXIT_SUCCESS : EXIT_FAILURE;
}

/****************************************************************************
 * Name: canopt_ciflush
 *
 * Description:
 *   CANIOC_IFLUSH drops queued messages: the ioctl succeeds and a
 *   nonblocking read afterwards returns EAGAIN.
 *
 ****************************************************************************/

static int canopt_ciflush(FAR struct canopt_args_s *args)
{
  struct can_msg_s msg;
  ssize_t nbytes;
  int ret;
  int ioerr;
  int rderr;
  int fd;

  fd = open(args->endpoint, O_RDWR | O_NONBLOCK);
  if (fd < 0)
    {
      return canopt_cfail(args->cmd, "open");
    }

  canopt_ready();
  usleep(1000 * 1000);

  ret    = ioctl(fd, CANIOC_IFLUSH, 0);
  ioerr  = ret < 0 ? errno : 0;
  nbytes = read(fd, &msg, sizeof(msg));
  rderr  = nbytes < 0 ? errno : 0;
  close(fd);

  if (ret == 0 && nbytes < 0 && rderr == EAGAIN)
    {
      printf("canopt: PASS ciflush\n");
      return EXIT_SUCCESS;
    }

  printf("canopt: FAIL ciflush ioctl=%d errno=%d read=%zd errno=%d\n",
         ret, ioerr, nbytes, rderr);
  return EXIT_FAILURE;
}

/****************************************************************************
 * Name: canopt_crtr
 *
 * Description:
 *   CANIOC_RTR sends a remote request and returns the host's response.
 *
 ****************************************************************************/

static int canopt_crtr(FAR struct canopt_args_s *args)
{
  struct canioc_rtr_s rtr;
  struct can_msg_s msg;
  int ret;
  int fd;

  fd = open(args->endpoint, O_RDWR);
  if (fd < 0)
    {
      return canopt_cfail(args->cmd, "open");
    }

  memset(&msg, 0, sizeof(msg));
  msg.cm_hdr.ch_id  = CANOPT_RTR_ID;
  msg.cm_hdr.ch_dlc = 8;

  rtr.ci_timeout.tv_sec  = 2;
  rtr.ci_timeout.tv_nsec = 0;
  rtr.ci_msg             = &msg;

  ret = ioctl(fd, CANIOC_RTR, (unsigned long)&rtr);
  close(fd);

  if (ret < 0)
    {
      return canopt_cfail(args->cmd, "ioctl");
    }

  if (msg.cm_hdr.ch_id != CANOPT_RTR_ID || msg.cm_hdr.ch_dlc != 8 ||
      !canopt_data_ok(msg.cm_data, CANOPT_RTR_ID, 8))
    {
      printf("canopt: FAIL crtr response id=%x dlc=%u\n",
             (unsigned int)msg.cm_hdr.ch_id, msg.cm_hdr.ch_dlc);
      return EXIT_FAILURE;
    }

  printf("canopt: PASS crtr\n");
  return EXIT_SUCCESS;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int canopt_char_main(FAR struct canopt_args_s *args)
{
  FAR const char *cmd = args->cmd;

  if (strcmp(cmd, "crx") == 0)
    {
      return canopt_crx(args);
    }
  else if (strcmp(cmd, "ctx") == 0)
    {
      return canopt_ctx(args);
    }
  else if (strcmp(cmd, "cioctl") == 0)
    {
      return canopt_cioctl(args);
    }
  else if (strcmp(cmd, "calign") == 0)
    {
      return canopt_calign(args);
    }
  else if (strcmp(cmd, "cnonblock") == 0)
    {
      return canopt_cnonblock(args);
    }
#ifdef CONFIG_CAN_ERRORS
  else if (strcmp(cmd, "coverflow") == 0)
    {
      return canopt_coverflow(args);
    }
#endif
  else if (strcmp(cmd, "cfionread") == 0)
    {
      return canopt_cfionread(args);
    }
  else if (strcmp(cmd, "ciflush") == 0)
    {
      return canopt_ciflush(args);
    }
  else if (strcmp(cmd, "crtr") == 0)
    {
      return canopt_crtr(args);
    }

  printf("canopt: FAIL %s unknown character driver check\n", cmd);
  return EXIT_FAILURE;
}

#endif /* CONFIG_CAN */
