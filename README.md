# PiTenna
This is the repository for all of the code needed for the Tenna cosplay.  It is currently based on BigRig Creates' implementation for his specific set up and contains assets from Deltarune and BunnyBii.

Please tag BigRig Creates and BunnyBii if you use these files!

<strong><u>3D FILES ARE FOUND HERE  </u></strong>
https://github.com/BigRig-Creates/pitenna-3d-files

Also, here is a google drive to BunnyBii's original files, animations, and notes for their version of the project:
https://drive.google.com/drive/mobile/folders/1RQNVArisyQt1mSsYmyS5tIiOVSgfHcZs?safe=active


<strong><u>This code will need to be tested for your own implementation, and neither BigRig nor BunnyBii have the capacity to help directly with troubleshooting at this time.  </u></strong>

This repository does welcome improvements via pull requests for the code and 3D models, and also welcomes issue reporting via GitHub Issues.


<strong><u>PLEASE ALSO NOTE  </u></strong>

The Geekworm X1203 UPS was powerful enough to run everything, but the screen must not be turned on while the Pi boots!

That being said, here is a rundown of BigRig Creates'software implementation:

Pitenna is a program for the Raspberry Pi 5 that drives two HDMI displays at once: a live camera
feed to a pair of Xreal Air glasses, and a menu-driven set of images, slideshows,
videos and music to a 4:3 monitor used as Tenna's face. A Bluetooth gamepad picks
what plays. A small copy of the face screen is composited into the corner of the
glasses view so you can see what the outside world is seeing.

## Hardware

- Raspberry Pi 5 (the dual-HDMI DRM setup this relies on is Pi 5 specific)
- A libcamera-supported camera (developed with a Camera Module 3 NoIR; any camera
  libcamera enumerates will be auto-detected)
- Two HDMI displays, wired as described below
- A Bluetooth or USB gamepad (the microphone 3D file is made to fit the CRKD ATOM)
- Optional: Geekworm X1203 UPS for the on-screen battery readout.  Reccomended for max amp output.


Here is a list of affiliated links to BigRig Creates' specific set up:

Gloves: https://amzn.to/4c5nkIv

Pi camera 3: https://amzn.to/4asbF5c

Lighter Monitor: https://amzn.to/4qJInnD

Battery Pack: https://amzn.to/4qYG2p2

Pi 5: https://amzn.to/4tK9Rfq

Pi 5 Power HAT: https://amzn.to/3MmGj6Z

Pi 5 Cooling: https://amzn.to/4s2u52m

Pi 5 HDMI: https://amzn.to/4ru1mDC

Antenna Tubing: https://amzn.to/4qJorRD

Helmet Fan for cooling: https://amzn.to/4rvm5Ht

Nose Suction cup: https://amzn.to/3MQmAwx

Shield for screen (might be better options; this scratches easily): https://amzn.to/46djMAc

Screws: https://amzn.to/4tS3O8M

Spray Paint: https://amzn.to/40jEMBJ

Helmet: https://amzn.to/3ZQTDDB

Red suit: https://amzn.to/4b1WNKX

Mic Cover: https://amzn.to/3ZNZfOU

Neck protector: https://amzn.to/4tDKxYq

Mini game controller: https://amzn.to/4auh8sm

Tie: https://amzn.to/4rveo3W

Pants: https://amzn.to/4aYRcFg

Weird neck handkerchief thing: https://amzn.to/4aJN2Qn

Xreal Air (find cheaper options): https://amzn.to/46EcUvZ


### Which port goes where

This matters and is easy to get backwards. The code's `HDMI0`/`HDMI1` names refer
to the *content*, not the physical port:

| Physical port | DRM connector | ALSA card  | What plugs in                    |
|---------------|---------------|------------|----------------------------------|
| HDMI0         | `HDMI-A-1`    | `vc4hdmi0` | 1024x768 4:3 monitor (the face)  |
| HDMI1         | `HDMI-A-2`    | `vc4hdmi1` | 1920x1080 Xreal Air glasses      |

Audio plays out of the **HDMI0** card, not the glasses.

Confirm what your Pi actually enumerated before going further:

```bash
for c in /sys/class/drm/card*-HDMI*; do echo "$c: $(cat $c/status)"; done
```

## Installation

### 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y python3-picamera2 python3-kms++ python3-pygame \
                        python3-pil python3-numpy ffmpeg alsa-utils
```

`python3-kms++` provides `pykms` and is not on PyPI. `ffmpeg` decodes video for
the face screen; without it, video menu entries silently fall back to blank.

### 2. Python packages

```bash
pip3 install -r requirements.txt
```

On Bookworm, `pip3` refuses to touch the system environment. Either use
`pip3 install --break-system-packages -r requirements.txt`, or install the
remaining pure-Python bits from apt:

```bash
sudo apt-get install -y python3-evdev python3-smbus2 python3-psutil
```

### 3. Display modes

The Pi 5 uses the KMS driver, so the old `hdmi_*` options in `config.txt` do
nothing. Force modes with kernel arguments in `/boot/firmware/cmdline.txt`
instead, appended to the single existing line:

```
video=HDMI-A-1:1024x768@60 video=HDMI-A-2:1920x1080@60
```

In `/boot/firmware/config.txt`, make sure you have:

```
dtoverlay=vc4-kms-v3d
max_framebuffers=2
camera_auto_detect=1
```

Reboot, then re-check the connector table above.

### 4. Permissions

The app talks to DRM, evdev and I2C directly, so the user running it needs:

```bash
sudo usermod -aG video,render,input,i2c,audio "$USER"
```

Log out and back in for this to take effect. If you skip the `input` group,
the gamepad will not be detected and the app will run with no controls.

Enable I2C for the battery gauge with `sudo raspi-config`
(Interface Options → I2C), then verify:

```bash
python3 scripts/check_battery.py
```

The app runs fine without a battery; it just shows `BAT: N/A (I2C)`.

### 5. Audio

Volume control goes through an ALSA softvol plugin on the HDMI0 card. Create
`~/.asoundrc`:

```
pcm.hdmi0softvol {
    type softvol
    slave.pcm "plughw:CARD=vc4hdmi0,DEV=0"
    control {
        name "HDMI0 Master"
        card vc4hdmi0
    }
}

pcm.!default {
    type plug
    slave.pcm "hdmi0softvol"
}
```

The control only appears in `amixer` after something has played through it
once. Test with:

```bash
speaker-test -c2 -twav -D hdmi0softvol
amixer -c vc4hdmi0 sset 'HDMI0 Master' 70%
```

If you skip this, playback still works via the `plughw:CARD=vc4hdmi0,DEV=0`
fallback, but the START/SELECT volume buttons will do nothing.

## Running

```bash
./scripts/run_pitenna.sh
```

The launcher clears anything already holding the camera or the audio device,
prompting first; pass `--force` to kill without asking. Press `ESC` or `q` in
the terminal to exit.

Do not run `code/main.py` directly unless you know the environment is clean —
the launcher sets the audio defaults the app expects.

### Start on boot

Edit `pitenna.service` if your user is not `tenna` or the checkout is not at
`/home/tenna/pitenna`, then:

```bash
sudo cp pitenna.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pitenna
journalctl -u pitenna -f
```

For the process-priority boost to work under systemd, give the user
passwordless sudo for `renice`, `ionice` and `chrt`. It is optional; without it
the app logs a warning and carries on at normal priority.

## Controls

Both the D-pad and the face buttons are rotated 90° clockwise by default on the microphone,
matching a controller mounted vertically with an upwards facing D-Pad. Set `DPAD_ROTATION=none` if yours is
mounted normally, or `ccw` for the other orientation.

| Button        | Action                                        |
|---------------|-----------------------------------------------|
| `Y`           | Toggle the `MenuUp` menu                      |
| `B`           | Toggle the `MenuDown` menu                    |
| `A`           | Toggle the `MenuLeft` menu                    |
| `X`           | Toggle the `MenuRight` menu                   |
| `START`       | Volume up                                     |
| `SELECT`      | Volume down                                   |

With a menu open, the D-pad picks one of the four thumbnails, plays it, and
closes the menu. Pressing the same face button again dismisses the menu without
choosing anything.

With no menu open, the D-pad acts directly:

| Direction     | Action                                                     |
|---------------|------------------------------------------------------------|
| Up            | Return the face screen to `Blank.jpg`                       |
| Left          | Stop the currently looping music                            |
| Down (held)   | Loop `TennaTalk.wav` and cycle the active slideshow frames  |

Releasing Down stops the talking audio and resets the slideshow to frame one.

## Adding media

Menus are built from the `media/` tree at startup. Four menus, four slots each:

```
media/MenuUp/SelectUp/      -> the Up slot of the Y menu
media/MenuDown/SelectLeft/  -> the Left slot of the B menu
...
```

Each `Select*` folder needs exactly one thumbnail (any file whose name starts
with `thumb`, case-insensitive) plus one piece of content. What you put beside
the thumbnail decides the behaviour:

| Contents beside the thumbnail        | Behaviour                                            |
|--------------------------------------|------------------------------------------------------|
| A single image                       | Shown on the face screen                              |
| A single video (`.mp4`, `.mkv`, ...) | Plays once with audio, then returns to `Blank.jpg`    |
| A single audio file                  | Loops until you press D-pad Left                      |
| One subfolder of images              | Slideshow, advanced by holding D-pad Down             |
| One subfolder of `UP`/`DOWN`/`LEFT`/`RIGHT` folders | Nested menu, one audio track per direction |

A `Select*` folder with a thumbnail but no usable content is skipped, and its
slot simply will not appear in the menu.

Keep one thumbnail and one media file per folder. If a folder holds several of
either, the last one alphabetically wins, which is rarely what you meant.

## Tuning

All of these are environment variables read at startup:

| Variable                  | Default        | Purpose                                        |
|---------------------------|----------------|------------------------------------------------|
| `DPAD_ROTATION`           | `cw`           | `cw`, `ccw` or `none`                           |
| `GPU_OVERLAY_FPS`         | `8`            | Corner-preview refresh rate on the glasses      |
| `HDMI1_CAPTURE_FPS`       | `30`           | Face-screen capture rate                        |
| `HDMI1_PREVIEW_FPS_VIDEO` | `10`           | Corner-preview rate during video playback       |
| `HDMI1_DRM_CONNECTOR`     | `HDMI-A-1`     | Connector driving the face screen               |
| `DRM_PREVIEW_CONNECTOR`   | `HDMI-A-2`     | Connector driving the glasses                   |
| `HDMI1_AUDIO_DEVICE`      | `hdmi0softvol` | ALSA device for playback                        |
| `HDMI1_VOLUME_CARD`       | `vc4hdmi0`     | Card holding the volume control                 |
| `HDMI1_VOLUME_CONTROL`    | `HDMI0 Master` | Mixer control name                              |
| `HDMI1_VOLUME_STEP`       | `3.0`          | Percent per START/SELECT press                  |
| `FFMPEG_BIN`              | `ffmpeg`       | Path to the ffmpeg binary                       |

The `HDMI1_*` audio names are historical: they configure the HDMI0 card, which
is where sound actually comes out.

Layout constants that are not environment-driven live at the top of
`code/main.py` — `OVERLAY_POSITION`, `OVERLAY_MAX_WIDTH`, `OVERLAY_MARGIN` and
`SWAP_RGB_CHANNELS` (set that last one if red and blue look swapped).

## Troubleshooting

**Nothing on either screen.** Check the connector table; the two displays are
easy to swap. `dmesg | grep -i hdmi` shows what the kernel detected.

**"Camera not connected" on the glasses.** Run `rpicam-hello --list-cameras`.
If that finds nothing, the app cannot either. The rest of the menu system still
works without a camera.

**No controls.** Confirm the pad is paired and that you are in the `input`
group. The app prints `InputHandler: opened controller device ...` at startup
for each pad it finds.

**Video plays with no sound.** Look for `HDMI1 video: audio active on ...` in
the log. If it fell through to `running without audio`, the softvol device in
`~/.asoundrc` is not resolving; test it with `speaker-test` as above.

**Volume buttons do nothing.** The softvol control does not exist yet. Play
something through `hdmi0softvol` once, then check `amixer -c vc4hdmi0 scontrols`.

**Face screen stays blank.** The app needs either `/dev/fb1` or a spare DRM
plane. Startup logs `HDMI1 DRM plane ready on ...` when it has one; if it does
not, it falls back to the `code/hdmi1_drm.py` helper process.

## Licence

Code is released under CC BY 4.0; see `CC-BY-4.0.txt`. Media assets in `media/`
are the property of their respective owners and are not covered by that licence.
