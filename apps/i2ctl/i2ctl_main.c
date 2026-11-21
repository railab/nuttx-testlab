/****************************************************************************
 * apps/i2ctl/i2ctl_main.c
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

#include <nuttx/i2c/i2c_master.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define I2CTL_MAXLEN   64
#define I2CTL_MAXMSGS  4

/****************************************************************************
 * Private Data
 ****************************************************************************/

static uint8_t g_buf[I2CTL_MAXMSGS][I2CTL_MAXLEN];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void i2ctl_usage(void)
{
  fprintf(stderr,
          "usage: i2ctl [-d dev] -a addr [-T] {-w hex | -z | -r len}...\n"
          "  messages are sent in one I2CIOC_TRANSFER, in order\n");
}

static int i2ctl_hex(FAR const char *hex, FAR uint8_t *buf)
{
  int len = 0;

  while (hex[0] != '\0' && hex[1] != '\0' && len < I2CTL_MAXLEN)
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
  struct i2c_msg_s msgs[I2CTL_MAXMSGS];
  struct i2c_transfer_s xfer;
  FAR const char *dev = "/dev/i2c0";
  uint16_t flags = 0;
  long addr = -1;
  int nmsgs = 0;
  int opt;
  int ret;
  int fd;
  int i;
  int k;

  memset(msgs, 0, sizeof(msgs));
  while ((opt = getopt(argc, argv, "d:a:Tw:zr:")) != ERROR)
    {
      switch (opt)
        {
          case 'd':
            dev = optarg;
            break;

          case 'a':
            addr = strtol(optarg, NULL, 0);
            break;

          case 'T':
            flags = I2C_M_TEN;
            break;

          case 'w':
          case 'z':
          case 'r':
            if (nmsgs >= I2CTL_MAXMSGS)
              {
                i2ctl_usage();
                return EXIT_FAILURE;
              }

            msgs[nmsgs].buffer = g_buf[nmsgs];
            if (opt == 'w')
              {
                msgs[nmsgs].length = i2ctl_hex(optarg, g_buf[nmsgs]);
              }
            else if (opt == 'r')
              {
                msgs[nmsgs].flags = I2C_M_READ;
                msgs[nmsgs].length = atoi(optarg);
                memset(g_buf[nmsgs], 0xee, I2CTL_MAXLEN);
              }

            if (msgs[nmsgs].length < 0 ||
                msgs[nmsgs].length > I2CTL_MAXLEN)
              {
                i2ctl_usage();
                return EXIT_FAILURE;
              }

            nmsgs++;
            break;

          default:
            i2ctl_usage();
            return EXIT_FAILURE;
        }
    }

  if (addr < 0 || nmsgs == 0)
    {
      i2ctl_usage();
      return EXIT_FAILURE;
    }

  for (i = 0; i < nmsgs; i++)
    {
      msgs[i].frequency = 100000;
      msgs[i].addr = (uint16_t)addr;
      msgs[i].flags |= flags;
    }

  fd = open(dev, O_RDWR);
  if (fd < 0)
    {
      printf("i2ctl: FAIL open errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  xfer.msgv = msgs;
  xfer.msgc = nmsgs;
  ret = ioctl(fd, I2CIOC_TRANSFER, (unsigned long)&xfer);
  if (ret < 0)
    {
      printf("i2ctl: FAIL errno=%d\n", errno);
      close(fd);
      return EXIT_FAILURE;
    }

  close(fd);

  for (i = 0; i < nmsgs; i++)
    {
      if (msgs[i].flags & I2C_M_READ)
        {
          printf("i2ctl: rx=");
          for (k = 0; k < msgs[i].length; k++)
            {
              printf("%02x", msgs[i].buffer[k]);
            }

          printf("\n");
        }
    }

  printf("i2ctl: PASS\n");
  return EXIT_SUCCESS;
}
