/****************************************************************************
 * apps/rpmbtl/rpmbtl_main.c
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

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <nuttx/mmcsd.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define RPMBTL_FRAME     512
#define RPMBTL_MAXCMDS   3

/* Offsets of the struct virtio_rpmb_frame fields */

#define RPMBTL_KEY_MAC   196
#define RPMBTL_DATA      228
#define RPMBTL_NONCE     484
#define RPMBTL_COUNTER   500
#define RPMBTL_ADDRESS   504
#define RPMBTL_COUNT     506
#define RPMBTL_RESULT    508
#define RPMBTL_REQ_RESP  510

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct rpmbtl_multi_s
{
  uint64_t num_of_cmds;
  struct mmc_ioc_cmd cmds[RPMBTL_MAXCMDS];
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

static uint8_t g_frames[RPMBTL_MAXCMDS][RPMBTL_FRAME];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void rpmbtl_usage(void)
{
  fprintf(stderr,
          "usage: rpmbtl [-d dev] -k keyhex\n"
          "       rpmbtl [-d dev] -c noncehex\n"
          "       rpmbtl [-d dev] -w addr -n counter -s seed -m machex\n"
          "       rpmbtl [-d dev] -r addr -o noncehex\n");
}

static int rpmbtl_hex(FAR const char *hex, FAR uint8_t *buf, int len)
{
  int i;

  if (strlen(hex) != len * 2)
    {
      return -1;
    }

  for (i = 0; i < len; i++)
    {
      unsigned int byte;

      if (sscanf(hex + 2 * i, "%2x", &byte) != 1)
        {
          return -1;
        }

      buf[i] = (uint8_t)byte;
    }

  return 0;
}

static void rpmbtl_put16(FAR uint8_t *frame, int off, uint16_t val)
{
  frame[off] = val >> 8;
  frame[off + 1] = val & 0xff;
}

static uint32_t rpmbtl_get(FAR const uint8_t *frame, int off, int len)
{
  uint32_t val = 0;
  int i;

  for (i = 0; i < len; i++)
    {
      val = (val << 8) | frame[off + i];
    }

  return val;
}

static void rpmbtl_print(FAR const char *name, FAR const uint8_t *buf,
                         int len)
{
  int i;

  printf("rpmbtl: %s=", name);
  for (i = 0; i < len; i++)
    {
      printf("%02x", buf[i]);
    }

  printf("\n");
}

/* Send nreq request frames and receive one response frame */

static int rpmbtl_transfer(FAR const char *dev, int nreq)
{
  struct rpmbtl_multi_s multi;
  FAR const uint8_t *resp = g_frames[nreq];
  int ret;
  int fd;
  int i;

  memset(&multi, 0, sizeof(multi));
  multi.num_of_cmds = nreq + 1;
  for (i = 0; i <= nreq; i++)
    {
      multi.cmds[i].write_flag = i < nreq;
      multi.cmds[i].blksz = RPMBTL_FRAME;
      multi.cmds[i].blocks = 1;
      multi.cmds[i].data_ptr = (uintptr_t)g_frames[i];
    }

  fd = open(dev, O_RDWR);
  if (fd < 0)
    {
      printf("rpmbtl: FAIL open errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  ret = ioctl(fd, MMC_IOC_MULTI_CMD, (unsigned long)&multi);
  close(fd);
  if (ret < 0)
    {
      printf("rpmbtl: FAIL errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  printf("rpmbtl: resp=0x%04" PRIx32 " result=0x%04" PRIx32
         " counter=%" PRIu32 " address=%" PRIu32 " count=%" PRIu32 "\n",
         rpmbtl_get(resp, RPMBTL_REQ_RESP, 2),
         rpmbtl_get(resp, RPMBTL_RESULT, 2),
         rpmbtl_get(resp, RPMBTL_COUNTER, 4),
         rpmbtl_get(resp, RPMBTL_ADDRESS, 2),
         rpmbtl_get(resp, RPMBTL_COUNT, 2));
  rpmbtl_print("nonce", resp + RPMBTL_NONCE, 16);
  rpmbtl_print("mac", resp + RPMBTL_KEY_MAC, 32);
  rpmbtl_print("frame", resp + RPMBTL_DATA, RPMBTL_FRAME - RPMBTL_DATA);
  printf("rpmbtl: PASS\n");
  return EXIT_SUCCESS;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  FAR const char *dev = "/dev/rpmb";
  FAR uint8_t *req = g_frames[0];
  FAR const char *hex = NULL;
  FAR const char *mac = NULL;
  uint32_t counter = 0;
  int kind = 0;
  int addr = 0;
  int seed = 0;
  int nreq = 1;
  int opt;
  int i;

  memset(g_frames, 0, sizeof(g_frames));
  while ((opt = getopt(argc, argv, "d:k:c:w:r:n:s:m:o:")) != ERROR)
    {
      switch (opt)
        {
          case 'd':
            dev = optarg;
            break;

          case 'k':
            kind = MMC_RPMB_WRITE_KEY;
            hex = optarg;
            break;

          case 'c':
            kind = MMC_RPMB_READ_CNT;
            hex = optarg;
            break;

          case 'w':
            kind = MMC_RPMB_WRITE;
            addr = atoi(optarg);
            break;

          case 'r':
            kind = MMC_RPMB_READ;
            addr = atoi(optarg);
            break;

          case 'n':
            counter = strtoul(optarg, NULL, 0);
            break;

          case 's':
            seed = atoi(optarg);
            break;

          case 'm':
            mac = optarg;
            break;

          case 'o':
            hex = optarg;
            break;

          default:
            rpmbtl_usage();
            return EXIT_FAILURE;
        }
    }

  rpmbtl_put16(req, RPMBTL_REQ_RESP, kind);
  rpmbtl_put16(req, RPMBTL_COUNT, 1);
  rpmbtl_put16(req, RPMBTL_ADDRESS, addr);

  switch (kind)
    {
      case MMC_RPMB_WRITE_KEY:
        if (rpmbtl_hex(hex, req + RPMBTL_KEY_MAC, 32) < 0)
          {
            rpmbtl_usage();
            return EXIT_FAILURE;
          }

        rpmbtl_put16(g_frames[1], RPMBTL_REQ_RESP, MMC_RPMB_READ_RESP);
        rpmbtl_put16(g_frames[1], RPMBTL_COUNT, 1);
        nreq = 2;
        break;

      case MMC_RPMB_READ_CNT:
      case MMC_RPMB_READ:
        if (hex == NULL || rpmbtl_hex(hex, req + RPMBTL_NONCE, 16) < 0)
          {
            rpmbtl_usage();
            return EXIT_FAILURE;
          }

        break;

      case MMC_RPMB_WRITE:
        if (mac == NULL || rpmbtl_hex(mac, req + RPMBTL_KEY_MAC, 32) < 0)
          {
            rpmbtl_usage();
            return EXIT_FAILURE;
          }

        for (i = 0; i < 256; i++)
          {
            req[RPMBTL_DATA + i] = (uint8_t)(seed + i * 31 + 7);
          }

        req[RPMBTL_COUNTER] = counter >> 24;
        req[RPMBTL_COUNTER + 1] = (counter >> 16) & 0xff;
        req[RPMBTL_COUNTER + 2] = (counter >> 8) & 0xff;
        req[RPMBTL_COUNTER + 3] = counter & 0xff;
        rpmbtl_put16(g_frames[1], RPMBTL_REQ_RESP, MMC_RPMB_READ_RESP);
        rpmbtl_put16(g_frames[1], RPMBTL_COUNT, 1);
        nreq = 2;
        break;

      default:
        rpmbtl_usage();
        return EXIT_FAILURE;
    }

  return rpmbtl_transfer(dev, nreq);
}
