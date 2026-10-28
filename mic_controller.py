"""
Windows default-microphone mute control (Core Audio via pycaw).

MicMonitor owns a COM-initialised thread: it polls the real mute state (so
mutes from Teams, MicMute, etc. are picked up too) and services toggle
requests coming from other threads such as the pynput key listener.
"""
import threading

import comtypes
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

POLL_SEC = 0.5


def _endpoint():
    # Re-resolved on every call so a newly plugged headset becomes the target.
    device = AudioUtilities.GetMicrophone()
    if device is None:
        return None
    iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return iface.QueryInterface(IAudioEndpointVolume)


class MicMonitor:
    def __init__(self, on_change, poll_sec=POLL_SEC):
        """on_change(live) is called with True (mic live) / False (muted or no mic)."""
        self.on_change = on_change
        self.poll_sec = poll_sec
        self._toggle = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._live = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._toggle.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def request_toggle(self):
        self._toggle.set()

    def _run(self):
        comtypes.CoInitialize()
        try:
            while not self._stop.is_set():
                try:
                    ep = _endpoint()
                    if ep is None:
                        self._toggle.clear()
                        live = False
                    else:
                        if self._toggle.is_set():
                            self._toggle.clear()
                            ep.SetMute(0 if ep.GetMute() else 1, None)
                        live = not ep.GetMute()
                except Exception as e:
                    print(f"[mic] error: {e}")
                    live = False

                if live != self._live:
                    self._live = live
                    print(f"[mic] {'LIVE' if live else 'MUTED'}")
                    self.on_change(live)

                # Wakes early on a toggle request so the press feels instant.
                self._toggle.wait(self.poll_sec)
        finally:
            comtypes.CoUninitialize()
