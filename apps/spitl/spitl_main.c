/****************************************************************************
 * apps/spitl/spitl_main.c
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
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <nuttx/spi/spi.h>
#include <nuttx/spi/spi_transfer.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define SPITL_MAXLEN    64
#define SPITL_MAXTRANS  4

/****************************************************************************
 * Private Data
 ****************************************************************************/

static uint8_t g_tx[SPITL_MAXTRANS][SPITL_MAXLEN];
static uint8_t g_rx[SPITL_MAXTRANS][SPITL_MAXLEN];
static size_t g_len[SPITL_MAXTRANS];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void spitl_usage(void)
{
  fprintf(stderr,
          "usage: spitl [-d dev] [-c cs] [-m mode] [-b bits] [-f hz] [-K]\n"
          "             {-x hex | -w hex | -r len}...\n"
          "  -x full duplex, -w tx only, -r rx only; one SPIIOC_TRANSFER\n"
          "  -K keep chip select asserted between the transfers\n");
}

static int spitl_hex(FAR const char *hex, FAR uint8_t *buf)
{
  int len = 0;

  while (hex[0] != '\0' && hex[1] != '\0' && len < SPITL_MAXLEN)
    {
      unsigned int byte;

      if (sscanf(hex, "%2x", &byte) != 1)
        {
          return -1;
        }

      buf[len++] = (uint8_t)byte;
      hex += 2;
    }

  return hex[0] == '\0' ? len : -1;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  struct spi_trans_s trans[SPITL_MAXTRANS];
  struct spi_sequence_s seq;
  FAR const char *dev = "/dev/spi0";
  bool keep = false;
  int wordsize;
  int ntrans = 0;
  int cs = 0;
  int len;
  int opt;
  int ret;
  int fd;
  int i;
  int k;

  memset(trans, 0, sizeof(trans));
  memset(&seq, 0, sizeof(seq));
  seq.mode = SPIDEV_MODE0;
  seq.nbits = 8;
  seq.frequency = 1000000;

  while ((opt = getopt(argc, argv, "d:c:m:b:f:Kx:w:r:")) != ERROR)
    {
      switch (opt)
        {
          case 'd':
            dev = optarg;
            break;

          case 'c':
            cs = atoi(optarg);
            break;

          case 'm':
            seq.mode = atoi(optarg);
            break;

          case 'b':
            seq.nbits = atoi(optarg);
            break;

          case 'f':
            seq.frequency = strtoul(optarg, NULL, 0);
            break;

          case 'K':
            keep = true;
            break;

          case 'x':
          case 'w':
          case 'r':
            if (ntrans >= SPITL_MAXTRANS)
              {
                spitl_usage();
                return EXIT_FAILURE;
              }

            memset(g_rx[ntrans], 0xee, SPITL_MAXLEN);
            len = opt == 'r' ? atoi(optarg) :
                               spitl_hex(optarg, g_tx[ntrans]);
            if (len <= 0 || len > SPITL_MAXLEN)
              {
                spitl_usage();
                return EXIT_FAILURE;
              }

            g_len[ntrans] = len;
            trans[ntrans].txbuffer = opt != 'r' ? g_tx[ntrans] : NULL;
            trans[ntrans].rxbuffer = opt != 'w' ? g_rx[ntrans] : NULL;
            ntrans++;
            break;

          default:
            spitl_usage();
            return EXIT_FAILURE;
        }
    }

  if (ntrans == 0)
    {
      spitl_usage();
      return EXIT_FAILURE;
    }

  wordsize = seq.nbits > 8 ? 2 : 1;
  for (i = 0; i < ntrans; i++)
    {
      trans[i].deselect = !keep || i == ntrans - 1;
      trans[i].nwords = g_len[i] / wordsize;
    }

  seq.dev = SPIDEV_ID(SPIDEVTYPE_USER, cs);
  seq.ntrans = ntrans;
  seq.trans = trans;

  fd = open(dev, O_RDWR);
  if (fd < 0)
    {
      printf("spitl: FAIL open errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  ret = ioctl(fd, SPIIOC_TRANSFER, (unsigned long)&seq);
  close(fd);
  if (ret < 0)
    {
      printf("spitl: FAIL errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  for (i = 0; i < ntrans; i++)
    {
      if (trans[i].rxbuffer != NULL)
        {
          printf("spitl: rx%d=", i);
          for (k = 0; k < g_len[i]; k++)
            {
              printf("%02x", g_rx[i][k]);
            }

          printf("\n");
        }
    }

  printf("spitl: PASS\n");
  return EXIT_SUCCESS;
}
