/****************************************************************************
 * apps/canopt/canopt.h
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

#ifndef __APPS_CANOPT_CANOPT_H
#define __APPS_CANOPT_CANOPT_H

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <time.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

/* End-of-sequence marker frame sent by the host (classic, SFF) */

#define CANOPT_MARKER_ID     0x7fe

/* Frame sent by the host only to wake up a blocked reader; ignored */

#define CANOPT_KICK_ID       0x7fd

#define CANOPT_MAX_FILTERS   8
#define CANOPT_IDS_LEN       400

/****************************************************************************
 * Public Types
 ****************************************************************************/

/* One frame of the shared host/node frame tables */

struct canopt_frame_s
{
  uint32_t id;      /* CAN ID, may carry CAN_EFF_FLAG and CAN_RTR_FLAG */
  uint8_t  len;     /* Payload length in bytes (DLC for RTR frames) */
  uint8_t  flags;   /* CANFD_BRS / CANFD_ESI, CAN FD frames only */
  bool     fd;      /* CAN FD frame */
};

struct canopt_args_s
{
  FAR const char *cmd;        /* Check to run */
  FAR const char *endpoint;   /* SocketCAN ifname or character device */
  uint32_t filt_id[CANOPT_MAX_FILTERS];
  uint32_t filt_mask[CANOPT_MAX_FILTERS];
  int      nfilters;          /* -F count */
  bool     nofilter;          /* -Z: install an empty filter list */
  bool     maxfilter;         /* -M: install the maximum filter count */
  bool     fd;                /* -f: CAN FD socket */
  bool     queued;            /* -q: let frames queue before reading */
  int      mode;              /* -m: timestamp mode */
  int      count;             /* -n: frame count */
  int      gap_ms;            /* -g: host frame gap in ms */
  int      loopback;          /* -l: CAN_RAW_LOOPBACK value */
  int      recv_own;          /* -o: CAN_RAW_RECV_OWN_MSGS value */
  int      timeout;           /* -t: timeout in seconds */
};

/* Timestamp modes (-m) */

enum canopt_tsmode_e
{
  CANOPT_TS_NONE = 0,
  CANOPT_TS_US,
  CANOPT_TS_NS
};

/****************************************************************************
 * Public Data
 ****************************************************************************/

extern const struct canopt_frame_s g_canopt_classic[];
extern const size_t g_canopt_nclassic;
extern const struct canopt_frame_s g_canopt_fdframes[];
extern const size_t g_canopt_nfd;

/****************************************************************************
 * Public Function Prototypes
 ****************************************************************************/

FAR const struct canopt_frame_s *canopt_find(uint32_t id);
uint8_t canopt_data(uint32_t id, uint8_t len, size_t k);
void canopt_fill(FAR uint8_t *data, uint32_t id, uint8_t len);
bool canopt_data_ok(FAR const uint8_t *data, uint32_t id, uint8_t len);
int canopt_elapsed_ms(FAR const struct timespec *start);
int canopt_remain_ms(FAR const struct timespec *start, int timeout_s);
void canopt_ids_add(FAR char *ids, uint32_t id);
void canopt_ready(void);

#ifdef CONFIG_NET_CAN
int canopt_sock_main(FAR struct canopt_args_s *args);
#endif

#ifdef CONFIG_CAN
int canopt_char_main(FAR struct canopt_args_s *args);
#endif

#endif /* __APPS_CANOPT_CANOPT_H */
