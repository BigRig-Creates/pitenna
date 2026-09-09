#!/usr/bin/env python3
"""
PiTenna - Dual Display Camera Overlay System
- HDMI0: Live camera feed with HDMI1 content overlaid at 50% opacity
- HDMI1: Separate display content
"""

import sys
import os
import time
import threading
import subprocess
import signal
import shutil
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

import numpy as np
from picamera2 import Picamera2
try:
    from picamera2.previews import DrmPreview
    from picamera2.previews import drm_preview as drm_preview_mod
except ImportError:
    DrmPreview = None
    drm_preview_mod = None
import pykms
from PIL import Image, ImageDraw
try:
    from x1203_monitor import read_voltage, read_soc, GAUGE_ADDR
    from smbus2 import SMBus
    X1203_AVAILABLE = True
except ImportError:
    X1203_AVAILABLE = False
    read_voltage = None
    read_soc = None
    GAUGE_ADDR = None
    SMBus = None
import pygame  # Still needed for image processing (scaling, compositing)
try:
    from framebuffer import Framebuffer
except ImportError:
    Framebuffer = None

# Boost CPU/GPU priority for this process
def boost_process_priority():
    """Set high priority and CPU affinity for better performance"""
    try:
        def _try_cmd(cmd, label):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    print(f"{label} succeeded")
                    return True
            except Exception:
                pass
            return False

        elevated = False

        # Prefer sudo helpers if not root (renice/ionice/chrt) to avoid permission warnings.
        if os.geteuid() != 0:
            elevated |= _try_cmd(['sudo', '-n', 'renice', '-10', str(os.getpid())], "sudo renice")
            elevated |= _try_cmd(['sudo', '-n', 'ionice', '-c', '2', '-n', '0', str(os.getpid())], "sudo ionice")
            elevated |= _try_cmd(['sudo', '-n', 'chrt', '-f', '20', str(os.getpid())], "sudo chrt")

        # Fall back to os.nice if still not elevated
        if not elevated:
            try:
                os.nice(-10)
                print("Set process priority to high (nice=-10)")
            except PermissionError:
                print("Warning: Could not set high priority (need root or appropriate permissions)")
            except AttributeError:
                pass

        # Set CPU affinity to use all cores (Pi 5 has 4 cores)
        try:
            import psutil
            p = psutil.Process(os.getpid())
            cpu_count = os.cpu_count() or 4
            p.cpu_affinity(list(range(cpu_count)))
            print(f"Set CPU affinity to use all {cpu_count} cores")
        except ImportError:
            pass
        except (psutil.AccessDenied, AttributeError):
            pass

    except Exception as e:
        print(f"Could not boost process priority: {e}")

def log_system_health(prefix=""):
    """Log basic thermal/throttle/voltage status if available."""
    def _run(cmd):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            return ""
        return ""
    temp = _run(['vcgencmd', 'measure_temp'])
    throttled = _run(['vcgencmd', 'get_throttled'])
    volt = _run(['vcgencmd', 'measure_volts'])
    if prefix:
        prefix = f"{prefix} "
    print(f"{prefix}Thermals: {temp or 'n/a'}, throttled: {throttled or 'n/a'}, volt: {volt or 'n/a'}")


# Configuration
HDMI0_SIZE = (1920, 1080)  # Xreal Air glasses (16:9 display)
HDMI1_SIZE = (1024, 768)   # 4:3 monitor
OVERLAY_POSITION = 'top-right'
OVERLAY_MAX_WIDTH = 300
OVERLAY_MARGIN = 10
SWAP_RGB_CHANNELS = False  # Set to True if red/blue look swapped on HDMI0
USE_GPU_PREVIEW = True  # Use Picamera2's DRM preview for zero-copy camera display
MAX_MIRROR_FPS = 60.0
DPAD_ROTATION = os.environ.get("DPAD_ROTATION", "cw").lower()  # 'cw', 'ccw', 'none'
MENU_BUTTON_MAP = {
    'Y': 'MenuUp',
    'X': 'MenuRight',
    'B': 'MenuDown',
    'A': 'MenuLeft',
}
MENU_THUMB_MAX_WIDTH = 360
MENU_THUMB_MAX_HEIGHT = 240
MENU_OVERLAY_ALPHA = 150
MENU_THUMB_SPACING = 120
FFMPEG_BINARY = os.environ.get("FFMPEG_BIN", "ffmpeg")
# Audio now routes through HDMI0 (4:3 screen); use softvol master control.
HDMI1_AUDIO_DEVICE = os.environ.get("HDMI1_AUDIO_DEVICE", "hdmi0softvol")
HDMI1_AUDIO_ENABLED = os.environ.get("HDMI1_AUDIO_ENABLED", "1").lower() not in ("0", "false", "off", "no")
HDMI1_VOLUME_CARD = os.environ.get("HDMI1_VOLUME_CARD", "vc4hdmi0")
# HDMI0 uses softvol master control for unified volume control
HDMI1_VOLUME_CONTROL = os.environ.get("HDMI1_VOLUME_CONTROL", "HDMI0 Master")
VOLUME_DISPLAY_SECONDS = 2.0
HDMI1_VIDEO_GAIN_DB = float(os.environ.get("HDMI1_VIDEO_GAIN_DB", "3.0"))
HDMI1_VOLUME_STEP = float(os.environ.get("HDMI1_VOLUME_STEP", "3.0"))
# Default scale matches HDMI1 panel; override via env if needed.
HDMI1_FFMPEG_SCALE = os.environ.get("HDMI1_FFMPEG_SCALE", f"{HDMI1_SIZE[0]}:{HDMI1_SIZE[1]}")
# Fallback to the same hardware device; avoid dmix/default by default.
HDMI1_AUDIO_DEVICE_FALLBACK = os.environ.get("HDMI1_AUDIO_DEVICE_FALLBACK", "plughw:CARD=vc4hdmi0,DEV=0")
BLANK_IMAGE_NAME = "Blank.jpg"
TENNA_TALK_FILENAME = "TennaTalk.wav"
TVTIME_SFX_FILENAME = "TVTIMESFX.wav"
SLIDESHOW_ADVANCE_SECONDS = 0.12

def _normalize_fps(value, default):
    """Clamp/validate FPS targets so overlays can keep up with video playback."""
    try:
        fps = float(value)
    except (TypeError, ValueError):
        fps = default
    fps = max(1.0, min(fps, MAX_MIRROR_FPS))
    return fps

def _parse_scale(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        cleaned = value.lower().replace('x', ':')
        w_str, h_str = cleaned.split(':', 1)
        w, h = int(w_str), int(h_str)
        if w > 0 and h > 0:
            return (w, h)
    except Exception:
        pass
    return fallback

GPU_OVERLAY_FPS = _normalize_fps(os.environ.get("GPU_OVERLAY_FPS", 8.0), 10.0)
HDMI1_CAPTURE_FPS = _normalize_fps(os.environ.get("HDMI1_CAPTURE_FPS", 30.0), 30.0)
GPU_OVERLAY_UPDATE_INTERVAL = 1.0 / GPU_OVERLAY_FPS
HDMI1_CAPTURE_INTERVAL = 1.0 / HDMI1_CAPTURE_FPS
HDMI1_PREVIEW_FPS_VIDEO = _normalize_fps(os.environ.get("HDMI1_PREVIEW_FPS_VIDEO", 10.0), 10.0)
HDMI1_PREVIEW_INTERVAL_VIDEO = 1.0 / HDMI1_PREVIEW_FPS_VIDEO
# The 4:3 monitor hangs off the first connector; the glasses off the second.
HDMI1_DRM_CONNECTOR = os.environ.get("HDMI1_DRM_CONNECTOR", "HDMI-A-1")
DRM_PREVIEW_CONNECTOR = os.environ.get("DRM_PREVIEW_CONNECTOR", "HDMI-A-2")


def _configure_drm_preview_manager():
    if not drm_preview_mod or not DRM_PREVIEW_CONNECTOR:
        return
    try:
        class _NamedConnectorDrmManager(drm_preview_mod.DrmManager):
            def __init__(self, connector_name: str):
                super().__init__()
                self.connector_name = connector_name

            def add(self, drm_preview):
                with self.lock:
                    if self.use_count == 0:
                        self.card = pykms.Card()
                        self.resman = pykms.ResourceManager(self.card)
                        conn = self.resman.reserve_connector(self.connector_name)
                        self.crtc = self.resman.reserve_crtc(conn)
                    self.use_count += 1
                drm_preview.card = self.card
                drm_preview.resman = self.resman
                drm_preview.crtc = self.crtc

        drm_preview_mod.DrmPreview._manager = _NamedConnectorDrmManager(DRM_PREVIEW_CONNECTOR)
        print(f"DRM preview connector forced to {DRM_PREVIEW_CONNECTOR}")
    except Exception as e:
        print(f"Warning: Failed to force DRM preview connector ({DRM_PREVIEW_CONNECTOR}): {e}")


def rotate_direction(direction: str) -> str:
    """Rotate DPAD direction according to configured orientation."""
    order = ['up', 'right', 'down', 'left']
    direction = direction.lower()
    if direction not in order:
        return direction
    if DPAD_ROTATION == 'cw':
        shift = 1
    elif DPAD_ROTATION in ('ccw', 'counterclockwise'):
        shift = -1
    else:
        return direction
    idx = order.index(direction)
    return order[(idx + shift) % len(order)]


def is_video_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in {'.mp4', '.mov', '.mkv', '.avi', '.m4v'}


def is_image_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in {'.png', '.jpg', '.jpeg', '.bmp', '.gif'}


def is_music_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in {'.mp3', '.wav', '.ogg', '.flac', '.m4a'}


@dataclass
class MediaItem:
    menu: str
    direction: str
    media_path: str
    media_type: str
    thumb_surface: Optional[pygame.Surface]
    label: str
    image_sequence: Optional[List[str]] = None
    sub_menu: Optional[str] = None  # For nested menus like OST, COVERS


class MenuManager:
    """Load menu assets (thumbnails + media) from media/Menu*/Select* folders."""

    def __init__(self, media_root: str):
        self.media_root = media_root
        self.menus: Dict[str, Dict[str, MediaItem]] = {}
        self._load_media()

    def _load_media(self):
        if not os.path.isdir(self.media_root):
            print(f"MenuManager: media root '{self.media_root}' not found")
            return
        for entry in sorted(os.listdir(self.media_root)):
            if not entry.lower().startswith('menu'):
                continue
            menu_path = os.path.join(self.media_root, entry)
            if not os.path.isdir(menu_path):
                continue
            items: Dict[str, MediaItem] = {}
            for select_entry in sorted(os.listdir(menu_path)):
                if not select_entry.lower().startswith('select'):
                    continue
                direction = select_entry[6:].lower()
                select_path = os.path.join(menu_path, select_entry)
                if not os.path.isdir(select_path):
                    continue
                thumb_path = None
                media_path = None
                image_sequence: List[str] = []
                subdirs: List[str] = []
                for file_name in sorted(os.listdir(select_path)):
                    file_path = os.path.join(select_path, file_name)
                    if os.path.isdir(file_path):
                        subdirs.append(file_path)
                        continue
                    if not os.path.isfile(file_path):
                        continue
                    lower = file_name.lower()
                    if lower.startswith('thumb'):
                        thumb_path = file_path
                    else:
                        media_path = file_path
                
                # Check if subdirs contain sub-menus (OST, COVERS with Select* subdirectories)
                sub_menu_name = None
                for subdir in subdirs:
                    subdir_basename = os.path.basename(subdir)
                    # Check if this subdirectory contains Select* subdirectories (UP, DOWN, LEFT, RIGHT)
                    has_select_dirs = False
                    for subsub in os.listdir(subdir):
                        subsub_path = os.path.join(subdir, subsub)
                        if os.path.isdir(subsub_path) and subsub.upper() in ('UP', 'DOWN', 'LEFT', 'RIGHT'):
                            has_select_dirs = True
                            break
                    if has_select_dirs:
                        sub_menu_name = subdir_basename
                        # Load the sub-menu items
                        self._load_sub_menu(entry, sub_menu_name, subdir)
                        break
                
                # If we found a sub-menu, create a MediaItem pointing to it
                if sub_menu_name:
                    if not thumb_path:
                        continue
                    thumb_surface = self._load_thumbnail_surface(thumb_path)
                    items[direction] = MediaItem(
                        menu=entry,
                        direction=direction,
                        media_path=select_path,  # Keep path for reference
                        media_type='submenu',
                        thumb_surface=thumb_surface,
                        label=sub_menu_name.replace('_', ' ').title(),
                        sub_menu=sub_menu_name,
                    )
                    continue
                
                # Support folders that contain only a thumb + a single subfolder of images
                if not media_path and len(subdirs) == 1:
                    seq_dir = subdirs[0]
                    for img_name in sorted(os.listdir(seq_dir)):
                        img_path = os.path.join(seq_dir, img_name)
                        if os.path.isfile(img_path) and is_image_file(img_path):
                            image_sequence.append(img_path)
                    if image_sequence:
                        media_path = image_sequence[0]
                        media_type = 'slideshow'
                        label = os.path.basename(seq_dir)
                    else:
                        media_type = None
                        label = None
                else:
                    media_type = None
                    label = None
                if not thumb_path or not media_path:
                    continue
                if media_type is None:
                    if is_music_file(media_path):
                        media_type = 'music'
                    elif is_video_file(media_path):
                        media_type = 'video'
                    else:
                        media_type = 'image'
                    label = os.path.splitext(os.path.basename(media_path))[0]
                thumb_sequence = image_sequence if image_sequence else None
                thumb_surface = self._load_thumbnail_surface(thumb_path)
                items[direction] = MediaItem(
                    menu=entry,
                    direction=direction,
                    media_path=media_path,
                    media_type=media_type,
                    thumb_surface=thumb_surface,
                    label=label.replace('_', ' ').title(),
                    image_sequence=thumb_sequence,
                )
            if items:
                self.menus[entry] = items
        print(f"MenuManager: loaded menus {list(self.menus.keys())}")
    
    def _load_sub_menu(self, parent_menu: str, sub_menu_name: str, sub_menu_path: str):
        """Load a sub-menu (like OST, COVERS) with its Select* subdirectories."""
        menu_key = f"{parent_menu}_{sub_menu_name}"
        items: Dict[str, MediaItem] = {}
        
        for select_entry in sorted(os.listdir(sub_menu_path)):
            if not select_entry.upper() in ('UP', 'DOWN', 'LEFT', 'RIGHT'):
                continue
            direction = select_entry.lower()
            select_path = os.path.join(sub_menu_path, select_entry)
            if not os.path.isdir(select_path):
                continue
            
            thumb_path = None
            media_path = None
            for file_name in sorted(os.listdir(select_path)):
                file_path = os.path.join(select_path, file_name)
                if not os.path.isfile(file_path):
                    continue
                lower = file_name.lower()
                if lower.startswith('thumb'):
                    thumb_path = file_path
                elif is_music_file(file_path):
                    media_path = file_path
            
            if not thumb_path or not media_path:
                continue
            
            media_type = 'music'
            label = os.path.splitext(os.path.basename(media_path))[0]
            thumb_surface = self._load_thumbnail_surface(thumb_path)
            items[direction] = MediaItem(
                menu=menu_key,
                direction=direction,
                media_path=media_path,
                media_type=media_type,
                thumb_surface=thumb_surface,
                label=label.replace('_', ' ').title(),
            )
        
        if items:
            self.menus[menu_key] = items
            print(f"MenuManager: loaded sub-menu {menu_key} with {len(items)} items")

    def _load_thumbnail_surface(self, path: str) -> Optional[pygame.Surface]:
        try:
            img = Image.open(path).convert("RGBA")
        except Exception as exc:
            print(f"MenuManager: could not open thumbnail {path}: {exc}")
            return None
        width, height = img.size
        scale = min(
            MENU_THUMB_MAX_WIDTH / width if width else 1.0,
            MENU_THUMB_MAX_HEIGHT / height if height else 1.0,
            1.0,
        )
        if scale < 1.0:
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        data = img.tobytes()
        try:
            surface = pygame.image.frombuffer(data, img.size, "RGBA").copy()
            return surface
        except Exception as exc:
            print(f"MenuManager: could not convert thumbnail {path}: {exc}")
            return None

    def has_menu(self, menu_name: str) -> bool:
        return menu_name in self.menus

    def get_menu(self, menu_name: str) -> Optional[Dict[str, MediaItem]]:
        return self.menus.get(menu_name)

    def get_item(self, menu_name: str, direction: str) -> Optional[MediaItem]:
        menu = self.get_menu(menu_name)
        if not menu:
            return None
        return menu.get(direction.lower())

    def get_default_item(self) -> Optional[MediaItem]:
        for menu_name in sorted(self.menus.keys()):
            menu = self.menus[menu_name]
            for direction in ('up', 'right', 'down', 'left'):
                if direction in menu:
                    return menu[direction]
            # fallback to any
            for item in menu.values():
                return item
        return None


class MenuOverlay:
    """Render a four-direction thumbnail overlay in the center of HDMI0."""

    def __init__(self, manager: MenuManager, screen_size: Tuple[int, int]):
        self.manager = manager
        self.screen_size = screen_size
        self.active_menu: Optional[str] = None
        self.menu_stack: List[str] = []  # Stack for nested menu navigation
        self.visible = False
        self.title_font = pygame.font.Font(None, 72)
        self.label_font = pygame.font.Font(None, 42)
        self.spacing = MENU_THUMB_SPACING

    def toggle(self, menu_name: str) -> bool:
        if self.visible and self.active_menu == menu_name:
            self.deactivate()
            return False
        return self.activate(menu_name)

    def activate(self, menu_name: str) -> bool:
        if not self.manager.has_menu(menu_name):
            print(f"MenuOverlay: menu {menu_name} unavailable")
            return False
        self.active_menu = menu_name
        self.visible = True
        print(f"MenuOverlay: activated {menu_name}")
        return True

    def deactivate(self):
        if self.visible:
            print("MenuOverlay: hidden")
        self.visible = False
        self.active_menu = None
        self.menu_stack = []

    def select(self, direction: str) -> Optional[MediaItem]:
        if not self.visible or not self.active_menu:
            return None
        direction = direction.lower()
        item = self.manager.get_item(self.active_menu, direction)
        if item and item.media_type == 'submenu' and item.sub_menu:
            # Navigate to sub-menu
            sub_menu_name = f"{self.active_menu}_{item.sub_menu}"
            if self.manager.has_menu(sub_menu_name):
                self.menu_stack.append(self.active_menu)
                self.active_menu = sub_menu_name
                print(f"MenuOverlay: navigated to sub-menu {sub_menu_name}")
                return None  # Don't return item, just navigate
        return item

    def draw(self, target_surface: pygame.Surface) -> bool:
        if not self.visible or not self.active_menu:
            return False
        menu = self.manager.get_menu(self.active_menu)
        if not menu:
            return False
        overlay = pygame.Surface(self.screen_size, pygame.SRCALPHA)
        overlay.fill((0, 0, 0, MENU_OVERLAY_ALPHA))
        target_surface.blit(overlay, (0, 0))

        drawn = False
        center_x = self.screen_size[0] // 2
        center_y = self.screen_size[1] // 2
        for direction, item in menu.items():
            if not item.thumb_surface:
                continue
            w, h = item.thumb_surface.get_size()
            pos_x, pos_y = self._position_for(direction, w, h, center_x, center_y)
            target_surface.blit(item.thumb_surface, (pos_x, pos_y))
            border_rect = pygame.Rect(pos_x, pos_y, w, h).inflate(14, 14)
            pygame.draw.rect(target_surface, (230, 230, 230), border_rect, width=4)
            label_surface = self.label_font.render(item.label, True, (255, 255, 255))
            label_pos = (
                border_rect.centerx - label_surface.get_width() // 2,
                border_rect.bottom + 8,
            )
            target_surface.blit(label_surface, label_pos)
            drawn = True

        if drawn:
            # Show menu path for nested menus
            if self.menu_stack:
                title_text = f"{self.menu_stack[-1]} > {self.active_menu.split('_')[-1]}"
            else:
                title_text = self.active_menu
            title_surface = self.title_font.render(title_text, True, (255, 255, 255))
            title_y = max(20, center_y - MENU_THUMB_MAX_HEIGHT - 200)
            target_surface.blit(
                title_surface,
                (center_x - title_surface.get_width() // 2, title_y),
            )
        return drawn

    def _position_for(self, direction: str, w: int, h: int, center_x: int, center_y: int) -> Tuple[int, int]:
        if direction == 'up':
            return center_x - w // 2, center_y - h - self.spacing
        if direction == 'down':
            return center_x - w // 2, center_y + self.spacing
        if direction == 'left':
            return center_x - w - self.spacing, center_y - h // 2
        if direction == 'right':
            return center_x + self.spacing, center_y - h // 2
        # default center
        return center_x - w // 2, center_y - h // 2


class HDMI1VideoPlayer:
    """Decode video frames via ffmpeg and feed them back into PiTenna."""

    def __init__(
        self,
        video_path: str,
        frame_size: Tuple[int, int],
        frame_callback,
        audio_device: Optional[str] = None,
        on_finished: Optional[callable] = None,
    ):
        self.video_path = video_path
        self.frame_size = frame_size
        self.frame_callback = frame_callback
        self.thread: Optional[threading.Thread] = None
        self.process: Optional[subprocess.Popen] = None
        self.running = False
        self.audio_device = audio_device
        self.audio_failure = threading.Event()
        self.on_finished = on_finished

    def start(self):
        if self.thread and self.thread.is_alive():
            return True
        if not shutil.which(FFMPEG_BINARY):
            print(
                "HDMI1VideoPlayer: ffmpeg binary not found; install it with "
                "'sudo apt install ffmpeg' to enable video playback."
            )
            return False
        if not os.path.exists(self.video_path):
            print(f"HDMI1VideoPlayer: video {self.video_path} missing.")
            return False
        self.running = True
        self.thread = threading.Thread(target=self._run, name="HDMI1VideoPlayer", daemon=True)
        self.thread.start()
        print(f"HDMI1VideoPlayer: playing {self.video_path}")
        return True

    def stop(self):
        self.running = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=1.0)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
        # Only fire finished callback if we actually had a worker thread
        if self.on_finished and self.thread:
            try:
                self.on_finished()
            except Exception:
                pass
        if self.thread:
            self.thread.join(timeout=1.0)
            self.thread = None

    def _run(self):
        width, height = self.frame_size
        frame_bytes = width * height * 3
        command = [
            FFMPEG_BINARY,
            '-loglevel', 'warning',
            '-hide_banner',
            '-i', self.video_path,
        ]
        # Video output (raw frames to stdout)
        command += [
            '-map', '0:v:0',
            '-vf', f'scale={width}:{height}',
            '-pix_fmt', 'rgb24',
            '-f', 'rawvideo',
            'pipe:1',
        ]
        # Optional audio output directly to HDMI1
        if self.audio_device:
            command += [
                '-map', '0:a:0?',
                '-ac', '2',
                '-ar', '44100',
                '-af', f'volume={HDMI1_VIDEO_GAIN_DB}dB',
                '-f', 'alsa',
                self.audio_device,
            ]
        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=frame_bytes * 2,
            )
        except FileNotFoundError:
            print("HDMI1VideoPlayer: ffmpeg executable not found.")
            self.running = False
            return

        if self.process.stderr:
            threading.Thread(
                target=self._drain_stderr_watch,
                args=(self.process.stderr, self.audio_failure),
                name="HDMI1VideoPlayer.stderr",
                daemon=True,
            ).start()

        while self.running and self.process and self.process.stdout:
            data = self.process.stdout.read(frame_bytes)
            if not data or len(data) < frame_bytes:
                break
            frame_array = np.frombuffer(data, dtype=np.uint8)
            if frame_array.size != frame_bytes:
                continue
            frame_array = frame_array.reshape((height, width, 3))
            self.frame_callback(frame_array)
        self.running = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=1.0)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
        # Only fire finished callback if we actually started and ran
        if self.on_finished and self.thread:
            try:
                self.on_finished()
            except Exception:
                pass

    @staticmethod
    def _drain_stderr_watch(stream, audio_failure_event: threading.Event):
        try:
            for raw_line in iter(stream.readline, b''):
                line = raw_line.decode(errors='ignore').strip()
                if line:
                    print(f"[ffmpeg] {line}")
                    lower = line.lower()
                    if ("cannot open audio device" in lower or
                            "could not write header" in lower or
                            "error sending frames to consumers" in lower):
                        audio_failure_event.set()
        except Exception:
            pass


class DualDisplayCamera:
    def __init__(self):
        self.camera = None
        self.fb = None  # Framebuffer for direct output
        self.preview = None  # DRM preview (GPU mode)
        self.hdmi1_screen = None
        self.running = False
        self.use_gpu_preview = USE_GPU_PREVIEW
        self.overlay_last_update = 0.0
        self.overlay_dirty = True
        self.frame_times = []
        self.last_frame_time = time.time()
        self._last_callback_time = time.time()
        self.fb1 = None
        self.manual_hdmi1 = False
        self.last_hdmi1_surface = None
        self.last_hdmi1_capture = 0.0
        self.hdmi1_drm = None
        self.hdmi1_content = None
        self.hdmi1_media_path = None
        self.hdmi1_writeback = None
        self._writeback_warned = False
        self.project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.media_manager: Optional[MenuManager] = None
        self.menu_overlay: Optional[MenuOverlay] = None
        self.current_media_item: Optional[MediaItem] = None
        self.hdmi1_video_player: Optional[HDMI1VideoPlayer] = None
        self._hdmi1_frame_array: Optional[np.ndarray] = None
        self._hdmi1_frame_lock = threading.Lock()
        self._hdmi1_surface_dirty = False
        self._last_preview_video_update = 0.0
        self._video_finished_event = threading.Event()
        self.video_playing = False
        self._camera_prev_fps = None
        self.volume_supported = True
        self._volume_display_until = 0.0
        self._volume_last_db = None
        self._volume_last_text = None
        self.tvtime_sound = None
        self.blank_surface: Optional[pygame.Surface] = None
        self.slideshow_images: List[str] = []
        self.slideshow_index: int = 0
        self.slideshow_last_advance: float = 0.0
        self.dpad_down_held: bool = False
        self.tenna_talk_sound = None
        self.tenna_talk_channel = None
        self.music_channel = None
        self.music_sound = None
        self.slideshow_cache: Dict[str, pygame.Surface] = {}
        
        # Kill any existing processes that might be using the camera
        self.kill_existing_processes()
        
        try:
            from input_handler import InputHandler
            self.input_handler = InputHandler()
            self.input_handler.start()
            print("Input handler started")
        except Exception as e:
            print(f"Warning: Could not initialize input handler: {e}")
            self.input_handler = None
        
        # Initialize camera (configure but do not start yet)
        self.init_camera()
        
        # Initialize displays / previews
        self.init_displays()
        
        # Start streaming now that preview/output is ready
        self.start_camera_stream()

        print(
            f"HDMI1 capture target: {HDMI1_CAPTURE_FPS:.1f} fps "
            f"({HDMI1_CAPTURE_INTERVAL * 1000:.1f} ms interval)"
        )
        print(
            f"Overlay refresh target: {GPU_OVERLAY_FPS:.1f} fps "
            f"({GPU_OVERLAY_UPDATE_INTERVAL * 1000:.1f} ms interval)"
        )
    
    def kill_existing_processes(self):
        """Kill any existing processes that might be using the camera"""
        print("Checking for existing processes...")
        
        try:
            # Get current process PID
            current_pid = os.getpid()
            
            # Find processes using the camera or running main.py
            result = subprocess.run(
                ['ps', 'aux'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            killed_any = False
            for line in result.stdout.split('\n'):
                if 'python3' in line and 'main.py' in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            pid = int(parts[1])
                            if pid != current_pid:
                                print(f"Killing existing process PID {pid}")
                                try:
                                    os.kill(pid, signal.SIGTERM)
                                    killed_any = True
                                except ProcessLookupError:
                                    pass  # Process already gone
                                except PermissionError:
                                    # Try with sudo if we don't have permission
                                    subprocess.run(['sudo', 'kill', str(pid)], 
                                                  capture_output=True)
                                    killed_any = True
                        except (ValueError, IndexError):
                            pass
            
            # Also check for processes using media devices
            try:
                result = subprocess.run(
                    ['fuser', '/dev/media0', '/dev/media1'],
                    capture_output=True,
                    text=True,
                    stderr=subprocess.DEVNULL
                )
                if result.returncode == 0:
                    pids = result.stdout.strip().split()
                    for pid_str in pids:
                        try:
                            pid = int(pid_str)
                            if pid != current_pid:
                                print(f"Killing process {pid} using media device")
                                try:
                                    os.kill(pid, signal.SIGTERM)
                                    killed_any = True
                                except (ProcessLookupError, PermissionError):
                                    pass
                        except ValueError:
                            pass
            except FileNotFoundError:
                pass  # fuser not available
            
            if killed_any:
                print("Waiting for processes to terminate...")
                time.sleep(2)  # Give processes time to clean up
            else:
                print("No conflicting processes found")
                
        except Exception as e:
            print(f"Warning: Could not check for existing processes: {e}")
        
    def init_camera(self):
        """Initialize camera (auto-detects module and uses preview config)."""
        print("Initializing camera...")
        # Try available cameras from libcamera (supports Arducam + official Pi cameras).
        self.camera = None
        camera_error = None
        try:
            camera_info = Picamera2.global_camera_info()
        except Exception as e:
            camera_info = []
            camera_error = e

        if camera_info:
            print("Detected cameras:")
            for info in camera_info:
                print(f"  {info}")
        else:
            print("No cameras reported by libcamera.")

        # Prefer cameras by reported Num, fall back to indices 0/1.
        candidate_indices = []
        for info in camera_info:
            num = info.get("Num")
            if isinstance(num, int):
                candidate_indices.append(num)
        for idx in (0, 1):
            if idx not in candidate_indices:
                candidate_indices.append(idx)

        for idx in candidate_indices:
            try:
                self.camera = Picamera2(idx)
                print(f"Camera initialized on index {idx}")
                break
            except Exception as e:
                camera_error = e
                self.camera = None

        if self.camera is None:
            print(f"Error: Camera not connected or not available: {camera_error}")
            self.camera_available = False
            self.camera_size = (1280, 720)  # Default size for fallback display (720p)
            return
        
        self.camera_available = True
        # Best-effort camera model logging.
        try:
            props = getattr(self.camera, "camera_properties", {}) or {}
            model = (
                props.get("Model")
                or props.get("model")
                or props.get("Name")
                or props.get("name")
                or props.get("SensorModel")
                or props.get("sensor_model")
            )
            if model:
                print(f"Camera model detected: {model}")
        except Exception:
            pass
        
        # Get available camera modes
        modes = self.camera.sensor_modes
        print(f"Available sensor modes: {len(modes)}")
        for i, mode in enumerate(modes):
            print(f"  Mode {i}: {mode['size']} @ {mode['fps']:.1f}fps, crop_limits: {mode.get('crop_limits', 'N/A')}")
        if not modes:
            print("Warning: No sensor modes reported; camera may be misconfigured.")
            self.camera_available = False
            self.camera_size = (1280, 720)
            return
        
        # Pick a sensible preview size; let libcamera pick the sensor mode
        camera_width = 1280  # 720p target keeps FPS high and load reasonable
        camera_height = 720
        self.camera_size = (camera_width, camera_height)
        
        print(f"Using preview size: {camera_width}x{camera_height} (libcamera will choose best mode)")
        print("  Goal: full sensor FOV where possible, balanced FPS/power for head-mounted display")

        camera_config = self.camera.create_preview_configuration(
            main={"size": (camera_width, camera_height), "format": "RGB888"},
            buffer_count=4
        )
        self.camera.configure(camera_config)
        
        # Set camera controls for dynamic auto-exposure and (if supported) autofocus
        try:
            max_fps = None
            try:
                ctrl = self.camera.camera_controls.get("FrameRate", {})
                max_fps = ctrl.get("max", None)
            except Exception:
                pass
            target_fps = 30.0
            if max_fps is not None:
                target_fps = min(target_fps, max_fps)
            controls = {
                "FrameRate": target_fps,
                "AeEnable": True,
            }
            # Attempt continuous autofocus when available (e.g., IMX708/Arducam AF modules)
            try:
                if "AfMode" in self.camera.camera_controls:
                    controls["AfMode"] = 2  # continuous autofocus
            except Exception:
                pass
            controls = {k: v for k, v in controls.items() if v is not None}
            self.camera.set_controls(controls)
        except Exception:
            pass  # Some controls may not be available on all cameras
        
        # Camera is configured but not started; start_camera_stream() runs it
        # once the display/preview is ready.

    
    def _on_camera_request(self, request):
        """Picamera2 post-callback to track FPS in GPU preview mode."""
        if not self.use_gpu_preview:
            return
        current_time = time.time()
        frame_time = current_time - getattr(self, '_last_callback_time', current_time)
        self._last_callback_time = current_time
        if frame_time <= 0:
            return
        self.frame_times.append(frame_time)
        if len(self.frame_times) > 60:
            self.frame_times.pop(0)
        avg_frame_time = sum(self.frame_times) / len(self.frame_times)
        if avg_frame_time > 0:
            self.current_fps = 1.0 / avg_frame_time
            self.overlay_dirty = True
    
    def start_camera_stream(self):
        """Start camera streaming after preview/display is ready."""
        if not self.camera_available or self.camera is None:
            print("Camera not available, skipping stream start.")
            return
        print("Starting camera stream...")
        try:
            self.camera.start()
        except Exception as e:
            print(f"Error starting camera: {e}")
            self.camera_available = False
            raise
        
        if self.use_gpu_preview:
            # Use Picamera2 callback to track FPS without pulling frames into Python
            self.camera.post_callback = self._on_camera_request
            print("GPU preview mode active (post-callback enabled).")
        else:
            print("CPU framebuffer mode active (will capture frames synchronously).")
        
        # Log sensor crop info now that camera is running
        try:
            metadata = self.camera.capture_metadata()
            scaler_crop = metadata.get('ScalerCrop', [0, 0, 0, 0])
            if len(scaler_crop) >= 4:
                crop_x, crop_y, crop_w, crop_h = scaler_crop[:4]
                req_w, req_h = self.camera_size
                print("Camera initialized")
                print(f"  Requested size: {req_w}x{req_h}")
                print(f"  Sensor crop: {crop_w}x{crop_h} at ({crop_x}, {crop_y})")
                sensor_aspect = crop_w / crop_h if crop_h else 0
                requested_aspect = req_w / req_h if req_h else 0
                if abs(sensor_aspect - requested_aspect) > 0.1:
                    print("  Warning: camera is cropping the sensor")
                    print(f"     Sensor aspect: {sensor_aspect:.2f}, requested aspect: {requested_aspect:.2f}")
                else:
                    print("  No significant cropping (aspect ratios match)")
            else:
                print(f"Camera initialized - output size: {self.camera_size}")
        except Exception as e:
            print(f"Warning: Could not read camera metadata: {e}")
        
    def init_displays(self):
        """Initialize dual HDMI displays using direct framebuffer access"""
        print("Initializing displays...")
        
        # Disable console cursor on framebuffer (HDMI0)
        # The blinking cursor is from the console - disable it
        try:
            # Disable console cursor blinking (multiple methods for compatibility)
            os.system('setterm -cursor off >/dev/tty1 2>&1 || true')
            # Alternative: use escape sequences to hide cursor
            os.system('echo -e "\033[?25l" >/dev/tty1 2>&1 || true')
            # Disable console on framebuffer 0 if possible
            if os.path.exists('/sys/class/graphics/fbcon/cursor_blink'):
                os.system('echo 0 > /sys/class/graphics/fbcon/cursor_blink 2>/dev/null || true')
            # Try to move console to different framebuffer
            if os.path.exists('/sys/class/vtconsole/vtcon1'):
                os.system('echo 0 > /sys/class/vtconsole/vtcon1/bind 2>/dev/null || true')
        except:
            pass  # Ignore if commands fail
        
        # Prefer GPU preview (DRM/DMABUF) for zero-copy camera display
        if self.use_gpu_preview and DrmPreview is not None and getattr(self, 'camera_available', False) and self.camera is not None:
            try:
                _configure_drm_preview_manager()
                self.preview = DrmPreview(width=HDMI0_SIZE[0], height=HDMI0_SIZE[1])
                self.camera.start_preview(self.preview)
                print("GPU DRM preview initialized (zero-copy).")
            except Exception as e:
                print(f"Warning: Could not initialize GPU preview: {e}")
                self.use_gpu_preview = False
                self.preview = None
        elif self.use_gpu_preview and not getattr(self, 'camera_available', False):
            print("Camera not available, disabling GPU preview.")
            self.use_gpu_preview = False
            self.preview = None
        elif self.use_gpu_preview and DrmPreview is None:
            print("DrmPreview class not available; falling back to framebuffer.")
            self.use_gpu_preview = False
        
        if not self.use_gpu_preview:
            # Use direct framebuffer access for display (fallback path)
            try:
                from framebuffer import Framebuffer
                self.fb = Framebuffer('/dev/fb0')
                if self.fb.open(HDMI0_SIZE[0], HDMI0_SIZE[1]):
                    print(f"HDMI0 framebuffer initialized: {HDMI0_SIZE[0]}x{HDMI0_SIZE[1]}")
                else:
                    raise Exception("Could not open framebuffer")
            except Exception as e:
                print(f"Error: Could not initialize framebuffer: {e}")
                raise

        # Initialize pygame for image processing/off-screen work
        os.environ['SDL_VIDEODRIVER'] = 'dummy'
        os.environ.setdefault('SDL_AUDIODRIVER', 'alsa')
        os.environ.setdefault('AUDIODEV', HDMI1_AUDIO_DEVICE)
        if not pygame.get_init():
            pygame.init()
        if not pygame.mixer.get_init():
            try:
                # Initialize with enough channels for music, TennaTalk, and sound effects
                pygame.mixer.init(channels=8)  # Channel 0: TennaTalk, Channel 1: Music, others: SFX
            except Exception as exc:
                print(f"Warning: Could not initialize audio mixer (TennaTalk disabled): {exc}")
        pygame.font.init()
        self.font = pygame.font.Font(None, 48)
        self.tenna_talk_sound = self._load_tenna_talk_sound()
        self.tvtime_sound = self._load_sound_file(TVTIME_SFX_FILENAME)

        # Geekworm X1203 battery monitoring (MAX17048 at I2C 0x36)
        self.battery_bus = None
        self.battery_percent = 0.0
        self.battery_last_update = 0.0
        if X1203_AVAILABLE:
            for bus_num in (1, 0, 10, 13, 14):
                try:
                    self.battery_bus = SMBus(bus_num)
                    test_voltage = read_voltage(self.battery_bus)
                    if test_voltage > 0:
                        print(f"X1203 battery monitor detected on I2C bus {bus_num} (addr 0x{GAUGE_ADDR:02x})")
                        self._update_battery_stats(force=True)
                        break
                    self.battery_bus.close()
                    self.battery_bus = None
                except Exception:
                    if self.battery_bus:
                        try:
                            self.battery_bus.close()
                        except Exception:
                            pass
                    self.battery_bus = None
            if not self.battery_bus:
                print("Warning: X1203 battery monitor not detected on any I2C bus.")
        else:
            print("Warning: X1203 monitor module not available (missing smbus2 or x1203_monitor).")
        
        # Rolling frame-time window used for the on-screen FPS readout
        self.current_fps = 0.0
        self.frame_times = []
        self.last_frame_time = time.time()


        # HDMI1 handling
        self.hdmi1_fb_path = '/dev/fb1'
        self.hdmi1_available = os.path.exists(self.hdmi1_fb_path)
        self.hdmi1_process = None
        self.manual_hdmi1 = False
        self.fb1 = None
        if self.hdmi1_available:
            try:
                self.fb1 = Framebuffer(self.hdmi1_fb_path)
                if self.fb1.open(HDMI1_SIZE[0], HDMI1_SIZE[1]):
                    print("HDMI1 framebuffer opened for capture/write.")
                    self.manual_hdmi1 = True
                else:
                    print("Warning: Could not open HDMI1 framebuffer.")
                    self.fb1 = None
            except Exception as e:
                print(f"Warning: Failed to open HDMI1 framebuffer: {e}")
                self.fb1 = None
        
        # If we have a DRM preview, try driving HDMI1 via the same DRM device
        if self.use_gpu_preview and not self.manual_hdmi1 and self.preview:
            if self.setup_hdmi1_drm_plane():
                self.manual_hdmi1 = True
        
        # Start helper only if needed
        if not self.manual_hdmi1:
            self.start_hdmi1_helper()
        
        print("Displays initialized")
        self.init_menu_system()

    def init_menu_system(self):
        """Load menu assets and prepare default HDMI1 content."""
        media_root = os.path.join(self.project_root, 'media')
        try:
            self.media_manager = MenuManager(media_root)
            if self.media_manager.menus:
                self.menu_overlay = MenuOverlay(self.media_manager, HDMI0_SIZE)
        except Exception as exc:
            print(f"Menu system init failed: {exc}")
            self.media_manager = None
            self.menu_overlay = None
            return

        default_item = None
        if self.media_manager:
            default_item = self.media_manager.get_default_item()

        # Always start with placeholder on HDMI1/preview until the user picks content
        self.apply_placeholder_media(push=True)

        # Remember the first selectable item for reference
        if default_item:
            self.current_media_item = default_item
        else:
            self.current_media_item = None

    def apply_placeholder_media(self, push: bool = False):
        """Show Blank.jpg (or fallback placeholder) on HDMI1 and preview."""
        self.show_blank(push=push)

    def set_hdmi1_media(self, media_item: MediaItem, push: bool = False):
        """Switch HDMI1 to the specified media (image or looping video)."""
        if not media_item:
            return
        print(f"HDMI1: switching to {media_item.media_path} ({media_item.media_type})")
        self.current_media_item = media_item
        self.hdmi1_media_path = media_item.media_path
        self.dpad_down_held = False
        self.pause_tenna_talk()

        # Play TVTIME SFX once when selecting TVTIME items
        self._maybe_play_tvtime(media_item)

        if media_item.media_type == 'video':
            self.clear_slideshow_state()
            if not self.manual_hdmi1:
                print("Warning: HDMI1 video requested but we lack manual control of HDMI1.")
            if self.start_hdmi1_video(media_item.media_path):
                return
            print("HDMI1: falling back to placeholder because video playback could not start.")
            self.apply_placeholder_media(push=True)
            return
        if media_item.media_type == 'slideshow':
            self.stop_hdmi1_video()
            self.slideshow_images = list(media_item.image_sequence or [])
            if not self.slideshow_images:
                # Fallback: treat as a single image if sequence missing
                self.slideshow_images = [media_item.media_path]
            self.slideshow_cache.clear()
            self.slideshow_index = 0
            self.slideshow_last_advance = time.time()
            self._show_slideshow_frame(0, push=True)
            return

        # Static image path
        self.clear_slideshow_state()
        self.stop_hdmi1_video()
        surface = self._load_surface_from_path(media_item.media_path, HDMI1_SIZE)
        if surface is None:
            print(f"HDMI1: failed to load surface for {media_item.media_path}, keeping previous content")
            return
        self.hdmi1_content = surface
        self.last_hdmi1_surface = surface.copy()
        self.overlay_dirty = True
        if push or self.manual_hdmi1:
            self.push_hdmi1_media()

    def start_hdmi1_video(self, media_path: str):
        """Start decoding video frames for HDMI1 + preview overlay."""
        self.stop_hdmi1_video()
        # Stop music when video starts
        self.stop_music()
        decode_size = _parse_scale(HDMI1_FFMPEG_SCALE, HDMI1_SIZE)
        self._video_finished_event.clear()
        log_system_health(prefix="[video start]")
        # Lower camera framerate while external video is playing to reduce load
        self._camera_prev_fps = None
        try:
            if self.camera:
                self._camera_prev_fps = getattr(self, "camera_frame_rate", None)
                # Request ~20fps during video playback to reduce load
                self.camera.set_controls({"FrameRate": 20.0})
                self.camera_frame_rate = 20.0
        except Exception:
            pass
        # Temporarily release pygame mixer so ffmpeg can grab ALSA
        self._mixer_was_init = pygame.mixer.get_init()
        if self._mixer_was_init:
            try:
                pygame.mixer.quit()
            except Exception:
                pass
        primary_device = HDMI1_AUDIO_DEVICE.strip() if HDMI1_AUDIO_ENABLED else None
        fallback_device = HDMI1_AUDIO_DEVICE_FALLBACK.strip() if HDMI1_AUDIO_ENABLED else None
        device_candidates = []
        if primary_device:
            device_candidates.append(primary_device)
        if fallback_device and fallback_device != primary_device:
            device_candidates.append(fallback_device)
        # Only hardware devices now; still end with mute so video can play without audio
        device_candidates.append(None)
        print(f"HDMI1 video: decode size {decode_size[0]}x{decode_size[1]} (env HDMI1_FFMPEG_SCALE={HDMI1_FFMPEG_SCALE})")
        print(f"HDMI1 video: audio devices to try (in order): {device_candidates}")

        for idx, device in enumerate(device_candidates):
            label = device or "none (mute)"
            print(f"HDMI1 video: attempting audio device {idx+1}/{len(device_candidates)} -> {label}")
            player = HDMI1VideoPlayer(
                media_path,
                decode_size,
                frame_callback=self._handle_video_frame,
                audio_device=device,
                on_finished=None,
            )
            if not player.start():
                continue
            # Give ffmpeg a moment to report audio device failures
            time.sleep(0.3)
            if player.audio_failure.is_set():
                print(f"HDMI1 video: audio device '{label}' failed to open; trying next.")
                player.stop()
                self._video_finished_event.clear()
                continue
            # Success: wire finish callback now
            player.on_finished = self._on_video_finished
            self.hdmi1_video_player = player
            self.video_playing = True
            if device:
                print(f"HDMI1 video: audio active on '{device}'")
            else:
                print("HDMI1 video: running without audio (last resort).")
            return True
        # If we reach here, all devices failed
        self.apply_placeholder_media(push=True)
        self.video_playing = False
        return False

    def _on_video_finished(self):
        self._video_finished_event.set()

    def _handle_video_finished(self):
        """Return to default content when a video completes."""
        self._video_finished_event.clear()
        self.video_playing = False
        self.hdmi1_video_player = None
        
        # Check if this was ItsTVtime.mp4 and display its thumbnail
        video_path = None
        if self.current_media_item and self.current_media_item.media_path:
            video_path = self.current_media_item.media_path
        elif hasattr(self, 'hdmi1_media_path') and self.hdmi1_media_path:
            video_path = self.hdmi1_media_path
        
        # Special handling for ItsTVtime.mp4 - show its thumbnail after playback
        if video_path and "itstvtime.mp4" in os.path.basename(video_path).lower():
            # Find thumbnail in the same directory as the video
            video_dir = os.path.dirname(video_path)
            thumb_path = os.path.join(video_dir, "Thumb.jpg")
            if not os.path.exists(thumb_path):
                # Try alternative location (parent directory)
                parent_dir = os.path.dirname(video_dir)
                thumb_path = os.path.join(parent_dir, "Thumb.jpg")
            
            if os.path.exists(thumb_path):
                print(f"Displaying thumbnail for ItsTVtime.mp4: {thumb_path}")
                # Load thumbnail at full HDMI1_SIZE to avoid pixelation
                thumb_surface = self._load_surface_from_path(thumb_path, HDMI1_SIZE)
                if thumb_surface:
                    self.hdmi1_content = thumb_surface.copy()
                    self.last_hdmi1_surface = self.hdmi1_content.copy()
                    self.overlay_dirty = True
                    if self.manual_hdmi1:
                        self.push_hdmi1_media()
                    self.current_media_item = None
                    return
        
        self.current_media_item = None
        self.show_blank(push=True)
        self.overlay_dirty = True
        # Restore camera framerate after video
        try:
            if self.camera:
                target_fps = self._camera_prev_fps if self._camera_prev_fps else 56.0
                self.camera.set_controls({"FrameRate": target_fps})
                self.camera_frame_rate = target_fps
        except Exception:
            pass
        self._camera_prev_fps = None
        # Restore mixer if we released it for video playback
        if getattr(self, "_mixer_was_init", False) and not pygame.mixer.get_init():
            try:
                pygame.mixer.init()
                self.tenna_talk_sound = self._load_tenna_talk_sound()
            except Exception:
                pass
        self._mixer_was_init = False

    def _maybe_play_tvtime(self, media_item: MediaItem):
        """Play TVTIME SFX if the selected item is TVTIME-related."""
        if not self.tvtime_sound:
            return
        label = (media_item.label or "").lower()
        path = (media_item.media_path or "").lower()
        if "tvtime" in label or "tvtime" in path:
            try:
                self.tvtime_sound.play()
            except Exception as exc:
                print(f"TVTIME sound play failed: {exc}")

    def stop_hdmi1_video(self):
        if self.hdmi1_video_player:
            self.hdmi1_video_player.stop()
            self.hdmi1_video_player = None
        self.video_playing = False
        with self._hdmi1_frame_lock:
            self._hdmi1_frame_array = None
            self._hdmi1_surface_dirty = False

    def _handle_video_frame(self, frame_array: np.ndarray):
        """Receive raw RGB frame (H, W, 3) from HDMI1VideoPlayer."""
        now = time.time()
        with self._hdmi1_frame_lock:
            if (now - self._last_preview_video_update) >= HDMI1_PREVIEW_INTERVAL_VIDEO:
                self._hdmi1_frame_array = np.copy(frame_array)
                self._hdmi1_surface_dirty = True
                self._last_preview_video_update = now
            self.last_hdmi1_capture = now
        self.overlay_dirty = True
        self.present_hdmi1_frame_array(frame_array)

    def _consume_video_frame_surface(self) -> Optional[pygame.Surface]:
        """Create a pygame surface from the latest decoded video frame (main thread)."""
        if not self._hdmi1_surface_dirty:
            return None
        frame_copy = None
        with self._hdmi1_frame_lock:
            if self._hdmi1_frame_array is not None:
                frame_copy = self._hdmi1_frame_array.copy()
            self._hdmi1_surface_dirty = False
        if frame_copy is None:
            return None
        try:
            height, width, _ = frame_copy.shape
            surface = pygame.image.frombuffer(
                frame_copy.tobytes(), (width, height), "RGB"
            ).copy()
        except Exception:
            surface = pygame.surfarray.make_surface(frame_copy.swapaxes(0, 1))
        self.last_hdmi1_surface = surface
        return surface

    def present_hdmi1_frame_array(self, frame_array: np.ndarray):
        """Write RGB frame directly to HDMI1 output."""
        if self.hdmi1_drm:
            height, width, _ = frame_array.shape
            expected_w, expected_h = self.hdmi1_drm["size"]
            if (width, height) != (expected_w, expected_h):
                # Should already be scaled by ffmpeg, but guard just in case
                resized = np.zeros((expected_h, expected_w, 3), dtype=np.uint8)
                min_h = min(expected_h, height)
                min_w = min(expected_w, width)
                resized[:min_h, :min_w] = frame_array[:min_h, :min_w]
                frame_array = resized
                height, width = expected_h, expected_w
            bgrx = np.zeros((height, width, 4), dtype=np.uint8)
            bgrx[:, :, 0] = frame_array[:, :, 2]
            bgrx[:, :, 1] = frame_array[:, :, 1]
            bgrx[:, :, 2] = frame_array[:, :, 0]
            self._write_hdmi1_buffer(bgrx)
            self._commit_hdmi1_plane()
        elif self.fb1:
            try:
                self.fb1.write_image(frame_array)
            except Exception as exc:
                if not hasattr(self, '_fb1_frame_error'):
                    print(f"HDMI1 framebuffer video write failed: {exc}")
                    self._fb1_frame_error = True

    def _load_surface_from_path(self, media_path: str, target_size: Tuple[int, int]) -> Optional[pygame.Surface]:
        """Load image from disk and return pygame surface sized to HDMI1."""
        try:
            img = Image.open(media_path)
            if img.mode == 'RGBA':
                background = Image.new('RGB', img.size, (0, 0, 0))
                background.paste(img, mask=img.split()[3])
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail(target_size, Image.Resampling.LANCZOS)
            background = Image.new('RGB', target_size, 'black')
            x_offset = (target_size[0] - img.size[0]) // 2
            y_offset = (target_size[1] - img.size[1]) // 2
            background.paste(img, (x_offset, y_offset))
            surface = pygame.image.fromstring(background.tobytes(), target_size, 'RGB')
            return surface
        except Exception as exc:
            print(f"Could not load media at {media_path}: {exc}")
            return None

    def _load_blank_surface(self) -> Optional[pygame.Surface]:
        """Load and cache the Blank.jpg surface as the default fallback."""
        if self.blank_surface is not None:
            return self.blank_surface
        blank_path = os.path.join(self.project_root, 'media', BLANK_IMAGE_NAME)
        surface = self._load_surface_from_path(blank_path, HDMI1_SIZE)
        if surface:
            self.blank_surface = surface
            self.hdmi1_media_path = blank_path
            return self.blank_surface
        return None

    def show_blank(self, push: bool = False):
        """Display the default Blank.jpg (or fallback) and stop active loops."""
        self.stop_hdmi1_video()
        self.clear_slideshow_state()
        self.dpad_down_held = False
        self.pause_tenna_talk()
        surface = self._load_blank_surface()
        if surface is None:
            surface = self.generate_hdmi1_content()
        if surface is None:
            return
        self.hdmi1_content = surface.copy()
        self.last_hdmi1_surface = self.hdmi1_content.copy()
        self.current_media_item = None
        self.overlay_dirty = True
        if push or self.manual_hdmi1:
            self.push_hdmi1_media()

    def _load_tenna_talk_sound(self):
        """Try to load TennaTalk.wav; return None if unavailable."""
        if not pygame.mixer.get_init():
            return None
        return self._load_sound_file(TENNA_TALK_FILENAME)

    def _load_sound_file(self, filename: str):
        """Load a sound file from media directory if available."""
        path = os.path.join(self.project_root, 'media', filename)
        if not os.path.exists(path):
            return None
        try:
            return pygame.mixer.Sound(path)
        except Exception as exc:
            print(f"Warning: Could not load audio {filename}: {exc}")
            return None

    def start_tenna_talk(self):
        """Start or resume looping TennaTalk audio while held."""
        if not self.tenna_talk_sound or not pygame.mixer.get_init():
            return
        if self.tenna_talk_channel and self.tenna_talk_channel.get_busy():
            self.tenna_talk_channel.unpause()
            return
        try:
            self.tenna_talk_channel = self.tenna_talk_sound.play(loops=-1)
        except Exception as exc:
            if not hasattr(self, "_tenna_talk_warned"):
                print(f"Warning: Could not start TennaTalk playback: {exc}")
                self._tenna_talk_warned = True

    def pause_tenna_talk(self):
        """Pause TennaTalk playback (keep position for fast resume)."""
        if self.tenna_talk_channel:
            try:
                self.tenna_talk_channel.pause()
            except Exception:
                try:
                    self.tenna_talk_channel.stop()
                except Exception:
                    pass
    
    def play_music(self, music_path: str):
        """Play a music file. Stops any currently playing music."""
        if not pygame.mixer.get_init():
            print("Warning: Mixer not initialized, cannot play music")
            return
        
        # Stop previous music if playing
        self.stop_music()
        
        try:
            self.music_sound = pygame.mixer.Sound(music_path)
            # Use channel 1 for music (channel 0 is for TennaTalk)
            if pygame.mixer.get_num_channels() > 1:
                self.music_channel = pygame.mixer.Channel(1)
            else:
                # Fallback: use any available channel
                self.music_channel = pygame.mixer.find_channel(force=True)
            
            if self.music_channel:
                self.music_channel.play(self.music_sound, loops=-1)  # Loop music
                print(f"Playing music: {music_path}")
            else:
                print("Warning: No available channel for music playback")
        except Exception as e:
            print(f"Error playing music {music_path}: {e}")
            self.music_sound = None
            self.music_channel = None
    
    def stop_music(self):
        """Stop currently playing music."""
        if self.music_channel:
            try:
                self.music_channel.stop()
            except:
                pass
            self.music_channel = None
        self.music_sound = None

    def _render_text_with_stroke(self, font, text: str, text_color=(255, 255, 255), stroke_color=(0, 0, 0), stroke_width=2):
        """Render text with white color and black stroke for better visibility."""
        stroke_surfaces = []
        for dx in range(-stroke_width, stroke_width + 1):
            for dy in range(-stroke_width, stroke_width + 1):
                if dx != 0 or dy != 0:
                    stroke_surf = font.render(text, True, stroke_color)
                    stroke_surfaces.append((stroke_surf, (dx, dy)))
        text_surface = font.render(text, True, text_color)
        w = text_surface.get_width() + stroke_width * 2
        h = text_surface.get_height() + stroke_width * 2
        final_surface = pygame.Surface((w, h), pygame.SRCALPHA)
        for stroke_surf, (dx, dy) in stroke_surfaces:
            final_surface.blit(stroke_surf, (stroke_width + dx, stroke_width + dy))
        final_surface.blit(text_surface, (stroke_width, stroke_width))
        return final_surface

    def _update_battery_stats(self, force=False):
        """Update battery percentage from X1203 (MAX17048) every 10s."""
        if not self.battery_bus:
            return
        now = time.time()
        if not force and (now - self.battery_last_update) < 10.0:
            return
        try:
            self.battery_percent = read_soc(self.battery_bus)
            self.battery_last_update = now
        except Exception:
            pass

    def clear_slideshow_state(self):
        """Reset slideshow tracking when switching to non-slideshow media."""
        self.slideshow_images = []
        self.slideshow_index = 0
        self.slideshow_last_advance = 0.0
        self.slideshow_cache.clear()

    def _show_slideshow_frame(self, index: int, push: bool = False):
        """Display a specific frame from the active slideshow."""
        if not self.slideshow_images:
            return
        index = index % len(self.slideshow_images)
        path = self.slideshow_images[index]
        surface = self.slideshow_cache.get(path)
        if surface is None:
            surface = self._load_surface_from_path(path, HDMI1_SIZE)
            if surface:
                self.slideshow_cache[path] = surface
        if surface is None:
            return
        self.slideshow_index = index
        self.hdmi1_media_path = path
        self.hdmi1_content = surface
        self.last_hdmi1_surface = surface.copy()
        self.overlay_dirty = True
        if push or self.manual_hdmi1:
            self.push_hdmi1_media()

    def reset_slideshow_to_first(self, push: bool = False):
        """Return slideshow to its first image and pause cycling."""
        if not self.slideshow_images:
            return
        self.slideshow_last_advance = time.time()
        self._show_slideshow_frame(0, push=push)

    def advance_slideshow(self, push: bool = False):
        """Advance to the next image in the slideshow."""
        if not self.slideshow_images:
            return
        if len(self.slideshow_images) == 1:
            return
        next_index = (self.slideshow_index + 1) % len(self.slideshow_images)
        self._show_slideshow_frame(next_index, push=push)
        
    def generate_hdmi1_content(self):
        """Last-resort HDMI1 content: the blank plate, or a marked black frame."""
        media_dir = os.path.join(self.project_root, 'media')
        for name in (BLANK_IMAGE_NAME, 'placeholder2.png'):
            path = os.path.join(media_dir, name)
            if not os.path.exists(path):
                continue
            surface = self._load_surface_from_path(path, HDMI1_SIZE)
            if surface is not None:
                self.hdmi1_media_path = path
                return surface

        # Nothing loadable on disk: a red-bordered black frame makes that obvious.
        print(f"Warning: no usable HDMI1 placeholder found in {media_dir}")
        img = Image.new('RGB', HDMI1_SIZE, color='black')
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, HDMI1_SIZE[0] - 1, HDMI1_SIZE[1] - 1], outline='red', width=5)
        return pygame.image.fromstring(img.tobytes(), HDMI1_SIZE, 'RGB')

    def push_hdmi1_media(self):
        """Ensure HDMI1 shows the static media when we're managing it directly."""
        if not self.manual_hdmi1 or self.hdmi1_content is None:
            return
        if self.hdmi1_drm:
            self.present_hdmi1_drm(self.hdmi1_content)
            return
        if not self.fb1:
            return
        try:
            img_array = pygame.surfarray.array3d(self.hdmi1_content).swapaxes(0, 1)
            self.fb1.write_image(img_array)
            self.last_hdmi1_surface = self.hdmi1_content.copy()
            self.last_hdmi1_capture = time.time()
        except Exception as e:
            print(f"HDMI1 framebuffer update failed: {e}")
    
    def capture_hdmi1_surface(self, force=False):
        """Grab the latest HDMI1 framebuffer image as a pygame surface."""
        video_surface = self._consume_video_frame_surface()
        if video_surface is not None:
            return video_surface
        if not self.manual_hdmi1:
            return None
        if self.hdmi1_drm:
            surface = self._read_hdmi1_drm_surface()
            if surface is not None:
                self.last_hdmi1_surface = surface
                self.last_hdmi1_capture = time.time()
                return surface
            return self.last_hdmi1_surface
        if not self.fb1:
            return None
        now = time.time()
        if (not force and self.last_hdmi1_surface is not None and
                (now - self.last_hdmi1_capture) < HDMI1_CAPTURE_INTERVAL):
            return self.last_hdmi1_surface
        
        frame = self.fb1.read_image()
        if frame is None:
            return self.last_hdmi1_surface
        
        try:
            surface = pygame.surfarray.make_surface(frame.swapaxes(0, 1))
            self.last_hdmi1_surface = surface
            self.last_hdmi1_capture = now
            return surface
        except Exception as e:
            if not hasattr(self, '_hdmi1_capture_warned'):
                print(f"HDMI1 capture failed: {e}")
                self._hdmi1_capture_warned = True
            return self.last_hdmi1_surface

    def setup_hdmi1_drm_plane(self):
        """Reserve DRM resources for HDMI1 using the existing DrmPreview card."""
        if not self.preview:
            return False
        card = getattr(self.preview, "card", None)
        resman = getattr(self.preview, "resman", None)
        if card is None or resman is None:
            print("GPU preview card/resources unavailable; cannot attach HDMI1 plane.")
            return False
        try:
            connector = resman.reserve_connector(HDMI1_DRM_CONNECTOR)
        except Exception as e:
            print(f"Could not reserve DRM connector {HDMI1_DRM_CONNECTOR}: {e}")
            return False
        if connector is None:
            print(f"DRM connector {HDMI1_DRM_CONNECTOR} unavailable.")
            return False
        if not connector.connected():
            print(f"Connector {HDMI1_DRM_CONNECTOR} is not connected.")
            return False
        crtc = resman.reserve_crtc(connector)
        if crtc is None:
            print("Could not reserve CRTC for HDMI1.")
            return False
        mode = connector.get_default_mode()
        plane = (
            resman.reserve_primary_plane(crtc, format=pykms.PixelFormat.XRGB8888)
            or resman.reserve_plane(
                crtc,
                type=pykms.PlaneType.Primary,
                format=pykms.PixelFormat.XRGB8888,
            )
        )
        if plane is None:
            print("Could not reserve primary DRM plane for HDMI1.")
            return False
        try:
            fb = pykms.DumbFramebuffer(
                card, mode.hdisplay, mode.vdisplay, "XR24"
            )
            fb_map = fb.map(0).cast("B")
        except Exception as e:
            print(f"Failed to allocate DRM framebuffer for HDMI1: {e}")
            return False
        mode_blob = mode.to_blob(card)
        state = {
            "card": card,
            "resman": resman,
            "connector": connector,
            "crtc": crtc,
            "mode": mode,
            "mode_blob": mode_blob,
            "plane": plane,
            "fb": fb,
            "map": fb_map,
            "configured": False,
            "size": (mode.hdisplay, mode.vdisplay),
        }
        self.hdmi1_drm = state
        print(
            f"HDMI1 DRM plane ready on {HDMI1_DRM_CONNECTOR} "
            f"({mode.hdisplay}x{mode.vdisplay})"
        )
        self._prime_hdmi1_plane()
        self.try_setup_writeback_capture()
        return True

    def _prime_hdmi1_plane(self):
        """Blank HDMI1 plane so the connector leaves standby immediately."""
        if not self.hdmi1_drm:
            return
        width, height = self.hdmi1_drm["size"]
        blank = np.zeros((height, width, 4), dtype=np.uint8)
        self._write_hdmi1_buffer(blank)
        self._commit_hdmi1_plane(include_modeset=True)

    def _write_hdmi1_buffer(self, bgrx_array):
        """Copy a BGRA/X frame into the DRM dumb framebuffer."""
        if not self.hdmi1_drm:
            return
        width, height = self.hdmi1_drm["size"]
        expected_shape = (height, width, 4)
        data = np.asarray(bgrx_array, dtype=np.uint8)
        if data.shape != expected_shape:
            raise ValueError(
                f"HDMI1 buffer shape mismatch: got {data.shape}, expected {expected_shape}"
            )
        mv = self.hdmi1_drm["map"]
        stride = self.hdmi1_drm["fb"].stride(0)
        row_bytes = width * 4
        if stride == row_bytes:
            mv[:data.nbytes] = np.ascontiguousarray(data, dtype=np.uint8).tobytes()
        else:
            row_view = memoryview(mv)
            for y in range(height):
                src = np.ascontiguousarray(data[y], dtype=np.uint8).tobytes()
                start = y * stride
                row_view[start:start + row_bytes] = src
        try:
            self.hdmi1_drm["fb"].flush()
        except Exception:
            pass

    def _commit_hdmi1_plane(self, include_modeset=False):
        """Issue an atomic commit for the HDMI1 plane (optionally with modeset)."""
        if not self.hdmi1_drm:
            return
        state = self.hdmi1_drm
        width, height = state["size"]
        ctx = pykms.AtomicReq(state["card"])
        if include_modeset or not state.get("configured", False):
            ctx.add_connector(state["connector"], state["crtc"])
            ctx.add_crtc(state["crtc"], state["mode_blob"])
        ctx.add_plane(
            state["plane"],
            state["fb"],
            state["crtc"],
            (0, 0, width, height),
            (0, 0, width, height),
        )
        try:
            ctx.commit_sync(include_modeset or not state.get("configured", False))
            state["configured"] = True
        except Exception as e:
            if not hasattr(self, "_hdmi1_commit_warned"):
                print(f"HDMI1 DRM commit failed: {e}")
                self._hdmi1_commit_warned = True
    
    def _read_hdmi1_drm_surface(self):
        """Read the current HDMI1 framebuffer into a pygame surface."""
        if not self.hdmi1_drm:
            return None
        width, height = self.hdmi1_drm["size"]
        mv = self.hdmi1_drm["map"]
        stride = self.hdmi1_drm["fb"].stride(0)
        row_bytes = width * 4
        try:
            buf = np.frombuffer(mv, dtype=np.uint8, count=stride * height)
            if stride == row_bytes:
                np_frame = buf.reshape((height, width, 4))
            else:
                np_frame = np.zeros((height, width, 4), dtype=np.uint8)
                for y in range(height):
                    start = y * stride
                    row = buf[start:start + row_bytes]
                    np_frame[y] = row.reshape((width, 4))
        except Exception as e:
            if not hasattr(self, "_hdmi1_capture_warned"):
                print(f"HDMI1 DRM capture failed: {e}")
                self._hdmi1_capture_warned = True
            return None
        rgb = np_frame[:, :, :3][:, :, ::-1].copy()
        try:
            surface = pygame.image.frombuffer(
                rgb.tobytes(), (width, height), "RGB"
            )
        except Exception:
            surface = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))
        return surface

    def try_setup_writeback_capture(self):
        """Attempt to locate a DRM writeback connector for HDMI1 mirroring."""
        if not self.preview or not hasattr(self.preview, "card"):
            return False
        card = self.preview.card
        writeback_connector = None
        for connector in getattr(card, "connectors", []):
            name = getattr(connector, "fullname", "")
            if name and "writeback" in name.lower():
                writeback_connector = connector
                break
        if writeback_connector is None:
            if not self._writeback_warned:
                print(
                    "Warning: No DRM writeback connector advertised on this GPU; "
                    "falling back to shared framebuffer reads for HDMI1 mirroring."
                )
                self._writeback_warned = True
            return False
        self.hdmi1_writeback = writeback_connector
        print(f"DRM writeback connector ready: {writeback_connector.fullname}")
        return True
    
    def present_hdmi1_drm(self, surface):
        """Present a pygame surface on HDMI1 via DRM."""
        if not self.hdmi1_drm or surface is None:
            return
        width, height = self.hdmi1_drm["size"]
        if surface.get_size() != (width, height):
            scaled = pygame.transform.smoothscale(surface, (width, height))
        else:
            scaled = surface
        rgb = pygame.surfarray.array3d(scaled).swapaxes(0, 1)
        bgrx = np.zeros((height, width, 4), dtype=np.uint8)
        bgrx[:, :, 0] = rgb[:, :, 2]
        bgrx[:, :, 1] = rgb[:, :, 1]
        bgrx[:, :, 2] = rgb[:, :, 0]
        self._write_hdmi1_buffer(bgrx)
        self._commit_hdmi1_plane()
        self.last_hdmi1_surface = scaled.copy()
        self.last_hdmi1_capture = time.time()
    
    def start_hdmi1_helper(self):
        """Launch fallback helper process to drive HDMI1 if needed."""
        script_dir = os.path.dirname(__file__)
        helper_candidates = []
        if self.use_gpu_preview:
            helper_candidates.append(os.path.join(script_dir, 'hdmi1_drm.py'))
        helper_candidates.append(os.path.join(script_dir, 'hdmi1_output.py'))
        for helper in helper_candidates:
            if not os.path.exists(helper):
                continue
            try:
                env = os.environ.copy()
                if getattr(self, 'hdmi1_media_path', None):
                    env['HDMI1_MEDIA_PATH'] = str(self.hdmi1_media_path)
                env.setdefault('PYTHONUNBUFFERED', '1')
                self.hdmi1_process = subprocess.Popen(
                    [sys.executable, helper],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env
                )
                print(f"Started HDMI1 helper ({os.path.basename(helper)}) PID {self.hdmi1_process.pid}")
                time.sleep(0.5)
                if self.hdmi1_process.poll() is None:
                    return
                output, _ = self.hdmi1_process.communicate()
                print(f"HDMI1 helper exited with code {self.hdmi1_process.returncode}")
                print(f"HDMI1 helper output:\n{output}")
                self.hdmi1_process = None
            except Exception as e:
                print(f"Could not start HDMI1 helper {helper}: {e}")
                import traceback
                traceback.print_exc()
        if self.hdmi1_process is None:
            print("Warning: No HDMI1 helper could be started; HDMI1 may remain blank.")
    
    def _fit_overlay(self, source: pygame.Surface) -> Tuple[pygame.Surface, Tuple[int, int]]:
        """Scale the HDMI1 preview into its corner slot and return it with its position."""
        src_w, src_h = source.get_size()
        aspect_ratio = (src_h / src_w) if src_w else 1.0

        width = min(OVERLAY_MAX_WIDTH, HDMI0_SIZE[0] - 2 * OVERLAY_MARGIN)
        height = int(width * aspect_ratio)
        max_height = HDMI0_SIZE[1] - 2 * OVERLAY_MARGIN
        if height > max_height and aspect_ratio:
            height = max_height
            width = int(height / aspect_ratio)
        width = max(1, width)
        height = max(1, height)

        if OVERLAY_POSITION in ('left', 'top-left', 'bottom-left'):
            x = OVERLAY_MARGIN
        else:
            x = HDMI0_SIZE[0] - width - OVERLAY_MARGIN
        if OVERLAY_POSITION in ('top-left', 'top-right'):
            y = OVERLAY_MARGIN
        else:
            y = HDMI0_SIZE[1] - height - OVERLAY_MARGIN
        x = max(0, min(x, HDMI0_SIZE[0] - width))
        y = max(0, min(y, HDMI0_SIZE[1] - height))

        return pygame.transform.scale(source, (width, height)), (x, y)

    def _draw_status_text(self, target: pygame.Surface):
        """Draw the FPS and battery readout in the top-left corner."""
        font = getattr(self, 'font', None)
        if not font:
            return
        lines = [f"FPS: {self.current_fps:.1f}"]
        if self.battery_bus:
            lines.append(f"BAT: {self.battery_percent:.1f}%")
        else:
            lines.append("BAT: N/A (I2C)")
        y_offset = OVERLAY_MARGIN
        for line in lines:
            text_surface = self._render_text_with_stroke(font, line)
            target.blit(text_surface, (OVERLAY_MARGIN, y_offset))
            y_offset += text_surface.get_height() + 5

    def get_overlay_source_surface(self, force=False):
        """Return the surface to use for the HDMI1 preview overlay."""
        # During video playback, keep the overlay light by showing the current item's thumbnail if available.
        if self.video_playing and self.current_media_item and self.current_media_item.thumb_surface:
            return self.current_media_item.thumb_surface
        video_surface = self._consume_video_frame_surface()
        if video_surface is not None:
            return video_surface
        surface = self.capture_hdmi1_surface(force=force)
        if surface is not None:
            return surface
        return self.hdmi1_content
    
    def process_events(self):
        """Handle input events from the controller or the local terminal."""
        if self.input_handler:
            input_event = self.input_handler.get_input(timeout=0)
            if input_event:
                input_type, value = input_event
                if input_type == 'controller':
                    event_type, payload = value
                    if event_type == 'button':
                        self.handle_controller_button(payload)
                    elif event_type == 'dpad':
                        pressed = True
                        if isinstance(payload, dict):
                            direction = rotate_direction(payload.get('direction', ''))
                            pressed = bool(payload.get('pressed', True))
                        else:
                            direction = rotate_direction(payload)
                        self.handle_controller_direction(direction, pressed=pressed)
                elif input_type == 'keyboard':
                    if value in ('\x1b', 'q'):
                        self.running = False
        else:
            self._poll_stdin_keys()
        if self.input_handler:
            self._poll_stdin_keys()

        # Handle finished video (non-looping) if flagged by player thread
        if self._video_finished_event.is_set():
            self._handle_video_finished()

    def handle_controller_button(self, button: str):
        """Map controller face buttons to menu overlays."""
        button = (button or '').upper()
        if button == 'START':
            self.adjust_volume(+HDMI1_VOLUME_STEP)
            return
        if button == 'SELECT':
            self.adjust_volume(-HDMI1_VOLUME_STEP)
            return
        menu_name = MENU_BUTTON_MAP.get(button)
        if menu_name and self.menu_overlay and self.media_manager and self.media_manager.has_menu(menu_name):
            self.menu_overlay.toggle(menu_name)
            if self.menu_overlay.visible:
                self.end_down_hold(reset_slideshow=True)
            self.overlay_dirty = True

    def handle_controller_direction(self, direction: str, pressed: bool = True):
        """Handle rotated DPAD input for selecting menu thumbnails or direct actions."""
        direction = (direction or '').lower()
        if not direction:
            return
        # If a menu is visible, DPAD behaves as a selector (press only)
        if self.menu_overlay and self.menu_overlay.visible:
            if not pressed:
                return
            item = self.menu_overlay.select(direction)
            if item:
                # If it's a music file, play it and close menus
                if item.media_type == 'music':
                    self.play_music(item.media_path)
                    self.menu_overlay.deactivate()
                    self.overlay_dirty = True
                    self.end_down_hold(reset_slideshow=False)
                else:
                    # For other media types, use normal handling
                    self.set_hdmi1_media(item)
                    self.menu_overlay.deactivate()
                    self.overlay_dirty = True
                    self.end_down_hold(reset_slideshow=False)
            return

        # Menu not visible: direct DPAD actions
        if direction == 'up' and pressed:
            self.end_down_hold(reset_slideshow=True)
            self.show_blank(push=True)
            return
        
        # Stop music on left dpad (remember rotation is already applied)
        if direction == 'left' and pressed:
            self.stop_music()
            return

        if direction == 'down':
            if pressed:
                self.begin_down_hold()
            else:
                self.end_down_hold(reset_slideshow=True)

    def begin_down_hold(self):
        """Mark DPAD down as held and kick off audio/animation."""
        if self.menu_overlay and self.menu_overlay.visible:
            return
        self.dpad_down_held = True
        self.slideshow_last_advance = time.time()
        self.start_tenna_talk()

    def end_down_hold(self, reset_slideshow: bool = True):
        """Stop DPAD hold behaviors and optionally reset slideshow."""
        if not self.dpad_down_held and not reset_slideshow:
            return
        self.dpad_down_held = False
        self.pause_tenna_talk()
        if reset_slideshow and self.current_media_item and self.current_media_item.media_type == 'slideshow':
            self.reset_slideshow_to_first(push=True)

    def update_hold_actions(self):
        """Advance slideshow and audio while DPAD down is held."""
        if self.menu_overlay and self.menu_overlay.visible:
            if self.dpad_down_held:
                self.end_down_hold(reset_slideshow=True)
            return
        if not self.dpad_down_held:
            return
        self.start_tenna_talk()
        if not (self.current_media_item and self.current_media_item.media_type == 'slideshow'):
            return
        if len(self.slideshow_images) <= 1:
            return
        now = time.time()
        if (now - self.slideshow_last_advance) >= SLIDESHOW_ADVANCE_SECONDS:
            self.advance_slideshow(push=True)
            self.slideshow_last_advance = now
    
    def _poll_stdin_keys(self):
        """Allow ESC/Q from the local terminal to trigger cleanup."""
        try:
            import sys
            import select
            if not sys.stdin or not sys.stdin.isatty():
                return
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                return
            key = sys.stdin.read(1)
            if key in ('\x1b', 'q'):
                print('Keyboard exit requested; shutting down...')
                self.running = False
        except Exception:
            pass

    def run(self):
        """Main loop"""
        print("Starting main loop...")
        self.running = True
        log_system_health(prefix="[startup]")

        if self.hdmi1_content is None and not self.current_media_item:
            # Final fallback if no menu assets were available
            self.hdmi1_content = self.generate_hdmi1_content()
            self.push_hdmi1_media()
        print(f"HDMI1 preview: {OVERLAY_POSITION}, max width {OVERLAY_MAX_WIDTH}px")
        self.overlay_dirty = True

        if self.use_gpu_preview:
            self.run_gpu_preview()
            return

        clock = pygame.time.Clock()
        error_font = pygame.font.Font(None, 120)

        try:
            while self.running:
                self.process_events()
                self.update_hold_actions()
                if self._video_finished_event.is_set():
                    self._handle_video_finished()

                hdmi0_composite = pygame.Surface(HDMI0_SIZE)
                hdmi0_composite.fill((0, 0, 0))

                if not getattr(self, 'camera_available', False) or self.camera is None:
                    error_surface = error_font.render("CAMERA NOT CONNECTED", True, (255, 0, 0))
                    text_rect = error_surface.get_rect(
                        center=(HDMI0_SIZE[0] // 2, HDMI0_SIZE[1] // 2)
                    )
                    hdmi0_composite.blit(error_surface, text_rect)
                else:
                    try:
                        camera_array = self.camera.capture_array()
                    except Exception as e:
                        print(f"Error capturing camera frame: {e}")
                        camera_array = None

                    if camera_array is not None:
                        if camera_array.ndim != 3 or camera_array.shape[2] != 3:
                            print(f"Warning: unexpected camera array shape {camera_array.shape}")

                        # picamera2's RGB888 output is already RGB ordered.
                        if SWAP_RGB_CHANNELS:
                            camera_array = camera_array[:, :, ::-1]

                        h, w = camera_array.shape[:2]
                        new_w = HDMI0_SIZE[0]
                        new_h = int(h * (HDMI0_SIZE[0] / w))
                        camera_surface = pygame.surfarray.make_surface(camera_array.swapaxes(0, 1))
                        if (new_w, new_h) != (w, h):
                            camera_surface = pygame.transform.scale(camera_surface, (new_w, new_h))

                        # Full width, centred vertically if the aspect is not an exact match.
                        hdmi0_composite.blit(camera_surface, (0, (HDMI0_SIZE[1] - new_h) // 2))

                current_time = time.time()
                self.frame_times.append(current_time - self.last_frame_time)
                self.last_frame_time = current_time
                if len(self.frame_times) > 30:
                    self.frame_times.pop(0)
                avg_frame_time = sum(self.frame_times) / len(self.frame_times)
                self.current_fps = 1.0 / avg_frame_time if avg_frame_time > 0 else 0.0

                self._update_battery_stats()
                self._draw_status_text(hdmi0_composite)

                overlay_source = self.get_overlay_source_surface()
                if overlay_source is None:
                    overlay_source = self.hdmi1_content
                if overlay_source is not None:
                    scaled, position = self._fit_overlay(overlay_source)
                    hdmi0_composite.blit(scaled, position)

                if self.menu_overlay and self.menu_overlay.visible:
                    self.menu_overlay.draw(hdmi0_composite)

                if self.fb:
                    composite_array = np.swapaxes(pygame.surfarray.array3d(hdmi0_composite), 0, 1)
                    self.fb.write_image(composite_array)

                clock.tick(60)

        except KeyboardInterrupt:
            print("\nInterrupted by user")
        finally:
            self.cleanup()
    
    def run_gpu_preview(self):
        """Main loop when using DRM zero-copy preview."""
        print("Running GPU preview mode (DRM zero-copy).")
        try:
            self.overlay_dirty = True
            self.update_gpu_overlay(force=True)
            self.push_hdmi1_media()
            while self.running:
                self.process_events()
                self.update_hold_actions()
                if self._video_finished_event.is_set():
                    self._handle_video_finished()
                now = time.time()
                if self.overlay_dirty or (now - self.overlay_last_update) >= GPU_OVERLAY_UPDATE_INTERVAL:
                    self.update_gpu_overlay()
                time.sleep(0.01)
        except KeyboardInterrupt:
            print("\nInterrupted by user")
        finally:
            self.cleanup()
    
    def update_gpu_overlay(self, force=False):
        """Build and push overlay to DRM plane (GPU preview mode)."""
        if not self.use_gpu_preview or not self.preview:
            return

        now = time.time()
        if not force and (now - self.overlay_last_update) < GPU_OVERLAY_UPDATE_INTERVAL and not self.overlay_dirty:
            return
        
        # During video playback, update overlays less often to reduce load
        if self.video_playing and not force:
            if (now - self.overlay_last_update) < max(GPU_OVERLAY_UPDATE_INTERVAL, 0.25):
                return

        overlay_source = self.get_overlay_source_surface(force=True)
        if overlay_source is None:
            overlay_source = self.hdmi1_content
        if overlay_source is None:
            return
        
        self.overlay_last_update = now

        overlay_surface = pygame.Surface(HDMI0_SIZE, pygame.SRCALPHA)
        scaled, position = self._fit_overlay(overlay_source)
        overlay_surface.blit(scaled, position)
        self._update_battery_stats()
        self._draw_status_text(overlay_surface)

        if self.menu_overlay and self.menu_overlay.visible:
            self.menu_overlay.draw(overlay_surface)

        # Show volume overlay briefly after adjustments
        if self._volume_display_until > time.time():
            vol_text = "VOL"
            if self._volume_last_text:
                vol_text = f"VOL {self._volume_last_text}"
            elif self._volume_last_db is not None:
                vol_text = f"VOL {self._volume_last_db}"
            font = getattr(self, "font", None)
            if font:
                txt_surface = font.render(vol_text, True, (255, 255, 255))
                bg_rect = txt_surface.get_rect()
                bg_rect.inflate_ip(20, 12)
                # Place below FPS/BAT block
                bg_rect.topleft = (10, 110)
                bg = pygame.Surface(bg_rect.size, pygame.SRCALPHA)
                bg.fill((0, 0, 0, 180))
                overlay_surface.blit(bg, bg_rect.topleft)
                overlay_surface.blit(txt_surface, (bg_rect.left + 10, bg_rect.top + 6))

        overlay_bytes = pygame.image.tostring(overlay_surface, 'RGBA')
        overlay_array = np.frombuffer(overlay_bytes, dtype=np.uint8).reshape(
            (HDMI0_SIZE[1], HDMI0_SIZE[0], 4)
        )

        # Channel order already matches what the plane expects; the copy is only
        # to get a writable buffer, and alpha is preserved so the camera shows through.
        try:
            self.preview.set_overlay(overlay_array.copy())
            self.overlay_dirty = False
        except Exception as e:
            if not hasattr(self, '_overlay_error_logged'):
                print(f"Overlay update failed: {e}")
                self._overlay_error_logged = True
            self.overlay_dirty = False
    
    def cleanup(self):
        """Clean up resources"""
        print("Cleaning up...")
        self.running = False
        
        self.stop_hdmi1_video()

        # Stop input handler
        if self.input_handler:
            self.input_handler.stop()
        
        # Stop HDMI1 process if running
        if self.hdmi1_process:
            try:
                self.hdmi1_process.terminate()
                self.hdmi1_process.wait(timeout=2)
            except:
                self.hdmi1_process.kill()
        
        if self.preview:
            try:
                self.preview.stop()
            except:
                pass
            self.preview = None
        if self.hdmi1_drm:
            try:
                self.hdmi1_drm["crtc"].disable_mode()
            except Exception:
                pass
            try:
                self.hdmi1_drm["map"].release()
            except Exception:
                pass
            self.hdmi1_drm = None
        if self.fb1:
            self.fb1.close()
            self.fb1 = None
        if self.camera:
            self.camera.stop()
            self.camera.close()
        if self.fb:
            self.fb.close()
        pygame.quit()
        print("Cleanup complete")

    def adjust_volume(self, delta_percent: float):
        """Adjust HDMI1 output volume via amixer; log if unsupported."""
        if not self.volume_supported:
            return
        if not shutil.which("amixer"):
            print("Volume control requires 'amixer' (sudo apt install alsa-utils).")
            return
        step = abs(delta_percent)
        step_str = f"{step:.1f}%{'+' if delta_percent > 0 else '-'}"
        control_name = HDMI1_VOLUME_CONTROL
        rel = f"{step:.1f}%{'+' if delta_percent > 0 else '-'}"
        cmds = [
            ["amixer", "-D", HDMI1_AUDIO_DEVICE, "sset", control_name, rel],
            ["amixer", "-c", HDMI1_VOLUME_CARD, "sset", control_name, rel],
        ]
        try:
            self._volume_last_text = step_str
            for cmd in cmds:
                print(f"Volume: trying {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if result.returncode == 0:
                    # Try to read back the dB value
                    db_val = None
                    pct_val = None
                    try:
                        grep = subprocess.run(
                            ["amixer", "-D", HDMI1_AUDIO_DEVICE, "cget", f"name={control_name}"],
                            capture_output=True, text=True, check=False
                        )
                        if grep.returncode == 0:
                            out = grep.stdout
                            import re
                            m_pct = re.search(r"\[([0-9]+)%\]", out)
                            if m_pct:
                                db_val = m_pct.group(1) + "%"
                            else:
                                m_db = re.search(r"\[([-\d\.]+) dB\]", out)
                                if m_db:
                                    db_val = m_db.group(1)
                        else:
                            # Try card-based readback if device read fails
                            grep = subprocess.run(
                                ["amixer", "-c", HDMI1_VOLUME_CARD, "cget", f"name={control_name}"],
                                capture_output=True, text=True, check=False
                            )
                            if grep.returncode == 0:
                                import re
                                m_pct = re.search(r"\[([0-9]+)%\]", grep.stdout)
                                if m_pct:
                                    db_val = m_pct.group(1) + "%"
                                else:
                                    m_db = re.search(r"\[([-\d\.]+) dB\]", grep.stdout)
                                    if m_db:
                                        db_val = m_db.group(1)
                                if db_val is None:
                                    m_vals = re.search(r"values=([0-9]+),", grep.stdout)
                                    m_max = re.search(r"max=([0-9]+)", grep.stdout)
                                    if m_vals and m_max:
                                        try:
                                            val = int(m_vals.group(1))
                                            vmax = int(m_max.group(1))
                                            pct_val = f"{int(val * 100 / max(vmax,1))}%"
                                        except Exception:
                                            pass
                    except Exception:
                        pass
                    self._volume_last_db = db_val or pct_val
                    if db_val or pct_val:
                        self._volume_last_text = db_val or pct_val
                    self._volume_display_until = time.time() + VOLUME_DISPLAY_SECONDS
                    print("Volume: success")
                    return
            message = result.stderr.strip() or result.stdout.strip() or "unknown error"
            print(f"Volume adjustment not supported on this HDMI card: {message}")
            print("Consider using a softvol plugin or external volume control.")
            self.volume_supported = False
        except Exception as exc:
            print(f"Volume adjustment error: {exc}")
            self.volume_supported = False


def main():
    """Main entry point"""
    print("PiTenna - Dual Display Camera Overlay System")
    print("=" * 50)
    
    # Boost CPU/GPU priority for better performance
    boost_process_priority()
    
    try:
        app = DualDisplayCamera()
        app.run()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

