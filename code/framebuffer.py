#!/usr/bin/env python3
"""
Direct framebuffer access for Raspberry Pi.
Supports reading/writing images directly to /dev/fbX without pygame.
"""

import os
import numpy as np


class Framebuffer:
    def __init__(self, device='/dev/fb0'):
        self.device = device
        self.fb = None
        self.width = None
        self.height = None
        self.bpp = None
        self.stride = None

    def open(self, width, height):
        """Open framebuffer and read actual parameters"""
        try:
            fb_node = os.path.basename(self.device) or 'fb0'
            sysfs_base = f'/sys/class/graphics/{fb_node}'

            try:
                with open(os.path.join(sysfs_base, 'virtual_size'), 'r') as f:
                    fb_size = f.read().strip().split(',')
                    fb_width = int(fb_size[0])
                    fb_height = int(fb_size[1])
            except Exception:
                fb_width, fb_height = width, height

            try:
                with open(os.path.join(sysfs_base, 'bits_per_pixel'), 'r') as f:
                    self.bpp = int(f.read().strip())
            except Exception:
                self.bpp = 16

            try:
                with open(os.path.join(sysfs_base, 'stride'), 'r') as f:
                    self.stride = int(f.read().strip())
            except Exception:
                self.stride = fb_width * (self.bpp // 8)

            self.fb = open(self.device, 'r+b', buffering=0)
            self.width = fb_width
            self.height = fb_height
            print(f"Framebuffer: {self.width}x{self.height}, {self.bpp}bpp, stride={self.stride}")
            return True
        except Exception as e:
            print(f"Error opening framebuffer {self.device}: {e}")
            return False

    def read_image(self):
        """
        Read current framebuffer contents and return an RGB numpy array (H, W, 3).
        Returns None on failure or unsupported format.
        """
        if self.fb is None:
            return None

        try:
            buffer_size = self.stride * self.height
            self.fb.seek(0)
            data = self.fb.read(buffer_size)
            if len(data) < buffer_size:
                return None

            if self.bpp == 32:
                row = np.frombuffer(data, dtype=np.uint8).reshape((self.height, self.stride))
                row = row[:, :self.width * 4]
                bgrx = row.reshape((self.height, self.width, 4))
                rgb = np.empty((self.height, self.width, 3), dtype=np.uint8)
                rgb[:, :, 0] = bgrx[:, :, 2]
                rgb[:, :, 1] = bgrx[:, :, 1]
                rgb[:, :, 2] = bgrx[:, :, 0]
                return rgb
            elif self.bpp == 16:
                row = np.frombuffer(data, dtype=np.uint16).reshape((self.height, self.stride // 2))
                row = row[:, :self.width]
                r = ((row >> 11) & 0x1F).astype(np.uint8)
                g = ((row >> 5) & 0x3F).astype(np.uint8)
                b = (row & 0x1F).astype(np.uint8)
                rgb = np.empty((self.height, self.width, 3), dtype=np.uint8)
                rgb[:, :, 0] = (r << 3) | (r >> 2)
                rgb[:, :, 1] = (g << 2) | (g >> 4)
                rgb[:, :, 2] = (b << 3) | (b >> 2)
                return rgb
            else:
                return None
        except Exception as e:
            print(f"Error reading framebuffer {self.device}: {e}")
            return None

    def write_image(self, image_array):
        """
        Write numpy array (H, W, 3) RGB image to framebuffer.
        Converts to RGB565 or BGRX depending on the framebuffer's bit depth.
        """
        if self.fb is None:
            return False

        try:
            h, w = image_array.shape[:2]

            if w != self.width or h != self.height:
                # Centre-crop/pad for near matches; a full resize is much more
                # expensive and should be rare.
                if abs(w - self.width) < 50 and abs(h - self.height) < 50:
                    output = np.zeros((self.height, self.width, 3), dtype=np.uint8)
                    y_offset = max(0, (self.height - h) // 2)
                    x_offset = max(0, (self.width - w) // 2)
                    copy_h = min(h, self.height - y_offset)
                    copy_w = min(w, self.width - x_offset)
                    output[y_offset:y_offset + copy_h, x_offset:x_offset + copy_w] = \
                        image_array[:copy_h, :copy_w]
                    image_array = output
                else:
                    from PIL import Image
                    img = Image.fromarray(image_array, 'RGB')
                    img = img.resize((self.width, self.height), Image.Resampling.NEAREST)
                    image_array = np.array(img)

            if self.bpp == 16:
                # RGB565: 5 bits red, 6 bits green, 5 bits blue. Undithered.
                r = (image_array[:, :, 0] >> 3).astype(np.uint16)
                g = (image_array[:, :, 1] >> 2).astype(np.uint16)
                b = (image_array[:, :, 2] >> 3).astype(np.uint16)
                rgb565 = (r << 11) | (g << 5) | b
                data = rgb565.astype(np.uint16).tobytes()
                buffer_size = self.stride * self.height
            elif self.bpp == 32:
                buffer_size = self.width * self.height * 4
                bgrx = np.empty((self.height, self.width, 4), dtype=np.uint8)
                bgrx[:, :, 0] = image_array[:, :, 2]
                bgrx[:, :, 1] = image_array[:, :, 1]
                bgrx[:, :, 2] = image_array[:, :, 0]
                bgrx[:, :, 3] = 0
                data = bgrx.tobytes()
            else:
                raise ValueError(f"Unsupported bpp: {self.bpp}")

            self.fb.seek(0)
            if len(data) >= buffer_size:
                self.fb.write(data[:buffer_size])
            else:
                self.fb.write(data + b'\x00' * (buffer_size - len(data)))

            return True
        except Exception as e:
            print(f"Error writing to framebuffer: {e}")
            return False

    def close(self):
        """Close framebuffer"""
        if self.fb:
            self.fb.close()
            self.fb = None
