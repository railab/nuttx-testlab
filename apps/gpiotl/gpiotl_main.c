/****************************************************************************
 * apps/gpiotl/gpiotl_main.c
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
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <nuttx/ioexpander/gpio.h>
#include <nuttx/ioexpander/gpio_virtio.h>

/****************************************************************************
 * Private Data
 ****************************************************************************/

static FAR const char *g_names[] =
{
  "in", "out", "high", "low", "rising", "falling", "both"
};

static const enum gpio_pintype_e g_types[] =
{
  GPIO_INPUT_PIN, GPIO_OUTPUT_PIN, GPIO_INTERRUPT_HIGH_PIN,
  GPIO_INTERRUPT_LOW_PIN, GPIO_INTERRUPT_RISING_PIN,
  GPIO_INTERRUPT_FALLING_PIN, GPIO_INTERRUPT_BOTH_PIN
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void gpiotl_usage(void)
{
  fprintf(stderr,
          "usage: gpiotl -r count\n"
          "       gpiotl -d dev [-t type] [-w 0|1] [-R]\n"
          "       gpiotl -d dev -i type [-n count] [-T sec]\n"
          "  type: in out high low rising falling both\n");
}

static int gpiotl_type(FAR const char *name)
{
  int i;

  for (i = 0; i < sizeof(g_types) / sizeof(g_types[0]); i++)
    {
      if (strcmp(name, g_names[i]) == 0)
        {
          return g_types[i];
        }
    }

  return -1;
}

/* Register lines 0..count-1 of the first virtio-gpio device as
 * /dev/gpio0.., as board code does with gpio_lower_half().
 */

static int gpiotl_register(int count)
{
  FAR struct ioexpander_dev_s *ioe = virtio_gpio_get_ioe(0);
  int ret;
  int i;

  if (ioe == NULL)
    {
      printf("gpiotl: FAIL no virtio-gpio device\n");
      return EXIT_FAILURE;
    }

  for (i = 0; i < count; i++)
    {
      ret = gpio_lower_half(ioe, i, GPIO_INPUT_PIN, i);
      if (ret < 0)
        {
          printf("gpiotl: FAIL register %d: %d\n", i, ret);
          return EXIT_FAILURE;
        }
    }

  printf("gpiotl: PASS registered %d\n", count);
  return EXIT_SUCCESS;
}

/* Count interrupt signals until count arrived or sec seconds passed */

static int gpiotl_irq(int fd, int type, int count, int sec)
{
  struct sigevent notify;
  struct timespec timeout;
  sigset_t set;
  int got = 0;
  int ret;

  sigemptyset(&set);
  sigaddset(&set, SIGUSR1);
  sigprocmask(SIG_BLOCK, &set, NULL);

  ret = ioctl(fd, GPIOC_SETPINTYPE, (unsigned long)type);
  if (ret < 0)
    {
      printf("gpiotl: FAIL setpintype errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  memset(&notify, 0, sizeof(notify));
  notify.sigev_notify = SIGEV_SIGNAL;
  notify.sigev_signo = SIGUSR1;
  ret = ioctl(fd, GPIOC_REGISTER, (unsigned long)&notify);
  if (ret < 0)
    {
      printf("gpiotl: FAIL register errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  printf("gpiotl: ready\n");
  fflush(stdout);

  timeout.tv_sec = sec;
  timeout.tv_nsec = 0;
  while (got < count)
    {
      if (sigtimedwait(&set, NULL, &timeout) != SIGUSR1)
        {
          break;
        }

      got++;
    }

  ioctl(fd, GPIOC_UNREGISTER, 0);
  ioctl(fd, GPIOC_SETPINTYPE, (unsigned long)GPIO_INPUT_PIN);
  printf("gpiotl: %s irq n=%d\n", got == count ? "PASS" : "FAIL", got);
  return got == count ? EXIT_SUCCESS : EXIT_FAILURE;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  FAR const char *dev = NULL;
  bool doread = false;
  int settype = -1;
  int irqtype = -1;
  int value = -1;
  int count = 1;
  int sec = 10;
  int nreg = 0;
  int opt;
  int ret;
  int fd;

  while ((opt = getopt(argc, argv, "r:d:t:w:Ri:n:T:")) != ERROR)
    {
      switch (opt)
        {
          case 'r':
            nreg = atoi(optarg);
            break;

          case 'd':
            dev = optarg;
            break;

          case 't':
            settype = gpiotl_type(optarg);
            break;

          case 'w':
            value = atoi(optarg) != 0;
            break;

          case 'R':
            doread = true;
            break;

          case 'i':
            irqtype = gpiotl_type(optarg);
            break;

          case 'n':
            count = atoi(optarg);
            break;

          case 'T':
            sec = atoi(optarg);
            break;

          default:
            gpiotl_usage();
            return EXIT_FAILURE;
        }
    }

  if (nreg > 0)
    {
      return gpiotl_register(nreg);
    }

  if (dev == NULL || (settype < 0 && irqtype < 0 && value < 0 && !doread))
    {
      gpiotl_usage();
      return EXIT_FAILURE;
    }

  fd = open(dev, O_RDWR);
  if (fd < 0)
    {
      printf("gpiotl: FAIL open errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  if (irqtype >= 0)
    {
      ret = gpiotl_irq(fd, irqtype, count, sec);
      close(fd);
      return ret;
    }

  if (settype >= 0 &&
      ioctl(fd, GPIOC_SETPINTYPE, (unsigned long)settype) < 0)
    {
      printf("gpiotl: FAIL setpintype errno=%d\n", errno);
      close(fd);
      return EXIT_FAILURE;
    }

  if (value >= 0 && ioctl(fd, GPIOC_WRITE, (unsigned long)value) < 0)
    {
      printf("gpiotl: FAIL write errno=%d\n", errno);
      close(fd);
      return EXIT_FAILURE;
    }

  if (doread)
    {
      bool in;

      if (ioctl(fd, GPIOC_READ, (unsigned long)((uintptr_t)&in)) < 0)
        {
          printf("gpiotl: FAIL read errno=%d\n", errno);
          close(fd);
          return EXIT_FAILURE;
        }

      printf("gpiotl: value=%d\n", in);
    }

  close(fd);
  printf("gpiotl: PASS\n");
  return EXIT_SUCCESS;
}
