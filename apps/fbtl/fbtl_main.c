/****************************************************************************
 * apps/fbtl/fbtl_main.c
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
#include <sys/mman.h>

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <nuttx/video/fb.h>

/****************************************************************************
 * Private Types
 ****************************************************************************/

struct fbtl_s
{
  int fd;
  struct fb_videoinfo_s vinfo;
  struct fb_planeinfo_s pinfo;
};

/****************************************************************************
 * Private Functions
 ****************************************************************************/

static void fbtl_usage(void)
{
  fprintf(stderr,
          "usage: fbtl -d dev -i\n"
          "       fbtl -d dev -f x,y,w,h,rgb [-w] [-u]\n"
          "       fbtl -d dev -p yoffset\n");
}

/* Convert 0xRRGGBB to the pixel value of the framebuffer format */

static uint32_t fbtl_pixel(FAR const struct fbtl_s *fb, uint32_t rgb)
{
  if (fb->vinfo.fmt == FB_FMT_RGB16_565)
    {
      return ((rgb >> 8) & 0xf800) | ((rgb >> 5) & 0x07e0) |
             ((rgb >> 3) & 0x001f);
    }

  return rgb & 0xffffff;
}

/* Fill one row of w pixels into buf */

static void fbtl_row(FAR const struct fbtl_s *fb, FAR uint8_t *buf,
                     int w, uint32_t pixel)
{
  int bytes = fb->pinfo.bpp >> 3;
  int i;
  int k;

  for (i = 0; i < w; i++)
    {
      for (k = 0; k < bytes; k++)
        {
          buf[i * bytes + k] = (uint8_t)(pixel >> (8 * k));
        }
    }
}

static int fbtl_fill_mmap(FAR struct fbtl_s *fb, FAR const int *r,
                          uint32_t pixel)
{
  FAR uint8_t *mem;
  int bytes = fb->pinfo.bpp >> 3;
  int y;

  mem = mmap(NULL, fb->pinfo.fblen, PROT_READ | PROT_WRITE,
             MAP_SHARED | MAP_FILE, fb->fd, 0);
  if (mem == MAP_FAILED)
    {
      printf("fbtl: mmap failed %d\n", errno);
      return -1;
    }

  for (y = r[1]; y < r[1] + r[3]; y++)
    {
      fbtl_row(fb, mem + y * fb->pinfo.stride + r[0] * bytes, r[2], pixel);
    }

  munmap(mem, fb->pinfo.fblen);
  return 0;
}

static int fbtl_fill_write(FAR struct fbtl_s *fb, FAR const int *r,
                           uint32_t pixel)
{
  FAR uint8_t *row;
  int bytes = fb->pinfo.bpp >> 3;
  size_t len = r[2] * bytes;
  int ret = 0;
  int y;

  row = malloc(len);
  if (row == NULL)
    {
      return -1;
    }

  fbtl_row(fb, row, r[2], pixel);
  for (y = r[1]; y < r[1] + r[3]; y++)
    {
      off_t off = y * fb->pinfo.stride + r[0] * bytes;

      if (lseek(fb->fd, off, SEEK_SET) != off ||
          write(fb->fd, row, len) != (ssize_t)len)
        {
          printf("fbtl: write failed %d\n", errno);
          ret = -1;
          break;
        }
    }

  free(row);
  return ret;
}

static int fbtl_fill(FAR struct fbtl_s *fb, FAR const char *arg,
                     bool usewrite, bool update)
{
  struct fb_area_s area;
  unsigned long rgb;
  int r[4];
  int ret;

  if (sscanf(arg, "%d,%d,%d,%d,%lx", &r[0], &r[1], &r[2], &r[3],
             &rgb) != 5 || r[0] < 0 || r[1] < 0 || r[2] <= 0 ||
      r[3] <= 0 || r[0] + r[2] > fb->vinfo.xres ||
      r[1] + r[3] > fb->pinfo.yres_virtual)
    {
      fbtl_usage();
      return -1;
    }

  ret = usewrite ? fbtl_fill_write(fb, r, fbtl_pixel(fb, rgb)) :
                   fbtl_fill_mmap(fb, r, fbtl_pixel(fb, rgb));
  if (ret < 0 || !update)
    {
      return ret;
    }

  area.x = r[0];
  area.y = r[1];
  area.w = r[2];
  area.h = r[3];
  ret = ioctl(fb->fd, FBIO_UPDATE, (unsigned long)&area);
  if (ret < 0)
    {
      printf("fbtl: update failed %d\n", errno);
    }

  return ret;
}

static int fbtl_pan(FAR struct fbtl_s *fb, int yoffset)
{
  struct fb_planeinfo_s pinfo;
  int ret;

  pinfo = fb->pinfo;
  pinfo.xoffset = 0;
  pinfo.yoffset = yoffset;
  ret = ioctl(fb->fd, FBIOPAN_DISPLAY, (unsigned long)&pinfo);
  if (ret < 0)
    {
      printf("fbtl: pan failed %d\n", errno);
    }

  return ret;
}

/****************************************************************************
 * Public Functions
 ****************************************************************************/

int main(int argc, FAR char *argv[])
{
  struct fbtl_s fb;
  FAR const char *dev = "/dev/fb0";
  FAR const char *fill = NULL;
  bool usewrite = false;
  bool update = true;
  bool info = false;
  int pan = -1;
  int opt;
  int ret = 0;

  while ((opt = getopt(argc, argv, "d:if:wup:")) != ERROR)
    {
      switch (opt)
        {
          case 'd':
            dev = optarg;
            break;

          case 'i':
            info = true;
            break;

          case 'f':
            fill = optarg;
            break;

          case 'w':
            usewrite = true;
            break;

          case 'u':
            update = false;
            break;

          case 'p':
            pan = atoi(optarg);
            break;

          default:
            fbtl_usage();
            return EXIT_FAILURE;
        }
    }

  if (!info && fill == NULL && pan < 0)
    {
      fbtl_usage();
      return EXIT_FAILURE;
    }

  memset(&fb, 0, sizeof(fb));
  fb.fd = open(dev, O_RDWR);
  if (fb.fd < 0)
    {
      printf("fbtl: FAIL open %d\n", errno);
      return EXIT_FAILURE;
    }

  if (ioctl(fb.fd, FBIOGET_VIDEOINFO, (unsigned long)&fb.vinfo) < 0 ||
      ioctl(fb.fd, FBIOGET_PLANEINFO, (unsigned long)&fb.pinfo) < 0)
    {
      printf("fbtl: FAIL info %d\n", errno);
      close(fb.fd);
      return EXIT_FAILURE;
    }

  if (info)
    {
      printf("fbtl: info fmt=%d xres=%d yres=%d bpp=%d stride=%d "
             "fblen=%zu xvirt=%" PRIu32 " yvirt=%" PRIu32 "\n",
             fb.vinfo.fmt, fb.vinfo.xres, fb.vinfo.yres, fb.pinfo.bpp,
             fb.pinfo.stride, fb.pinfo.fblen, fb.pinfo.xres_virtual,
             fb.pinfo.yres_virtual);
    }

  if (fill != NULL)
    {
      ret = fbtl_fill(&fb, fill, usewrite, update);
    }

  if (ret == 0 && pan >= 0)
    {
      ret = fbtl_pan(&fb, pan);
    }

  close(fb.fd);
  printf("fbtl: %s\n", ret < 0 ? "FAIL" : "PASS");
  return ret < 0 ? EXIT_FAILURE : EXIT_SUCCESS;
}
