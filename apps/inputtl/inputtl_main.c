/****************************************************************************
 * apps/inputtl/inputtl_main.c
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
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifdef CONFIG_INPUT_MOUSE
#  include <nuttx/input/mouse.h>
#endif
#ifdef CONFIG_INPUT_KEYBOARD
#  include <nuttx/input/keyboard.h>
#endif
#ifdef CONFIG_INPUT_TOUCHSCREEN
#  include <nuttx/input/touchscreen.h>
#endif

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define INPUTTL_TIMEOUT  10
#define INPUTTL_BUFSIZE  64

/****************************************************************************
 * Private Data
 ****************************************************************************/

static unsigned char g_buf[INPUTTL_BUFSIZE];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static int inputtl_elapsed_ms(FAR const struct timespec *start)
{
  struct timespec now;

  clock_gettime(CLOCK_MONOTONIC, &now);
  return (int)((now.tv_sec - start->tv_sec) * 1000 +
               (now.tv_nsec - start->tv_nsec) / 1000000);
}

/* Print one report and return its size, 0 for an unknown kind */

static size_t inputtl_print(int kind, FAR const unsigned char *buf)
{
  switch (kind)
    {
#ifdef CONFIG_INPUT_MOUSE
      case 'm':
        {
          FAR const struct mouse_report_s *r =
            (FAR const struct mouse_report_s *)buf;

          printf("inputtl: mouse buttons=%u x=%d y=%d\n",
                 (unsigned int)r->buttons, (int)r->x, (int)r->y);
          return sizeof(*r);
        }
#endif

#ifdef CONFIG_INPUT_KEYBOARD
      case 'k':
        {
          FAR const struct keyboard_event_s *e =
            (FAR const struct keyboard_event_s *)buf;

          printf("inputtl: key type=%lu code=%lu\n",
                 (unsigned long)e->type, (unsigned long)e->code);
          return sizeof(*e);
        }
#endif

#ifdef CONFIG_INPUT_TOUCHSCREEN
      case 't':
        {
          FAR const struct touch_sample_s *s =
            (FAR const struct touch_sample_s *)buf;

          printf("inputtl: touch npoints=%d flags=0x%02x x=%d y=%d\n",
                 s->npoints, (unsigned int)s->point[0].flags,
                 (int)s->point[0].x, (int)s->point[0].y);
          return SIZEOF_TOUCH_SAMPLE_S(1);
        }
#endif

      default:
        return 0;
    }
}

static void inputtl_usage(FAR const char *progname)
{
  printf("Usage: %s -m|-k|-t -d dev [-n count] [-T sec]\n", progname);
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  FAR const char *dev = NULL;
  struct timespec start;
  struct pollfd pfd;
  size_t size = 0;
  int timeout = INPUTTL_TIMEOUT;
  int count = 1;
  int kind = 0;
  int got = 0;
  int remain;
  ssize_t ret;
  int opt;
  int fd;

  while ((opt = getopt(argc, argv, "mktd:n:T:")) != ERROR)
    {
      switch (opt)
        {
          case 'm':
          case 'k':
          case 't':
            kind = opt;
            break;
          case 'd':
            dev = optarg;
            break;
          case 'n':
            count = atoi(optarg);
            break;
          case 'T':
            timeout = atoi(optarg);
            break;
          default:
            inputtl_usage(argv[0]);
            return EXIT_FAILURE;
        }
    }

  switch (kind)
    {
#ifdef CONFIG_INPUT_MOUSE
      case 'm':
        size = sizeof(struct mouse_report_s);
        break;
#endif
#ifdef CONFIG_INPUT_KEYBOARD
      case 'k':
        size = sizeof(struct keyboard_event_s);
        break;
#endif
#ifdef CONFIG_INPUT_TOUCHSCREEN
      case 't':
        size = SIZEOF_TOUCH_SAMPLE_S(1);
        break;
#endif
      default:
        break;
    }

  if (size == 0 || size > sizeof(g_buf) || dev == NULL || count <= 0 ||
      timeout <= 0)
    {
      inputtl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  fd = open(dev, O_RDONLY | O_NONBLOCK);
  if (fd < 0)
    {
      printf("inputtl: open %s failed %d\n", dev, errno);
      printf("inputtl: FAIL n=0\n");
      return EXIT_FAILURE;
    }

  printf("inputtl: ready %s\n", dev);
  fflush(stdout);
  clock_gettime(CLOCK_MONOTONIC, &start);

  while (got < count)
    {
      remain = timeout * 1000 - inputtl_elapsed_ms(&start);
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

      ret = read(fd, g_buf, size);
      if (ret < 0)
        {
          if (errno == EAGAIN || errno == EINTR)
            {
              continue;
            }

          printf("inputtl: read failed %d\n", errno);
          break;
        }

      if ((size_t)ret != size)
        {
          printf("inputtl: short read %zd\n", ret);
          break;
        }

      inputtl_print(kind, g_buf);
      got++;
    }

  close(fd);
  printf("inputtl: %s n=%d\n", got == count ? "PASS" : "FAIL", got);
  return got == count ? EXIT_SUCCESS : EXIT_FAILURE;
}
