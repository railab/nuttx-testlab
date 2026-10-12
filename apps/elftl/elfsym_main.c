/****************************************************************************
 * apps/elftl/elfsym_main.c
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

#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

/****************************************************************************
 * Public Functions
 ****************************************************************************/

/* A loadable program that calls libc functions programs commonly use:
 * getpid(), gettid() and putchar().  It only loads if the system symbol
 * table exports all of them.
 */

int main(int argc, FAR char *argv[])
{
  pid_t pid = getpid();
  pid_t tid = gettid();

  if (pid <= 0 || tid != pid)
    {
      printf("elfsym: FAIL pid=%d tid=%d\n", (int)pid, (int)tid);
      return EXIT_FAILURE;
    }

  printf("elfsym: PASS pid=%d", (int)pid);
  putchar('\n');
  return EXIT_SUCCESS;
}
