/****************************************************************************
 * apps/unixtl/unixtl_main.c
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

/* AF_UNIX socket checks.  Every expectation is taken from POSIX.1-2024
 * (XSH socket(), socketpair(), bind(), connect(), send(), sendmsg(),
 * sendto(), recv(), recvmsg(), poll(), XBD <sys/socket.h>, <sys/un.h>).
 * "unixtl <case>" runs one case and prints one verdict line:
 * "unixtl: PASS <case>" or "unixtl: FAIL <case> line=<n> errno=<e> <expr>".
 */

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>

#include <sys/param.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/un.h>

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define UNIXTL_PATH(n)   "/var/run/unixtl." n
#define UNIXTL_MAXFDS    16
#define UNIXTL_BIGLEN    16384
#define UNIXTL_NIOV      8
#define UNIXTL_IOVLEN    13
#define UNIXTL_WAIT_MS   2000

/* check_iov() modes */

#define IOV_STREAM       0
#define IOV_SCATTER      1

/* CHECK() records the first failed expectation and leaves the case. */

#define CHECK(c) \
  do \
    { \
      if (!(c)) \
        { \
          g_line  = __LINE__; \
          g_errno = errno; \
          g_expr  = #c; \
          goto errout; \
        } \
    } \
  while (0)

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct unixtl_case_s
{
  FAR const char *name;
  CODE int (*func)(void);
};

/****************************************************************************
 * Private Function Prototypes
 ****************************************************************************/

static int case_dgram_trunc(void);
static int case_eof_pair(void);
static int case_epipe_pair(void);
static int case_epipe_path(void);
static int case_connect_refused(void);
static int case_connect_noent(void);
static int case_connect_prototype(void);
static int case_bind_inuse_type(void);
static int case_bind_file(void);
static int case_poll_stream(void);
static int case_iov_dgram_recv(void);
static int case_iov_stream(void);
#ifdef CONFIG_NET_LOCAL_SCM
static int case_scm_ctrunc(void);
#endif

/****************************************************************************
 * Private Data
 ****************************************************************************/

static const struct unixtl_case_s g_cases[] =
{
  {"dgram_trunc", case_dgram_trunc},
  {"eof_pair", case_eof_pair},
  {"epipe_pair", case_epipe_pair},
  {"epipe_path", case_epipe_path},
  {"connect_refused", case_connect_refused},
  {"connect_noent", case_connect_noent},
  {"connect_prototype", case_connect_prototype},
  {"bind_inuse_type", case_bind_inuse_type},
  {"bind_file", case_bind_file},
  {"poll_stream", case_poll_stream},
  {"iov_dgram_recv", case_iov_dgram_recv},
  {"iov_stream", case_iov_stream},
#ifdef CONFIG_NET_LOCAL_SCM
  {"scm_ctrunc", case_scm_ctrunc},
#endif
};

static int g_fds[UNIXTL_MAXFDS];
static FAR const char *g_paths[UNIXTL_MAXFDS];
static int g_line;
static int g_errno;
static FAR const char *g_expr;
static volatile sig_atomic_t g_sigpipe;
static uint8_t g_txbuf[UNIXTL_BIGLEN];
static uint8_t g_rxbuf[UNIXTL_BIGLEN];

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void sig_handler(int signo)
{
  if (signo == SIGPIPE)
    {
      g_sigpipe++;
    }
}

static int track(int fd)
{
  int i;

  if (fd < 0)
    {
      return fd;
    }

  for (i = 0; i < UNIXTL_MAXFDS; i++)
    {
      if (g_fds[i] < 0)
        {
          g_fds[i] = fd;
          return fd;
        }
    }

  close(fd);
  errno = EMFILE;
  return -1;
}

static int xclose(int fd)
{
  int i;

  for (i = 0; i < UNIXTL_MAXFDS; i++)
    {
      if (g_fds[i] == fd)
        {
          g_fds[i] = -1;
        }
    }

  return close(fd);
}

/* Close every descriptor and remove every name bound by the case:
 * unlink() of a bound name lets the next run bind it again.
 */

static void close_all(void)
{
  int i;

  for (i = 0; i < UNIXTL_MAXFDS; i++)
    {
      if (g_fds[i] >= 0)
        {
          close(g_fds[i]);
          g_fds[i] = -1;
        }

      if (g_paths[i] != NULL)
        {
          unlink(g_paths[i]);
          g_paths[i] = NULL;
        }
    }
}

static void fill(FAR uint8_t *buf, size_t len, unsigned int seed)
{
  size_t i;

  for (i = 0; i < len; i++)
    {
      buf[i] = (uint8_t)(seed + i * 31 + 7);
    }
}

static bool same(FAR const uint8_t *buf, size_t len, unsigned int seed)
{
  size_t i;

  for (i = 0; i < len; i++)
    {
      if (buf[i] != (uint8_t)(seed + i * 31 + 7))
        {
          return false;
        }
    }

  return true;
}

/* bind() of AF_UNIX: "the application shall ensure that a null terminator
 * after the pathname is included in the sun_path member", and address_len
 * covers it.
 */

static socklen_t mkaddr(FAR struct sockaddr_un *sa, FAR const char *path)
{
  memset(sa, 0, sizeof(*sa));
  sa->sun_family = AF_UNIX;
  strlcpy(sa->sun_path, path, sizeof(sa->sun_path));
  return offsetof(struct sockaddr_un, sun_path) + strlen(path) + 1;
}

static int sock(int type)
{
  return track(socket(AF_UNIX, type, 0));
}

static int pair(int type, FAR int *sv)
{
  if (socketpair(AF_UNIX, type, 0, sv) < 0)
    {
      return -1;
    }

  track(sv[0]);
  track(sv[1]);
  return 0;
}

static int do_bind(int fd, FAR const char *path)
{
  struct sockaddr_un sa;
  socklen_t len = mkaddr(&sa, path);
  int i;

  if (bind(fd, (FAR struct sockaddr *)&sa, len) < 0)
    {
      return -1;
    }

  for (i = 0; i < UNIXTL_MAXFDS; i++)
    {
      if (g_paths[i] == NULL)
        {
          g_paths[i] = path;
          break;
        }
    }

  return 0;
}

static int do_connect(int fd, FAR const char *path)
{
  struct sockaddr_un sa;
  socklen_t len = mkaddr(&sa, path);

  return connect(fd, (FAR struct sockaddr *)&sa, len);
}

static ssize_t do_sendto(int fd, FAR const void *buf, size_t len,
                         FAR const char *path)
{
  struct sockaddr_un sa;
  socklen_t salen = mkaddr(&sa, path);

  return sendto(fd, buf, len, 0, (FAR struct sockaddr *)&sa, salen);
}

static int bound(int type, FAR const char *path)
{
  int fd = sock(type);

  if (fd >= 0 && do_bind(fd, path) < 0)
    {
      xclose(fd);
      return -1;
    }

  return fd;
}

static int listener(FAR const char *path)
{
  int fd = bound(SOCK_STREAM, path);

  if (fd >= 0 && listen(fd, 4) < 0)
    {
      xclose(fd);
      return -1;
    }

  return fd;
}

/* A connected stream socket pair over a pathname: connect() of the client,
 * then accept() on the listener.
 */

static int path_pair(FAR const char *path, FAR int *sv)
{
  int l = listener(path);

  if (l < 0)
    {
      return -1;
    }

  sv[0] = sock(SOCK_STREAM);
  if (sv[0] < 0 || do_connect(sv[0], path) < 0)
    {
      return -1;
    }

  sv[1] = track(accept(l, NULL, NULL));
  if (sv[1] < 0)
    {
      return -1;
    }

  return xclose(l);
}

/* Wait until a descriptor has an event, or 'ms' milliseconds passed. */

static short wait_event(int fd, short events, int ms)
{
  struct pollfd pfd;

  pfd.fd      = fd;
  pfd.events  = events;
  pfd.revents = 0;
  if (poll(&pfd, 1, ms) < 0)
    {
      return -1;
    }

  return pfd.revents;
}

/* Receive one datagram, waiting at most UNIXTL_WAIT_MS. */

static ssize_t recv_dgram(int fd, FAR void *buf, size_t len)
{
  if (wait_event(fd, POLLIN, UNIXTL_WAIT_MS) <= 0)
    {
      errno = ETIMEDOUT;
      return -1;
    }

  return recv(fd, buf, len, 0);
}

static bool sigpipe_seen(void)
{
  int i;

  for (i = 0; i < 10 && g_sigpipe == 0; i++)
    {
      usleep(10000);
    }

  return g_sigpipe != 0;
}

/* recvmsg(): "For message-based sockets, such as SOCK_DGRAM and
 * SOCK_SEQPACKET, the entire message shall be read in a single operation.
 * If a message is too long to fit in the supplied buffers, and MSG_PEEK is
 * not set in the flags argument, the excess bytes shall be discarded, and
 * MSG_TRUNC shall be set in the msg_flags member of the msghdr structure."
 * A 300 byte datagram is read into 100 bytes; the next read returns the
 * next datagram.
 */

static int check_trunc(int tx, int rx, FAR const char *to)
{
  struct msghdr msg;
  struct iovec iov;
  ssize_t n;

  fill(g_txbuf, 300, 30);
  n = to ? do_sendto(tx, g_txbuf, 300, to) : send(tx, g_txbuf, 300, 0);
  if (n != 300)
    {
      return -1;
    }

  fill(g_txbuf, 50, 31);
  n = to ? do_sendto(tx, g_txbuf, 50, to) : send(tx, g_txbuf, 50, 0);
  if (n != 50 || wait_event(rx, POLLIN, UNIXTL_WAIT_MS) <= 0)
    {
      return -1;
    }

  memset(&msg, 0, sizeof(msg));
  iov.iov_base   = g_rxbuf;
  iov.iov_len    = 100;
  msg.msg_iov    = &iov;
  msg.msg_iovlen = 1;
  n = recvmsg(rx, &msg, 0);
  if (n != 100 || !same(g_rxbuf, 100, 30) ||
      (msg.msg_flags & MSG_TRUNC) == 0)
    {
      errno = EIO;
      return -1;
    }

  n = recv_dgram(rx, g_rxbuf, sizeof(g_rxbuf));
  if (n != 50 || !same(g_rxbuf, 50, 31))
    {
      errno = EIO;
      return -1;
    }

  return 0;
}

/****************************************************************************
 * Name: case_dgram_trunc
 *
 * recvmsg() of a datagram larger than the buffer discards the excess and
 * reports it with MSG_TRUNC, on a socketpair() and between sockets bound
 * to pathnames.
 ****************************************************************************/

static int case_dgram_trunc(void)
{
  int sv[2];
  int a;
  int b;

  CHECK(pair(SOCK_DGRAM, sv) == 0);
  CHECK(check_trunc(sv[0], sv[1], NULL) == 0);
  a = bound(SOCK_DGRAM, UNIXTL_PATH("ta"));
  CHECK(a >= 0);
  b = bound(SOCK_DGRAM, UNIXTL_PATH("tb"));
  CHECK(b >= 0);
  CHECK(check_trunc(b, a, UNIXTL_PATH("ta")) == 0);
  return 0;

errout:
  return -1;
}

/* recv(): "If no messages are available to be received and the peer has
 * performed an orderly shutdown, recv() shall return 0."  Data sent before
 * close() is received first.
 */

static int check_eof(int tx, int rx)
{
  size_t off = 0;
  ssize_t n;

  fill(g_txbuf, 100, 70);
  if (send(tx, g_txbuf, 100, 0) != 100 || xclose(tx) < 0)
    {
      return -1;
    }

  while (off < 100)
    {
      if (wait_event(rx, POLLIN, UNIXTL_WAIT_MS) <= 0)
        {
          errno = ETIMEDOUT;
          return -1;
        }

      n = recv(rx, g_rxbuf + off, sizeof(g_rxbuf) - off, 0);
      if (n <= 0)
        {
          errno = EIO;
          return -1;
        }

      off += n;
    }

  if (off != 100 || !same(g_rxbuf, 100, 70) ||
      wait_event(rx, POLLIN, UNIXTL_WAIT_MS) <= 0)
    {
      errno = EIO;
      return -1;
    }

  if (recv(rx, g_rxbuf, sizeof(g_rxbuf), 0) != 0 ||
      recv(rx, g_rxbuf, sizeof(g_rxbuf), 0) != 0)
    {
      errno = EIO;
      return -1;
    }

  return 0;
}

/****************************************************************************
 * Name: case_eof_pair
 ****************************************************************************/

static int case_eof_pair(void)
{
  int sv[2];

  CHECK(pair(SOCK_STREAM, sv) == 0);
  CHECK(check_eof(sv[0], sv[1]) == 0);
  CHECK(pair(SOCK_STREAM, sv) == 0);
  CHECK(check_eof(sv[1], sv[0]) == 0);
  return 0;

errout:
  return -1;
}

/* send(): EPIPE "The socket is shut down for writing, or the socket is
 * connection-mode and is no longer connected.  In the latter case, and if
 * the socket is of type SOCK_STREAM or SOCK_SEQPACKET and the MSG_NOSIGNAL
 * flag is not set, the SIGPIPE signal is generated to the calling thread."
 * MSG_NOSIGNAL: "The [EPIPE] error shall still be returned."
 */

static int check_epipe(int fd, int peer)
{
  if (xclose(peer) < 0)
    {
      return -1;
    }

  g_sigpipe = 0;
  errno = 0;
  if (send(fd, "x", 1, MSG_NOSIGNAL) >= 0 || errno != EPIPE)
    {
      return -1;
    }

  if (sigpipe_seen())
    {
      errno = EINTR;
      return -1;
    }

  errno = 0;
  if (send(fd, "x", 1, 0) >= 0 || errno != EPIPE)
    {
      return -1;
    }

  if (!sigpipe_seen() || g_sigpipe != 1)
    {
      errno = ESRCH;
      return -1;
    }

  return 0;
}

/****************************************************************************
 * Name: case_epipe_pair / case_epipe_path
 ****************************************************************************/

static int case_epipe_pair(void)
{
  int sv[2];

  CHECK(pair(SOCK_STREAM, sv) == 0);
  CHECK(check_epipe(sv[0], sv[1]) == 0);
  return 0;

errout:
  return -1;
}

static int case_epipe_path(void)
{
  int sv[2];

  CHECK(path_pair(UNIXTL_PATH("pp"), sv) == 0);
  CHECK(check_epipe(sv[0], sv[1]) == 0);
  CHECK(path_pair(UNIXTL_PATH("pq"), sv) == 0);
  CHECK(check_epipe(sv[1], sv[0]) == 0);
  return 0;

errout:
  return -1;
}

/****************************************************************************
 * Name: case_connect_refused
 *
 * connect(): ECONNREFUSED "The target address was not listening for
 * connections or refused the connection request."  The address is bound
 * to a stream socket that never called listen().
 ****************************************************************************/

static int case_connect_refused(void)
{
  int c;

  CHECK(bound(SOCK_STREAM, UNIXTL_PATH("rf")) >= 0);
  c = sock(SOCK_STREAM);
  CHECK(c >= 0);
  errno = 0;
  CHECK(do_connect(c, UNIXTL_PATH("rf")) < 0 && errno == ECONNREFUSED);
  return 0;

errout:
  return -1;
}

/****************************************************************************
 * Name: case_connect_noent
 *
 * connect(): "If the address family of the socket is AF_UNIX, then
 * connect() shall fail if: [ENOENT] A component of the pathname does not
 * name an existing file or the pathname is an empty string."
 ****************************************************************************/

static int case_connect_noent(void)
{
  int c;

  c = sock(SOCK_STREAM);
  CHECK(c >= 0);
  errno = 0;
  CHECK(do_connect(c, UNIXTL_PATH("none")) < 0 && errno == ENOENT);
  return 0;

errout:
  return -1;
}

/****************************************************************************
 * Name: case_connect_prototype
 *
 * connect(): EPROTOTYPE "The specified address has a different type than
 * the socket bound to the specified peer address."
 ****************************************************************************/

static int case_connect_prototype(void)
{
  int c;

  CHECK(bound(SOCK_DGRAM, UNIXTL_PATH("td")) >= 0);
  c = sock(SOCK_STREAM);
  CHECK(c >= 0);
  errno = 0;
  CHECK(do_connect(c, UNIXTL_PATH("td")) < 0 && errno == EPROTOTYPE);

  CHECK(listener(UNIXTL_PATH("ts")) >= 0);
  c = sock(SOCK_DGRAM);
  CHECK(c >= 0);
  errno = 0;
  CHECK(do_connect(c, UNIXTL_PATH("ts")) < 0 && errno == EPROTOTYPE);
  return 0;

errout:
  return -1;
}

/****************************************************************************
 * Name: case_bind_inuse_type
 *
 * bind(): EADDRINUSE "The specified address is already in use."  An
 * AF_UNIX address is a pathname, whichever socket type holds it.
 ****************************************************************************/

static int case_bind_inuse_type(void)
{
  int fd;

  CHECK(bound(SOCK_STREAM, UNIXTL_PATH("va")) >= 0);
  fd = sock(SOCK_DGRAM);
  CHECK(fd >= 0);
  errno = 0;
  CHECK(do_bind(fd, UNIXTL_PATH("va")) < 0 && errno == EADDRINUSE);

  CHECK(bound(SOCK_DGRAM, UNIXTL_PATH("vb")) >= 0);
  fd = sock(SOCK_STREAM);
  CHECK(fd >= 0);
  errno = 0;
  CHECK(do_bind(fd, UNIXTL_PATH("vb")) < 0 && errno == EADDRINUSE);
  return 0;

errout:
  return -1;
}

/****************************************************************************
 * Name: case_bind_file
 *
 * bind(): "If the pathname in the sun_path member of address names an
 * existing file, including a symbolic link, bind() shall treat the address
 * as already in use".
 ****************************************************************************/

static int case_bind_file(void)
{
  struct sockaddr_un sa;
  socklen_t len;
  int fd;

  /* Plain bind(): a name that was bound by mistake is not unlink()ed. */

  len = mkaddr(&sa, "/dev/null");
  CHECK(access("/dev/null", F_OK) == 0);
  fd = sock(SOCK_STREAM);
  CHECK(fd >= 0);
  errno = 0;
  CHECK(bind(fd, (FAR struct sockaddr *)&sa, len) < 0 &&
        errno == EADDRINUSE);
  fd = sock(SOCK_DGRAM);
  CHECK(fd >= 0);
  errno = 0;
  CHECK(bind(fd, (FAR struct sockaddr *)&sa, len) < 0 &&
        errno == EADDRINUSE);
  return 0;

errout:
  return -1;
}

/* poll(): POLLIN "Data other than high-priority data may be read without
 * blocking."  POLLOUT "Normal data may be written without blocking."
 * The writer fills the socket with O_NONBLOCK until EAGAIN; POLLOUT is
 * then clear, and set again once the peer has read the data.
 */

static int check_poll(int a, int b, size_t unit)
{
  size_t sent = 0;
  size_t got = 0;
  ssize_t n;
  short ev;

  ev = wait_event(a, POLLIN | POLLOUT, 0);
  if (ev != POLLOUT)
    {
      errno = EIO;
      return -1;
    }

  fill(g_txbuf, unit, 80);
  if (send(b, g_txbuf, unit, 0) != (ssize_t)unit ||
      (wait_event(a, POLLIN, UNIXTL_WAIT_MS) & POLLIN) == 0 ||
      recv(a, g_rxbuf, sizeof(g_rxbuf), 0) != (ssize_t)unit ||
      wait_event(a, POLLIN, 0) != 0)
    {
      errno = EIO;
      return -1;
    }

  if (fcntl(a, F_SETFL, fcntl(a, F_GETFL) | O_NONBLOCK) < 0 ||
      fcntl(b, F_SETFL, fcntl(b, F_GETFL) | O_NONBLOCK) < 0)
    {
      return -1;
    }

  while (sent < UNIXTL_BIGLEN * 4)
    {
      n = send(a, g_txbuf, unit, 0);
      if (n < 0)
        {
          break;
        }

      sent += n;
    }

  if (n >= 0 || (errno != EAGAIN && errno != EWOULDBLOCK) ||
      wait_event(a, POLLOUT, 0) != 0)
    {
      errno = EIO;
      return -1;
    }

  while (got < sent)
    {
      n = recv(b, g_rxbuf, sizeof(g_rxbuf), 0);
      if (n <= 0)
        {
          errno = EIO;
          return -1;
        }

      got += n;
    }

  if ((wait_event(a, POLLOUT, UNIXTL_WAIT_MS) & POLLOUT) == 0)
    {
      errno = EIO;
      return -1;
    }

  return 0;
}

/****************************************************************************
 * Name: case_poll_stream
 ****************************************************************************/

static int case_poll_stream(void)
{
  int sv[2];

  CHECK(pair(SOCK_STREAM, sv) == 0);
  CHECK(check_poll(sv[0], sv[1], 1) == 0);

  /* After the peer closed, recv() returns 0 without blocking. */

  CHECK(xclose(sv[1]) == 0);
  CHECK((wait_event(sv[0], POLLIN, UNIXTL_WAIT_MS) &
         (POLLIN | POLLHUP)) != 0);
  CHECK(recv(sv[0], g_rxbuf, 1, 0) == 0);
  return 0;

errout:
  return -1;
}

/* sendmsg(): "The data from each storage area indicated by msg_iov is sent
 * in turn."  recvmsg(): "Each storage area indicated by msg_iov is filled
 * with received data in turn".  UNIXTL_NIOV iovecs of different lengths:
 * IOV_STREAM sends with them and receives with one buffer, IOV_SCATTER
 * sends one buffer and receives the datagram with them.
 */

static int check_iov(int tx, int rx, FAR const char *to, int mode)
{
  struct iovec iov[UNIXTL_NIOV];
  struct sockaddr_un sa;
  struct msghdr msg;
  size_t total = 0;
  size_t off = 0;
  ssize_t n;
  int i;

  for (i = 0; i < UNIXTL_NIOV; i++)
    {
      iov[i].iov_base = g_txbuf + total;
      iov[i].iov_len  = UNIXTL_IOVLEN + i;
      total += UNIXTL_IOVLEN + i;
    }

  fill(g_txbuf, total, 90);
  memset(&msg, 0, sizeof(msg));
  msg.msg_iov    = iov;
  msg.msg_iovlen = UNIXTL_NIOV;
  if (mode == IOV_SCATTER)
    {
      iov[0].iov_len = total;
      msg.msg_iovlen = 1;
    }

  if (to != NULL)
    {
      msg.msg_name    = &sa;
      msg.msg_namelen = mkaddr(&sa, to);
    }

  if (sendmsg(tx, &msg, 0) != (ssize_t)total)
    {
      return -1;
    }

  memset(g_rxbuf, 0, sizeof(g_rxbuf));
  if (mode != IOV_SCATTER)
    {
      while (off < total)
        {
          if (wait_event(rx, POLLIN, UNIXTL_WAIT_MS) <= 0)
            {
              errno = ETIMEDOUT;
              return -1;
            }

          n = recv(rx, g_rxbuf + off, sizeof(g_rxbuf) - off, 0);
          if (n <= 0)
            {
              errno = EIO;
              return -1;
            }

          off += n;
        }

      return same(g_rxbuf, total, 90) ? 0 : -1;
    }

  for (i = 0; i < UNIXTL_NIOV; i++)
    {
      iov[i].iov_base = g_rxbuf + off;
      iov[i].iov_len  = UNIXTL_IOVLEN + i;
      off += UNIXTL_IOVLEN + i;
    }

  memset(&msg, 0, sizeof(msg));
  msg.msg_iov    = iov;
  msg.msg_iovlen = UNIXTL_NIOV;
  if (wait_event(rx, POLLIN, UNIXTL_WAIT_MS) <= 0)
    {
      errno = ETIMEDOUT;
      return -1;
    }

  if (recvmsg(rx, &msg, 0) != (ssize_t)total)
    {
      return -1;
    }

  if (!same(g_rxbuf, total, 90) || (msg.msg_flags & MSG_TRUNC) != 0)
    {
      errno = EIO;
      return -1;
    }

  return 0;
}

/****************************************************************************
 * Name: case_iov_dgram_recv / case_iov_stream
 ****************************************************************************/

static int case_iov_dgram_recv(void)
{
  int sv[2];
  int a;
  int b;

  CHECK(pair(SOCK_DGRAM, sv) == 0);
  CHECK(check_iov(sv[0], sv[1], NULL, IOV_SCATTER) == 0);
  a = bound(SOCK_DGRAM, UNIXTL_PATH("ic"));
  CHECK(a >= 0);
  b = bound(SOCK_DGRAM, UNIXTL_PATH("id"));
  CHECK(b >= 0);
  CHECK(check_iov(b, a, UNIXTL_PATH("ic"), IOV_SCATTER) == 0);
  return 0;

errout:
  return -1;
}

static int case_iov_stream(void)
{
  int sv[2];

  CHECK(pair(SOCK_STREAM, sv) == 0);
  CHECK(check_iov(sv[0], sv[1], NULL, IOV_STREAM) == 0);
  CHECK(path_pair(UNIXTL_PATH("is"), sv) == 0);
  CHECK(check_iov(sv[0], sv[1], NULL, IOV_STREAM) == 0);
  return 0;

errout:
  return -1;
}

#ifdef CONFIG_NET_LOCAL_SCM
static int send_fd(int fd, int pass)
{
  union
  {
    struct cmsghdr hdr;
    char buf[CMSG_SPACE(sizeof(int))];
  } ctl;

  FAR struct cmsghdr *cmsg;
  struct msghdr msg;
  struct iovec iov;

  memset(&msg, 0, sizeof(msg));
  memset(&ctl, 0, sizeof(ctl));
  iov.iov_base       = "F";
  iov.iov_len        = 1;
  msg.msg_iov        = &iov;
  msg.msg_iovlen     = 1;
  msg.msg_control    = ctl.buf;
  msg.msg_controllen = sizeof(ctl.buf);
  cmsg               = CMSG_FIRSTHDR(&msg);
  cmsg->cmsg_level   = SOL_SOCKET;
  cmsg->cmsg_type    = SCM_RIGHTS;
  cmsg->cmsg_len     = CMSG_LEN(sizeof(int));
  memcpy(CMSG_DATA(cmsg), &pass, sizeof(int));
  return sendmsg(fd, &msg, 0) == 1 ? 0 : -1;
}

/****************************************************************************
 * Name: case_scm_ctrunc
 *
 * recvmsg(): MSG_CTRUNC "Control data was truncated."  The control buffer
 * holds a header but no room for the descriptor.
 ****************************************************************************/

static int case_scm_ctrunc(void)
{
  struct cmsghdr ctl;
  struct msghdr msg;
  struct iovec iov;
  char data;
  int sv[2];
  int pfd[2];

  CHECK(pair(SOCK_STREAM, sv) == 0);
  CHECK(pipe(pfd) == 0);
  track(pfd[0]);
  track(pfd[1]);
  CHECK(send_fd(sv[0], pfd[1]) == 0);

  memset(&msg, 0, sizeof(msg));
  iov.iov_base       = &data;
  iov.iov_len        = 1;
  msg.msg_iov        = &iov;
  msg.msg_iovlen     = 1;
  msg.msg_control    = &ctl;
  msg.msg_controllen = sizeof(ctl);
  CHECK(recvmsg(sv[1], &msg, 0) == 1 && data == 'F');
  CHECK((msg.msg_flags & MSG_CTRUNC) != 0);
  return 0;

errout:
  return -1;
}
#endif

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  struct sigaction act;
  size_t i;
  int ret;

  if (argc != 2)
    {
      printf("unixtl: FAIL usage: unixtl <case>\n");
      return EXIT_FAILURE;
    }

  for (i = 0; i < nitems(g_cases); i++)
    {
      if (strcmp(argv[1], g_cases[i].name) == 0)
        {
          break;
        }
    }

  if (i == nitems(g_cases))
    {
      printf("unixtl: FAIL %s unknown case\n", argv[1]);
      return EXIT_FAILURE;
    }

  memset(&act, 0, sizeof(act));
  act.sa_handler = sig_handler;
  sigemptyset(&act.sa_mask);
  sigaction(SIGPIPE, &act, NULL);

  for (ret = 0; ret < UNIXTL_MAXFDS; ret++)
    {
      g_fds[ret] = -1;
    }

  g_line = 0;
  ret = g_cases[i].func();
  close_all();
  if (ret == 0)
    {
      printf("unixtl: PASS %s\n", g_cases[i].name);
      return EXIT_SUCCESS;
    }

  printf("unixtl: FAIL %s line=%d errno=%d %s\n", g_cases[i].name,
         g_line, g_errno, g_expr != NULL ? g_expr : "");
  return EXIT_FAILURE;
}
