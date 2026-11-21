/****************************************************************************
 * apps/sndtl/sndtl_main.c
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
#include <inttypes.h>
#include <mqueue.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <nuttx/audio/audio.h>

/****************************************************************************
 * Pre-processor Definitions
 ****************************************************************************/

#define SNDTL_MAXBUFS  16
#define SNDTL_MARKLEN  7

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct sndtl_s
{
  int fd;
  bool capture;
  uint32_t rate;
  uint8_t channels;
  uint8_t bits;
  uint32_t total;          /* Bytes to play or capture */
  uint32_t done;           /* Bytes handed to (play) or got from the driver */
  uint32_t pause_at;       /* Pause after this many dequeued buffers */
  int pause_ms;
  int fill;                /* -f: constant byte, else -1 */
  int id;                  /* -s: marker id, else -1 */
  int timeout;
  int nbufs;
  int queued;
  uint32_t err;
  uint32_t nonzero;        /* Captured bytes that are not 0 */
  uint32_t ms;             /* From start to the last buffer back */
  FAR struct ap_buffer_s *bufs[SNDTL_MAXBUFS];
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

/* Byte at stream offset off: the marker "SNDTL<id>" (id two hex
 * digits) first, then nettl's pattern; with -f, the fill byte only.
 */

static uint8_t sndtl_pattern(FAR const struct sndtl_s *s, uint32_t off)
{
  static const char hex[] = "0123456789abcdef";

  if (s->fill >= 0)
    {
      return (uint8_t)s->fill;
    }

  if (s->id >= 0 && off < SNDTL_MARKLEN)
    {
      switch (off)
        {
          case 5:
            return hex[(s->id >> 4) & 0xf];

          case 6:
            return hex[s->id & 0xf];

          default:
            return "SNDTL"[off];
        }
    }

  return (uint8_t)(off * 31 + 7);
}

static void sndtl_usage(void)
{
  fprintf(stderr,
          "usage: sndtl -q dev\n"
          "       sndtl -i dev [-r rate] [-n ch] [-b bits]\n"
          "       sndtl -p|-c dev [-r rate] [-n ch] [-b bits] [-l bytes]\n"
          "             [-f byte | -s id] [-P bufs [-W ms]] [-t sec]\n");
}

static int sndtl_query(FAR const char *dev)
{
  struct audio_caps_s caps;
  int fd;
  int ret;
  int i;

  fd = open(dev, O_RDONLY);
  if (fd < 0)
    {
      printf("sndtl: FAIL query open %d\n", errno);
      return EXIT_FAILURE;
    }

  memset(&caps, 0, sizeof(caps));
  caps.ac_len = sizeof(caps);
  caps.ac_type = AUDIO_TYPE_QUERY;
  caps.ac_subtype = AUDIO_TYPE_QUERY;
  ret = ioctl(fd, AUDIOIOC_GETCAPS, (unsigned long)&caps);
  if (ret < 0)
    {
      goto err;
    }

  printf("sndtl: caps type=0x%02x formats=0x%04x\n",
         caps.ac_controls.b[0], caps.ac_format.hw);

  memset(&caps, 0, sizeof(caps));
  caps.ac_len = sizeof(caps);
  caps.ac_type = AUDIO_TYPE_QUERY;
  caps.ac_subtype = AUDIO_FMT_PCM;
  ret = ioctl(fd, AUDIOIOC_GETCAPS, (unsigned long)&caps);
  if (ret < 0)
    {
      goto err;
    }

  printf("sndtl: caps subformats=");
  for (i = 0; i < 4 && caps.ac_controls.b[i] != 0; i++)
    {
      printf("%s0x%02x", i ? "," : "", caps.ac_controls.b[i]);
    }

  printf("\n");

  memset(&caps, 0, sizeof(caps));
  caps.ac_len = sizeof(caps);
  caps.ac_type = AUDIO_TYPE_OUTPUT;
  caps.ac_subtype = AUDIO_TYPE_QUERY;
  ret = ioctl(fd, AUDIOIOC_GETCAPS, (unsigned long)&caps);
  if (ret < 0)
    {
      caps.ac_type = AUDIO_TYPE_INPUT;
      ret = ioctl(fd, AUDIOIOC_GETCAPS, (unsigned long)&caps);
      if (ret < 0)
        {
          goto err;
        }
    }

  printf("sndtl: caps channels=0x%02x rates=0x%04x\n",
         caps.ac_channels, caps.ac_controls.hw[0]);
  close(fd);
  printf("sndtl: PASS query\n");
  return EXIT_SUCCESS;

err:
  printf("sndtl: FAIL query getcaps %d\n", errno);
  close(fd);
  return EXIT_FAILURE;
}

/* Return true if dev is an input device */

static bool sndtl_isinput(FAR const char *dev)
{
  struct audio_caps_s caps;
  int fd;

  fd = open(dev, O_RDONLY);
  if (fd < 0)
    {
      return false;
    }

  memset(&caps, 0, sizeof(caps));
  caps.ac_len = sizeof(caps);
  caps.ac_type = AUDIO_TYPE_QUERY;
  caps.ac_subtype = AUDIO_TYPE_QUERY;
  if (ioctl(fd, AUDIOIOC_GETCAPS, (unsigned long)&caps) < 0)
    {
      caps.ac_controls.b[0] = 0;
    }

  close(fd);
  return caps.ac_controls.b[0] == AUDIO_TYPE_INPUT;
}

/* Fill a playback buffer with the next pattern bytes, or check the
 * pattern of a capture buffer's bytes.
 */

static void sndtl_fill(FAR struct sndtl_s *s, FAR struct ap_buffer_s *apb)
{
  uint32_t len = s->total - s->done;
  uint32_t i;

  if (len > apb->nmaxbytes)
    {
      len = apb->nmaxbytes;
    }

  for (i = 0; i < len; i++)
    {
      apb->samp[i] = sndtl_pattern(s, s->done + i);
    }

  apb->nbytes = len;
  apb->curbyte = 0;
  apb->flags = 0;
  s->done += len;
}

static int sndtl_enqueue(FAR struct sndtl_s *s, FAR struct ap_buffer_s *apb)
{
  struct audio_buf_desc_s desc;
  int ret;

  if (!s->capture)
    {
      if (s->done >= s->total)
        {
          return 0;
        }

      sndtl_fill(s, apb);
    }
  else
    {
      /* As nxrecorder: the whole buffer is to be filled */

      memset(apb->samp, 0xff, apb->nmaxbytes);
      apb->nbytes = apb->nmaxbytes;
      apb->curbyte = 0;
    }

  memset(&desc, 0, sizeof(desc));
  desc.u.buffer = apb;
  ret = ioctl(s->fd, AUDIOIOC_ENQUEUEBUFFER, (unsigned long)&desc);
  if (ret < 0)
    {
      printf("sndtl: enqueue failed %d\n", errno);
      s->err++;
      return -1;
    }

  s->queued++;
  return 1;
}

static int sndtl_wait(FAR struct sndtl_s *s, FAR struct audio_msg_s *msg)
{
  struct audio_msg_timeout_s tmsg;
  int ret;

  memset(&tmsg, 0, sizeof(tmsg));
  tmsg.ticks = s->timeout * CLK_TCK;
  ret = ioctl(s->fd, AUDIOIOC_RECEIVEMSG, (unsigned long)&tmsg);
  if (ret < 0)
    {
      printf("sndtl: receivemsg failed %d\n", errno);
      return -1;
    }

  *msg = tmsg.msg;
  return 0;
}

static void sndtl_captured(FAR struct sndtl_s *s,
                           FAR struct ap_buffer_s *apb)
{
  uint32_t len = apb->nbytes;
  uint32_t i;

  if (len > s->total - s->done)
    {
      len = s->total - s->done;
    }

  for (i = 0; i < len; i++)
    {
      if (apb->samp[i] != 0)
        {
          s->nonzero++;
        }
    }

  s->done += len;
}

static int sndtl_setup(FAR struct sndtl_s *s, FAR const char *dev,
                       FAR struct ap_buffer_info_s *info)
{
  struct audio_caps_desc_s cap;
  struct audio_buf_desc_s desc;
  int ret;
  int i;

  s->fd = open(dev, O_RDWR);
  if (s->fd < 0)
    {
      printf("sndtl: open failed %d\n", errno);
      return -1;
    }

  ret = ioctl(s->fd, AUDIOIOC_RESERVE, 0);
  if (ret < 0)
    {
      printf("sndtl: reserve failed %d\n", errno);
      return -1;
    }

  memset(&cap, 0, sizeof(cap));
  cap.caps.ac_len = sizeof(struct audio_caps_s);
  cap.caps.ac_type = s->capture ? AUDIO_TYPE_INPUT : AUDIO_TYPE_OUTPUT;
  cap.caps.ac_channels = s->channels;
  cap.caps.ac_controls.hw[0] = s->rate & 0xffff;
  cap.caps.ac_controls.b[2] = s->bits;
  cap.caps.ac_controls.b[3] = (s->rate >> 16) & 0xff;
  ret = ioctl(s->fd, AUDIOIOC_CONFIGURE, (unsigned long)&cap);
  if (ret < 0)
    {
      printf("sndtl: configure failed %d\n", errno);
      return -1;
    }

  ret = ioctl(s->fd, AUDIOIOC_GETBUFFERINFO, (unsigned long)info);
  if (ret < 0)
    {
      printf("sndtl: getbufferinfo failed %d\n", errno);
      return -1;
    }

  printf("sndtl: bufinfo nbuffers=%d size=%d\n",
         (int)info->nbuffers, (int)info->buffer_size);

  s->nbufs = info->nbuffers < SNDTL_MAXBUFS ?
             info->nbuffers : SNDTL_MAXBUFS;
  for (i = 0; i < s->nbufs; i++)
    {
      memset(&desc, 0, sizeof(desc));
      desc.numbytes = info->buffer_size;
      desc.u.pbuffer = &s->bufs[i];
      ret = ioctl(s->fd, AUDIOIOC_ALLOCBUFFER, (unsigned long)&desc);
      if (ret != sizeof(desc))
        {
          printf("sndtl: allocbuffer failed %d %d\n", ret, errno);
          s->nbufs = i;
          return -1;
        }
    }

  ret = ioctl(s->fd, AUDIOIOC_REGISTERMQ, (unsigned long)AUDIO_MQ_KERNEL);
  if (ret < 0)
    {
      printf("sndtl: registermq failed %d\n", errno);
      return -1;
    }

  return 0;
}

static void sndtl_teardown(FAR struct sndtl_s *s)
{
  struct audio_buf_desc_s desc;
  int i;

  if (s->fd < 0)
    {
      return;
    }

  for (i = 0; i < s->nbufs; i++)
    {
      memset(&desc, 0, sizeof(desc));
      desc.u.buffer = s->bufs[i];
      ioctl(s->fd, AUDIOIOC_FREEBUFFER, (unsigned long)&desc);
    }

  ioctl(s->fd, AUDIOIOC_UNREGISTERMQ, 0);
  ioctl(s->fd, AUDIOIOC_RELEASE, 0);
  close(s->fd);
  s->fd = -1;
}

/* Wait for the buffers in flight, refilling them while data is left */

static int sndtl_stream(FAR struct sndtl_s *s)
{
  struct audio_msg_s msg;
  uint32_t ndeq = 0;

  while (s->queued > 0)
    {
      if (sndtl_wait(s, &msg) < 0)
        {
          return -1;
        }

      if (msg.msg_id != AUDIO_MSG_DEQUEUE)
        {
          if (msg.msg_id == AUDIO_MSG_UNDERRUN)
            {
              printf("sndtl: underrun\n");
            }

          continue;
        }

      s->queued--;
      ndeq++;
      if (s->capture)
        {
          sndtl_captured(s, msg.u.ptr);
          if (s->done + s->queued * s->bufs[0]->nmaxbytes >= s->total)
            {
              continue;
            }
        }

      if (s->pause_at > 0 && ndeq == s->pause_at)
        {
          if (ioctl(s->fd, AUDIOIOC_PAUSE, 0) < 0)
            {
              printf("sndtl: pause failed %d\n", errno);
              return -1;
            }

          usleep(s->pause_ms * 1000);
          if (ioctl(s->fd, AUDIOIOC_RESUME, 0) < 0)
            {
              printf("sndtl: resume failed %d\n", errno);
              return -1;
            }

          printf("sndtl: paused %d ms\n", s->pause_ms);
        }

      if (sndtl_enqueue(s, msg.u.ptr) < 0)
        {
          return -1;
        }
    }

  return 0;
}

static int sndtl_stop(FAR struct sndtl_s *s)
{
  struct audio_msg_s msg;

  if (ioctl(s->fd, AUDIOIOC_STOP, 0) < 0)
    {
      printf("sndtl: stop failed %d\n", errno);
      return -1;
    }

  for (; ; )
    {
      if (sndtl_wait(s, &msg) < 0)
        {
          return -1;
        }

      if (msg.msg_id == AUDIO_MSG_COMPLETE)
        {
          return 0;
        }
    }
}

static int sndtl_run(FAR struct sndtl_s *s, FAR const char *dev)
{
  struct ap_buffer_info_s info;
  struct timespec start;
  struct timespec end;
  int ret;
  int i;

  memset(&info, 0, sizeof(info));
  if (sndtl_setup(s, dev, &info) < 0)
    {
      return -1;
    }

  for (i = 0; i < s->nbufs; i++)
    {
      if (s->capture && i * info.buffer_size >= s->total)
        {
          break;
        }

      ret = sndtl_enqueue(s, s->bufs[i]);
      if (ret <= 0)
        {
          break;
        }
    }

  if (s->err > 0)
    {
      return -1;
    }

  clock_gettime(CLOCK_MONOTONIC, &start);
  if (ioctl(s->fd, AUDIOIOC_START, 0) < 0)
    {
      printf("sndtl: start failed %d\n", errno);
      return -1;
    }

  if (sndtl_stream(s) < 0)
    {
      return -1;
    }

  clock_gettime(CLOCK_MONOTONIC, &end);
  s->ms = (end.tv_sec - start.tv_sec) * 1000 +
          (end.tv_nsec - start.tv_nsec) / 1000000;

  return sndtl_stop(s);
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  struct sndtl_s s;
  FAR const char *dev = NULL;
  int mode = 0;
  int opt;
  int ret;

  memset(&s, 0, sizeof(s));
  s.fd = -1;
  s.rate = 48000;
  s.channels = 2;
  s.bits = 16;
  s.total = 19200;
  s.pause_ms = 200;
  s.timeout = 10;
  s.fill = -1;
  s.id = -1;

  while ((opt = getopt(argc, argv, "q:i:p:c:r:n:b:l:f:s:P:W:t:")) != ERROR)
    {
      switch (opt)
        {
          case 'q':
          case 'i':
          case 'p':
          case 'c':
            mode = opt;
            dev = optarg;
            break;

          case 'r':
            s.rate = strtoul(optarg, NULL, 0);
            break;

          case 'n':
            s.channels = atoi(optarg);
            break;

          case 'b':
            s.bits = atoi(optarg);
            break;

          case 'l':
            s.total = strtoul(optarg, NULL, 0);
            break;

          case 'f':
            s.fill = strtol(optarg, NULL, 0) & 0xff;
            break;

          case 's':
            s.id = strtol(optarg, NULL, 0) & 0xff;
            break;

          case 'P':
            s.pause_at = strtoul(optarg, NULL, 0);
            break;

          case 'W':
            s.pause_ms = atoi(optarg);
            break;

          case 't':
            s.timeout = atoi(optarg);
            break;

          default:
            sndtl_usage();
            return EXIT_FAILURE;
        }
    }

  if (mode == 0 || s.total == 0)
    {
      sndtl_usage();
      return EXIT_FAILURE;
    }

  if (mode == 'q')
    {
      return sndtl_query(dev);
    }

  if (mode == 'i')
    {
      struct ap_buffer_info_s info;

      s.capture = sndtl_isinput(dev);
      ret = sndtl_setup(&s, dev, &info);
      sndtl_teardown(&s);
      printf("sndtl: %s info\n", ret < 0 ? "FAIL" : "PASS");
      return ret < 0 ? EXIT_FAILURE : EXIT_SUCCESS;
    }

  s.capture = mode == 'c';
  ret = sndtl_run(&s, dev);
  sndtl_teardown(&s);

  if (ret < 0 || s.err > 0 || s.done != s.total)
    {
      printf("sndtl: FAIL %s bytes=%" PRIu32 " err=%" PRIu32 "\n",
             s.capture ? "capture" : "play", s.done, s.err);
      return EXIT_FAILURE;
    }

  if (s.capture)
    {
      printf("sndtl: PASS capture bytes=%" PRIu32 " nonzero=%" PRIu32
             " ms=%" PRIu32 "\n", s.done, s.nonzero, s.ms);
    }
  else
    {
      printf("sndtl: PASS play bytes=%" PRIu32 " ms=%" PRIu32 "\n",
             s.done, s.ms);
    }

  return EXIT_SUCCESS;
}
