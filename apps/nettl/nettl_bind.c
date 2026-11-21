/****************************************************************************
 * apps/nettl/nettl_bind.c
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

/* UDP bind() conflict rules.
 *
 * POSIX bind(): "[EADDRINUSE] The specified address is already in use."
 * POSIX 2.10.16: "The SO_REUSEADDR option indicates that the rules used
 * in validating addresses supplied in a bind() should allow reuse of
 * local addresses."
 */

/****************************************************************************
 * Included Files
 ****************************************************************************/

#include <nuttx/config.h>

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>

#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include "nettl.h"

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define NETTL_BIND_ANY     0   /* Wildcard address */
#define NETTL_BIND_NODE    1   /* The -c address */
#define NETTL_BIND_LOOP    2   /* 127.0.0.1 or ::1 */

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct nettl_bindsock_s
{
  bool reuse;    /* Set SO_REUSEADDR */
  int  kind;     /* NETTL_BIND_ANY, _NODE or _LOOP */
};

struct nettl_bindcase_s
{
  FAR const char         *name;
  struct nettl_bindsock_s sock[2];
  int                     expect;  /* errno of the 2nd bind(), 0: success */
};

/****************************************************************************
 * Private Data
 ****************************************************************************/

static const struct nettl_bindcase_s g_bindcases[] =
{
  {
    "wild-spec",
    {
      { false, NETTL_BIND_ANY },
      { false, NETTL_BIND_NODE }
    },
    EADDRINUSE
  },
  {
    "spec-wild",
    {
      { false, NETTL_BIND_NODE },
      { false, NETTL_BIND_ANY }
    },
    EADDRINUSE
  },
  {
    "spec-spec",
    {
      { false, NETTL_BIND_NODE },
      { false, NETTL_BIND_LOOP }
    },
    0
  },
  {
    "reuse-wild-spec",
    {
      { true, NETTL_BIND_ANY },
      { true, NETTL_BIND_NODE }
    },
    0
  },
  {
    "reuse-spec-wild",
    {
      { true, NETTL_BIND_NODE },
      { true, NETTL_BIND_ANY }
    },
    0
  }
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/* Fill the address of 'kind' for 'family', return its length or 0 */

static socklen_t nettl_bind_addr(int family, int kind,
                                 FAR const char *addr,
                                 FAR struct sockaddr_storage *ss,
                                 uint16_t port)
{
  FAR struct sockaddr_in *sa = (FAR struct sockaddr_in *)ss;
  FAR struct sockaddr_in6 *sa6 = (FAR struct sockaddr_in6 *)ss;

  memset(ss, 0, sizeof(*ss));
  if (family == AF_INET6)
    {
      sa6->sin6_family = AF_INET6;
      sa6->sin6_port   = htons(port);
      if (kind == NETTL_BIND_LOOP)
        {
          sa6->sin6_addr = in6addr_loopback;
        }
      else if (kind == NETTL_BIND_NODE &&
               (addr == NULL ||
                inet_pton(AF_INET6, addr, &sa6->sin6_addr) != 1))
        {
          return 0;
        }

      return sizeof(*sa6);
    }

  sa->sin_family = AF_INET;
  sa->sin_port   = htons(port);
  if (kind == NETTL_BIND_LOOP)
    {
      sa->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    }
  else if (kind == NETTL_BIND_NODE &&
           (addr == NULL || inet_pton(AF_INET, addr, &sa->sin_addr) != 1))
    {
      return 0;
    }

  return sizeof(*sa);
}

/* Open a UDP socket as 'bs' describes and bind it.  Return the socket in
 * *sd (-1 if socket() or setsockopt() failed) and 0 or the errno of
 * bind().
 */

static int nettl_bind_open(FAR const struct nettl_bindsock_s *bs,
                           FAR const char *addr, bool ipv6, uint16_t port,
                           FAR int *sd)
{
  struct sockaddr_storage ss;
  socklen_t salen;
  int family;
  int on = 1;

  family = ipv6 ? AF_INET6 : AF_INET;
  salen  = nettl_bind_addr(family, bs->kind, addr, &ss, port);
  *sd    = -1;
  if (salen == 0)
    {
      printf("nettl: bad address %s\n", addr != NULL ? addr : "(none)");
      return EINVAL;
    }

  *sd = socket(family, SOCK_DGRAM, 0);
  if (*sd < 0)
    {
      printf("nettl: socket failed %d\n", errno);
      return errno;
    }

  if (bs->reuse &&
      setsockopt(*sd, SOL_SOCKET, SO_REUSEADDR, &on, sizeof(on)) < 0)
    {
      printf("nettl: setsockopt failed %d\n", errno);
      close(*sd);
      *sd = -1;
      return errno;
    }

  return bind(*sd, (FAR struct sockaddr *)&ss, salen) < 0 ? errno : 0;
}

/* Two binds on one port; the second must give the errno of the case */

static int nettl_bind_bindcase(FAR const struct nettl_bindcase_s *bc,
                               FAR const char *addr, bool ipv6,
                               uint16_t port)
{
  int first;
  int second;
  int sd[2];
  bool pass;

  first  = nettl_bind_open(&bc->sock[0], addr, ipv6, port, &sd[0]);
  second = -1;
  sd[1]  = -1;
  if (first == 0)
    {
      second = nettl_bind_open(&bc->sock[1], addr, ipv6, port, &sd[1]);
    }

  pass = first == 0 && sd[1] >= 0 && second == bc->expect;
  printf("nettl: %s tx=0 rx=0 lost=0 err=%d case=%s first=%d second=%d "
         "expect=%d\n", pass ? "PASS" : "FAIL", pass ? 0 : 1, bc->name,
         first, second, bc->expect);

  if (sd[1] >= 0)
    {
      close(sd[1]);
    }

  if (sd[0] >= 0)
    {
      close(sd[0]);
    }

  return pass ? EXIT_SUCCESS : EXIT_FAILURE;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/****************************************************************************
 * Name: nettl_bind_check
 *
 * Description:
 *   Run the bind case 'name'.  'addr' is the node address in the -6
 *   family.
 *
 ****************************************************************************/

int nettl_bind_check(FAR const char *name, FAR const char *addr,
                     bool ipv6, uint16_t port)
{
  size_t i;

  for (i = 0; i < sizeof(g_bindcases) / sizeof(g_bindcases[0]); i++)
    {
      if (strcmp(name, g_bindcases[i].name) == 0)
        {
          return nettl_bind_bindcase(&g_bindcases[i], addr, ipv6, port);
        }
    }

  printf("nettl: unknown case %s\n", name);
  printf("nettl: FAIL tx=0 rx=0 lost=0 err=1 case=%s\n", name);
  return EXIT_FAILURE;
}
