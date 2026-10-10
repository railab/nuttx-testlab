/****************************************************************************
 * apps/nettl/nettl_main.c
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

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <poll.h>
#include <time.h>
#include <unistd.h>
#include <errno.h>

#include <sys/socket.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define NETTL_PORT_DEFAULT     5001
#define NETTL_TCP_BYTES        65536
#define NETTL_UDP_COUNT        100
#define NETTL_UDP_LEN          512
#define NETTL_TIMEOUT          10
#define NETTL_UDP_RETRIES      3
#define NETTL_UDP_STALE_MAX    16
#define NETTL_UDP_END          0xffffffffu
#define NETTL_UDP_COUNT_MAX    0xfffffffeu
#define NETTL_UDP_HDR          4
#define NETTL_BUFSIZE          CONFIG_TESTLAB_NETTL_BUFSIZE
#define NETTL_UDP_LEN_DEFAULT  (NETTL_UDP_LEN < NETTL_BUFSIZE ? \
                                 NETTL_UDP_LEN : NETTL_BUFSIZE)
#define NETTL_REUSE_MAX        4
#define NETTL_CONN_MAX         8

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct nettl_args_s
{
  bool        server;
  bool        udp;
  bool        write_only;
  bool        ipv6;
  FAR char   *addr;
  FAR char   *group;
  uint16_t    port;
  size_t      count;
  size_t      len;
  size_t      reuse;
  size_t      conns;
  int         timeout;
  int         delay;
  int         keepidle;
  bool        keeppoll;
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

static uint8_t g_txbuf[NETTL_BUFSIZE + NETTL_UDP_HDR];
static uint8_t g_rxbuf[NETTL_BUFSIZE + NETTL_UDP_HDR];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static uint8_t nettl_pattern(size_t offset)
{
  return (uint8_t)(offset * 31 + 7);
}

static void nettl_fill(FAR uint8_t *buf, size_t offset, size_t len)
{
  size_t i;

  for (i = 0; i < len; i++)
    {
      buf[i] = nettl_pattern(offset + i);
    }
}

static size_t nettl_check(FAR const uint8_t *buf, size_t offset,
                          size_t len)
{
  size_t err = 0;
  size_t i;

  for (i = 0; i < len; i++)
    {
      if (buf[i] != nettl_pattern(offset + i))
        {
          err++;
        }
    }

  return err;
}

static int nettl_sendall(int sd, FAR const uint8_t *buf, size_t len)
{
  size_t sent = 0;
  ssize_t ret;

  while (sent < len)
    {
      ret = send(sd, buf + sent, len - sent, 0);
      if (ret < 0)
        {
          if (errno == EINTR)
            {
              continue;
            }

          return -1;
        }

      sent += (size_t)ret;
    }

  return 0;
}

static bool nettl_parse_uint(FAR const char *arg, unsigned long minval,
                             unsigned long maxval, FAR unsigned long *val)
{
  FAR char *end;
  unsigned long parsed;

  if (arg == NULL || arg[0] == '\0' || arg[0] == '-')
    {
      return false;
    }

  errno = 0;
  parsed = strtoul(arg, &end, 10);
  if (errno != 0 || *end != '\0' || parsed < minval || parsed > maxval)
    {
      return false;
    }

  *val = parsed;
  return true;
}

static void nettl_settimeout(int sd, int timeout)
{
  struct timeval tv;

  tv.tv_sec  = timeout;
  tv.tv_usec = 0;
  setsockopt(sd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
}

/* Fill the socket address for the server (any address) or the client
 * (args->addr) and return its length, 0 on a bad address.
 */

static socklen_t nettl_sockaddr(FAR const struct nettl_args_s *args,
                                FAR struct sockaddr_storage *ss)
{
  FAR struct sockaddr_in *sa = (FAR struct sockaddr_in *)ss;
#ifdef CONFIG_NET_IPv6
  FAR struct sockaddr_in6 *sa6 = (FAR struct sockaddr_in6 *)ss;
#endif

  memset(ss, 0, sizeof(*ss));

#ifdef CONFIG_NET_IPv6
  if (args->ipv6)
    {
      sa6->sin6_family = AF_INET6;
      sa6->sin6_port   = htons(args->port);
      if (!args->server &&
          inet_pton(AF_INET6, args->addr, &sa6->sin6_addr) != 1)
        {
          return 0;
        }

      return sizeof(*sa6);
    }
#endif

  sa->sin_family = AF_INET;
  sa->sin_port   = htons(args->port);
  if (args->server)
    {
      sa->sin_addr.s_addr = htonl(INADDR_ANY);
    }
  else if (inet_pton(AF_INET, args->addr, &sa->sin_addr) != 1)
    {
      return 0;
    }

  return sizeof(*sa);
}

/* Join the multicast group args->group on the default device */

static int nettl_join(int sd, FAR const struct nettl_args_s *args)
{
#ifdef CONFIG_NET_MLD
  if (args->ipv6)
    {
      struct ipv6_mreq mreq6;

      memset(&mreq6, 0, sizeof(mreq6));
      if (inet_pton(AF_INET6, args->group, &mreq6.ipv6mr_multiaddr) != 1)
        {
          errno = EINVAL;
          return -1;
        }

      return setsockopt(sd, IPPROTO_IPV6, IPV6_JOIN_GROUP, &mreq6,
                        sizeof(mreq6));
    }
#endif

#ifdef CONFIG_NET_IGMP
  if (!args->ipv6)
    {
      struct ip_mreq mreq;

      memset(&mreq, 0, sizeof(mreq));
      if (inet_pton(AF_INET, args->group, &mreq.imr_multiaddr) != 1)
        {
          errno = EINVAL;
          return -1;
        }

      mreq.imr_interface.s_addr = htonl(INADDR_ANY);
      return setsockopt(sd, IPPROTO_IP, IP_ADD_MEMBERSHIP, &mreq,
                        sizeof(mreq));
    }
#endif

  errno = ENOSYS;
  return -1;
}

static int nettl_socket(FAR const struct nettl_args_s *args,
                        FAR struct sockaddr_storage *ss,
                        FAR socklen_t *salen)
{
  int sd;

  *salen = nettl_sockaddr(args, ss);
  if (*salen == 0)
    {
      printf("nettl: bad address %s\n", args->addr);
      return -1;
    }

  sd = socket(ss->ss_family, args->udp ? SOCK_DGRAM : SOCK_STREAM, 0);
  if (sd < 0)
    {
      printf("nettl: socket failed %d\n", errno);
      return -1;
    }

  if (args->server)
    {
      int on = 1;

      setsockopt(sd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on));
      if (bind(sd, (FAR struct sockaddr *)ss, *salen) < 0)
        {
          printf("nettl: bind failed %d\n", errno);
          close(sd);
          return -1;
        }

      if (args->group != NULL && nettl_join(sd, args) < 0)
        {
          printf("nettl: join %s failed %d\n", args->group, errno);
          close(sd);
          return -1;
        }
    }

  nettl_settimeout(sd, args->timeout);
  return sd;
}

static int nettl_tcp_server_write(int cd,
                                   FAR const struct nettl_args_s *args)
{
  size_t tx  = 0;
  size_t err = 0;
  size_t chunk;

  while (tx < args->count)
    {
      chunk = args->count - tx;
      if (chunk > NETTL_BUFSIZE)
        {
          chunk = NETTL_BUFSIZE;
        }

      nettl_fill(g_txbuf, tx, chunk);
      if (nettl_sendall(cd, g_txbuf, chunk) < 0)
        {
          err++;
          break;
        }

      tx += chunk;
    }

  close(cd);
  printf("nettl: %s rx=0 err=%zu\n", err == 0 ? "PASS" : "FAIL", err);
  return err == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int nettl_tcp_server(FAR const struct nettl_args_s *args)
{
  struct sockaddr_storage sa;
  socklen_t salen;
  size_t rx  = 0;
  size_t err = 0;
  ssize_t ret;
  int sd;
  int cd;

  sd = nettl_socket(args, &sa, &salen);
  if (sd < 0 || listen(sd, 1) < 0)
    {
      printf("nettl: FAIL rx=0 err=1\n");
      return EXIT_FAILURE;
    }

  printf("nettl: listening tcp %u\n", args->port);

  if (args->delay > 0)
    {
      sleep(args->delay);
    }

  cd = accept(sd, NULL, NULL);
  close(sd);
  if (cd < 0)
    {
      printf("nettl: accept failed %d\n", errno);
      printf("nettl: FAIL rx=0 err=1\n");
      return EXIT_FAILURE;
    }

  nettl_settimeout(cd, args->timeout);

  if (args->write_only)
    {
      return nettl_tcp_server_write(cd, args);
    }

  while ((ret = recv(cd, g_rxbuf, NETTL_BUFSIZE, 0)) > 0)
    {
      err += nettl_check(g_rxbuf, rx, ret);
      rx  += ret;
      if (nettl_sendall(cd, g_rxbuf, (size_t)ret) < 0)
        {
          err++;
          break;
        }
    }

  if (ret < 0)
    {
      printf("nettl: recv failed %d\n", errno);
      err++;
    }

  close(cd);
  printf("nettl: %s rx=%zu err=%zu\n", err == 0 ? "PASS" : "FAIL", rx, err);
  return err == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}

/* Echo args->conns TCP connections at once, verifying each stream */

static int nettl_tcp_server_multi(FAR const struct nettl_args_s *args)
{
  struct sockaddr_storage sa;
  struct pollfd fds[NETTL_CONN_MAX + 1];
  size_t rx[NETTL_CONN_MAX];
  size_t accepted = 0;
  size_t done     = 0;
  size_t total    = 0;
  size_t err      = 0;
  socklen_t salen;
  ssize_t ret;
  size_t i;
  int sd;

  sd = nettl_socket(args, &sa, &salen);
  if (sd < 0 || listen(sd, args->conns) < 0)
    {
      printf("nettl: FAIL rx=0 err=1\n");
      return EXIT_FAILURE;
    }

  printf("nettl: listening tcp %u\n", args->port);

  fds[0].fd     = sd;
  fds[0].events = POLLIN;
  for (i = 0; i < args->conns; i++)
    {
      fds[i + 1].fd     = -1;
      fds[i + 1].events = POLLIN;
      rx[i]             = 0;
    }

  while (done < args->conns)
    {
      if (poll(fds, args->conns + 1, args->timeout * 1000) <= 0)
        {
          printf("nettl: poll timeout or error %d\n", errno);
          err++;
          break;
        }

      if ((fds[0].revents & POLLIN) != 0)
        {
          int cd = accept(sd, NULL, NULL);

          if (cd < 0)
            {
              printf("nettl: accept failed %d\n", errno);
              err++;
              break;
            }

          fds[++accepted].fd = cd;
          if (accepted == args->conns)
            {
              fds[0].fd = -1;  /* Stop polling the listener */
            }
        }

      for (i = 1; i <= accepted; i++)
        {
          if (fds[i].fd < 0 ||
              (fds[i].revents & (POLLIN | POLLHUP | POLLERR)) == 0)
            {
              continue;
            }

          ret = recv(fds[i].fd, g_rxbuf, NETTL_BUFSIZE, 0);
          if (ret > 0)
            {
              err      += nettl_check(g_rxbuf, rx[i - 1], ret);
              rx[i - 1] += ret;
              total    += ret;
              if (nettl_sendall(fds[i].fd, g_rxbuf, (size_t)ret) < 0)
                {
                  err++;
                }

              continue;
            }

          if (ret < 0)
            {
              printf("nettl: recv failed %d\n", errno);
              err++;
            }

          close(fds[i].fd);
          fds[i].fd = -1;
          done++;
        }
    }

  for (i = 1; i <= accepted; i++)
    {
      if (fds[i].fd >= 0)
        {
          close(fds[i].fd);
        }
    }

  close(sd);
  err += args->conns - done;
  printf("nettl: %s rx=%zu err=%zu\n", err == 0 ? "PASS" : "FAIL",
         total, err);
  return err == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}

/* Enable TCP keep-alive (idle seconds, 1 s interval, 3 probes) and wait
 * idle in recv() (or in poll() with -P) for args->timeout seconds.  A
 * timeout means the peer is alive; any other result fails the client.
 */

static int nettl_keepalive_idle(int sd, FAR const struct nettl_args_s *args)
{
#ifdef CONFIG_NET_TCP_KEEPALIVE
  struct pollfd pfd;
  struct timeval idle;
  struct timeval intvl;
  socklen_t len;
  int soerr;
  int on  = 1;
  int cnt = 3;
  ssize_t ret;

  /* NuttX takes the keep-alive times as struct timeval (an int argument
   * is in deciseconds).
   */

  idle.tv_sec   = args->keepidle;
  idle.tv_usec  = 0;
  intvl.tv_sec  = 1;
  intvl.tv_usec = 0;

  if (setsockopt(sd, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof(on)) < 0 ||
      setsockopt(sd, IPPROTO_TCP, TCP_KEEPIDLE, &idle,
                 sizeof(idle)) < 0 ||
      setsockopt(sd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl,
                 sizeof(intvl)) < 0 ||
      setsockopt(sd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt)) < 0)
    {
      printf("nettl: keepalive setsockopt failed %d\n", errno);
      return -1;
    }

  if (args->keeppoll)
    {
      pfd.fd      = sd;
      pfd.events  = POLLIN;
      pfd.revents = 0;

      ret = poll(&pfd, 1, args->timeout * 1000);
      if (ret == 0)
        {
          printf("nettl: idle ok\n");
          return 0;
        }

      soerr = 0;
      len   = sizeof(soerr);
      if (getsockopt(sd, SOL_SOCKET, SO_ERROR, &soerr, &len) < 0)
        {
          soerr = -errno;
        }

      printf("nettl: poll revents 0x%x so_error %d\n",
             ret < 0 ? 0 : pfd.revents, soerr);
      return -1;
    }

  ret = recv(sd, g_rxbuf, NETTL_BUFSIZE, 0);
  if (ret < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
    {
      printf("nettl: idle ok\n");
      return 0;
    }

  printf("nettl: recv failed %d\n", ret < 0 ? errno : 0);
  return -1;
#else
  printf("nettl: no keepalive support\n");
  return -1;
#endif
}

static int nettl_tcp_client(FAR const struct nettl_args_s *args)
{
  struct sockaddr_storage sa;
  socklen_t salen;
  size_t tx  = 0;
  size_t rx  = 0;
  size_t err = 0;
  ssize_t ret;
  size_t chunk;
  size_t got;
  int sd;

  sd = nettl_socket(args, &sa, &salen);
  if (sd < 0 ||
      connect(sd, (FAR struct sockaddr *)&sa, salen) < 0)
    {
      printf("nettl: connect failed %d\n", errno);
      printf("nettl: FAIL tx=0 rx=0 err=1\n");
      return EXIT_FAILURE;
    }

  if (args->keepidle > 0 && nettl_keepalive_idle(sd, args) < 0)
    {
      err++;
    }

  while (tx < args->count && err == 0)
    {
      chunk = args->count - tx;
      if (chunk > NETTL_BUFSIZE)
        {
          chunk = NETTL_BUFSIZE;
        }

      nettl_fill(g_txbuf, tx, chunk);
      if (nettl_sendall(sd, g_txbuf, chunk) < 0)
        {
          printf("nettl: send failed %d\n", errno);
          err++;
          break;
        }

      tx += chunk;

      /* Lock-step echo: read back exactly what was sent */

      for (got = 0; got < chunk; got += ret)
        {
          ret = recv(sd, g_rxbuf + got, chunk - got, 0);
          if (ret <= 0)
            {
              printf("nettl: recv failed %d\n", ret < 0 ? errno : 0);
              err++;
              break;
            }
        }

      err += nettl_check(g_rxbuf, rx, got);
      rx  += got;
    }

  close(sd);
  printf("nettl: %s tx=%zu rx=%zu err=%zu\n",
         (err == 0 && rx == args->count) ? "PASS" : "FAIL", tx, rx, err);
  return (err == 0 && rx == args->count) ? EXIT_SUCCESS : EXIT_FAILURE;
}

static void nettl_put32(FAR uint8_t *buf, uint32_t val)
{
  buf[0] = (uint8_t)(val >> 24);
  buf[1] = (uint8_t)(val >> 16);
  buf[2] = (uint8_t)(val >> 8);
  buf[3] = (uint8_t)val;
}

static uint32_t nettl_get32(FAR const uint8_t *buf)
{
  return ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
         ((uint32_t)buf[2] << 8) | (uint32_t)buf[3];
}

static int nettl_udp_server(FAR const struct nettl_args_s *args)
{
  struct sockaddr_storage sa;
  struct sockaddr_storage from;
  socklen_t salen;
  socklen_t fromlen;
  size_t rx  = 0;
  size_t err = 0;
  ssize_t ret;
  uint32_t seq;
  int sd;

  sd = nettl_socket(args, &sa, &salen);
  if (sd < 0)
    {
      printf("nettl: FAIL rx=0 err=1\n");
      return EXIT_FAILURE;
    }

  printf("nettl: listening udp %u\n", args->port);

  for (; ; )
    {
      /* recvfrom() never stores more than sizeof(g_rxbuf) bytes, i.e.
       * NETTL_BUFSIZE + NETTL_UDP_HDR, so a datagram longer than that
       * cannot be observed here; no MSG_TRUNC-style check is needed.
       */

      fromlen = sizeof(from);
      ret = recvfrom(sd, g_rxbuf, sizeof(g_rxbuf), 0,
                     (FAR struct sockaddr *)&from, &fromlen);
      if (ret < NETTL_UDP_HDR)
        {
          printf("nettl: recvfrom failed %d\n", ret < 0 ? errno : 0);
          err++;
          break;
        }

      seq = nettl_get32(g_rxbuf);
      if (seq == NETTL_UDP_END)
        {
          break;
        }

      err += nettl_check(g_rxbuf + NETTL_UDP_HDR,
                         (size_t)seq * (ret - NETTL_UDP_HDR),
                         ret - NETTL_UDP_HDR);
      rx++;
      sendto(sd, g_rxbuf, ret, 0, (FAR struct sockaddr *)&from, fromlen);
    }

  close(sd);
  printf("nettl: %s rx=%zu err=%zu\n", err == 0 ? "PASS" : "FAIL", rx, err);
  return err == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int nettl_elapsed_ms(FAR const struct timespec *start)
{
  struct timespec now;

  clock_gettime(CLOCK_MONOTONIC, &now);
  return (int)((now.tv_sec - start->tv_sec) * 1000 +
               (now.tv_nsec - start->tv_nsec) / 1000000);
}

static int nettl_udp_server_reuse(FAR const struct nettl_args_s *args)
{
  struct sockaddr_storage sa;
  socklen_t salen;
  struct pollfd fds[NETTL_REUSE_MAX];
  size_t got[NETTL_REUSE_MAX];
  int sds[NETTL_REUSE_MAX];
  struct timespec start;
  size_t expected;
  size_t done = 0;
  size_t rx   = 0;
  size_t err  = 0;
  size_t i;

  memset(got, 0, sizeof(got));

  for (i = 0; i < args->reuse; i++)
    {
      sds[i] = nettl_socket(args, &sa, &salen);
      if (sds[i] < 0)
        {
          while (i-- > 0)
            {
              close(sds[i]);
            }

          printf("nettl: FAIL rx=0 err=1\n");
          return EXIT_FAILURE;
        }
    }

  printf("nettl: listening udp %u\n", args->port);
  clock_gettime(CLOCK_MONOTONIC, &start);

  while (done < args->reuse)
    {
      int remain_ms = args->timeout * 1000 - nettl_elapsed_ms(&start);
      int nready;

      if (remain_ms <= 0)
        {
          break;
        }

      for (i = 0; i < args->reuse; i++)
        {
          fds[i].fd     = sds[i];
          fds[i].events = POLLIN;
        }

      nready = poll(fds, args->reuse, remain_ms);
      if (nready <= 0)
        {
          break;
        }

      for (i = 0; i < args->reuse; i++)
        {
          ssize_t ret;
          uint32_t seq;

          if (!(fds[i].revents & POLLIN) || got[i] >= args->count)
            {
              continue;
            }

          ret = recv(sds[i], g_rxbuf, sizeof(g_rxbuf), 0);
          if (ret < NETTL_UDP_HDR)
            {
              err++;
              continue;
            }

          seq = nettl_get32(g_rxbuf);
          if (seq == NETTL_UDP_END)
            {
              continue;
            }

          if ((size_t)ret != args->len + NETTL_UDP_HDR)
            {
              err++;
            }
          else
            {
              err += nettl_check(g_rxbuf + NETTL_UDP_HDR,
                                 (size_t)seq * args->len, args->len);
            }

          rx++;
          if (++got[i] == args->count)
            {
              done++;
            }
        }
    }

  for (i = 0; i < args->reuse; i++)
    {
      close(sds[i]);
    }

  expected = args->reuse * args->count;
  err += expected > rx ? expected - rx : 0;

  printf("nettl: %s rx=%zu err=%zu\n", err == 0 ? "PASS" : "FAIL", rx, err);
  return err == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}

static int nettl_udp_client(FAR const struct nettl_args_s *args)
{
  struct sockaddr_storage sa;
  socklen_t salen;
  size_t rx   = 0;
  size_t lost = 0;
  size_t err  = 0;
  ssize_t ret;
  size_t seq;
  int retry;
  int stale;
  bool matched;
  bool badlen;
  int sd;

  sd = nettl_socket(args, &sa, &salen);
  if (sd < 0)
    {
      printf("nettl: FAIL tx=0 rx=0 lost=0 err=1\n");
      return EXIT_FAILURE;
    }

  for (seq = 0; seq < args->count; seq++)
    {
      nettl_put32(g_txbuf, seq);
      nettl_fill(g_txbuf + NETTL_UDP_HDR, seq * args->len, args->len);

      matched = false;
      badlen  = false;

      for (retry = 0; retry < NETTL_UDP_RETRIES && !matched; retry++)
        {
          if (sendto(sd, g_txbuf, args->len + NETTL_UDP_HDR, 0,
                     (FAR struct sockaddr *)&sa, salen) < 0)
            {
              continue;  /* send failure consumes this retry */
            }

          /* Drain late duplicates of earlier sequence numbers without
           * consuming a retry; only a timeout/recv error or exhausting
           * the stale-datagram bound below ends this attempt.
           */

          for (stale = 0; stale < NETTL_UDP_STALE_MAX; stale++)
            {
              ret = recv(sd, g_rxbuf, sizeof(g_rxbuf), 0);
              if (ret < 0)
                {
                  break;  /* timeout/error consumes this retry */
                }

              if ((size_t)ret >= NETTL_UDP_HDR &&
                  nettl_get32(g_rxbuf) == seq)
                {
                  matched = true;
                  badlen  = (ret != (ssize_t)(args->len + NETTL_UDP_HDR));
                  break;
                }

              /* Stale datagram for a different seq: discard and retry
               * the recv() without resending or using up a retry.
               */
            }
        }

      if (!matched)
        {
          lost++;
          continue;
        }

      if (badlen)
        {
          err++;
        }
      else
        {
          err += nettl_check(g_rxbuf + NETTL_UDP_HDR, seq * args->len,
                             args->len);
        }

      rx++;
    }

  nettl_put32(g_txbuf, NETTL_UDP_END);
  for (retry = 0; retry < NETTL_UDP_RETRIES; retry++)
    {
      sendto(sd, g_txbuf, NETTL_UDP_HDR, 0,
             (FAR struct sockaddr *)&sa, salen);
    }

  close(sd);
  printf("nettl: %s tx=%zu rx=%zu lost=%zu err=%zu\n",
         (lost == 0 && err == 0) ? "PASS" : "FAIL",
         args->count, rx, lost, err);
  return (lost == 0 && err == 0) ? EXIT_SUCCESS : EXIT_FAILURE;
}

static void nettl_usage(FAR const char *progname)
{
  printf("Usage: %s -s [-6] [-u] [-g group] [-p port] [-n count] "
         "[-l len] [-t sec] [-w] [-D sec] [-L n] [-C n]\n", progname);
  printf("       %s -c addr [-6] [-u] [-p port] [-n count] [-l len] "
         "[-t sec] [-k idle [-P]]\n", progname);
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  struct nettl_args_s args;
  int opt;

  memset(&args, 0, sizeof(args));
  args.port    = NETTL_PORT_DEFAULT;
  args.len     = NETTL_UDP_LEN_DEFAULT;
  args.timeout = NETTL_TIMEOUT;

  while ((opt = getopt(argc, argv, "sc:6ug:p:n:l:t:wD:L:C:k:P")) != ERROR)
    {
      unsigned long val;

      switch (opt)
        {
          case 's':
            args.server = true;
            break;
          case 'c':
            args.addr = optarg;
            break;
#ifdef CONFIG_NET_IPv6
          case '6':
            args.ipv6 = true;
            break;
#endif
          case 'u':
            args.udp = true;
            break;
          case 'g':
            args.group = optarg;
            break;
          case 'w':
            args.write_only = true;
            break;
          case 'D':
            if (!nettl_parse_uint(optarg, 0, INT_MAX, &val))
              {
                nettl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.delay = (int)val;
            break;

          case 'k':
            if (!nettl_parse_uint(optarg, 1, INT_MAX, &val))
              {
                nettl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.keepidle = (int)val;
            break;
          case 'P':
            args.keeppoll = true;
            break;
          case 'L':
            if (!nettl_parse_uint(optarg, 1, NETTL_REUSE_MAX, &val))
              {
                nettl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.reuse = (size_t)val;
            break;
          case 'C':
            if (!nettl_parse_uint(optarg, 1, NETTL_CONN_MAX, &val))
              {
                nettl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.conns = (size_t)val;
            break;
          case 'p':
            if (!nettl_parse_uint(optarg, 1, 65535, &val))
              {
                nettl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.port = (uint16_t)val;
            break;
          case 'n':
            if (!nettl_parse_uint(optarg, 1, ULONG_MAX, &val))
              {
                nettl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.count = (size_t)val;
            break;
          case 'l':
            if (!nettl_parse_uint(optarg, 1, NETTL_BUFSIZE, &val))
              {
                nettl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.len = (size_t)val;
            break;
          case 't':
            if (!nettl_parse_uint(optarg, 1, INT_MAX, &val))
              {
                nettl_usage(argv[0]);
                return EXIT_FAILURE;
              }

            args.timeout = (int)val;
            break;
          default:
            nettl_usage(argv[0]);
            return EXIT_FAILURE;
        }
    }

  if (args.server == (args.addr != NULL))
    {
      nettl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  if (args.udp && (args.len == 0 || args.len > NETTL_BUFSIZE))
    {
      nettl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  /* UDP sequence numbers are uint32_t and 0xffffffff is reserved as the
   * end-of-stream marker, so the datagram count must leave room for it.
   */

  if (args.udp && args.count > NETTL_UDP_COUNT_MAX)
    {
      nettl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  /* -w and -C are TCP server options; -L and -g are UDP server options. */

  if (((args.write_only || args.conns > 0) && (!args.server || args.udp)) ||
      ((args.reuse > 0 || args.group != NULL) &&
       (!args.server || !args.udp)))
    {
      nettl_usage(argv[0]);
      return EXIT_FAILURE;
    }

  if (args.count == 0)
    {
      args.count = args.udp ? NETTL_UDP_COUNT : NETTL_TCP_BYTES;
    }

  if (args.server)
    {
      if (args.udp)
        {
          return args.reuse > 0 ? nettl_udp_server_reuse(&args) :
                                   nettl_udp_server(&args);
        }

      return args.conns > 1 ? nettl_tcp_server_multi(&args) :
                              nettl_tcp_server(&args);
    }

  return args.udp ? nettl_udp_client(&args) : nettl_tcp_client(&args);
}
