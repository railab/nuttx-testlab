/****************************************************************************
 * apps/tlstl/tlstl_main.c
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

#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <unistd.h>
#include <errno.h>

#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/pk.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>
#ifdef CONFIG_MBEDTLS_PSA_CRYPTO_C
#  include <psa/crypto.h>
#endif

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define TLSTL_PORT_DEFAULT  5101
#define TLSTL_BYTES         65536
#define TLSTL_TIMEOUT       10
#define TLSTL_THREADS_MAX   8
#define TLSTL_BUFSIZE       CONFIG_TESTLAB_TLSTL_BUFSIZE
#define TLSTL_FILE_MAX      16384

/* How the peer ended the TLS stream */

#define TLSTL_END_NONE      0  /* Session ended before reading the end */
#define TLSTL_END_NOTIFY    1  /* close_notify alert */
#define TLSTL_END_EOF       2  /* TCP FIN without close_notify */
#define TLSTL_END_RESET     3  /* Connection reset */
#define TLSTL_END_TIMEOUT   4  /* No data within the timeout */
#define TLSTL_END_ERROR     5  /* Other TLS or socket error */

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct tlstl_args_s
{
  bool        server;
  bool        recvonly;
  FAR char   *addr;
  FAR char   *host;
  FAR char   *ca;
  FAR char   *cert;
  FAR char   *key;
  uint16_t    port;
  int         version;
  size_t      count;
  int         sessions;
  int         threads;
  int         timeout;
};

struct tlstl_sess_s
{
  int          fd;
  size_t       tx;
  size_t       rx;
  size_t       bad;
  int          ret;
  int          sockerr;
  uint32_t     verify;
  int          end;
  FAR const char *ver;
  bool         ok;
};

struct tlstl_s
{
  struct tlstl_args_s      args;
  mbedtls_entropy_context  entropy;
  mbedtls_ctr_drbg_context drbg;
  mbedtls_ssl_config       conf;
  mbedtls_x509_crt         ca;
  mbedtls_x509_crt         crt;
  mbedtls_pk_context       pk;
  int                      lfd;
  pthread_mutex_t          acceptlock;
  pthread_mutex_t          lock;
  int                      next;
  int                      nok;
  size_t                   tx;
  size_t                   rx;
  struct tlstl_sess_s      result;
  bool                     failed;
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

static FAR const char *g_endname[] =
{
  "none", "notify", "eof", "reset", "timeout", "error"
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static uint8_t tlstl_pattern(size_t offset)
{
  return (uint8_t)(offset * 31 + 7);
}

static void tlstl_fill(FAR uint8_t *buf, size_t offset, size_t len)
{
  size_t i;

  for (i = 0; i < len; i++)
    {
      buf[i] = tlstl_pattern(offset + i);
    }
}

static size_t tlstl_check(FAR const uint8_t *buf, size_t offset,
                          size_t len)
{
  size_t bad = 0;
  size_t i;

  for (i = 0; i < len; i++)
    {
      if (buf[i] != tlstl_pattern(offset + i))
        {
          bad++;
        }
    }

  return bad;
}

/* Socket callbacks: the errno of a failed call is kept in the session. */

static int tlstl_send(FAR void *ctx, FAR const unsigned char *buf,
                      size_t len)
{
  FAR struct tlstl_sess_s *s = ctx;
  ssize_t n;

  do
    {
      n = send(s->fd, buf, len, MSG_NOSIGNAL);
    }
  while (n < 0 && errno == EINTR);

  if (n >= 0)
    {
      return (int)n;
    }

  s->sockerr = errno;
  if (errno == EAGAIN || errno == EWOULDBLOCK)
    {
      return MBEDTLS_ERR_SSL_WANT_WRITE;
    }

  if (errno == ECONNRESET || errno == EPIPE)
    {
      return MBEDTLS_ERR_NET_CONN_RESET;
    }

  return MBEDTLS_ERR_NET_SEND_FAILED;
}

static int tlstl_recv(FAR void *ctx, FAR unsigned char *buf, size_t len,
                      uint32_t ms)
{
  FAR struct tlstl_sess_s *s = ctx;
  struct pollfd pfd;
  ssize_t n;
  int ret;

  pfd.fd = s->fd;
  pfd.events = POLLIN;
  pfd.revents = 0;

  do
    {
      ret = poll(&pfd, 1, ms == 0 ? -1 : (int)ms);
    }
  while (ret < 0 && errno == EINTR);

  if (ret == 0)
    {
      return MBEDTLS_ERR_SSL_TIMEOUT;
    }

  if (ret < 0)
    {
      s->sockerr = errno;
      return MBEDTLS_ERR_NET_RECV_FAILED;
    }

  do
    {
      n = recv(s->fd, buf, len, 0);
    }
  while (n < 0 && errno == EINTR);

  if (n >= 0)
    {
      return (int)n;
    }

  s->sockerr = errno;
  if (errno == EAGAIN || errno == EWOULDBLOCK)
    {
      return MBEDTLS_ERR_SSL_WANT_READ;
    }

  if (errno == ECONNRESET)
    {
      return MBEDTLS_ERR_NET_CONN_RESET;
    }

  return MBEDTLS_ERR_NET_RECV_FAILED;
}

static int tlstl_endcode(int ret)
{
  switch (ret)
    {
      case MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY:
        return TLSTL_END_NOTIFY;

      case 0:
      case MBEDTLS_ERR_SSL_CONN_EOF:
        return TLSTL_END_EOF;

      case MBEDTLS_ERR_NET_CONN_RESET:
        return TLSTL_END_RESET;

      case MBEDTLS_ERR_SSL_TIMEOUT:
        return TLSTL_END_TIMEOUT;

      default:
        return TLSTL_END_ERROR;
    }
}

/* mbedtls_ssl_read() without the retry codes; returns > 0 bytes read or
 * the stream end condition (0 or a negative mbedtls error).
 */

static int tlstl_read(FAR mbedtls_ssl_context *ssl, FAR uint8_t *buf,
                      size_t len)
{
  int ret;

  do
    {
      ret = mbedtls_ssl_read(ssl, buf, len);
    }
  while (ret == MBEDTLS_ERR_SSL_WANT_READ ||
         ret == MBEDTLS_ERR_SSL_WANT_WRITE ||
         ret == MBEDTLS_ERR_SSL_RECEIVED_NEW_SESSION_TICKET);

  return ret;
}

static int tlstl_write(FAR mbedtls_ssl_context *ssl,
                       FAR const uint8_t *buf, size_t len)
{
  size_t done = 0;
  int ret;

  while (done < len)
    {
      ret = mbedtls_ssl_write(ssl, buf + done, len - done);
      if (ret == MBEDTLS_ERR_SSL_WANT_READ ||
          ret == MBEDTLS_ERR_SSL_WANT_WRITE)
        {
          continue;
        }

      if (ret < 0)
        {
          return ret;
        }

      done += ret;
    }

  return 0;
}

/* Read until the peer ends the stream; data must not follow. */

static void tlstl_drain(FAR mbedtls_ssl_context *ssl,
                        FAR struct tlstl_sess_s *s, FAR uint8_t *buf)
{
  int ret;

  for (; ; )
    {
      ret = tlstl_read(ssl, buf, TLSTL_BUFSIZE);
      if (ret <= 0)
        {
          break;
        }

      s->bad += ret;
    }

  s->end = tlstl_endcode(ret);
  if (s->end != TLSTL_END_NOTIFY && s->end != TLSTL_END_EOF)
    {
      s->ret = ret;
    }
}

/* Client echo: send the pattern chunk by chunk, read each echo back. */

static int tlstl_client_echo(FAR struct tlstl_s *t,
                             FAR mbedtls_ssl_context *ssl,
                             FAR struct tlstl_sess_s *s,
                             FAR uint8_t *txbuf, FAR uint8_t *rxbuf)
{
  size_t len;
  size_t got;
  int ret;

  while (s->tx < t->args.count)
    {
      len = t->args.count - s->tx;
      len = len < TLSTL_BUFSIZE ? len : TLSTL_BUFSIZE;
      tlstl_fill(txbuf, s->tx, len);
      ret = tlstl_write(ssl, txbuf, len);
      if (ret < 0)
        {
          return ret;
        }

      s->tx += len;
      for (got = 0; got < len; got += ret)
        {
          ret = tlstl_read(ssl, rxbuf, len - got);
          if (ret <= 0)
            {
              s->end = tlstl_endcode(ret);
              return s->end == TLSTL_END_NOTIFY ||
                     s->end == TLSTL_END_EOF ? 0 : ret;
            }

          s->bad += tlstl_check(rxbuf, s->rx, ret);
          s->rx += ret;
        }
    }

  return 0;
}

/* Receive only: read the pattern until the peer ends the stream. */

static void tlstl_client_recv(FAR mbedtls_ssl_context *ssl,
                              FAR struct tlstl_sess_s *s,
                              FAR uint8_t *rxbuf)
{
  int ret;

  for (; ; )
    {
      ret = tlstl_read(ssl, rxbuf, TLSTL_BUFSIZE);
      if (ret <= 0)
        {
          break;
        }

      s->bad += tlstl_check(rxbuf, s->rx, ret);
      s->rx += ret;
    }

  s->end = tlstl_endcode(ret);
  if (s->end != TLSTL_END_NOTIFY && s->end != TLSTL_END_EOF)
    {
      s->ret = ret;
    }
}

/* Server echo: echo everything until the peer ends the stream. */

static void tlstl_server_echo(FAR mbedtls_ssl_context *ssl,
                              FAR struct tlstl_sess_s *s,
                              FAR uint8_t *rxbuf)
{
  size_t len;
  int ret;

  for (; ; )
    {
      ret = tlstl_read(ssl, rxbuf, TLSTL_BUFSIZE);
      if (ret <= 0)
        {
          break;
        }

      len = ret;
      s->bad += tlstl_check(rxbuf, s->rx, len);
      s->rx += len;
      ret = tlstl_write(ssl, rxbuf, len);
      if (ret < 0)
        {
          s->end = tlstl_endcode(ret);
          s->ret = ret;
          return;
        }

      s->tx += len;
    }

  s->end = tlstl_endcode(ret);
  if (s->end != TLSTL_END_NOTIFY && s->end != TLSTL_END_EOF)
    {
      s->ret = ret;
    }
}

static int tlstl_connect(FAR struct tlstl_s *t)
{
  struct sockaddr_in addr;
  int fd;

  fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0)
    {
      return -1;
    }

  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_port = htons(t->args.port);
  if (inet_pton(AF_INET, t->args.addr, &addr.sin_addr) != 1 ||
      connect(fd, (FAR struct sockaddr *)&addr, sizeof(addr)) < 0)
    {
      close(fd);
      return -1;
    }

  return fd;
}

/* One worker at a time waits on the listening socket, so the worker that
 * poll() wakes for a connection is the one that accepts it; a second
 * woken worker would block in accept() without the timeout.
 */

static int tlstl_accept(FAR struct tlstl_s *t)
{
  struct pollfd pfd;
  int ret;

  pthread_mutex_lock(&t->acceptlock);
  pfd.fd = t->lfd;
  pfd.events = POLLIN;
  pfd.revents = 0;
  ret = poll(&pfd, 1, t->args.timeout * 1000);
  if (ret > 0)
    {
      ret = accept(t->lfd, NULL, NULL);
    }
  else if (ret == 0)
    {
      errno = ETIMEDOUT;
      ret = -1;
    }

  pthread_mutex_unlock(&t->acceptlock);
  return ret;
}

static int tlstl_handshake(FAR mbedtls_ssl_context *ssl)
{
  int ret;

  do
    {
      ret = mbedtls_ssl_handshake(ssl);
    }
  while (ret == MBEDTLS_ERR_SSL_WANT_READ ||
         ret == MBEDTLS_ERR_SSL_WANT_WRITE);

  return ret;
}

/* Close: send close_notify; a client then waits for the peer's one. */

static void tlstl_close(FAR struct tlstl_s *t, FAR mbedtls_ssl_context *ssl,
                        FAR struct tlstl_sess_s *s, FAR uint8_t *buf)
{
  int ret;

  do
    {
      ret = mbedtls_ssl_close_notify(ssl);
    }
  while (ret == MBEDTLS_ERR_SSL_WANT_READ ||
         ret == MBEDTLS_ERR_SSL_WANT_WRITE);

  if (ret < 0 && s->ret == 0)
    {
      s->ret = ret;
    }

  if (!t->args.server && s->end == TLSTL_END_NONE && ret == 0)
    {
      tlstl_drain(ssl, s, buf);
    }
}

static void tlstl_session(FAR struct tlstl_s *t, FAR struct tlstl_sess_s *s)
{
  mbedtls_ssl_context ssl;
  FAR uint8_t *txbuf;
  FAR uint8_t *rxbuf;
  int ret;

  memset(s, 0, sizeof(*s));
  s->ver = "-";
  mbedtls_ssl_init(&ssl);
  txbuf = malloc(TLSTL_BUFSIZE);
  rxbuf = malloc(TLSTL_BUFSIZE);
  if (txbuf == NULL || rxbuf == NULL)
    {
      s->sockerr = ENOMEM;
      s->end = TLSTL_END_ERROR;
      goto out;
    }

  s->fd = t->args.server ? tlstl_accept(t) : tlstl_connect(t);
  if (s->fd < 0)
    {
      s->sockerr = errno;
      s->end = TLSTL_END_ERROR;
      goto out;
    }

  ret = mbedtls_ssl_setup(&ssl, &t->conf);
  if (ret == 0 && !t->args.server)
    {
      ret = mbedtls_ssl_set_hostname(&ssl, t->args.host);
    }

  if (ret != 0)
    {
      s->ret = ret;
      s->end = TLSTL_END_ERROR;
      goto out_close;
    }

  mbedtls_ssl_set_bio(&ssl, s, tlstl_send, NULL, tlstl_recv);
  ret = tlstl_handshake(&ssl);
  s->verify = mbedtls_ssl_get_verify_result(&ssl);
  if (ret != 0)
    {
      s->ret = ret;
      s->end = tlstl_endcode(ret) == TLSTL_END_NOTIFY ?
               TLSTL_END_ERROR : tlstl_endcode(ret);
      goto out_close;
    }

  s->ver = mbedtls_ssl_get_version(&ssl);
  if (t->args.server)
    {
      tlstl_server_echo(&ssl, s, rxbuf);
    }
  else if (t->args.recvonly)
    {
      tlstl_client_recv(&ssl, s, rxbuf);
    }
  else
    {
      ret = tlstl_client_echo(t, &ssl, s, txbuf, rxbuf);
      if (ret < 0)
        {
          s->ret = ret;
          s->end = tlstl_endcode(ret);
        }
    }

  if (s->end == TLSTL_END_NONE || s->end == TLSTL_END_NOTIFY)
    {
      tlstl_close(t, &ssl, s, rxbuf);
    }

  s->ok = s->ret == 0 && s->bad == 0 && s->end == TLSTL_END_NOTIFY &&
          (t->args.count == 0 || s->rx == t->args.count);

out_close:
  close(s->fd);

out:
  mbedtls_ssl_free(&ssl);
  free(txbuf);
  free(rxbuf);
}

static FAR void *tlstl_worker(FAR void *arg)
{
  FAR struct tlstl_s *t = arg;
  struct tlstl_sess_s s;
  int index;

  for (; ; )
    {
      pthread_mutex_lock(&t->lock);
      index = t->failed ? t->args.sessions : t->next++;
      pthread_mutex_unlock(&t->lock);
      if (index >= t->args.sessions)
        {
          break;
        }

      tlstl_session(t, &s);

      pthread_mutex_lock(&t->lock);
      t->tx += s.tx;
      t->rx += s.rx;
      if (s.ok)
        {
          t->nok++;
        }

      if (!s.ok && !t->failed)
        {
          t->failed = true;
          t->result = s;
        }
      else if (!t->failed)
        {
          t->result = s;
        }

      pthread_mutex_unlock(&t->lock);
      if (!s.ok)
        {
          printf("tlstl: session %d end=%s ret=-0x%04x verify=0x%08"
                 PRIx32 " errno=%d rx=%zu bad=%zu\n", index,
                 g_endname[s.end], (unsigned int)-s.ret, s.verify,
                 s.sockerr, s.rx, s.bad);
        }
    }

  return NULL;
}

static int tlstl_loadfile(FAR const char *path, FAR uint8_t **buf,
                          FAR size_t *len)
{
  struct stat st;
  ssize_t n;
  int fd;

  fd = open(path, O_RDONLY);
  if (fd < 0)
    {
      return -1;
    }

  if (fstat(fd, &st) < 0 || st.st_size <= 0 || st.st_size > TLSTL_FILE_MAX)
    {
      close(fd);
      return -1;
    }

  /* PEM parsing needs the terminating NUL counted in the length */

  *buf = malloc(st.st_size + 1);
  if (*buf == NULL)
    {
      close(fd);
      return -1;
    }

  n = read(fd, *buf, st.st_size);
  close(fd);
  if (n != st.st_size)
    {
      free(*buf);
      return -1;
    }

  (*buf)[n] = '\0';
  *len = n + 1;
  return 0;
}

static int tlstl_loadcrt(FAR mbedtls_x509_crt *crt, FAR const char *path)
{
  FAR uint8_t *buf;
  size_t len;
  int ret;

  if (tlstl_loadfile(path, &buf, &len) < 0)
    {
      fprintf(stderr, "tlstl: cannot read %s: %d\n", path, errno);
      return -1;
    }

  ret = mbedtls_x509_crt_parse(crt, buf, len);
  free(buf);
  if (ret != 0)
    {
      fprintf(stderr, "tlstl: bad certificate %s: -0x%04x\n", path,
              (unsigned int)-ret);
      return -1;
    }

  return 0;
}

static int tlstl_loadkey(FAR struct tlstl_s *t, FAR const char *path)
{
  FAR uint8_t *buf;
  size_t len;
  int ret;

  if (tlstl_loadfile(path, &buf, &len) < 0)
    {
      fprintf(stderr, "tlstl: cannot read %s: %d\n", path, errno);
      return -1;
    }

  ret = mbedtls_pk_parse_key(&t->pk, buf, len, NULL, 0,
                             mbedtls_ctr_drbg_random, &t->drbg);
  free(buf);
  if (ret != 0)
    {
      fprintf(stderr, "tlstl: bad key %s: -0x%04x\n", path,
              (unsigned int)-ret);
      return -1;
    }

  return 0;
}

static int tlstl_listen(FAR struct tlstl_s *t)
{
  struct sockaddr_in addr;
  int one = 1;

  t->lfd = socket(AF_INET, SOCK_STREAM, 0);
  if (t->lfd < 0)
    {
      return -1;
    }

  setsockopt(t->lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  memset(&addr, 0, sizeof(addr));
  addr.sin_family = AF_INET;
  addr.sin_port = htons(t->args.port);
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  if (bind(t->lfd, (FAR struct sockaddr *)&addr, sizeof(addr)) < 0 ||
      listen(t->lfd, TLSTL_THREADS_MAX) < 0)
    {
      fprintf(stderr, "tlstl: listen failed: %d\n", errno);
      return -1;
    }

  printf("tlstl: listening on %u\n", t->args.port);
  fflush(stdout);
  return 0;
}

static int tlstl_setup(FAR struct tlstl_s *t)
{
  FAR struct tlstl_args_s *a = &t->args;
  int ret;

  ret = mbedtls_ctr_drbg_seed(&t->drbg, mbedtls_entropy_func, &t->entropy,
                              (FAR const unsigned char *)"tlstl", 5);
  if (ret != 0)
    {
      fprintf(stderr, "tlstl: drbg seed: -0x%04x\n", (unsigned int)-ret);
      return -1;
    }

  ret = mbedtls_ssl_config_defaults(&t->conf, a->server ?
                                    MBEDTLS_SSL_IS_SERVER :
                                    MBEDTLS_SSL_IS_CLIENT,
                                    MBEDTLS_SSL_TRANSPORT_STREAM,
                                    MBEDTLS_SSL_PRESET_DEFAULT);
  if (ret != 0)
    {
      return -1;
    }

  mbedtls_ssl_conf_rng(&t->conf, mbedtls_ctr_drbg_random, &t->drbg);
  mbedtls_ssl_conf_read_timeout(&t->conf, a->timeout * 1000);
  mbedtls_ssl_conf_min_tls_version(&t->conf, a->version == 13 ?
                                   MBEDTLS_SSL_VERSION_TLS1_3 :
                                   MBEDTLS_SSL_VERSION_TLS1_2);
  mbedtls_ssl_conf_max_tls_version(&t->conf, a->version == 12 ?
                                   MBEDTLS_SSL_VERSION_TLS1_2 :
                                   MBEDTLS_SSL_VERSION_TLS1_3);

  if (a->server)
    {
      if (tlstl_loadcrt(&t->crt, a->cert) < 0 || tlstl_loadkey(t, a->key))
        {
          return -1;
        }

      mbedtls_ssl_conf_authmode(&t->conf, MBEDTLS_SSL_VERIFY_NONE);
      ret = mbedtls_ssl_conf_own_cert(&t->conf, &t->crt, &t->pk);
      if (ret != 0)
        {
          return -1;
        }

      return tlstl_listen(t);
    }

  if (tlstl_loadcrt(&t->ca, a->ca) < 0)
    {
      return -1;
    }

  mbedtls_ssl_conf_authmode(&t->conf, MBEDTLS_SSL_VERIFY_REQUIRED);
  mbedtls_ssl_conf_ca_chain(&t->conf, &t->ca, NULL);
  return 0;
}

static void tlstl_usage(void)
{
  fprintf(stderr,
          "Usage: tlstl -c addr -A ca [-H host] [-R] [options]\n"
          "       tlstl -s -C cert -K key [options]\n"
          "  -p port   TCP port (default %d)\n"
          "  -V 12|13  only TLS 1.2 or 1.3 (default both)\n"
          "  -n bytes  client: bytes to echo (default %d); with -R or\n"
          "            -s: bytes expected (0: any)\n"
          "  -R        client: receive only, no echo\n"
          "  -r n      sessions (default 1)\n"
          "  -N n      concurrent sessions (default 1, max %d)\n"
          "  -t sec    read timeout (default %d)\n",
          TLSTL_PORT_DEFAULT, TLSTL_BYTES, TLSTL_THREADS_MAX,
          TLSTL_TIMEOUT);
}

static int tlstl_parse(FAR struct tlstl_args_s *a, int argc,
                       FAR char *argv[])
{
  bool count = false;
  int opt;

  memset(a, 0, sizeof(*a));
  a->port = TLSTL_PORT_DEFAULT;
  a->count = TLSTL_BYTES;
  a->sessions = 1;
  a->threads = 1;
  a->timeout = TLSTL_TIMEOUT;

  while ((opt = getopt(argc, argv, "c:sA:C:K:H:p:V:n:Rr:N:t:")) != -1)
    {
      switch (opt)
        {
          case 'c':
            a->addr = optarg;
            break;

          case 's':
            a->server = true;
            break;

          case 'A':
            a->ca = optarg;
            break;

          case 'C':
            a->cert = optarg;
            break;

          case 'K':
            a->key = optarg;
            break;

          case 'H':
            a->host = optarg;
            break;

          case 'p':
            a->port = (uint16_t)atoi(optarg);
            break;

          case 'V':
            a->version = atoi(optarg);
            break;

          case 'n':
            a->count = strtoul(optarg, NULL, 0);
            count = true;
            break;

          case 'R':
            a->recvonly = true;
            break;

          case 'r':
            a->sessions = atoi(optarg);
            break;

          case 'N':
            a->threads = atoi(optarg);
            break;

          case 't':
            a->timeout = atoi(optarg);
            break;

          default:
            return -1;
        }
    }

  if (a->server == (a->addr != NULL) ||
      (a->server && (a->cert == NULL || a->key == NULL)) ||
      (!a->server && a->ca == NULL) ||
      (a->version != 0 && a->version != 12 && a->version != 13) ||
      a->sessions < 1 || a->threads < 1 ||
      a->threads > TLSTL_THREADS_MAX || a->timeout < 1)
    {
      return -1;
    }

  if ((a->server || a->recvonly) && !count)
    {
      a->count = 0;
    }

  if (a->host == NULL)
    {
      a->host = a->addr;
    }

  return 0;
}

static void tlstl_init(FAR struct tlstl_s *t)
{
  mbedtls_entropy_init(&t->entropy);
  mbedtls_ctr_drbg_init(&t->drbg);
  mbedtls_ssl_config_init(&t->conf);
  mbedtls_x509_crt_init(&t->ca);
  mbedtls_x509_crt_init(&t->crt);
  mbedtls_pk_init(&t->pk);
  pthread_mutex_init(&t->acceptlock, NULL);
  pthread_mutex_init(&t->lock, NULL);
  t->lfd = -1;
  t->result.ver = "-";
}

static void tlstl_free(FAR struct tlstl_s *t)
{
  if (t->lfd >= 0)
    {
      close(t->lfd);
    }

  mbedtls_pk_free(&t->pk);
  mbedtls_x509_crt_free(&t->crt);
  mbedtls_x509_crt_free(&t->ca);
  mbedtls_ssl_config_free(&t->conf);
  mbedtls_ctr_drbg_free(&t->drbg);
  mbedtls_entropy_free(&t->entropy);
  pthread_mutex_destroy(&t->acceptlock);
  pthread_mutex_destroy(&t->lock);
#ifdef CONFIG_MBEDTLS_PSA_CRYPTO_C
  mbedtls_psa_crypto_free();
#endif
}

static int tlstl_run(FAR struct tlstl_s *t)
{
  pthread_t tid[TLSTL_THREADS_MAX];
  pthread_attr_t attr;
  int started = 0;
  int ret;
  int i;

  pthread_attr_init(&attr);
  pthread_attr_setstacksize(&attr, CONFIG_TESTLAB_TLSTL_STACKSIZE);
  for (i = 0; i < t->args.threads; i++)
    {
      ret = pthread_create(&tid[i], &attr, tlstl_worker, t);
      if (ret != 0)
        {
          fprintf(stderr, "tlstl: pthread_create: %d\n", ret);
          break;
        }

      started++;
    }

  pthread_attr_destroy(&attr);
  for (i = 0; i < started; i++)
    {
      pthread_join(tid[i], NULL);
    }

  return started == t->args.threads ? 0 : -1;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  FAR struct tlstl_s *t;
  struct tlstl_sess_s r;
  size_t tx;
  size_t rx;
  int sessions;
  bool pass;
  int ret;

  t = calloc(1, sizeof(*t));
  if (t == NULL)
    {
      return EXIT_FAILURE;
    }

  if (tlstl_parse(&t->args, argc, argv) < 0)
    {
      tlstl_usage();
      free(t);
      return EXIT_FAILURE;
    }

  tlstl_init(t);
#ifdef CONFIG_MBEDTLS_PSA_CRYPTO_C
  ret = psa_crypto_init();
  if (ret != PSA_SUCCESS)
    {
      fprintf(stderr, "tlstl: psa_crypto_init: %d\n", ret);
      ret = -1;
    }
#else
  ret = 0;
#endif
  if (ret == 0)
    {
      ret = tlstl_setup(t);
    }

  if (ret == 0)
    {
      ret = tlstl_run(t);
    }

  tlstl_free(t);
  r = t->result;
  tx = t->tx;
  rx = t->rx;
  sessions = t->args.sessions;
  pass = ret == 0 && t->nok == sessions;
  ret = t->nok;
  free(t);
  printf("tlstl: %s ver=%s sessions=%d/%d tx=%zu rx=%zu end=%s "
         "ret=-0x%04x verify=0x%08" PRIx32 " errno=%d\n",
         pass ? "PASS" : "FAIL", r.ver, ret, sessions, tx, rx,
         g_endname[r.end], (unsigned int)-r.ret, r.verify, r.sockerr);
  return pass ? EXIT_SUCCESS : EXIT_FAILURE;
}
