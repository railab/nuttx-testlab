/****************************************************************************
 * apps/canopt/canopt_table.c
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

#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include <nuttx/can.h>

#include "canopt.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define EFF       CAN_EFF_FLAG
#define RTR       CAN_RTR_FLAG
#define BRS       CANFD_BRS
#define ESI       CANFD_ESI

/****************************************************************************
 * Public Data
 ****************************************************************************/

/* Frame tables shared with the NTFC tests (ntfc/tests/_canopt_common.py).
 * Every CAN ID is unique across both tables.
 */

const struct canopt_frame_s g_canopt_classic[] =
{
  { 0x100,            0, 0, false },
  { 0x101,            1, 0, false },
  { 0x102,            2, 0, false },
  { 0x103,            3, 0, false },
  { 0x104,            4, 0, false },
  { 0x105,            5, 0, false },
  { 0x106,            6, 0, false },
  { 0x107,            7, 0, false },
  { 0x108,            8, 0, false },
  { 0x123,            8, 0, false },
  { 0x456,            8, 0, false },
  { 0x7ff,            8, 0, false },
  { 0x000,            8, 0, false },
  { EFF | 0x123,      8, 0, false },
  { EFF | 0x12345678, 8, 0, false },
  { EFF | 0x1fffffff, 8, 0, false },
  { RTR | 0x321,      4, 0, false },
  { RTR | EFF | 0x1234567, 2, 0, false },
};

const size_t g_canopt_nclassic =
  sizeof(g_canopt_classic) / sizeof(g_canopt_classic[0]);

const struct canopt_frame_s g_canopt_fdframes[] =
{
  { 0x200,           0,  0,         true },
  { 0x201,           1,  BRS,       true },
  { 0x202,           7,  ESI,       true },
  { EFF | 0x1800003, 8,  BRS | ESI, true },
  { 0x204,           12, 0,         true },
  { 0x205,           16, BRS,       true },
  { 0x206,           20, ESI,       true },
  { EFF | 0x1800007, 24, BRS | ESI, true },
  { 0x208,           32, 0,         true },
  { 0x209,           48, BRS,       true },
  { 0x20a,           64, ESI,       true },
};

const size_t g_canopt_nfd =
  sizeof(g_canopt_fdframes) / sizeof(g_canopt_fdframes[0]);

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/* Look up a frame table entry by its full CAN ID (with EFF/RTR flags) */

FAR const struct canopt_frame_s *canopt_find(uint32_t id)
{
  size_t i;

  for (i = 0; i < g_canopt_nclassic; i++)
    {
      if (g_canopt_classic[i].id == id)
        {
          return &g_canopt_classic[i];
        }
    }

  for (i = 0; i < g_canopt_nfd; i++)
    {
      if (g_canopt_fdframes[i].id == id)
        {
          return &g_canopt_fdframes[i];
        }
    }

  return NULL;
}

/* Payload byte k of a frame: (id & 0xff) + 7 * k + len */

uint8_t canopt_data(uint32_t id, uint8_t len, size_t k)
{
  return (uint8_t)((id & 0xff) + 7 * k + len);
}

void canopt_fill(FAR uint8_t *data, uint32_t id, uint8_t len)
{
  size_t k;

  for (k = 0; k < len; k++)
    {
      data[k] = canopt_data(id, len, k);
    }
}

bool canopt_data_ok(FAR const uint8_t *data, uint32_t id, uint8_t len)
{
  size_t k;

  /* RTR frames carry no payload */

  if ((id & CAN_RTR_FLAG) != 0)
    {
      return true;
    }

  for (k = 0; k < len; k++)
    {
      if (data[k] != canopt_data(id, len, k))
        {
          return false;
        }
    }

  return true;
}

int canopt_elapsed_ms(FAR const struct timespec *start)
{
  struct timespec now;

  clock_gettime(CLOCK_MONOTONIC, &now);
  return (int)((now.tv_sec - start->tv_sec) * 1000 +
               (now.tv_nsec - start->tv_nsec) / 1000000);
}

int canopt_remain_ms(FAR const struct timespec *start, int timeout_s)
{
  int remain = timeout_s * 1000 - canopt_elapsed_ms(start);

  return remain > 0 ? remain : 0;
}

/* Append a CAN ID (hex, with flags) to a comma separated list */

void canopt_ids_add(FAR char *ids, uint32_t id)
{
  size_t used = strlen(ids);

  if (used + 10 < CANOPT_IDS_LEN)
    {
      snprintf(ids + used, CANOPT_IDS_LEN - used, "%s%" PRIx32,
               used > 0 ? "," : "", id);
    }
}

void canopt_ready(void)
{
  printf("canopt: ready\n");
  fflush(stdout);
}
