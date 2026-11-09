/****************************************************************************
 * apps/hwtl/hwtl_main.c
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
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <time.h>

#ifdef CONFIG_TIMER
#  include <nuttx/timers/timer.h>
#endif

#ifdef CONFIG_ONESHOT
#  include <nuttx/timers/oneshot.h>
#endif

#ifdef CONFIG_RTC_DRIVER
#  include <nuttx/timers/rtc.h>
#endif

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define HWTL_DETAIL_MAX   96
#define HWTL_SIGNO        SIGUSR1

/* FIPS 140-2 statistical tests: one block of 20000 bits */

#define HWTL_RNG_BLOCK    2500
#define HWTL_RNG_ONES_MIN 9726
#define HWTL_RNG_ONES_MAX 10274

/* Helpers needed only by some subcommands */

#if defined(CONFIG_TIMER) || defined(CONFIG_RTC_PERIODIC)
#  define HWTL_HAVE_COUNT 1
#endif

#if defined(HWTL_HAVE_COUNT) || defined(CONFIG_ONESHOT) || \
    defined(CONFIG_RTC_ALARM)
#  define HWTL_HAVE_SIGWAIT 1
#endif

#if defined(CONFIG_RTC_DRIVER) && defined(CONFIG_RTC_DATETIME)
#  define HWTL_HAVE_RTC 1
#endif

/* RTC time check start: one year rollover two seconds after the start */

#define HWTL_RTC_YEAR     2026
#define HWTL_RTC_MON      11
#define HWTL_RTC_MDAY     31
#define HWTL_RTC_HOUR     23
#define HWTL_RTC_MIN      59
#define HWTL_RTC_SEC      58

/****************************************************************************
 * Private Types
 ****************************************************************************/

/* Result of one check */

enum hwtl_res_e
{
  HWTL_PASS = 0,   /* Check passed */
  HWTL_FAIL,       /* Check ran and failed */
  HWTL_ERROR       /* Bad arguments or device error */
};

/* One subcommand: name, argument count range and check function */

typedef enum hwtl_res_e (*hwtl_check_t)(FAR char **argv, FAR char *detail);

struct hwtl_cmd_s
{
  FAR const char *name;
  FAR const char *usage;
  int             minargs;
  int             maxargs;
  hwtl_check_t    check;
};

/****************************************************************************
 * Private Function Prototypes
 ****************************************************************************/

static enum hwtl_res_e hwtl_timer(FAR char **argv, FAR char *detail);
static enum hwtl_res_e hwtl_oneshot(FAR char **argv, FAR char *detail);
static enum hwtl_res_e hwtl_rng(FAR char **argv, FAR char *detail);
static enum hwtl_res_e hwtl_rtctime(FAR char **argv, FAR char *detail);
static enum hwtl_res_e hwtl_rtcalarm(FAR char **argv, FAR char *detail);
static enum hwtl_res_e hwtl_rtccancel(FAR char **argv, FAR char *detail);
static enum hwtl_res_e hwtl_rtcperiodic(FAR char **argv, FAR char *detail);

/****************************************************************************
 * Private Data
 ****************************************************************************/

static const struct hwtl_cmd_s g_hwtl_cmds[] =
{
  {
    "timer", "<dev> <period_us> <ms>", 3, 3, hwtl_timer
  },
  {
    "oneshot", "<dev> <us>", 2, 2, hwtl_oneshot
  },
  {
    "rng", "<dev> <blocks>", 2, 2, hwtl_rng
  },
  {
    "rtctime", "<dev> <ms>", 2, 2, hwtl_rtctime
  },
  {
    "rtcalarm", "<dev> <id> <s>", 3, 3, hwtl_rtcalarm
  },
  {
    "rtccancel", "<dev> <id> <s>", 3, 3, hwtl_rtccancel
  },
  {
    "rtcperiodic", "<dev> <period_ms> <ms>", 3, 3, hwtl_rtcperiodic
  }
};

#define HWTL_NCMDS (sizeof(g_hwtl_cmds) / sizeof(g_hwtl_cmds[0]))

/* One RNG block, too large for the stack */

static uint8_t g_hwtl_rngbuf[HWTL_RNG_BLOCK];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

#if defined(HWTL_HAVE_COUNT) || defined(HWTL_HAVE_RTC)
/* Milliseconds elapsed since start */

static long hwtl_elapsed_ms(FAR const struct timespec *start)
{
  struct timespec now;

  clock_gettime(CLOCK_MONOTONIC, &now);
  return (now.tv_sec - start->tv_sec) * 1000 +
         (now.tv_nsec - start->tv_nsec) / 1000000;
}
#endif

#ifdef HWTL_HAVE_COUNT

/* Count HWTL_SIGNO deliveries for ms milliseconds (signal blocked) */

static int hwtl_count_signals(int ms)
{
  struct timespec start;
  struct timespec ts;
  sigset_t set;
  int count = 0;
  long left;

  sigemptyset(&set);
  sigaddset(&set, HWTL_SIGNO);
  clock_gettime(CLOCK_MONOTONIC, &start);

  while ((left = ms - hwtl_elapsed_ms(&start)) > 0)
    {
      ts.tv_sec  = left / 1000;
      ts.tv_nsec = (left % 1000) * 1000000;

      if (sigtimedwait(&set, NULL, &ts) == HWTL_SIGNO)
        {
          count++;
        }
    }

  return count;
}
#endif

#ifdef HWTL_HAVE_SIGWAIT
/* Block HWTL_SIGNO, drop a pending one; restore with hwtl_sig_restore() */

static void hwtl_sig_block(FAR sigset_t *old)
{
  struct timespec ts;
  sigset_t set;

  sigemptyset(&set);
  sigaddset(&set, HWTL_SIGNO);
  sigprocmask(SIG_BLOCK, &set, old);

  ts.tv_sec  = 0;
  ts.tv_nsec = 0;
  sigtimedwait(&set, NULL, &ts);
}

/* Drop late signals (default action would end the task), then restore */

static void hwtl_sig_restore(FAR const sigset_t *old)
{
  struct timespec ts;
  sigset_t set;

  sigemptyset(&set);
  sigaddset(&set, HWTL_SIGNO);

  ts.tv_sec  = 0;
  ts.tv_nsec = 0;
  while (sigtimedwait(&set, NULL, &ts) == HWTL_SIGNO)
    {
    }

  sigprocmask(SIG_SETMASK, old, NULL);
}
#endif

/****************************************************************************
 * Timer and oneshot devices
 ****************************************************************************/

/* Periodic /dev/timerN: expirations counted against CLOCK_MONOTONIC */

static enum hwtl_res_e hwtl_timer(FAR char **argv, FAR char *detail)
{
#ifdef CONFIG_TIMER
  struct timer_notify_s notify;
  sigset_t old;
  uint32_t period = strtoul(argv[1], NULL, 0);
  int ms = atoi(argv[2]);
  int expect;
  int count;
  int tol;
  int fd;

  fd = open(argv[0], O_RDONLY);
  if (fd < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "open %s errno=%d", argv[0], errno);
      return HWTL_ERROR;
    }

  hwtl_sig_block(&old);

  memset(&notify, 0, sizeof(notify));
  notify.event.sigev_notify = SIGEV_SIGNAL;
  notify.event.sigev_signo  = HWTL_SIGNO;
  notify.pid                = getpid();
  notify.periodic           = true;

  if (ioctl(fd, TCIOC_SETTIMEOUT, (unsigned long)period) < 0 ||
      ioctl(fd, TCIOC_NOTIFICATION, (unsigned long)&notify) < 0 ||
      ioctl(fd, TCIOC_START, 0) < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "%s errno=%d", argv[0], errno);
      close(fd);
      hwtl_sig_restore(&old);
      return HWTL_ERROR;
    }

  count = hwtl_count_signals(ms);
  ioctl(fd, TCIOC_STOP, 0);
  close(fd);
  hwtl_sig_restore(&old);

  /* +-2 % and one period for the window edges */

  expect = (int)((uint64_t)ms * 1000 / period);
  tol    = expect / 50 + 1;

  snprintf(detail, HWTL_DETAIL_MAX, "%s period_us=%" PRIu32 " ms=%d "
           "count=%d expect=%d", argv[0], period, ms, count, expect);
  return (count >= expect - tol && count <= expect + tol) ?
         HWTL_PASS : HWTL_FAIL;
#else
  snprintf(detail, HWTL_DETAIL_MAX, "no CONFIG_TIMER");
  return HWTL_ERROR;
#endif
}

/* /dev/oneshotN: one expiry after us microseconds (0: half the maximum
 * delay), measured with CLOCK_MONOTONIC
 */

static enum hwtl_res_e hwtl_oneshot(FAR char **argv, FAR char *detail)
{
#ifdef CONFIG_ONESHOT
  struct oneshot_start_s start;
  struct timespec maxdelay;
  struct timespec t0;
  struct timespec t1;
  struct timespec wait;
  sigset_t set;
  sigset_t old;
  uint64_t maxus;
  uint64_t us = strtoull(argv[1], NULL, 0);
  int64_t elapsed;
  int fd;
  int ret;

  fd = open(argv[0], O_RDONLY);
  if (fd < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "open %s errno=%d", argv[0], errno);
      return HWTL_ERROR;
    }

  if (ioctl(fd, OSIOC_MAXDELAY, (unsigned long)&maxdelay) < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "maxdelay errno=%d", errno);
      close(fd);
      return HWTL_ERROR;
    }

  maxus = (uint64_t)maxdelay.tv_sec * 1000000 + maxdelay.tv_nsec / 1000;
  if (us == 0)
    {
      us = maxus / 2;
    }

  hwtl_sig_block(&old);

  memset(&start, 0, sizeof(start));
  start.pid                = getpid();
  start.event.sigev_notify = SIGEV_SIGNAL;
  start.event.sigev_signo  = HWTL_SIGNO;
  start.ts.tv_sec          = us / 1000000;
  start.ts.tv_nsec         = (us % 1000000) * 1000;

  clock_gettime(CLOCK_MONOTONIC, &t0);
  if (ioctl(fd, OSIOC_START, (unsigned long)&start) < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "start errno=%d", errno);
      close(fd);
      hwtl_sig_restore(&old);
      return HWTL_ERROR;
    }

  sigemptyset(&set);
  sigaddset(&set, HWTL_SIGNO);
  wait.tv_sec  = us / 1000000 + 1;
  wait.tv_nsec = 0;
  ret = sigtimedwait(&set, NULL, &wait);
  clock_gettime(CLOCK_MONOTONIC, &t1);
  elapsed = (int64_t)(t1.tv_sec - t0.tv_sec) * 1000000 +
            (t1.tv_nsec - t0.tv_nsec) / 1000;

  ioctl(fd, OSIOC_CANCEL, (unsigned long)&wait);
  close(fd);
  hwtl_sig_restore(&old);

  /* Early by at most one tick, late by at most two ticks + 2 ms */

  snprintf(detail, HWTL_DETAIL_MAX, "%s max_us=%" PRIu64 " us=%" PRIu64
           " elapsed_us=%" PRId64 " fired=%d", argv[0], maxus, us, elapsed,
           ret == HWTL_SIGNO);
  return (ret == HWTL_SIGNO && us <= maxus &&
          elapsed + CONFIG_USEC_PER_TICK >= (int64_t)us &&
          elapsed <= (int64_t)us + 2 * CONFIG_USEC_PER_TICK + 2000) ?
         HWTL_PASS : HWTL_FAIL;
#else
  snprintf(detail, HWTL_DETAIL_MAX, "no CONFIG_ONESHOT");
  return HWTL_ERROR;
#endif
}

/****************************************************************************
 * Random number generator
 ****************************************************************************/

/* Read one block, short reads allowed */

static int hwtl_rng_read(int fd, FAR uint8_t *buf, size_t len)
{
  ssize_t nread;
  size_t done = 0;

  while (done < len)
    {
      nread = read(fd, buf + done, len - done);
      if (nread <= 0)
        {
          return nread < 0 ? -errno : -EIO;
        }

      done += nread;
    }

  return 0;
}

/* FIPS 140-2 monobit and poker tests of one block */

static bool hwtl_rng_fips(FAR const uint8_t *buf, FAR int *ones,
                          FAR int *poker)
{
  uint32_t freq[16];
  uint64_t sum = 0;
  int i;

  memset(freq, 0, sizeof(freq));
  *ones = 0;

  for (i = 0; i < HWTL_RNG_BLOCK; i++)
    {
      *ones += __builtin_popcount(buf[i]);
      freq[buf[i] & 0x0f]++;
      freq[buf[i] >> 4]++;
    }

  /* X = 16 / 5000 * sum(f^2) - 5000 (x100), pass 2.16 < X < 46.17 */

  for (i = 0; i < 16; i++)
    {
      sum += (uint64_t)freq[i] * freq[i];
    }

  *poker = (int)(sum * 16 * 100 / 5000) - 5000 * 100;

  return *ones >= HWTL_RNG_ONES_MIN && *ones <= HWTL_RNG_ONES_MAX &&
         *poker > 216 && *poker < 4617;
}

/* <blocks> blocks of /dev/random: FIPS 140-2 monobit and poker tests per
 * block, no two equal consecutive 32-bit words in the whole stream
 */

static enum hwtl_res_e hwtl_rng(FAR char **argv, FAR char *detail)
{
  uint32_t prev = 0;
  uint32_t word;
  int blocks = atoi(argv[1]);
  int repeats = 0;
  int bad = 0;
  int ones = 0;
  int poker = 0;
  bool pass;
  int ret;
  int o;
  int p;
  int fd;
  int b;
  int i;

  if (blocks <= 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "blocks=%d", blocks);
      return HWTL_ERROR;
    }

  fd = open(argv[0], O_RDONLY);
  if (fd < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "open %s errno=%d", argv[0], errno);
      return HWTL_ERROR;
    }

  for (b = 0; b < blocks; b++)
    {
      ret = hwtl_rng_read(fd, g_hwtl_rngbuf, HWTL_RNG_BLOCK);
      if (ret < 0)
        {
          snprintf(detail, HWTL_DETAIL_MAX, "%s block=%d read errno=%d",
                   argv[0], b, -ret);
          close(fd);
          return HWTL_ERROR;
        }

      /* Report the first failing block, or the last one */

      pass = hwtl_rng_fips(g_hwtl_rngbuf, &o, &p);
      if (bad == 0)
        {
          ones  = o;
          poker = p;
        }

      if (!pass)
        {
          bad++;
        }

      for (i = 0; i < HWTL_RNG_BLOCK; i += sizeof(word))
        {
          memcpy(&word, &g_hwtl_rngbuf[i], sizeof(word));
          if ((b > 0 || i > 0) && word == prev)
            {
              repeats++;
            }

          prev = word;
        }
    }

  close(fd);

  snprintf(detail, HWTL_DETAIL_MAX, "%s blocks=%d bad=%d repeats=%d "
           "ones=%d poker=%d.%02d", argv[0], blocks, bad, repeats, ones,
           poker / 100, poker % 100);
  return (bad == 0 && repeats == 0) ? HWTL_PASS : HWTL_FAIL;
}

/****************************************************************************
 * RTC
 ****************************************************************************/

#ifdef HWTL_HAVE_RTC
/* RTC time as seconds since the epoch */

static time_t hwtl_rtc_secs(FAR const struct rtc_time *rt)
{
  struct tm tm;

  memset(&tm, 0, sizeof(tm));
  tm.tm_sec  = rt->tm_sec;
  tm.tm_min  = rt->tm_min;
  tm.tm_hour = rt->tm_hour;
  tm.tm_mday = rt->tm_mday;
  tm.tm_mon  = rt->tm_mon;
  tm.tm_year = rt->tm_year;
  return timegm(&tm);
}
#endif

/* Set the RTC, read it back now and after ms milliseconds: both reads
 * match the elapsed CLOCK_MONOTONIC time within one second, across a year
 * rollover
 */

static enum hwtl_res_e hwtl_rtctime(FAR char **argv, FAR char *detail)
{
#ifdef HWTL_HAVE_RTC
  struct rtc_time set;
  struct rtc_time now;
  struct timespec start;
  time_t base;
  long first;
  long delta;
  long elapsed;
  int ms = atoi(argv[1]);
  int fd;

  fd = open(argv[0], O_RDWR);
  if (fd < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "open %s errno=%d", argv[0], errno);
      return HWTL_ERROR;
    }

  memset(&set, 0, sizeof(set));
  set.tm_year = HWTL_RTC_YEAR - 1900;
  set.tm_mon  = HWTL_RTC_MON;
  set.tm_mday = HWTL_RTC_MDAY;
  set.tm_hour = HWTL_RTC_HOUR;
  set.tm_min  = HWTL_RTC_MIN;
  set.tm_sec  = HWTL_RTC_SEC;
  base = hwtl_rtc_secs(&set);

  if (ioctl(fd, RTC_SET_TIME, (unsigned long)&set) < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "set errno=%d", errno);
      close(fd);
      return HWTL_ERROR;
    }

  clock_gettime(CLOCK_MONOTONIC, &start);

  if (ioctl(fd, RTC_RD_TIME, (unsigned long)&now) < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "read errno=%d", errno);
      close(fd);
      return HWTL_ERROR;
    }

  first = hwtl_rtc_secs(&now) - base;

  usleep(ms * 1000);

  if (ioctl(fd, RTC_RD_TIME, (unsigned long)&now) < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "read errno=%d", errno);
      close(fd);
      return HWTL_ERROR;
    }

  elapsed = hwtl_elapsed_ms(&start);
  delta   = hwtl_rtc_secs(&now) - base;
  close(fd);

  snprintf(detail, HWTL_DETAIL_MAX, "%s first=%ld rtc_s=%ld elapsed_ms=%ld "
           "now=%04d-%02d-%02d %02d:%02d:%02d", argv[0], first, delta,
           elapsed, now.tm_year + 1900, now.tm_mon + 1, now.tm_mday,
           now.tm_hour, now.tm_min, now.tm_sec);
  return (first >= 0 && first <= 1 &&
          delta * 1000 >= elapsed - 1000 &&
          delta * 1000 <= elapsed + 1000) ? HWTL_PASS : HWTL_FAIL;
#else
  snprintf(detail, HWTL_DETAIL_MAX, "no CONFIG_RTC_DRIVER");
  return HWTL_ERROR;
#endif
}

#if defined(HWTL_HAVE_RTC) && defined(CONFIG_RTC_ALARM)
/* Set alarm id to fire in secs seconds, signal on HWTL_SIGNO */

static int hwtl_rtc_setalarm(int fd, int id, int secs)
{
  struct rtc_setrelative_s rel;

  memset(&rel, 0, sizeof(rel));
  rel.id                          = id;
  rel.pid                         = getpid();
  rel.event.sigev_notify          = SIGEV_SIGNAL;
  rel.event.sigev_signo           = HWTL_SIGNO;
  rel.event.sigev_value.sival_int = id;
  rel.reltime                     = secs;

  return ioctl(fd, RTC_SET_RELATIVE, (unsigned long)&rel);
}

/* Read whether alarm id is active, negative on error */

static int hwtl_rtc_active(int fd, int id)
{
  struct rtc_rdalarm_s rd;

  memset(&rd, 0, sizeof(rd));
  rd.id = id;
  if (ioctl(fd, RTC_RD_ALARM, (unsigned long)&rd) < 0)
    {
      return -errno;
    }

  return rd.active;
}
#endif

/* Alarm id in secs seconds: active after set, fires not before secs and
 * at most one second later (the RTC alarm has a one second resolution)
 */

static enum hwtl_res_e hwtl_rtcalarm(FAR char **argv, FAR char *detail)
{
#if defined(HWTL_HAVE_RTC) && defined(CONFIG_RTC_ALARM)
  struct timespec start;
  struct timespec wait;
  sigset_t set;
  sigset_t old;
  long elapsed;
  int id = atoi(argv[1]);
  int secs = atoi(argv[2]);
  int active;
  int ret;
  int fd;

  fd = open(argv[0], O_RDWR);
  if (fd < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "open %s errno=%d", argv[0], errno);
      return HWTL_ERROR;
    }

  hwtl_sig_block(&old);

  clock_gettime(CLOCK_MONOTONIC, &start);
  if (hwtl_rtc_setalarm(fd, id, secs) < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "alarm %d errno=%d", id, errno);
      close(fd);
      hwtl_sig_restore(&old);
      return HWTL_ERROR;
    }

  active = hwtl_rtc_active(fd, id);

  sigemptyset(&set);
  sigaddset(&set, HWTL_SIGNO);
  wait.tv_sec  = secs + 3;
  wait.tv_nsec = 0;
  ret = sigtimedwait(&set, NULL, &wait);
  elapsed = hwtl_elapsed_ms(&start);

  ioctl(fd, RTC_CANCEL_ALARM, (unsigned long)id);
  close(fd);
  hwtl_sig_restore(&old);

  snprintf(detail, HWTL_DETAIL_MAX, "%s id=%d s=%d active=%d fired=%d "
           "elapsed_ms=%ld", argv[0], id, secs, active, ret == HWTL_SIGNO,
           elapsed);
  return (active == 1 && ret == HWTL_SIGNO &&
          elapsed + CONFIG_USEC_PER_TICK / 1000 >= secs * 1000L &&
          elapsed <= (secs + 1) * 1000L + 100) ? HWTL_PASS : HWTL_FAIL;
#else
  snprintf(detail, HWTL_DETAIL_MAX, "no CONFIG_RTC_ALARM");
  return HWTL_ERROR;
#endif
}

/* Alarm id cancelled right after it is set: inactive, never fires */

static enum hwtl_res_e hwtl_rtccancel(FAR char **argv, FAR char *detail)
{
#if defined(HWTL_HAVE_RTC) && defined(CONFIG_RTC_ALARM)
  struct timespec wait;
  sigset_t set;
  sigset_t old;
  int id = atoi(argv[1]);
  int secs = atoi(argv[2]);
  int active;
  int ret;
  int fd;

  fd = open(argv[0], O_RDWR);
  if (fd < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "open %s errno=%d", argv[0], errno);
      return HWTL_ERROR;
    }

  hwtl_sig_block(&old);

  if (hwtl_rtc_setalarm(fd, id, secs) < 0 ||
      ioctl(fd, RTC_CANCEL_ALARM, (unsigned long)id) < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "alarm %d errno=%d", id, errno);
      close(fd);
      hwtl_sig_restore(&old);
      return HWTL_ERROR;
    }

  active = hwtl_rtc_active(fd, id);

  sigemptyset(&set);
  sigaddset(&set, HWTL_SIGNO);
  wait.tv_sec  = secs + 1;
  wait.tv_nsec = 0;
  ret = sigtimedwait(&set, NULL, &wait);

  close(fd);
  hwtl_sig_restore(&old);

  snprintf(detail, HWTL_DETAIL_MAX, "%s id=%d s=%d active=%d fired=%d",
           argv[0], id, secs, active, ret == HWTL_SIGNO);
  return (active == 0 && ret != HWTL_SIGNO) ? HWTL_PASS : HWTL_FAIL;
#else
  snprintf(detail, HWTL_DETAIL_MAX, "no CONFIG_RTC_ALARM");
  return HWTL_ERROR;
#endif
}

/* Periodic wakeup: wakeups counted in ms are within one of
 * ms / period_ms
 */

static enum hwtl_res_e hwtl_rtcperiodic(FAR char **argv, FAR char *detail)
{
#if defined(HWTL_HAVE_RTC) && defined(CONFIG_RTC_PERIODIC)
  struct rtc_setperiodic_s per;
  sigset_t old;
  int period = atoi(argv[1]);
  int ms = atoi(argv[2]);
  int expect;
  int count;
  int fd;

  if (period <= 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "period_ms=%d", period);
      return HWTL_ERROR;
    }

  fd = open(argv[0], O_RDWR);
  if (fd < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "open %s errno=%d", argv[0], errno);
      return HWTL_ERROR;
    }

  hwtl_sig_block(&old);

  memset(&per, 0, sizeof(per));
  per.pid                = getpid();
  per.event.sigev_notify = SIGEV_SIGNAL;
  per.event.sigev_signo  = HWTL_SIGNO;
  per.period.tv_sec      = period / 1000;
  per.period.tv_nsec     = (period % 1000) * 1000000;

  if (ioctl(fd, RTC_SET_PERIODIC, (unsigned long)&per) < 0)
    {
      snprintf(detail, HWTL_DETAIL_MAX, "periodic errno=%d", errno);
      close(fd);
      hwtl_sig_restore(&old);
      return HWTL_ERROR;
    }

  count = hwtl_count_signals(ms);
  ioctl(fd, RTC_CANCEL_PERIODIC, 0);
  close(fd);
  hwtl_sig_restore(&old);

  expect = ms / period;

  snprintf(detail, HWTL_DETAIL_MAX, "%s period_ms=%d ms=%d count=%d "
           "expect=%d", argv[0], period, ms, count, expect);
  return (count >= expect - 1 && count <= expect + 1) ?
         HWTL_PASS : HWTL_FAIL;
#else
  snprintf(detail, HWTL_DETAIL_MAX, "no CONFIG_RTC_PERIODIC");
  return HWTL_ERROR;
#endif
}

/****************************************************************************
 * Command dispatch
 ****************************************************************************/

static FAR const struct hwtl_cmd_s *hwtl_find(FAR const char *name)
{
  size_t i;

  for (i = 0; i < HWTL_NCMDS; i++)
    {
      if (strcmp(g_hwtl_cmds[i].name, name) == 0)
        {
          return &g_hwtl_cmds[i];
        }
    }

  return NULL;
}

static void hwtl_usage(void)
{
  size_t i;

  printf("Usage:\n");
  for (i = 0; i < HWTL_NCMDS; i++)
    {
      printf("       hwtl %s %s\n", g_hwtl_cmds[i].name,
             g_hwtl_cmds[i].usage);
    }
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  FAR const struct hwtl_cmd_s *cmd;
  FAR char *args[8];
  char detail[HWTL_DETAIL_MAX];
  enum hwtl_res_e res;
  int nargs;
  int i;

  if (argc < 2)
    {
      hwtl_usage();
      return EXIT_FAILURE;
    }

  cmd   = hwtl_find(argv[1]);
  nargs = argc - 2;
  if (cmd == NULL || nargs < cmd->minargs || nargs > cmd->maxargs)
    {
      hwtl_usage();
      return EXIT_FAILURE;
    }

  for (i = 0; i < nargs; i++)
    {
      args[i] = argv[i + 2];
    }

  for (; i < 8; i++)
    {
      args[i] = NULL;
    }

  detail[0] = '\0';
  res = cmd->check(args, detail);

  printf("hwtl: %s %s %s\n", res == HWTL_PASS ? "PASS" : "FAIL", cmd->name,
         detail);
  return res == HWTL_PASS ? EXIT_SUCCESS : EXIT_FAILURE;
}
