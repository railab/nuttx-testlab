/****************************************************************************
 * apps/sertl/sertl_main.c
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

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define SERTL_COUNT_DEFAULT   256
#define SERTL_TIMEOUT_DEFAULT 10
#define SERTL_BUFSIZE         64

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void sertl_usage(FAR const char *progname)
{
  printf("Usage: %s [-n bytes] [-t sec] <dev>\n", progname);
}

static int sertl_elapsed_ms(FAR const struct timespec *start)
{
  struct timespec now;

  clock_gettime(CLOCK_MONOTONIC, &now);
  return (int)((now.tv_sec - start->tv_sec) * 1000 +
               (now.tv_nsec - start->tv_nsec) / 1000000);
}

/* Disable the line discipline so that every byte is echoed unchanged */

static void sertl_raw(int fd)
{
#ifdef CONFIG_SERIAL_TERMIOS
  struct termios tio;

  if (tcgetattr(fd, &tio) == 0)
    {
      cfmakeraw(&tio);
      tcsetattr(fd, TCSANOW, &tio);
    }
#endif
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  FAR const char *dev;
  struct timespec start;
  struct pollfd pfd;
  char buf[SERTL_BUFSIZE];
  size_t count = SERTL_COUNT_DEFAULT;
  size_t rx = 0;
  int timeout = SERTL_TIMEOUT_DEFAULT;
  int remain;
  ssize_t nbytes;
  int opt;
  int fd;

  while ((opt = getopt(argc, argv, "n:t:")) != -1)
    {
      switch (opt)
        {
          case 'n':
            count = (size_t)strtoul(optarg, NULL, 10);
            break;
          case 't':
            timeout = atoi(optarg);
            break;
          default:
            sertl_usage(argv[0]);
            return EXIT_FAILURE;
        }
    }

  if (optind != argc - 1 || count == 0 || timeout <= 0)
    {
      sertl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  dev = argv[optind];
  fd  = open(dev, O_RDWR | O_NOCTTY);
  if (fd < 0)
    {
      printf("sertl: FAIL dev=%s open errno=%d\n", dev, errno);
      return EXIT_FAILURE;
    }

  sertl_raw(fd);
  printf("sertl: ready dev=%s\n", dev);
  clock_gettime(CLOCK_MONOTONIC, &start);

  while (rx < count)
    {
      remain = timeout * 1000 - sertl_elapsed_ms(&start);
      if (remain <= 0)
        {
          break;
        }

      pfd.fd      = fd;
      pfd.events  = POLLIN;
      pfd.revents = 0;
      if (poll(&pfd, 1, remain) <= 0)
        {
          continue;
        }

      nbytes = read(fd, buf, sizeof(buf));
      if (nbytes <= 0)
        {
          break;
        }

      if (write(fd, buf, nbytes) != nbytes)
        {
          printf("sertl: FAIL dev=%s write errno=%d rx=%zu\n", dev, errno,
                 rx);
          close(fd);
          return EXIT_FAILURE;
        }

      rx += nbytes;
    }

  close(fd);
  printf("sertl: %s dev=%s rx=%zu\n", rx == count ? "PASS" : "FAIL", dev,
         rx);
  return rx == count ? EXIT_SUCCESS : EXIT_FAILURE;
}
