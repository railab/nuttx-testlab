/****************************************************************************
 * apps/conode/conode_main.c
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
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <net/if.h>
#include <sys/socket.h>

#include <nuttx/can.h>

#include "netutils/netlib.h"

#include <canutils/lely/config.h>

#include <lely/can/net.h>
#include <lely/co/csdo.h>
#include <lely/co/dev.h>
#include <lely/co/nmt.h>
#include <lely/co/obj.h>
#include <lely/co/type.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define CONODE_MAXID       127
#define CONODE_SDO_TMO_MS  500
#define CONODE_POLL_MAX_MS 100

/* Object dictionary entries of the application */

#define OBJ_BOOTVAL        0x2000 /* written by the master at boot-up */
#define OBJ_SCRATCH        0x2001 /* free for SDO tests */
#define OBJ_COUNTER        0x2100 /* TPDO1, +1 per SYNC in OPERATIONAL */
#define OBJ_RXVAL          0x2200 /* [id] last counter received from id */
#define OBJ_RXCNT          0x2201 /* [id] PDOs received from id */
#define OBJ_RXLOST         0x2202 /* [id] counter values skipped */
#define OBJ_RXBAD          0x2203 /* [id] counter values not increasing */
#define OBJ_NMTST          0x2204 /* [id] last NMT state seen of id */
#define OBJ_HBTMO          0x2205 /* [id] heartbeat timeouts of id */
#define OBJ_HBRES          0x2206 /* [id] heartbeat timeouts resolved */
#define OBJ_BOOTUPS        0x2207 /* [id] boot-up messages of id */
#define OBJ_RXRESET        0x2210 /* write non-zero to clear the PDO stats */
#define OBJ_TXERR          0x2211 /* frames the socket did not accept */

/****************************************************************************
 * Private Types
 ****************************************************************************/

enum boot_step_e
{
  BOOT_IDLE,
  BOOT_DN_WAIT,  /* SDO download of OBJ_BOOTVAL in flight */
  BOOT_UP,       /* download done, read the value back */
  BOOT_UP_WAIT,  /* SDO upload of OBJ_BOOTVAL in flight */
  BOOT_START     /* value read back, send NMT start */
};

struct conode_slave_s
{
  uint8_t          id;
  bool             pending;  /* boot-up seen, boot the slave */
  int              step;
  uint16_t         boots;
  co_unsigned32_t  val;
  co_csdo_t       *sdo;
};

struct conode_rx_s
{
  uint32_t last;
  uint32_t cnt;
  uint32_t lost;
  uint32_t bad;
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

static int g_sock = -1;
static uint32_t g_txerr;
static int g_txerrno;
static volatile sig_atomic_t g_stop;
static struct timespec g_next;
static uint8_t g_id;
static bool g_master;
static uint8_t g_maxid;
static struct conode_slave_s g_slaves[CONODE_MAXID];
static int g_nslaves;
static uint8_t g_peers[CONODE_MAXID];
static int g_npeers;
static struct conode_rx_s g_rx[CONODE_MAXID + 1];
static co_dev_t *g_dev;
static co_nmt_t *g_nmt;

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/****************************************************************************
 * Name: conode_usage
 ****************************************************************************/

static void conode_usage(void)
{
  printf("Usage: conode -i <id> [-m] [-r <id,...>] [-s <sync ms>] "
         "[-b <hb ms>] [-c <hb timeout ms>] [-d <dev>] [slave id ...]\n"
         "  -i  node-ID (1-127)\n"
         "  -m  NMT master and SYNC producer; boots the slave ids\n"
         "  -r  receive TPDO1 of these node-IDs (master: the slaves)\n"
         "  -s  SYNC period in ms (master, default 100)\n"
         "  -b  heartbeat period in ms (default 100)\n"
         "  -c  heartbeat consumer timeout in ms (master, default 500)\n"
         "  -d  SocketCAN interface (default can0)\n");
}

/****************************************************************************
 * Name: conode_parse_ids
 ****************************************************************************/

static int conode_parse_ids(FAR char *arg, FAR uint8_t *ids, int max)
{
  FAR char *tok;
  FAR char *save;
  FAR char *end;
  long id;
  int n = 0;

  for (tok = strtok_r(arg, ",", &save); tok != NULL;
       tok = strtok_r(NULL, ",", &save))
    {
      id = strtol(tok, &end, 0);
      if (*end != '\0' || id < 1 || id > CONODE_MAXID || n >= max)
        {
          return -1;
        }

      ids[n++] = (uint8_t)id;
    }

  return n;
}

/****************************************************************************
 * Name: conode_state_name
 ****************************************************************************/

static FAR const char *conode_state_name(co_unsigned8_t st)
{
  switch (st)
    {
      case CO_NMT_ST_BOOTUP:
        return "BOOTUP";

      case CO_NMT_ST_STOP:
        return "STOPPED";

      case CO_NMT_ST_START:
        return "OPERATIONAL";

      case CO_NMT_ST_RESET_NODE:
        return "RESET_NODE";

      case CO_NMT_ST_RESET_COMM:
        return "RESET_COMM";

      case CO_NMT_ST_PREOP:
        return "PREOP";

      default:
        return "UNKNOWN";
    }
}

/****************************************************************************
 * Name: conode_sigterm
 ****************************************************************************/

static void conode_sigterm(int signo)
{
  g_stop = 1;
}

/****************************************************************************
 * Name: conode_can_init
 ****************************************************************************/

static int conode_can_init(FAR const char *ifname)
{
  struct sockaddr_can addr;
  struct ifreq ifr;

  g_sock = socket(PF_CAN, SOCK_RAW, CAN_RAW);
  if (g_sock < 0)
    {
      printf("conode: FAIL socket %d\n", errno);
      return -1;
    }

  strlcpy(ifr.ifr_name, ifname, IFNAMSIZ);
  if (netlib_ifup(ifr.ifr_name) < 0)
    {
      printf("conode: FAIL ifup %s %d\n", ifname, errno);
      return -1;
    }

  ifr.ifr_ifindex = if_nametoindex(ifr.ifr_name);
  if (ifr.ifr_ifindex == 0)
    {
      printf("conode: FAIL if_nametoindex %s %d\n", ifname, errno);
      return -1;
    }

  memset(&addr, 0, sizeof(addr));
  addr.can_family  = AF_CAN;
  addr.can_ifindex = ifr.ifr_ifindex;

  if (bind(g_sock, (FAR struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
      printf("conode: FAIL bind %d\n", errno);
      return -1;
    }

  if (fcntl(g_sock, F_SETFL, O_NONBLOCK) < 0)
    {
      printf("conode: FAIL fcntl %d\n", errno);
      return -1;
    }

  return 0;
}

/****************************************************************************
 * Name: conode_can_send
 ****************************************************************************/

static int conode_can_send(FAR const struct can_msg *msg, FAR void *data)
{
  struct can_frame frame;

  memset(&frame, 0, sizeof(frame));
  frame.can_id = msg->id;
  if (msg->flags & CAN_FLAG_IDE)
    {
      frame.can_id |= CAN_EFF_FLAG;
    }

  if (msg->flags & CAN_FLAG_RTR)
    {
      frame.can_id |= CAN_RTR_FLAG;
    }

  frame.can_dlc = msg->len;
  memcpy(frame.data, msg->data, msg->len);

  if (write(g_sock, &frame, sizeof(frame)) != sizeof(frame))
    {
      g_txerr++;
      g_txerrno = errno;
      return -1;
    }

  return 0;
}

/****************************************************************************
 * Name: conode_can_recv
 ****************************************************************************/

static int conode_can_recv(FAR struct can_msg *msg)
{
  struct can_frame frame;
  ssize_t nread;

  nread = read(g_sock, &frame, sizeof(frame));
  if (nread != sizeof(frame))
    {
      return 0;
    }

  memset(msg, 0, sizeof(*msg));
  msg->id = frame.can_id & CAN_EFF_MASK;
  if (frame.can_id & CAN_EFF_FLAG)
    {
      msg->flags |= CAN_FLAG_IDE;
    }

  if (frame.can_id & CAN_RTR_FLAG)
    {
      msg->flags |= CAN_FLAG_RTR;
    }

  msg->len = frame.can_dlc > 8 ? 8 : frame.can_dlc;
  memcpy(msg->data, frame.data, msg->len);
  return 1;
}

/****************************************************************************
 * Name: conode_on_next
 *
 * Description:
 *   Remember when the next Lely timer is due, to bound the poll() timeout.
 *
 ****************************************************************************/

static int conode_on_next(FAR const struct timespec *tp, FAR void *data)
{
  g_next = *tp;
  return 0;
}

/****************************************************************************
 * Name: conode_od_add
 ****************************************************************************/

static FAR co_sub_t *conode_od_add(uint16_t idx, uint8_t subidx,
                                   uint16_t type, uint32_t val)
{
  FAR co_obj_t *obj;
  FAR co_sub_t *sub;

  obj = co_dev_find_obj(g_dev, idx);
  if (obj == NULL)
    {
      obj = co_obj_create(idx);
      if (obj == NULL || co_dev_insert_obj(g_dev, obj) < 0)
        {
          return NULL;
        }
    }

  if (subidx > 0)
    {
      co_obj_set_code(obj, idx == 0x1016 || idx >= 0x2200 ?
                      CO_OBJECT_ARRAY : CO_OBJECT_RECORD);
    }

  sub = co_sub_create(subidx, type);
  if (sub == NULL || co_obj_insert_sub(obj, sub) < 0)
    {
      return NULL;
    }

  switch (type)
    {
      case CO_DEFTYPE_UNSIGNED8:
        co_sub_set_val_u8(sub, (co_unsigned8_t)val);
        break;

      case CO_DEFTYPE_UNSIGNED16:
        co_sub_set_val_u16(sub, (co_unsigned16_t)val);
        break;

      default:
        co_sub_set_val_u32(sub, val);
        break;
    }

  return sub;
}

/****************************************************************************
 * Name: conode_rx_set
 ****************************************************************************/

static void conode_rx_set(uint8_t id)
{
  co_dev_set_val_u32(g_dev, OBJ_RXCNT, id, g_rx[id].cnt);
  co_dev_set_val_u32(g_dev, OBJ_RXLOST, id, g_rx[id].lost);
  co_dev_set_val_u32(g_dev, OBJ_RXBAD, id, g_rx[id].bad);
}

/****************************************************************************
 * Name: conode_rx_ind
 *
 * Description:
 *   Download indication of OBJ_RXVAL, called for each received RPDO.
 *   Checks that the producer's counter grows by one per PDO.
 *
 ****************************************************************************/

static co_unsigned32_t conode_rx_ind(FAR co_sub_t *sub,
                                     FAR struct co_sdo_req *req,
                                     FAR void *data)
{
  FAR struct conode_rx_s *rx;
  co_unsigned32_t ac = 0;
  uint32_t val;
  uint8_t id;

  if (co_sub_on_dn(sub, req, &ac) < 0)
    {
      return ac;
    }

  id  = co_sub_get_subidx(sub);
  rx  = &g_rx[id];
  val = co_sub_get_val_u32(sub);

  if (rx->cnt > 0)
    {
      if (val == rx->last || val - rx->last > 0x80000000u)
        {
          rx->bad++;
        }
      else
        {
          rx->lost += val - rx->last - 1;
        }
    }

  rx->cnt++;
  rx->last = val;
  conode_rx_set(id);
  return 0;
}

/****************************************************************************
 * Name: conode_rx_reset
 ****************************************************************************/

static void conode_rx_reset(void)
{
  int i;

  memset(g_rx, 0, sizeof(g_rx));
  for (i = 1; i <= g_maxid; i++)
    {
      conode_rx_set(i);
    }

  co_dev_set_val_u8(g_dev, OBJ_RXRESET, 0, 0);
}

/****************************************************************************
 * Name: conode_od_build
 *
 * Description:
 *   Build the object dictionary for the node-ID and role.  Lely keeps a
 *   copy of it at co_nmt_create() and restores it on NMT reset, so it is
 *   complete before the NMT service is created.
 *
 ****************************************************************************/

static int conode_od_build(int sync_ms, int hb_ms, int hbc_ms)
{
  FAR co_sub_t *sub;
  bool ok = true;
  int i;

  g_dev = co_dev_create(g_id);
  if (g_dev == NULL)
    {
      return -1;
    }

  ok &= conode_od_add(0x1000, 0, CO_DEFTYPE_UNSIGNED32, 0) != NULL;
  ok &= conode_od_add(0x1001, 0, CO_DEFTYPE_UNSIGNED8, 0) != NULL;
  ok &= conode_od_add(0x1005, 0, CO_DEFTYPE_UNSIGNED32,
                      g_master ? 0x40000080 : 0x80) != NULL;
  ok &= conode_od_add(0x1006, 0, CO_DEFTYPE_UNSIGNED32,
                      g_master ? sync_ms * 1000 : 0) != NULL;
  ok &= conode_od_add(0x1017, 0, CO_DEFTYPE_UNSIGNED16, hb_ms) != NULL;
  ok &= conode_od_add(0x1018, 0, CO_DEFTYPE_UNSIGNED8, 1) != NULL;
  ok &= conode_od_add(0x1018, 1, CO_DEFTYPE_UNSIGNED32, 0x360) != NULL;

  /* Master: consume the heartbeat of every slave */

  if (g_master)
    {
      ok &= conode_od_add(0x1016, 0, CO_DEFTYPE_UNSIGNED8,
                          g_nslaves) != NULL;
      for (i = 0; i < g_nslaves; i++)
        {
          ok &= conode_od_add(0x1016, i + 1, CO_DEFTYPE_UNSIGNED32,
                              ((uint32_t)g_slaves[i].id << 16) |
                              hbc_ms) != NULL;
        }
    }

  /* RPDO n receives TPDO1 of peer n into OBJ_RXVAL[peer] */

  for (i = 0; i < g_npeers; i++)
    {
      ok &= conode_od_add(0x1400 + i, 0, CO_DEFTYPE_UNSIGNED8, 2) != NULL;
      ok &= conode_od_add(0x1400 + i, 1, CO_DEFTYPE_UNSIGNED32,
                          0x180 + g_peers[i]) != NULL;
      ok &= conode_od_add(0x1400 + i, 2, CO_DEFTYPE_UNSIGNED8,
                          0xff) != NULL;
      ok &= conode_od_add(0x1600 + i, 0, CO_DEFTYPE_UNSIGNED8, 1) != NULL;
      ok &= conode_od_add(0x1600 + i, 1, CO_DEFTYPE_UNSIGNED32,
                          ((uint32_t)OBJ_RXVAL << 16) |
                          ((uint32_t)g_peers[i] << 8) | 0x20) != NULL;
    }

  /* TPDO1 sends the counter on every SYNC */

  ok &= conode_od_add(0x1800, 0, CO_DEFTYPE_UNSIGNED8, 2) != NULL;
  ok &= conode_od_add(0x1800, 1, CO_DEFTYPE_UNSIGNED32,
                      0x180 + g_id) != NULL;
  ok &= conode_od_add(0x1800, 2, CO_DEFTYPE_UNSIGNED8, 1) != NULL;
  ok &= conode_od_add(0x1a00, 0, CO_DEFTYPE_UNSIGNED8, 1) != NULL;
  ok &= conode_od_add(0x1a00, 1, CO_DEFTYPE_UNSIGNED32,
                      ((uint32_t)OBJ_COUNTER << 16) | 0x20) != NULL;

  /* NMT startup: master boots itself to OPERATIONAL, slaves wait */

  ok &= conode_od_add(0x1f80, 0, CO_DEFTYPE_UNSIGNED32,
                      g_master ? 0x01 : 0x04) != NULL;

  ok &= conode_od_add(OBJ_BOOTVAL, 0, CO_DEFTYPE_UNSIGNED32, 0) != NULL;
  ok &= conode_od_add(OBJ_SCRATCH, 0, CO_DEFTYPE_UNSIGNED32, 0) != NULL;

  sub = conode_od_add(OBJ_COUNTER, 0, CO_DEFTYPE_UNSIGNED32, 0);
  if (sub != NULL)
    {
      co_sub_set_pdo_mapping(sub, 1);
    }

  ok &= sub != NULL;

  /* Per-node PDO statistics and NMT view, sub-index = node-ID */

  for (i = OBJ_RXVAL; i <= OBJ_BOOTUPS; i++)
    {
      int id;

      ok &= conode_od_add(i, 0, CO_DEFTYPE_UNSIGNED8, g_maxid) != NULL;
      for (id = 1; id <= g_maxid; id++)
        {
          sub = conode_od_add(i, id, i == OBJ_NMTST ?
                              CO_DEFTYPE_UNSIGNED8 : CO_DEFTYPE_UNSIGNED32,
                              i == OBJ_NMTST ? 0xff : 0);
          ok &= sub != NULL;
          if (sub != NULL && i == OBJ_RXVAL)
            {
              co_sub_set_pdo_mapping(sub, 1);
              co_sub_set_dn_ind(sub, conode_rx_ind, NULL);
            }
          else if (sub != NULL)
            {
              co_sub_set_access(sub, CO_ACCESS_RO);
            }
        }
    }

  ok &= conode_od_add(OBJ_RXRESET, 0, CO_DEFTYPE_UNSIGNED8, 0) != NULL;
  ok &= conode_od_add(OBJ_TXERR, 0, CO_DEFTYPE_UNSIGNED32, 0) != NULL;

  return ok ? 0 : -1;
}

/****************************************************************************
 * Name: conode_find_slave
 ****************************************************************************/

static FAR struct conode_slave_s *conode_find_slave(co_unsigned8_t id)
{
  int i;

  for (i = 0; i < g_nslaves; i++)
    {
      if (g_slaves[i].id == id)
        {
          return &g_slaves[i];
        }
    }

  return NULL;
}

/****************************************************************************
 * Name: conode_count
 ****************************************************************************/

static void conode_count(uint16_t idx, co_unsigned8_t id)
{
  co_dev_set_val_u32(g_dev, idx, id, co_dev_get_val_u32(g_dev, idx, id) + 1);
}

/****************************************************************************
 * Name: conode_on_st
 *
 * Description:
 *   NMT state of a node changed (its own, a boot-up or a heartbeat).
 *
 ****************************************************************************/

static void conode_on_st(FAR co_nmt_t *nmt, co_unsigned8_t id,
                         co_unsigned8_t st, FAR void *data)
{
  FAR struct conode_slave_s *slave;

  printf("conode: st %u %s\n", id, conode_state_name(st));

  if (id <= g_maxid)
    {
      co_dev_set_val_u8(g_dev, OBJ_NMTST, id, st);
      if (st == CO_NMT_ST_BOOTUP && id != g_id)
        {
          conode_count(OBJ_BOOTUPS, id);
        }
    }

  slave = conode_find_slave(id);
  if (g_master && slave != NULL && st == CO_NMT_ST_BOOTUP)
    {
      slave->pending = true;
    }
}

/****************************************************************************
 * Name: conode_on_hb
 ****************************************************************************/

static void conode_on_hb(FAR co_nmt_t *nmt, co_unsigned8_t id, int state,
                         int reason, FAR void *data)
{
  if (reason == CO_NMT_EC_TIMEOUT)
    {
      printf("conode: hb %u %s\n", id,
             state == CO_NMT_EC_OCCURRED ? "timeout" : "resolved");
      if (id <= g_maxid)
        {
          conode_count(state == CO_NMT_EC_OCCURRED ? OBJ_HBTMO : OBJ_HBRES,
                       id);
        }
    }
}

/****************************************************************************
 * Name: conode_on_sync
 *
 * Description:
 *   Called after TPDO1 went out for this SYNC: advance the counter so the
 *   next PDO carries the next value.
 *
 ****************************************************************************/

static void conode_on_sync(FAR co_nmt_t *nmt, co_unsigned8_t cnt,
                           FAR void *data)
{
  if (co_nmt_get_st(nmt) == CO_NMT_ST_START)
    {
      co_dev_set_val_u32(g_dev, OBJ_COUNTER, 0,
                         co_dev_get_val_u32(g_dev, OBJ_COUNTER, 0) + 1);
    }
}

/****************************************************************************
 * Name: conode_boot_fail
 ****************************************************************************/

static void conode_boot_fail(FAR struct conode_slave_s *slave,
                             FAR const char *what, co_unsigned32_t ac)
{
  printf("conode: boot %u FAIL %s 0x%08" PRIx32 "\n", slave->id, what, ac);
  slave->step = BOOT_IDLE;
}

/****************************************************************************
 * Name: conode_on_dn
 ****************************************************************************/

static void conode_on_dn(FAR co_csdo_t *sdo, co_unsigned16_t idx,
                         co_unsigned8_t subidx, co_unsigned32_t ac,
                         FAR void *data)
{
  FAR struct conode_slave_s *slave = data;

  if (ac != 0)
    {
      conode_boot_fail(slave, "download", ac);
      return;
    }

  slave->step = BOOT_UP;
}

/****************************************************************************
 * Name: conode_on_up
 ****************************************************************************/

static void conode_on_up(FAR co_csdo_t *sdo, co_unsigned16_t idx,
                         co_unsigned8_t subidx, co_unsigned32_t ac,
                         FAR const void *ptr, size_t n, FAR void *data)
{
  FAR struct conode_slave_s *slave = data;
  FAR const uint8_t *p = ptr;
  co_unsigned32_t val = 0;

  if (ac != 0)
    {
      conode_boot_fail(slave, "upload", ac);
      return;
    }

  if (n == 4)
    {
      val = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
            ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
    }

  if (n != 4 || val != slave->val)
    {
      conode_boot_fail(slave, "readback", val);
      return;
    }

  slave->step = BOOT_START;
}

/****************************************************************************
 * Name: conode_boot_poll
 *
 * Description:
 *   Boot each slave that sent a boot-up message: write OBJ_BOOTVAL over
 *   SDO, read it back, then start the slave with NMT.
 *
 ****************************************************************************/

static void conode_boot_poll(void)
{
  FAR struct conode_slave_s *slave;
  int i;

  for (i = 0; i < g_nslaves; i++)
    {
      slave = &g_slaves[i];

      if (slave->step == BOOT_IDLE && slave->pending)
        {
          slave->pending = false;
          slave->boots++;
          slave->val = ((uint32_t)slave->boots << 16) |
                       ((uint32_t)g_id << 8) | slave->id;
          slave->step = BOOT_DN_WAIT;
          if (co_csdo_dn_val_req(slave->sdo, OBJ_BOOTVAL, 0,
                                 CO_DEFTYPE_UNSIGNED32, &slave->val,
                                 conode_on_dn, slave) < 0)
            {
              conode_boot_fail(slave, "request", 0);
            }
        }
      else if (slave->step == BOOT_UP && co_csdo_is_idle(slave->sdo))
        {
          slave->step = BOOT_UP_WAIT;
          if (co_csdo_up_req(slave->sdo, OBJ_BOOTVAL, 0, conode_on_up,
                             slave) < 0)
            {
              conode_boot_fail(slave, "request", 0);
            }
        }
      else if (slave->step == BOOT_START)
        {
          slave->step = BOOT_IDLE;
          co_nmt_cs_req(g_nmt, CO_NMT_CS_START, slave->id);
          printf("conode: boot %u PASS 0x%08" PRIx32 "\n", slave->id,
                 slave->val);
        }
    }
}

/****************************************************************************
 * Name: conode_poll_ms
 ****************************************************************************/

static int conode_poll_ms(FAR const struct timespec *now)
{
  int64_t ms;

  ms = (int64_t)(g_next.tv_sec - now->tv_sec) * 1000 +
       (g_next.tv_nsec - now->tv_nsec + 999999) / 1000000;
  if (ms < 0)
    {
      return 0;
    }

  return ms > CONODE_POLL_MAX_MS ? CONODE_POLL_MAX_MS : (int)ms;
}

/****************************************************************************
 * Name: conode_report
 *
 * Description:
 *   Print the PDO statistics and the verdict: every received counter
 *   sequence is continuous.
 *
 ****************************************************************************/

static void conode_report(void)
{
  uint32_t cnt = 0;
  uint32_t lost = 0;
  uint32_t bad = 0;
  int i;

  for (i = 0; i < g_npeers; i++)
    {
      FAR struct conode_rx_s *rx = &g_rx[g_peers[i]];

      printf("conode: rpdo %u rx=%" PRIu32 " lost=%" PRIu32
             " bad=%" PRIu32 "\n", g_peers[i], rx->cnt, rx->lost, rx->bad);
      cnt  += rx->cnt;
      lost += rx->lost;
      bad  += rx->bad;
    }

  printf("conode: %s rx=%" PRIu32 " lost=%" PRIu32 " bad=%" PRIu32
         " txerr=%" PRIu32 " errno=%d\n",
         lost == 0 && bad == 0 && g_txerr == 0 ? "PASS" : "FAIL", cnt, lost,
         bad, g_txerr, g_txerrno);
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/****************************************************************************
 * Name: main
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  FAR const char *ifname = "can0";
  struct sigaction act;
  struct timespec now;
  struct pollfd pfd;
  struct can_msg msg;
  FAR can_net_t *net = NULL;
  uint8_t ids[CONODE_MAXID];
  int sync_ms = 100;
  int hb_ms = 100;
  int hbc_ms = 500;
  int ret = EXIT_FAILURE;
  int opt;
  int n;
  int i;

  g_id = 0;
  g_master = false;
  g_nslaves = 0;
  g_npeers = 0;
  g_maxid = 1;
  g_stop = 0;
  g_dev = NULL;
  g_nmt = NULL;
  memset(g_slaves, 0, sizeof(g_slaves));
  memset(g_rx, 0, sizeof(g_rx));
  g_txerr = 0;
  g_txerrno = 0;

  optind = 1;
  while ((opt = getopt(argc, argv, "i:mr:s:b:c:d:")) != -1)
    {
      switch (opt)
        {
          case 'i':
            g_id = (uint8_t)strtol(optarg, NULL, 0);
            break;

          case 'm':
            g_master = true;
            break;

          case 'r':
            g_npeers = conode_parse_ids(optarg, g_peers, CONODE_MAXID);
            break;

          case 's':
            sync_ms = atoi(optarg);
            break;

          case 'b':
            hb_ms = atoi(optarg);
            break;

          case 'c':
            hbc_ms = atoi(optarg);
            break;

          case 'd':
            ifname = optarg;
            break;

          default:
            conode_usage();
            return EXIT_FAILURE;
        }
    }

  for (; optind < argc && g_nslaves >= 0; optind++)
    {
      n = conode_parse_ids(argv[optind], ids, CONODE_MAXID - g_nslaves);
      for (i = 0; i < n; i++)
        {
          g_slaves[g_nslaves++].id = ids[i];
        }

      if (n < 0)
        {
          g_nslaves = -1;
        }
    }

  if (g_id < 1 || g_id > CONODE_MAXID || g_npeers < 0 || g_nslaves < 0 ||
      (!g_master && g_nslaves > 0) || sync_ms < 0 || hb_ms < 0 ||
      hbc_ms < 0)
    {
      conode_usage();
      return EXIT_FAILURE;
    }

  /* The master receives TPDO1 of its slaves unless told otherwise */

  if (g_master && g_npeers == 0)
    {
      for (i = 0; i < g_nslaves; i++)
        {
          g_peers[g_npeers++] = g_slaves[i].id;
        }
    }

  g_maxid = g_id;
  for (i = 0; i < g_npeers; i++)
    {
      g_maxid = g_peers[i] > g_maxid ? g_peers[i] : g_maxid;
    }

  for (i = 0; i < g_nslaves; i++)
    {
      g_maxid = g_slaves[i].id > g_maxid ? g_slaves[i].id : g_maxid;
    }

  setvbuf(stdout, NULL, _IONBF, 0);

  memset(&act, 0, sizeof(act));
  act.sa_handler = conode_sigterm;
  sigaction(SIGTERM, &act, NULL);

  if (conode_can_init(ifname) < 0)
    {
      goto errout;
    }

  net = can_net_create();
  if (net == NULL)
    {
      printf("conode: FAIL can_net_create\n");
      goto errout;
    }

  can_net_set_send_func(net, conode_can_send, NULL);
  can_net_set_next_func(net, conode_on_next, NULL);
  clock_gettime(CLOCK_MONOTONIC, &now);
  can_net_set_time(net, &now);

  if (conode_od_build(sync_ms, hb_ms, hbc_ms) < 0)
    {
      printf("conode: FAIL object dictionary\n");
      goto errout;
    }

  g_nmt = co_nmt_create(net, g_dev);
  if (g_nmt == NULL)
    {
      printf("conode: FAIL co_nmt_create\n");
      goto errout;
    }

  for (i = 0; i < g_nslaves; i++)
    {
      g_slaves[i].sdo = co_csdo_create(net, NULL, g_slaves[i].id);
      if (g_slaves[i].sdo == NULL || co_csdo_start(g_slaves[i].sdo) < 0)
        {
          printf("conode: FAIL co_csdo_create %u\n", g_slaves[i].id);
          goto errout;
        }

      co_csdo_set_timeout(g_slaves[i].sdo, CONODE_SDO_TMO_MS);
    }

  co_nmt_set_st_ind(g_nmt, conode_on_st, NULL);
  co_nmt_set_hb_ind(g_nmt, conode_on_hb, NULL);
  co_nmt_set_sync_ind(g_nmt, conode_on_sync, NULL);

  printf("conode: node %u %s running\n", g_id,
         g_master ? "master" : "slave");

  /* Boot: the master resets all slaves (NMT reset communication) and
   * starts itself, a slave sends its boot-up and stays PRE-OPERATIONAL.
   */

  co_nmt_cs_ind(g_nmt, CO_NMT_CS_RESET_NODE);

  pfd.fd = g_sock;
  pfd.events = POLLIN;

  while (!g_stop)
    {
      clock_gettime(CLOCK_MONOTONIC, &now);
      can_net_set_time(net, &now);

      while (conode_can_recv(&msg) > 0)
        {
          can_net_recv(net, &msg);
        }

      if (g_master)
        {
          conode_boot_poll();
        }

      if (co_dev_get_val_u32(g_dev, OBJ_TXERR, 0) != g_txerr)
        {
          co_dev_set_val_u32(g_dev, OBJ_TXERR, 0, g_txerr);
        }

      if (co_dev_get_val_u8(g_dev, OBJ_RXRESET, 0) != 0)
        {
          conode_rx_reset();
        }

      clock_gettime(CLOCK_MONOTONIC, &now);
      poll(&pfd, 1, conode_poll_ms(&now));
    }

  conode_report();
  ret = EXIT_SUCCESS;

errout:
  for (i = 0; i < g_nslaves; i++)
    {
      if (g_slaves[i].sdo != NULL)
        {
          co_csdo_destroy(g_slaves[i].sdo);
        }
    }

  if (g_nmt != NULL)
    {
      co_nmt_destroy(g_nmt);
    }

  if (g_dev != NULL)
    {
      co_dev_destroy(g_dev);
    }

  if (net != NULL)
    {
      can_net_destroy(net);
    }

  if (g_sock >= 0)
    {
      close(g_sock);
      g_sock = -1;
    }

  printf("conode: exit\n");
  return ret;
}
