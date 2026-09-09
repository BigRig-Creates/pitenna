#!/usr/bin/env python3
"""
Minimal DRM presenter for HDMI1.
Loads the image given by HDMI1_MEDIA_PATH (default media/Blank.jpg) and
presents it on connector HDMI-A-2 using /dev/dri/card1. Used as a fallback
on Raspberry Pi 5 dual-HDMI setups that expose no /dev/fb1.
"""
import os
import sys
import time
import signal
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import pykms
except ImportError as exc:
    print(f"pykms import failed: {exc}", file=sys.stderr)
    sys.exit(1)


CARD_PATH = os.environ.get("HDMI1_DRM_CARD", "/dev/dri/card1")
CONNECTOR_NAME = os.environ.get("HDMI1_DRM_CONNECTOR", "HDMI-A-2")
MEDIA_PATH = os.environ.get("HDMI1_MEDIA_PATH")
if not MEDIA_PATH:
    media_dir = Path(__file__).resolve().parent.parent / "media"
    MEDIA_PATH = media_dir / "Blank.jpg"
    if not MEDIA_PATH.exists():
        MEDIA_PATH = media_dir / "placeholder2.png"
MEDIA_PATH = Path(MEDIA_PATH)


def find_connector(card, name):
    for conn in card.connectors:
        if conn.fullname == name:
            return conn
    return None


def pick_crtc(card, connector):
    current = connector.get_current_crtc()
    if current:
        return current
    mask = connector.get_possible_crtcs()
    for crtc in card.crtcs:
        if mask & (1 << crtc.idx):
            return crtc
    raise RuntimeError("No compatible CRTC found for connector {}".format(connector.fullname))


def load_image(target_size):
    if not MEDIA_PATH.exists():
        raise FileNotFoundError(f"Media file not found: {MEDIA_PATH}")
    img = Image.open(MEDIA_PATH)
    if img.mode != "RGB":
        img = img.convert("RGB")
    width, height = target_size
    canvas = Image.new("RGB", (width, height), color="black")
    img.thumbnail((width, height), Image.Resampling.LANCZOS)
    x = (width - img.width) // 2
    y = (height - img.height) // 2
    canvas.paste(img, (x, y))
    return np.array(canvas, dtype=np.uint8)


def rgb_to_bgrx(rgb_arr):
    h, w, _ = rgb_arr.shape
    bgrx = np.empty((h, w, 4), dtype=np.uint8)
    bgrx[..., 0] = rgb_arr[..., 2]
    bgrx[..., 1] = rgb_arr[..., 1]
    bgrx[..., 2] = rgb_arr[..., 0]
    bgrx[..., 3] = 0
    return bgrx


class Hdmi1DrmPresenter:
    def __init__(self):
        self.card = pykms.Card(CARD_PATH)
        self.connector = find_connector(self.card, CONNECTOR_NAME)
        if self.connector is None:
            raise RuntimeError(f"Connector {CONNECTOR_NAME} not found on {CARD_PATH}")
        if not self.connector.connected():
            raise RuntimeError(f"Connector {CONNECTOR_NAME} is not connected.")
        self.crtc = pick_crtc(self.card, self.connector)
        self.mode = self.connector.get_default_mode()
        self.fb = pykms.DumbFramebuffer(
            self.card, self.mode.hdisplay, self.mode.vdisplay, "XR24"
        )

    def present(self):
        rgb = load_image((self.mode.hdisplay, self.mode.vdisplay))
        bgrx = rgb_to_bgrx(rgb)
        mv = self.fb.map(0).cast("B")
        mv[: bgrx.nbytes] = bgrx.tobytes()
        self.fb.flush()
        self.crtc.set_mode(self.connector, self.fb, self.mode)


def main():
    presenter = Hdmi1DrmPresenter()
    presenter.present()
    print(
        f"HDMI1 DRM presenter running on {CARD_PATH} connector {CONNECTOR_NAME} "
        f"({presenter.mode.hdisplay}x{presenter.mode.vdisplay})"
    )

    # Keep process alive; allow SIGHUP/SIGTERM to exit cleanly
    stop = False

    def handle_signals(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_signals)
    signal.signal(signal.SIGTERM, handle_signals)

    try:
        while not stop:
            time.sleep(1)
    finally:
        try:
            presenter.crtc.disable_mode()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"HDMI1 DRM presenter error: {exc}", file=sys.stderr)
        sys.exit(1)

