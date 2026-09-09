#!/usr/bin/env python3
"""
Input handler for Bluetooth/USB gamepads and terminal keypresses.
Gamepad events are read via evdev and queued as high-level actions.
"""

import threading
import queue
import select
import time
import os

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:  # pragma: no cover - optional dependency
    InputDevice = None
    ecodes = None
    list_devices = None

class InputHandler:
    """Handle input from keyboard, Bluetooth controller, etc."""
    
    def __init__(self):
        self.input_queue = queue.Queue()
        self.running = False
        self.thread = None
        self.controller_devices = []
        self._last_scan = 0.0
        self.hat_state = {"x": 0, "y": 0}
        self._hat_active = {"x": None, "y": None}
        
    def start(self):
        """Start input handling thread"""
        self.running = True
        self.thread = threading.Thread(target=self._input_loop, daemon=True)
        self.thread.start()
        
    def stop(self):
        """Stop input handling"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)
    
    def _input_loop(self):
        """Poll stdin and any connected gamepads until stopped."""
        while self.running:
            # Check for keyboard input
            try:
                import sys
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    key = sys.stdin.read(1)
                    if key:
                        self.input_queue.put(('keyboard', key))
            except:
                pass
            
            self._poll_controller_events()
    
    def get_input(self, timeout=0):
        """Get next input event, or None if timeout"""
        try:
            return self.input_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def has_input(self):
        """Check if input is available"""
        return not self.input_queue.empty()

    # --- Controller support helpers -------------------------------------------------

    def _poll_controller_events(self):
        """Read events from any connected Bluetooth controller."""
        if InputDevice is None or ecodes is None:
            # evdev not available
            time.sleep(0.05)
            return

        # Periodically rescan to catch newly connected devices
        now = time.time()
        if (not self.controller_devices) or (now - self._last_scan) > 5.0:
            self._open_controller_devices(append=True)
            self._last_scan = now
            if not self.controller_devices:
                time.sleep(0.2)
                return

        fds = [dev.fd for dev in self.controller_devices]
        try:
            readable, _, _ = select.select(fds, [], [], 0.05)
        except Exception:
            readable = []

        for dev in list(self.controller_devices):
            if dev.fd not in readable:
                continue
            try:
                for event in dev.read():
                    self._handle_controller_event(event)
            except OSError:
                # Device disconnected
                try:
                    dev.close()
                except Exception:
                    pass
                self.controller_devices.remove(dev)

    def _open_controller_devices(self, append: bool = False):
        """Detect and open controller input devices."""
        if not append:
            self.controller_devices = []
        if list_devices is None:
            return

        try:
            device_paths = list_devices()
        except Exception:
            device_paths = []

        seen_paths = set()
        for dev in self.controller_devices:
            dev_path = getattr(dev, "fn", None)
            if not dev_path and hasattr(dev, "path"):
                dev_path = getattr(dev, "path", None)
            if dev_path:
                seen_paths.add(dev_path)

        for path in device_paths:
            if path in seen_paths:
                continue
            try:
                dev = InputDevice(path)
            except PermissionError:
                print(f"InputHandler: permission denied opening {path}; try 'sudo usermod -aG input {os.environ.get('USER','')}' and re-login.")
                continue
            except Exception as exc:
                # log once for visibility
                print(f"InputHandler: could not open {path}: {exc}")
                continue

            if not self._is_gamepad_device(dev):
                try:
                    dev.close()
                except Exception:
                    pass
                continue

            try:
                dev.set_nonblocking(True)
            except Exception:
                pass
            self.controller_devices.append(dev)
            print(f"InputHandler: opened controller device '{dev.name}' at {path}")

    def _is_gamepad_device(self, dev: "InputDevice") -> bool:
        """Heuristically decide if an evdev device is a gamepad."""
        name = (dev.name or "").lower()
        tokens = (
            "controller",
            "gamepad",
            "crkd",
            "atom",
            "nintendo",
            "xbox",
            "xinput",
            "360",
            "one",
            "wireless controller",
            "microsoft",
        )
        if any(token in name for token in tokens):
            return True

        try:
            caps = dev.capabilities(verbose=True)
        except Exception:
            return False

        # Look for common gamepad buttons/axes
        keys = caps.get(ecodes.EV_KEY, [])
        abs_axes = caps.get(ecodes.EV_ABS, [])
        key_codes = {code for code, _ in keys} if keys and isinstance(keys[0], (list, tuple)) else set(keys)
        abs_codes = {code for code, _ in abs_axes} if abs_axes and isinstance(abs_axes[0], (list, tuple)) else set(abs_axes)

        wants_keys = {getattr(ecodes, k) for k in ("BTN_SOUTH", "BTN_EAST", "BTN_NORTH", "BTN_WEST") if hasattr(ecodes, k)}
        wants_axes = {getattr(ecodes, k) for k in ("ABS_HAT0X", "ABS_HAT0Y") if hasattr(ecodes, k)}

        if key_codes & wants_keys:
            return True
        if abs_codes & wants_axes:
            return True
        return False

    def _handle_controller_event(self, event):
        """Translate evdev events into high-level controller actions."""
        if event.type == ecodes.EV_KEY:
            if event.value != 1:
                return
            button_map = {
                ecodes.BTN_WEST: 'Y',
                ecodes.BTN_NORTH: 'X',
                ecodes.BTN_EAST: 'B',
                ecodes.BTN_SOUTH: 'A',
            }
            if hasattr(ecodes, 'BTN_START'):
                button_map[ecodes.BTN_START] = 'START'
            if hasattr(ecodes, 'BTN_SELECT'):
                button_map[ecodes.BTN_SELECT] = 'SELECT'
            mapped = button_map.get(event.code)
            if mapped:
                self.input_queue.put(('controller', ('button', mapped)))
        elif event.type == ecodes.EV_ABS and event.code in (ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y):
            axis = 'x' if event.code == ecodes.ABS_HAT0X else 'y'
            prev_value = self.hat_state.get(axis)
            if prev_value == event.value:
                return
            prev_direction = self._hat_active.get(axis)
            self.hat_state[axis] = event.value
            if event.value == 0:
                if prev_direction:
                    self.input_queue.put(('controller', ('dpad', {
                        'direction': prev_direction,
                        'pressed': False
                    })))
                    self._hat_active[axis] = None
                return
            direction = None
            if axis == 'x':
                direction = 'right' if event.value > 0 else 'left'
            else:
                # In evdev, negative Y is up
                direction = 'down' if event.value > 0 else 'up'
            if direction:
                if prev_direction and prev_direction != direction:
                    self.input_queue.put(('controller', ('dpad', {
                        'direction': prev_direction,
                        'pressed': False
                    })))
                self._hat_active[axis] = direction
                self.input_queue.put(('controller', ('dpad', {
                    'direction': direction,
                    'pressed': True
                })))


