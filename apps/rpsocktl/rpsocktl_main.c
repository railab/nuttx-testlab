/****************************************************************************
 * apps/rpsocktl/rpsocktl_main.c
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

#include <sys/types.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/uio.h>

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <netpacket/rpmsg.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define RPSOCKTL_TIMEOUT   10
#define RPSOCKTL_BUFSIZE   512
#define RPSOCKTL_BIGMSG    65536

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct rpsocktl_args_s
{
  FAR const char *cpu;      /* Client: CPU of the server */
  FAR const char *name;     /* Socket name */
  FAR const char *mode;     /* Server mode, client or local check */
  bool            server;
  bool            dgram;
  bool            local;
  size_t          count;
  int             timeout;
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

static const size_t g_dgram_lens[] =
{
  1, 2, 3, 64, 255, 256, 511, 1000
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void rpsocktl_usage(FAR const char *progname)
{
  printf("Usage: %s -s [-m echo|send|close|select] [-n bytes] "
         "[-t sec] name\n", progname);
  printf("       %s -s -u -c cpu [-t sec] name\n", progname);
  printf("       %s -c cpu [-u] [-m check] [-n bytes] [-t sec] name\n",
         progname);
  printf("       %s -e check\n", progname);
}

static uint8_t rpsocktl_byte(size_t off, unsigned int seed)
{
  return (uint8_t)((off * 31 + 7 + seed) & 0xff);
}

static void rpsocktl_fill(FAR uint8_t *buf, size_t off, size_t len,
                          unsigned int seed)
{
  size_t i;

  for (i = 0; i < len; i++)
    {
      buf[i] = rpsocktl_byte(off + i, seed);
    }
}

static size_t rpsocktl_check(FAR const uint8_t *buf, size_t off,
                             size_t len, unsigned int seed)
{
  size_t bad = 0;
  size_t i;

  for (i = 0; i < len; i++)
    {
      if (buf[i] != rpsocktl_byte(off + i, seed))
        {
          bad++;
        }
    }

  return bad;
}

static void rpsocktl_addr(FAR struct sockaddr_rpmsg *addr,
                          FAR const char *cpu, FAR const char *name)
{
  memset(addr, 0, sizeof(*addr));
  addr->rp_family = AF_RPMSG;
  strlcpy(addr->rp_cpu, cpu, sizeof(addr->rp_cpu));
  strlcpy(addr->rp_name, name, sizeof(addr->rp_name));
}

static void rpsocktl_rcvtimeo(int sd, int timeout)
{
  struct timeval tv;

  tv.tv_sec  = timeout;
  tv.tv_usec = 0;
  setsockopt(sd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
}

static void rpsocktl_alarm(int signo)
{
  UNUSED(signo);
}

/* Bound a blocking connect() with SIGALRM, without SA_RESTART */

static int rpsocktl_connect(int sd, FAR const struct sockaddr_rpmsg *addr,
                            int timeout)
{
  struct sigaction act;
  int ret;

  memset(&act, 0, sizeof(act));
  act.sa_handler = rpsocktl_alarm;
  sigemptyset(&act.sa_mask);
  sigaction(SIGALRM, &act, NULL);

  alarm(timeout);
  ret = connect(sd, (FAR const struct sockaddr *)addr, sizeof(*addr));
  alarm(0);
  return ret;
}

/* Wait up to timeout seconds for sd to become readable: 1 or 0 */

static int rpsocktl_readable(int sd, int timeout)
{
  struct timeval tv;
  fd_set rfds;
  int ret;

  FD_ZERO(&rfds);
  FD_SET(sd, &rfds);
  tv.tv_sec  = timeout;
  tv.tv_usec = 0;
  ret = select(sd + 1, &rfds, NULL, NULL, &tv);
  return ret > 0 && FD_ISSET(sd, &rfds) ? 1 : 0;
}

static int rpsocktl_sendall(int sd, size_t count, FAR size_t *tx)
{
  uint8_t buf[RPSOCKTL_BUFSIZE];
  ssize_t ret;

  while (*tx < count)
    {
      size_t len = count - *tx;

      if (len > sizeof(buf))
        {
          len = sizeof(buf);
        }

      rpsocktl_fill(buf, *tx, len, 0);
      ret = send(sd, buf, len, MSG_NOSIGNAL);
      if (ret <= 0)
        {
          return errno;
        }

      *tx += ret;
    }

  return 0;
}

/* Receive until end-of-file: 0 when it came, else the errno */

static int rpsocktl_drain(int sd, FAR uint8_t *buf, size_t bufsize,
                          FAR size_t *rx, bool echo)
{
  ssize_t ret;

  for (; ; )
    {
      ret = recv(sd, buf, bufsize, 0);
      if (ret == 0)
        {
          return 0;
        }

      if (ret < 0)
        {
          return errno;
        }

      *rx += ret;
      if (echo && send(sd, buf, ret, MSG_NOSIGNAL) != ret)
        {
          return errno;
        }
    }
}

static int rpsocktl_stream_server(FAR const struct rpsocktl_args_s *args)
{
  struct sockaddr_rpmsg addr;
  struct sockaddr_rpmsg peer;
  struct sockaddr_rpmsg peer2;
  uint8_t buf[RPSOCKTL_BUFSIZE];
  socklen_t len = sizeof(peer);
  size_t rx = 0;
  size_t tx = 0;
  int sel = -1;
  int err = 0;
  int sd;
  int cd;

  memset(&peer, 0, sizeof(peer));
  memset(&peer2, 0, sizeof(peer2));

  sd = socket(AF_RPMSG, SOCK_STREAM, 0);
  if (sd < 0)
    {
      printf("rpsocktl: FAIL server socket errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  rpsocktl_addr(&addr, "", args->name);
  if (bind(sd, (FAR struct sockaddr *)&addr, sizeof(addr)) < 0 ||
      listen(sd, 1) < 0)
    {
      printf("rpsocktl: FAIL server listen errno=%d\n", errno);
      close(sd);
      return EXIT_FAILURE;
    }

  printf("rpsocktl: listening name=%s\n", args->name);
  fflush(stdout);

  if (strcmp(args->mode, "select") == 0)
    {
      sel = rpsocktl_readable(sd, args->timeout);
      if (sel == 0)
        {
          printf("rpsocktl: FAIL server mode=select sel=0\n");
          close(sd);
          return EXIT_FAILURE;
        }
    }

  cd = accept(sd, (FAR struct sockaddr *)&peer, &len);
  if (cd < 0)
    {
      printf("rpsocktl: FAIL server accept errno=%d\n", errno);
      close(sd);
      return EXIT_FAILURE;
    }

  len = sizeof(peer2);
  getpeername(cd, (FAR struct sockaddr *)&peer2, &len);
  rpsocktl_rcvtimeo(cd, args->timeout);

  if (strcmp(args->mode, "send") == 0)
    {
      err = rpsocktl_sendall(cd, args->count, &tx);
      if (err == 0 && shutdown(cd, SHUT_WR) < 0)
        {
          err = errno;
        }

      if (err == 0)
        {
          err = rpsocktl_drain(cd, buf, sizeof(buf), &rx, false);
        }
    }
  else if (strcmp(args->mode, "close") == 0)
    {
      err = rpsocktl_sendall(cd, args->count, &tx);
    }
  else
    {
      err = rpsocktl_drain(cd, buf, sizeof(buf), &rx, true);
      tx = rx;
    }

  close(cd);
  close(sd);

  printf("rpsocktl: %s server mode=%s rx=%zu tx=%zu sel=%d peer=%s "
         "peer2=%s errno=%d\n", err == 0 ? "PASS" : "FAIL", args->mode,
         rx, tx, sel, peer.rp_cpu, peer2.rp_cpu, err);
  return err == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int rpsocktl_dgram_server(FAR const struct rpsocktl_args_s *args)
{
  struct sockaddr_rpmsg addr;
  FAR uint8_t *buf;
  ssize_t ret;
  size_t rx = 0;
  int err = 0;
  int sd;

  buf = malloc(RPSOCKTL_BIGMSG);
  sd = socket(AF_RPMSG, SOCK_DGRAM, 0);
  if (buf == NULL || sd < 0)
    {
      printf("rpsocktl: FAIL server socket errno=%d\n", errno);
      free(buf);
      return EXIT_FAILURE;
    }

  rpsocktl_addr(&addr, "", args->name);
  if (bind(sd, (FAR struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
      printf("rpsocktl: FAIL server bind errno=%d\n", errno);
      goto out;
    }

  printf("rpsocktl: listening name=%s\n", args->name);
  fflush(stdout);

  /* Both ends of a datagram socket connect to the other CPU */

  rpsocktl_addr(&addr, args->cpu, args->name);
  if (rpsocktl_connect(sd, &addr, args->timeout) < 0)
    {
      err = errno;
      goto out;
    }

  rpsocktl_rcvtimeo(sd, args->timeout);
  for (; ; )
    {
      ret = recv(sd, buf, RPSOCKTL_BIGMSG, 0);
      if (ret <= 0)
        {
          if (ret < 0 && errno != EAGAIN && errno != ETIMEDOUT)
            {
              err = errno;
            }

          break;
        }

      rx++;
      if (send(sd, buf, ret, 0) != ret)
        {
          err = errno;
          break;
        }
    }

out:
  close(sd);
  free(buf);
  printf("rpsocktl: %s server mode=dgram rx=%zu errno=%d\n",
         err == 0 ? "PASS" : "FAIL", rx, err);
  return err == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int rpsocktl_client_eof(int sd,
                               FAR const struct rpsocktl_args_s *args)
{
  uint8_t buf[RPSOCKTL_BUFSIZE];
  size_t rx = 0;
  size_t bad = 0;
  ssize_t ret;
  int err = 0;

  /* Let the data and the end of the stream arrive before reading */

  sleep(1);
  for (; ; )
    {
      ret = recv(sd, buf, sizeof(buf), 0);
      if (ret <= 0)
        {
          err = ret < 0 ? errno : 0;
          break;
        }

      bad += rpsocktl_check(buf, rx, ret, 0);
      rx += ret;
    }

  ret = err == 0 && rx == args->count && bad == 0;
  printf("rpsocktl: %s eof rx=%zu bad=%zu errno=%d\n",
         ret ? "PASS" : "FAIL", rx, bad, err);
  return ret ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int rpsocktl_client_selecteof(int sd,
                                     FAR const struct rpsocktl_args_s *args)
{
  ssize_t ret;
  char ch;
  int sel;
  int err;

  sleep(1);
  sel = rpsocktl_readable(sd, args->timeout);
  ret = recv(sd, &ch, 1, MSG_DONTWAIT);
  err = ret < 0 ? errno : 0;
  printf("rpsocktl: %s selecteof sel=%d ret=%zd errno=%d\n",
         sel == 1 && ret == 0 ? "PASS" : "FAIL", sel, ret, err);
  return sel == 1 && ret == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int rpsocktl_client_readv(int sd,
                                 FAR const struct rpsocktl_args_s *args)
{
  uint8_t buf0[40];
  uint8_t buf1[60];
  struct iovec iov[2];
  size_t total = 0;
  size_t bad = 0;
  ssize_t ret = 0;
  int err = 0;

  sleep(1);
  while (total < args->count)
    {
      iov[0].iov_base = buf0;
      iov[0].iov_len  = sizeof(buf0);
      iov[1].iov_base = buf1;
      iov[1].iov_len  = sizeof(buf1);
      ret = readv(sd, iov, 2);
      if (ret <= 0)
        {
          err = ret < 0 ? errno : 0;
          break;
        }

      if ((size_t)ret <= sizeof(buf0))
        {
          bad += rpsocktl_check(buf0, total, ret, 0);
        }
      else
        {
          bad += rpsocktl_check(buf0, total, sizeof(buf0), 0);
          bad += rpsocktl_check(buf1, total + sizeof(buf0),
                                ret - sizeof(buf0), 0);
        }

      total += ret;
    }

  ret = total == args->count && bad == 0;
  printf("rpsocktl: %s readv rx=%zu bad=%zu errno=%d\n",
         ret ? "PASS" : "FAIL", total, bad, err);
  return ret ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int rpsocktl_client_peer(int sd,
                                FAR const struct rpsocktl_args_s *args)
{
  struct sockaddr_rpmsg peer;
  socklen_t len = sizeof(peer);
  bool ok;
  int ret;

  memset(&peer, 0, sizeof(peer));
  ret = getpeername(sd, (FAR struct sockaddr *)&peer, &len);
  ok = ret == 0 && peer.rp_family == AF_RPMSG &&
       strcmp(peer.rp_cpu, args->cpu) == 0 &&
       strcmp(peer.rp_name, args->name) == 0;
  printf("rpsocktl: %s peer ret=%d family=%d cpu=%s name=%s errno=%d\n",
         ok ? "PASS" : "FAIL", ret, peer.rp_family, peer.rp_cpu,
         peer.rp_name, ret < 0 ? errno : 0);
  return ok ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int rpsocktl_client_isconn(int sd,
                                  FAR const struct rpsocktl_args_s *args)
{
  struct sockaddr_rpmsg addr;
  int ret;
  int err;

  rpsocktl_addr(&addr, args->cpu, args->name);
  ret = rpsocktl_connect(sd, &addr, args->timeout);
  err = ret < 0 ? errno : 0;
  printf("rpsocktl: %s isconn ret=%d errno=%d\n",
         ret < 0 && err == EISCONN ? "PASS" : "FAIL", ret, err);
  return ret < 0 && err == EISCONN ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int rpsocktl_client_epipe(int sd)
{
  ssize_t ret;
  char ch = 'x';
  int err;

  if (shutdown(sd, SHUT_WR) < 0)
    {
      printf("rpsocktl: FAIL epipe shutdown errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  ret = send(sd, &ch, 1, MSG_NOSIGNAL);
  err = ret < 0 ? errno : 0;
  printf("rpsocktl: %s epipe ret=%zd errno=%d\n",
         ret < 0 && err == EPIPE ? "PASS" : "FAIL", ret, err);
  return ret < 0 && err == EPIPE ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int rpsocktl_stream_client(FAR const struct rpsocktl_args_s *args)
{
  struct sockaddr_rpmsg addr;
  int ret;
  int sd;

  sd = socket(AF_RPMSG, SOCK_STREAM, 0);
  if (sd < 0)
    {
      printf("rpsocktl: FAIL %s socket errno=%d\n", args->mode, errno);
      return EXIT_FAILURE;
    }

  rpsocktl_addr(&addr, args->cpu, args->name);
  ret = rpsocktl_connect(sd, &addr, args->timeout);
  if (strcmp(args->mode, "refused") == 0)
    {
      int err = ret < 0 ? errno : 0;

      close(sd);
      printf("rpsocktl: %s refused ret=%d errno=%d\n",
             ret < 0 && err == ECONNREFUSED ? "PASS" : "FAIL", ret, err);
      return ret < 0 && err == ECONNREFUSED ? EXIT_SUCCESS : EXIT_FAILURE;
    }

  if (ret < 0)
    {
      printf("rpsocktl: FAIL %s connect errno=%d\n", args->mode, errno);
      close(sd);
      return EXIT_FAILURE;
    }

  rpsocktl_rcvtimeo(sd, args->timeout);

  if (strcmp(args->mode, "eof") == 0)
    {
      ret = rpsocktl_client_eof(sd, args);
    }
  else if (strcmp(args->mode, "selecteof") == 0)
    {
      ret = rpsocktl_client_selecteof(sd, args);
    }
  else if (strcmp(args->mode, "readv") == 0)
    {
      ret = rpsocktl_client_readv(sd, args);
    }
  else if (strcmp(args->mode, "peer") == 0)
    {
      ret = rpsocktl_client_peer(sd, args);
    }
  else if (strcmp(args->mode, "isconn") == 0)
    {
      ret = rpsocktl_client_isconn(sd, args);
    }
  else if (strcmp(args->mode, "epipe") == 0)
    {
      ret = rpsocktl_client_epipe(sd);
    }
  else
    {
      printf("rpsocktl: FAIL %s unknown check\n", args->mode);
      ret = EXIT_FAILURE;
    }

  close(sd);
  return ret;
}

/* Send one datagram of len pattern bytes and receive its echo */

static int rpsocktl_dgram_echo(int sd, FAR uint8_t *buf, size_t bufsize,
                               size_t len, unsigned int seed,
                               FAR ssize_t *got)
{
  rpsocktl_fill(buf, 0, len, seed);
  if (send(sd, buf, len, 0) != (ssize_t)len)
    {
      *got = -1;
      return errno;
    }

  memset(buf, 0, bufsize);
  *got = recv(sd, buf, bufsize, 0);
  if (*got < 0)
    {
      return errno;
    }

  return *got == (ssize_t)len && rpsocktl_check(buf, 0, len, seed) == 0 ?
         0 : EBADMSG;
}

static int rpsocktl_client_bounds(int sd, FAR uint8_t *buf)
{
  ssize_t got = 0;
  size_t i;
  int err = 0;

  for (i = 0; i < sizeof(g_dgram_lens) / sizeof(g_dgram_lens[0]); i++)
    {
      err = rpsocktl_dgram_echo(sd, buf, 2048, g_dgram_lens[i], i, &got);
      if (err != 0)
        {
          break;
        }
    }

  printf("rpsocktl: %s bounds n=%zu len=%zu got=%zd errno=%d\n",
         err == 0 ? "PASS" : "FAIL", i,
         i < sizeof(g_dgram_lens) / sizeof(g_dgram_lens[0]) ?
         g_dgram_lens[i] : 0, got, err);
  return err == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int rpsocktl_client_trunc(int sd, FAR uint8_t *buf)
{
  struct msghdr msg;
  struct iovec iov;
  ssize_t ret1;
  ssize_t ret2 = -1;
  size_t bad;
  bool ok;
  int err = 0;

  rpsocktl_fill(buf, 0, 200, 1);
  if (send(sd, buf, 200, 0) != 200)
    {
      printf("rpsocktl: FAIL trunc send errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  memset(buf, 0, 200);
  memset(&msg, 0, sizeof(msg));
  iov.iov_base   = buf;
  iov.iov_len    = 50;
  msg.msg_iov    = &iov;
  msg.msg_iovlen = 1;
  ret1 = recvmsg(sd, &msg, 0);
  if (ret1 < 0)
    {
      err = errno;
    }

  bad = rpsocktl_check(buf, 0, 50, 1);

  /* The rest of the first message is gone: the next one comes whole */

  if (err == 0)
    {
      err = rpsocktl_dgram_echo(sd, buf, 100, 30, 2, &ret2);
    }

  ok = ret1 == 50 && (msg.msg_flags & MSG_TRUNC) != 0 && bad == 0 &&
       err == 0;
  printf("rpsocktl: %s trunc ret1=%zd flags=0x%x bad=%zu ret2=%zd "
         "errno=%d\n", ok ? "PASS" : "FAIL", ret1, msg.msg_flags, bad,
         ret2, err);
  return ok ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int rpsocktl_client_msgsize(int sd, FAR uint8_t *buf)
{
  ssize_t ret;
  ssize_t got = 0;
  int big;
  int err;

  /* A message the socket cannot send at once fails with EMSGSIZE; one
   * it sends arrives whole
   */

  rpsocktl_fill(buf, 0, RPSOCKTL_BIGMSG, 3);
  ret = send(sd, buf, RPSOCKTL_BIGMSG, 0);
  big = ret < 0 ? errno : 0;
  if (ret == RPSOCKTL_BIGMSG)
    {
      got = recv(sd, buf, RPSOCKTL_BIGMSG, 0);
      if (got != RPSOCKTL_BIGMSG ||
          rpsocktl_check(buf, 0, RPSOCKTL_BIGMSG, 3) != 0)
        {
          big = EBADMSG;
        }
    }
  else if (ret >= 0)
    {
      big = EBADMSG;
    }

  err = rpsocktl_dgram_echo(sd, buf, 2048, 10, 4, &got);
  printf("rpsocktl: %s msgsize ret=%zd big=%d small=%d\n",
         (big == 0 || big == EMSGSIZE) && err == 0 ? "PASS" : "FAIL",
         ret, big, err);
  return (big == 0 || big == EMSGSIZE) && err == 0 ?
         EXIT_SUCCESS : EXIT_FAILURE;
}

static int rpsocktl_dgram_client(FAR const struct rpsocktl_args_s *args)
{
  struct sockaddr_rpmsg addr;
  FAR uint8_t *buf;
  int ret;
  int sd;

  buf = malloc(RPSOCKTL_BIGMSG);
  sd = socket(AF_RPMSG, SOCK_DGRAM, 0);
  if (buf == NULL || sd < 0)
    {
      printf("rpsocktl: FAIL %s socket errno=%d\n", args->mode, errno);
      free(buf);
      return EXIT_FAILURE;
    }

  rpsocktl_addr(&addr, args->cpu, args->name);
  if (rpsocktl_connect(sd, &addr, args->timeout) < 0)
    {
      printf("rpsocktl: FAIL %s connect errno=%d\n", args->mode, errno);
      close(sd);
      free(buf);
      return EXIT_FAILURE;
    }

  rpsocktl_rcvtimeo(sd, args->timeout);

  if (strcmp(args->mode, "bounds") == 0)
    {
      ret = rpsocktl_client_bounds(sd, buf);
    }
  else if (strcmp(args->mode, "trunc") == 0)
    {
      ret = rpsocktl_client_trunc(sd, buf);
    }
  else if (strcmp(args->mode, "msgsize") == 0)
    {
      ret = rpsocktl_client_msgsize(sd, buf);
    }
  else
    {
      printf("rpsocktl: FAIL %s unknown check\n", args->mode);
      ret = EXIT_FAILURE;
    }

  close(sd);
  free(buf);
  return ret;
}

/* One call on a socket of this node, expected to return want */

static int rpsocktl_local(FAR const char *check)
{
  struct sockaddr_rpmsg addr;
  socklen_t len = sizeof(addr);
  int want = 0;
  int type = SOCK_STREAM;
  int ret = -1;
  int err = 0;
  int sd;
  char ch = 'x';

  if (strcmp(check, "dgramlisten") == 0)
    {
      type = SOCK_DGRAM;
    }

  sd = socket(AF_RPMSG, type | SOCK_NONBLOCK, 0);
  if (sd < 0)
    {
      printf("rpsocktl: FAIL %s socket errno=%d\n", check, errno);
      return EXIT_FAILURE;
    }

  rpsocktl_addr(&addr, "", check);

  if (strcmp(check, "dgramlisten") == 0)
    {
      want = EOPNOTSUPP;
      ret = bind(sd, (FAR struct sockaddr *)&addr, sizeof(addr));
      if (ret == 0)
        {
          ret = listen(sd, 1);
        }
    }
  else if (strcmp(check, "unboundlisten") == 0)
    {
      want = EDESTADDRREQ;
      ret = listen(sd, 1);
    }
  else if (strcmp(check, "listen0") == 0)
    {
      ret = bind(sd, (FAR struct sockaddr *)&addr, sizeof(addr));
      if (ret == 0)
        {
          ret = listen(sd, 0);
        }
    }
  else if (strcmp(check, "acceptnl") == 0)
    {
      want = EINVAL;
      ret = bind(sd, (FAR struct sockaddr *)&addr, sizeof(addr));
      if (ret == 0)
        {
          ret = accept(sd, NULL, NULL);
        }
    }
  else if (strcmp(check, "recvnc") == 0)
    {
      want = ENOTCONN;
      ret = recv(sd, &ch, 1, 0);
    }
  else if (strcmp(check, "sendnc") == 0)
    {
      want = ENOTCONN;
      ret = send(sd, &ch, 1, MSG_NOSIGNAL);
    }
  else if (strcmp(check, "peernc") == 0)
    {
      want = ENOTCONN;
      ret = getpeername(sd, (FAR struct sockaddr *)&addr, &len);
    }
  else if (strcmp(check, "shutnc") == 0)
    {
      want = ENOTCONN;
      ret = shutdown(sd, SHUT_RDWR);
    }
  else
    {
      printf("rpsocktl: FAIL %s unknown check\n", check);
      close(sd);
      return EXIT_FAILURE;
    }

  err = ret < 0 ? errno : 0;
  close(sd);

  ret = want == 0 ? ret == 0 : ret < 0 && err == want;
  printf("rpsocktl: %s %s errno=%d want=%d\n", ret ? "PASS" : "FAIL",
         check, err, want);
  return ret ? EXIT_SUCCESS : EXIT_FAILURE;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  struct rpsocktl_args_s args;
  int opt;

  memset(&args, 0, sizeof(args));
  args.mode    = "echo";
  args.count   = 1000;
  args.timeout = RPSOCKTL_TIMEOUT;

  while ((opt = getopt(argc, argv, "sc:e:um:n:t:")) != ERROR)
    {
      switch (opt)
        {
          case 's':
            args.server = true;
            break;
          case 'c':
            args.cpu = optarg;
            break;
          case 'e':
            args.local = true;
            args.mode = optarg;
            break;
          case 'u':
            args.dgram = true;
            break;
          case 'm':
            args.mode = optarg;
            break;
          case 'n':
            args.count = strtoul(optarg, NULL, 0);
            break;
          case 't':
            args.timeout = atoi(optarg);
            break;
          default:
            rpsocktl_usage(argv[0]);
            return EXIT_FAILURE;
        }
    }

  if (args.local)
    {
      return rpsocktl_local(args.mode);
    }

  if (optind != argc - 1 || args.timeout <= 0 ||
      (args.server && args.dgram) != (args.server && args.cpu != NULL) ||
      (!args.server && args.cpu == NULL))
    {
      rpsocktl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  args.name = argv[optind];

  if (args.server)
    {
      return args.dgram ? rpsocktl_dgram_server(&args) :
                          rpsocktl_stream_server(&args);
    }

  return args.dgram ? rpsocktl_dgram_client(&args) :
                      rpsocktl_stream_client(&args);
}
