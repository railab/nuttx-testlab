/****************************************************************************
 * apps/elftl/elftl_main.c
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
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

/* ELFTL_NFUNCS global functions elftl_f000 ... elftl_f319: function i
 * returns x + i.  Each one is a separate symbol, and the table of
 * pointers to them needs one relocation per symbol.
 */

#define ELFTL_NFUNCS 320

#define ELFTL_FUNC(n) \
  int elftl_f##n(int x) \
  { \
    return x + (int)strtol(#n, NULL, 10); \
  }

#define ELFTL_FUNC10(n) \
  ELFTL_FUNC(n##0) ELFTL_FUNC(n##1) ELFTL_FUNC(n##2) ELFTL_FUNC(n##3) \
  ELFTL_FUNC(n##4) ELFTL_FUNC(n##5) ELFTL_FUNC(n##6) ELFTL_FUNC(n##7) \
  ELFTL_FUNC(n##8) ELFTL_FUNC(n##9)

#define ELFTL_FUNC100(n) \
  ELFTL_FUNC10(n##0) ELFTL_FUNC10(n##1) ELFTL_FUNC10(n##2) \
  ELFTL_FUNC10(n##3) ELFTL_FUNC10(n##4) ELFTL_FUNC10(n##5) \
  ELFTL_FUNC10(n##6) ELFTL_FUNC10(n##7) ELFTL_FUNC10(n##8) \
  ELFTL_FUNC10(n##9)

#define ELFTL_REF(n) elftl_f##n,

#define ELFTL_REF10(n) \
  ELFTL_REF(n##0) ELFTL_REF(n##1) ELFTL_REF(n##2) ELFTL_REF(n##3) \
  ELFTL_REF(n##4) ELFTL_REF(n##5) ELFTL_REF(n##6) ELFTL_REF(n##7) \
  ELFTL_REF(n##8) ELFTL_REF(n##9)

#define ELFTL_REF100(n) \
  ELFTL_REF10(n##0) ELFTL_REF10(n##1) ELFTL_REF10(n##2) \
  ELFTL_REF10(n##3) ELFTL_REF10(n##4) ELFTL_REF10(n##5) \
  ELFTL_REF10(n##6) ELFTL_REF10(n##7) ELFTL_REF10(n##8) \
  ELFTL_REF10(n##9)

#define ELFTL_STACK_FILE "/proc/self/stack"

/****************************************************************************
 * Private Types
 ****************************************************************************/

typedef CODE int (*elftl_func_t)(int x);

/****************************************************************************
 * Public Functions
 ****************************************************************************/

ELFTL_FUNC100(0)
ELFTL_FUNC100(1)
ELFTL_FUNC100(2)
ELFTL_FUNC10(30)
ELFTL_FUNC10(31)

/****************************************************************************
 * Private Data
 ****************************************************************************/

static const elftl_func_t g_elftl_funcs[ELFTL_NFUNCS] =
{
  ELFTL_REF100(0)
  ELFTL_REF100(1)
  ELFTL_REF100(2)
  ELFTL_REF10(30)
  ELFTL_REF10(31)
};

static FAR const char *g_elftl_words[] =
{
  "delta", "alpha", "echo", "charlie", "bravo"
};

static int g_elftl_calls;

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static int elftl_cmp(FAR const void *a, FAR const void *b)
{
  g_elftl_calls++;
  return strcmp(*(FAR const char * const *)a, *(FAR const char * const *)b);
}

/* Call every function through the table: entry i must return x + i */

static int elftl_relocs(void)
{
  long sum = 0;
  int i;

  for (i = 0; i < ELFTL_NFUNCS; i++)
    {
      int ret = g_elftl_funcs[i](1000);

      if (ret != 1000 + i)
        {
          printf("elftl: FAIL relocs i=%d ret=%d\n", i, ret);
          return EXIT_FAILURE;
        }

      sum += ret;
    }

  printf("elftl: PASS relocs n=%d sum=%ld\n", ELFTL_NFUNCS, sum);
  return EXIT_SUCCESS;
}

/* A mix of libc calls, data relocations and errno */

static int elftl_libc(void)
{
  FAR const char *words[5];
  FAR char *heap;
  char buf[64];
  struct timespec ts;
  int fd;

  memcpy(words, g_elftl_words, sizeof(words));
  qsort(words, 5, sizeof(words[0]), elftl_cmp);
  if (strcmp(words[0], "alpha") != 0 || strcmp(words[4], "echo") != 0 ||
      g_elftl_calls == 0)
    {
      printf("elftl: FAIL libc qsort\n");
      return EXIT_FAILURE;
    }

  snprintf(buf, sizeof(buf), "%d-%s-%x-%ld", 42, words[1], 0xbeef,
           strtol("-12345", NULL, 10));
  if (strcmp(buf, "42-bravo-beef--12345") != 0 || strlen(buf) != 20 ||
      strstr(buf, "bravo") != buf + 3 || strchr(buf, 'f') != buf + 12)
    {
      printf("elftl: FAIL libc snprintf %s\n", buf);
      return EXIT_FAILURE;
    }

  heap = malloc(16);
  if (heap == NULL)
    {
      printf("elftl: FAIL libc malloc\n");
      return EXIT_FAILURE;
    }

  strlcpy(heap, buf, 16);
  heap = realloc(heap, 4096);
  if (heap == NULL || memcmp(heap, buf, 15) != 0)
    {
      printf("elftl: FAIL libc realloc\n");
      free(heap);
      return EXIT_FAILURE;
    }

  memmove(heap + 1, heap, 15);
  free(heap);

  errno = 0;
  fd = open("/elftl/does/not/exist", O_RDONLY);
  if (fd >= 0 || errno != ENOENT)
    {
      printf("elftl: FAIL libc errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0)
    {
      printf("elftl: FAIL libc clock_gettime\n");
      return EXIT_FAILURE;
    }

  printf("elftl: PASS libc\n");
  return EXIT_SUCCESS;
}

/* Report the stack size and priority the loader gave this task */

static int elftl_stack(void)
{
  struct sched_param param;
  char line[80];
  long size = -1;
  FAR FILE *stream;

  stream = fopen(ELFTL_STACK_FILE, "r");
  if (stream == NULL)
    {
      printf("elftl: FAIL stack fopen errno=%d\n", errno);
      return EXIT_FAILURE;
    }

  while (fgets(line, sizeof(line), stream) != NULL)
    {
      if (strncmp(line, "StackSize:", 10) == 0)
        {
          size = strtol(line + 10, NULL, 10);
        }
    }

  fclose(stream);

  if (size < 0 || sched_getparam(0, &param) < 0)
    {
      printf("elftl: FAIL stack size=%ld\n", size);
      return EXIT_FAILURE;
    }

  printf("elftl: PASS stack size=%ld prio=%d\n", size,
         param.sched_priority);
  return EXIT_SUCCESS;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  int i;

  if (argc >= 2 && strcmp(argv[1], "args") == 0)
    {
      char list[128];
      size_t len = 0;

      list[0] = '\0';
      for (i = 2; i < argc && len < sizeof(list); i++)
        {
          len += snprintf(list + len, sizeof(list) - len, "%s%s", argv[i],
                          i + 1 < argc ? "," : "");
        }

      printf("elftl: PASS argc=%d args=%s\n", argc, list);
      return EXIT_SUCCESS;
    }

  if (argc == 3 && strcmp(argv[1], "exit") == 0)
    {
      printf("elftl: PASS exit=%d\n", atoi(argv[2]));
      return atoi(argv[2]);
    }

  if (argc == 2 && strcmp(argv[1], "relocs") == 0)
    {
      return elftl_relocs();
    }

  if (argc == 2 && strcmp(argv[1], "libc") == 0)
    {
      return elftl_libc();
    }

  if (argc == 2 && strcmp(argv[1], "stack") == 0)
    {
      return elftl_stack();
    }

  fprintf(stderr, "Usage: %s args [arg...] | exit <n> | relocs | libc | "
          "stack\n", argv[0]);
  return EXIT_FAILURE;
}
