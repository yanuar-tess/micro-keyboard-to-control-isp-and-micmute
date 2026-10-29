"""
Reusable LED controller for the mini keyboard.
Protocol reverse-engineered from the vendor's MINI_KEYBOARD.exe USB traffic — see README.

HID output report 3, 65 bytes:
  set:    03 FE B0 01 08 00 00 00 00 00 01 00 [color|mode] 00...
  commit: 03 FD FE FF 00...   (saves as power-on state; needed for mode changes)
"""
import threading
import time
from pywinusb import hid

VID = 0x514C
PID = 0x8851

# Byte 12 = color (high nibble) | mode (low nibble). Codes below use mode 1 (steady).
# Other modes, mapped with led_probe.py: 0 off, 2 hardware fast blink, 3/4 press-reactive,
# 5 steady white; 6-F are rejected. Both LEDs always share one color.
# There is no "off" color: nibbles 0x0 and 0x8-0xF all show white. Dark = mode 0,
# which (like any mode change) needs the commit packet, as does going back to lit.
OFF = 'off'
OFF_BYTE = 0x00
COLOR_CODES = {
    'white':  0x01,
    'red':    0x11,
    'orange': 0x21,
    'yellow': 0x31,
    'green':  0x41,
    'cyan':   0x51,
    'blue':   0x61,
    'purple': 0x71,
}


class LedController:
    def __init__(self, vid=VID, pid=PID):
        self.vid = vid
        self.pid = pid
        self._device = None
        self._report = None
        self._lock = threading.Lock()
        self._blink_stop = None
        self._blink_thread = None
        self._last_color = None
        self._dark = False  # LED is in mode 0 (committed off)
        try:
            self._open()
        except RuntimeError as e:
            # Not fatal: the first write (or the periodic refresh) reconnects
            # once the keyboard is plugged in.
            print(f"[led] {e} - will keep retrying")

    def _open(self):
        devices = hid.HidDeviceFilter(vendor_id=self.vid, product_id=self.pid).get_devices()
        if not devices:
            raise RuntimeError(f"Keyboard not found (VID=0x{self.vid:04X}, PID=0x{self.pid:04X})")

        for device in devices:
            try:
                device.open()
                reports = device.find_output_reports() + device.find_feature_reports()
                for report in reports:
                    if report.report_id == 3:
                        self._device = device
                        self._report = report
                        return
                device.close()
            except Exception:
                try:
                    device.close()
                except Exception:
                    pass

        raise RuntimeError("Could not find report ID 3 on keyboard")

    def _reopen(self):
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
        self._device = None
        self._report = None
        self._open()

    def _send(self, data):
        data = list(data) + [0x00] * (65 - len(data))
        # The handle goes stale when the keyboard is unplugged/replugged, so
        # reopen once and retry before giving up.
        try:
            if self._report is None:
                raise RuntimeError("not connected")
            self._report.set_raw_data(data)
            self._report.send()
        except Exception:
            self._reopen()
            self._report.set_raw_data(data)
            self._report.send()

    def _apply(self, color_name, commit):
        """color_name may be OFF (dark). Entering or leaving dark always commits."""
        color_name = color_name.lower()
        dark = color_name == OFF
        if not dark and color_name not in COLOR_CODES:
            raise ValueError(f"Unknown color '{color_name}'")

        color_byte = OFF_BYTE if dark else COLOR_CODES[color_name]
        commit = commit or dark != self._dark
        with self._lock:
            self._send([0x03, 0xFE, 0xB0, 0x01, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00,
                       0x01, 0x00, color_byte])
            # The color shows without the commit packet (verified with led_probe.py);
            # commit appears to save it as the power-on color, so only send it for
            # real steady-color changes, not every blink frame or refresh.
            if commit:
                time.sleep(0.02)
                self._send([0x03, 0xFD, 0xFE, 0xFF])
            self._dark = dark

    def set_color(self, color_name):
        # Remember the intended color even if the write fails, so refresh()
        # can restore it once the keyboard is plugged back in.
        changed = color_name != self._last_color
        self._last_color = color_name
        self._apply(color_name, commit=changed)

    def refresh(self):
        """Re-apply the last steady color; reconnects if the keyboard was replugged."""
        if self._blink_stop is not None or self._last_color is None:
            return
        self._apply(self._last_color, commit=False)

    def start_blink(self, color_a, color_b, interval_sec):
        self.start_pattern([(color_a, interval_sec), (color_b, interval_sec)])

    def start_pattern(self, steps):
        """Loop over [(color, seconds), ...]; the first color shows immediately.
        A color of OFF turns the LED dark (costs a commit per lit<->dark change)."""
        self.stop_blink()
        stop_event = threading.Event()

        def _loop():
            while not stop_event.is_set():
                for color, seconds in steps:
                    if stop_event.is_set():
                        return
                    try:
                        self._apply(color, commit=False)
                    except Exception:
                        pass
                    stop_event.wait(seconds)

        self._blink_stop = stop_event
        self._blink_thread = threading.Thread(target=_loop, daemon=True)
        self._blink_thread.start()

    def stop_blink(self):
        if self._blink_stop is not None:
            self._blink_stop.set()
            if self._blink_thread is not None:
                self._blink_thread.join(timeout=1.0)
            self._blink_stop = None
            self._blink_thread = None

    def close(self):
        self.stop_blink()
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None
            self._report = None
