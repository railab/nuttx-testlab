/****************************************************************************
 * apps/posixtl/posixtl_main.c
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

#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <time.h>
#include <sys/time.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define POSIXTL_NS_MS      1000000LL
#define POSIXTL_NS_S       1000000000LL

/* Signal of the timer and sigqueue() cases */

#define POSIXTL_SIGNO      SIGRTMIN

/* Period of the overrun_latched case and the number of periods that the
 * signal of the timer stays blocked.
 */

#define POSIXTL_PERIOD_MS  50
#define POSIXTL_PERIODS    10

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct posixtl_case_s
{
  FAR const char *name;
  CODE bool (*func)(void);
};

/****************************************************************************
 * Private Function Prototypes
 ****************************************************************************/

static bool posixtl_overrun_latched(void);
static bool posixtl_reload(void);
static bool posixtl_thread_exit(void);
static bool posixtl_sender_pid(void);
static bool posixtl_itimer_einval(void);

/****************************************************************************
 * Private Data
 ****************************************************************************/

static const struct posixtl_case_s g_cases[] =
{
  { "overrun_latched", posixtl_overrun_latched },
  { "reload", posixtl_reload },
  { "thread_exit", posixtl_thread_exit },
  { "sender_pid", posixtl_sender_pid },
  { "itimer_einval", posixtl_itimer_einval },
};

static char g_reason[160];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/* Record the reason of a failure.  Always returns false. */

static bool posixtl_fail(FAR const char *fmt, ...)
{
  va_list ap;

  va_start(ap, fmt);
  vsnprintf(g_reason, sizeof(g_reason), fmt, ap);
  va_end(ap);
  return false;
}

/* The cases accept their signals with sigtimedwait(): the handler only
 * sets SA_SIGINFO.
 */

static void posixtl_handler(int signo, FAR siginfo_t *info,
                            FAR void *context)
{
}

/* Catch signo with an SA_SIGINFO handler: POSIX queues the signals of
 * sigqueue() and SIGEV_SIGNAL only "if the SA_SIGINFO flag is set for
 * that signal".
 */

static bool posixtl_catch(int signo)
{
  struct sigaction act;

  memset(&act, 0, sizeof(act));
  act.sa_sigaction = posixtl_handler;
  act.sa_flags = SA_SIGINFO;
  sigemptyset(&act.sa_mask);
  if (sigaction(signo, &act, NULL) < 0)
    {
      return posixtl_fail("sigaction(%d) errno=%d", signo, errno);
    }

  return true;
}

static bool posixtl_mask(int how, int signo)
{
  sigset_t set;
  int ret;

  sigemptyset(&set);
  sigaddset(&set, signo);
  ret = pthread_sigmask(how, &set, NULL);
  if (ret != 0)
    {
      return posixtl_fail("pthread_sigmask(%d) ret=%d", signo, ret);
    }

  return true;
}

/* Catch and block signo */

static bool posixtl_setup(int signo)
{
  return posixtl_catch(signo) && posixtl_mask(SIG_BLOCK, signo);
}

static int64_t posixtl_ts2ns(FAR const struct timespec *ts)
{
  return (int64_t)ts->tv_sec * POSIXTL_NS_S + ts->tv_nsec;
}

static void posixtl_ns2ts(int64_t ns, FAR struct timespec *ts)
{
  ts->tv_sec = (time_t)(ns / POSIXTL_NS_S);
  ts->tv_nsec = (long)(ns % POSIXTL_NS_S);
}

static void posixtl_sleep_ms(int ms)
{
  struct timespec ts;

  posixtl_ns2ts(ms * POSIXTL_NS_MS, &ts);
  while (nanosleep(&ts, &ts) < 0 && errno == EINTR)
    {
    }
}

/* Accept signo within ms milliseconds.  Returns signo, or -1 with errno
 * (EAGAIN: no signal).
 */

static int posixtl_wait(int signo, int ms, FAR siginfo_t *info)
{
  struct timespec ts;
  sigset_t set;

  sigemptyset(&set);
  sigaddset(&set, signo);
  posixtl_ns2ts(ms * POSIXTL_NS_MS, &ts);
  memset(info, 0, sizeof(*info));
  return sigtimedwait(&set, info, &ts);
}

static bool posixtl_create(clockid_t clockid, int signo, int value,
                           FAR timer_t *timerid)
{
  struct sigevent ev;

  memset(&ev, 0, sizeof(ev));
  ev.sigev_notify = SIGEV_SIGNAL;
  ev.sigev_signo = signo;
  ev.sigev_value.sival_int = value;
  if (timer_create(clockid, &ev, timerid) < 0)
    {
      return posixtl_fail("timer_create(%d) errno=%d", (int)clockid,
                          errno);
    }

  return true;
}

static int posixtl_arm(timer_t timerid, int64_t value_ns,
                       int64_t interval_ns)
{
  struct itimerspec spec;

  posixtl_ns2ts(value_ns, &spec.it_value);
  posixtl_ns2ts(interval_ns, &spec.it_interval);
  return timer_settime(timerid, 0, &spec, NULL);
}

/* Check the siginfo_t of a timer signal.  POSIX (2.4.3): "If si_code is
 * one of SI_QUEUE, SI_TIMER, SI_ASYNCIO, or SI_MESGQ, then si_value shall
 * contain the application-specified signal value."  <signal.h>: SI_TIMER
 * "Signal generated by expiration of a timer set by timer_settime()".
 */

static bool posixtl_check_timer_info(FAR const char *what,
                                     FAR const siginfo_t *info,
                                     int signo, int value)
{
  if (info->si_signo != signo || info->si_code != SI_TIMER ||
      info->si_value.sival_int != value)
    {
      return posixtl_fail("%s si_signo=%d si_code=%d si_value=%d, "
                          "want %d SI_TIMER(%d) %d", what, info->si_signo,
                          info->si_code, info->si_value.sival_int, signo,
                          SI_TIMER, value);
    }

  return true;
}

/* Resolution of the timers.  timer_settime(): "Time values that are
 * between two consecutive non-negative integer multiples of the
 * resolution of the specified timer shall be rounded up to the larger
 * multiple of the resolution."  The time left that timer_gettime() reads
 * can be up to one resolution more than the time set.
 */

static int64_t posixtl_res(void)
{
  struct timespec res;

  if (clock_getres(CLOCK_MONOTONIC, &res) < 0)
    {
      return 0;
    }

  return posixtl_ts2ns(&res);
}

/* timer_settime(): "When a timer for which a signal is still pending
 * expires, no signal shall be queued, and a timer overrun shall occur."
 * "The value returned by timer_getoverrun() shall apply to the most recent
 * expiration signal delivery or acceptance for the timer."
 *
 * A periodic timer runs with its signal blocked for POSIXTL_PERIODS
 * periods, then the signal is accepted.  The timer goes on with the signal
 * blocked: the next signal is generated and expirations go by, but none is
 * delivered or accepted, so the count stays the one of the acceptance.
 */

static bool posixtl_overrun_latched(void)
{
  int64_t period = POSIXTL_PERIOD_MS * POSIXTL_NS_MS;
  siginfo_t info;
  timer_t timerid;
  int first;
  int ret;

  if (!posixtl_setup(POSIXTL_SIGNO) ||
      !posixtl_create(CLOCK_MONOTONIC, POSIXTL_SIGNO, 1, &timerid))
    {
      return false;
    }

  if (posixtl_arm(timerid, period, period) != 0)
    {
      posixtl_fail("timer_settime errno=%d", errno);
      goto out;
    }

  posixtl_sleep_ms(POSIXTL_PERIOD_MS * POSIXTL_PERIODS);
  if (posixtl_wait(POSIXTL_SIGNO, 2000, &info) != POSIXTL_SIGNO)
    {
      posixtl_fail("no timer signal errno=%d", errno);
      goto out;
    }

  if (!posixtl_check_timer_info("first", &info, POSIXTL_SIGNO, 1))
    {
      goto out;
    }

  first = timer_getoverrun(timerid);
  posixtl_sleep_ms(POSIXTL_PERIOD_MS * 7 / 2);
  ret = timer_getoverrun(timerid);
  if (ret != first)
    {
      posixtl_fail("overrun=%d at the acceptance, %d %d ms later with no "
                   "signal accepted, want the same", first, ret,
                   POSIXTL_PERIOD_MS * 7 / 2);
      goto out;
    }

  timer_delete(timerid);
  return true;

out:
  timer_delete(timerid);
  return false;
}

/* Check an itimerspec: it_value in value_lo..value_hi (plus one
 * resolution if value_hi is not zero) and it_interval.
 */

static bool posixtl_check_spec(FAR const char *what,
                               FAR const struct itimerspec *spec,
                               int64_t value_lo, int64_t value_hi,
                               int64_t interval)
{
  int64_t value = posixtl_ts2ns(&spec->it_value);
  int64_t ival = posixtl_ts2ns(&spec->it_interval);

  if (value_hi > 0)
    {
      value_hi += posixtl_res();
    }

  if (value < value_lo || value > value_hi || ival != interval)
    {
      return posixtl_fail("%s it_value=%lld it_interval=%lld, want "
                          "%lld..%lld %lld", what, (long long)value,
                          (long long)ival, (long long)value_lo,
                          (long long)value_hi, (long long)interval);
    }

  return true;
}

/* timer_settime(): "The reload value of the timer shall be set to the
 * value specified by the it_interval member of value."  timer_gettime():
 * "The it_interval member of value shall contain the reload value last
 * set by timer_settime()."  This holds for a call that disarms the timer
 * too.
 */

static bool posixtl_reload(void)
{
  struct itimerspec spec;
  timer_t timerid;
  int64_t ms = POSIXTL_NS_MS;
  bool ok;

  if (!posixtl_setup(POSIXTL_SIGNO) ||
      !posixtl_create(CLOCK_MONOTONIC, POSIXTL_SIGNO, 5, &timerid))
    {
      return false;
    }

  ok = posixtl_arm(timerid, 2000 * ms, 300 * ms) == 0;
  ok = ok && timer_gettime(timerid, &spec) == 0;
  ok = ok && posixtl_check_spec("armed", &spec, 1, 2000 * ms, 300 * ms);
  ok = ok && posixtl_arm(timerid, 0, 0) == 0;
  ok = ok && timer_gettime(timerid, &spec) == 0;
  ok = ok && posixtl_check_spec("disarmed with it_interval 0", &spec,
                                0, 0, 0);
  if (!ok && g_reason[0] == '\0')
    {
      posixtl_fail("timer_settime/gettime errno=%d", errno);
    }

  timer_delete(timerid);
  return ok;
}

static FAR void *posixtl_thread_timer(FAR void *arg)
{
  FAR timer_t *timerid = arg;

  if (!posixtl_create(CLOCK_MONOTONIC, POSIXTL_SIGNO, 0x77, timerid) ||
      posixtl_arm(*timerid, 300 * POSIXTL_NS_MS, 0) != 0)
    {
      return (FAR void *)1;
    }

  return NULL;
}

/* timer_create(): "The timer_create() function shall create a per-process
 * timer".  2.4.1 SIGEV_SIGNAL: "The signal specified in sigev_signo shall
 * be generated for the process when the event of interest occurs."  A
 * thread creates and arms a timer and exits: the timer belongs to the
 * process, so it stays and its signal comes to the process.
 */

static bool posixtl_thread_exit(void)
{
  pthread_t thread;
  siginfo_t info;
  timer_t timerid;
  FAR void *result;
  int ret;

  if (!posixtl_setup(POSIXTL_SIGNO))
    {
      return false;
    }

  ret = pthread_create(&thread, NULL, posixtl_thread_timer, &timerid);
  if (ret != 0)
    {
      return posixtl_fail("pthread_create=%d", ret);
    }

  pthread_join(thread, &result);
  if (result != NULL)
    {
      return posixtl_fail("thread: %s", g_reason);
    }

  if (posixtl_wait(POSIXTL_SIGNO, 2000, &info) != POSIXTL_SIGNO)
    {
      return posixtl_fail("no signal of the timer of the exited thread "
                          "errno=%d", errno);
    }

  timer_delete(timerid);
  return posixtl_check_timer_info("timer of the exited thread", &info,
                                  POSIXTL_SIGNO, 0x77);
}

/* 2.4.3: "SI_QUEUE The signal was sent by the sigqueue() function."  "If
 * si_code is SI_USER or SI_QUEUE, ... then the signal was generated by a
 * process and si_pid and si_uid shall be set to the process ID and the
 * real user ID of the sender, respectively."  "If si_code is one of
 * SI_QUEUE, ... then si_value shall contain the application-specified
 * signal value."
 */

static bool posixtl_check_queue_info(FAR const char *what,
                                     FAR const siginfo_t *info, int signo,
                                     int code, int value)
{
  if (info->si_signo != signo || info->si_code != code ||
      info->si_pid != getpid() || info->si_uid != getuid())
    {
      return posixtl_fail("%s si_signo=%d si_code=%d si_pid=%d si_uid=%d, "
                          "want %d %d %d %d", what, info->si_signo,
                          info->si_code, (int)info->si_pid,
                          (int)info->si_uid, signo, code, (int)getpid(),
                          (int)getuid());
    }

  if (code == SI_QUEUE && info->si_value.sival_int != value)
    {
      return posixtl_fail("%s si_value=%d, want %d", what,
                          info->si_value.sival_int, value);
    }

  return true;
}

static FAR void *posixtl_thread_send(FAR void *arg)
{
  union sigval value;

  value.sival_int = 0x99;
  if (sigqueue(getpid(), POSIXTL_SIGNO, value) != 0 ||
      kill(getpid(), SIGUSR1) != 0)
    {
      return (FAR void *)1;
    }

  return NULL;
}

/* 2.4.3: "If si_code is SI_USER or SI_QUEUE, ... si_pid and si_uid shall
 * be set to the process ID and the real user ID of the sender".  The
 * sender is a second thread of the process: its process ID is getpid().
 */

static bool posixtl_sender_pid(void)
{
  pthread_t thread;
  siginfo_t info;
  FAR void *result;
  int ret;

  if (!posixtl_setup(POSIXTL_SIGNO) || !posixtl_setup(SIGUSR1))
    {
      return false;
    }

  ret = pthread_create(&thread, NULL, posixtl_thread_send, NULL);
  if (ret != 0)
    {
      return posixtl_fail("pthread_create=%d", ret);
    }

  pthread_join(thread, &result);
  if (result != NULL)
    {
      return posixtl_fail("sigqueue/kill in the thread failed");
    }

  if (posixtl_wait(POSIXTL_SIGNO, 1000, &info) != POSIXTL_SIGNO)
    {
      return posixtl_fail("no queued signal errno=%d", errno);
    }

  if (!posixtl_check_queue_info("sigqueue() from a thread", &info,
                                POSIXTL_SIGNO, SI_QUEUE, 0x99))
    {
      return false;
    }

  if (posixtl_wait(SIGUSR1, 1000, &info) != SIGUSR1)
    {
      return posixtl_fail("no kill() signal errno=%d", errno);
    }

  return posixtl_check_queue_info("kill() from a thread", &info, SIGUSR1,
                                  SI_USER, 0);
}

/* setitimer() (POSIX.1-2017): "The setitimer() function shall fail if:
 * [EINVAL] The value argument is not in canonical form.  (In canonical
 * form, the number of microseconds is a non-negative integer less than
 * 1000000 and the number of seconds is a non-negative integer.)"
 */

static bool posixtl_itimer_einval(void)
{
  static const suseconds_t usecs[] =
  {
    1000000, -1
  };

  struct itimerval itv;
  unsigned int i;
  int ret;

  for (i = 0; i < 2; i++)
    {
      memset(&itv, 0, sizeof(itv));
      itv.it_value.tv_sec = 1;
      itv.it_value.tv_usec = usecs[i];
      errno = 0;
      ret = setitimer(ITIMER_REAL, &itv, NULL);
      if (ret != -1 || errno != EINVAL)
        {
          memset(&itv, 0, sizeof(itv));
          setitimer(ITIMER_REAL, &itv, NULL);
          return posixtl_fail("setitimer(tv_usec=%ld)=%d errno=%d, want "
                              "-1 EINVAL", (long)usecs[i], ret, errno);
        }
    }

  return true;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  unsigned int i;

  if (argc != 2)
    {
      fprintf(stderr, "Usage: %s <case>\n", argv[0]);
      return 2;
    }

  for (i = 0; i < sizeof(g_cases) / sizeof(g_cases[0]); i++)
    {
      if (strcmp(argv[1], g_cases[i].name) == 0)
        {
          g_reason[0] = '\0';
          if (g_cases[i].func())
            {
              printf("posixtl: PASS %s\n", argv[1]);
              return 0;
            }

          printf("posixtl: FAIL %s %s\n", argv[1], g_reason);
          return 1;
        }
    }

  printf("posixtl: FAIL %s unknown case\n", argv[1]);
  return 2;
}
