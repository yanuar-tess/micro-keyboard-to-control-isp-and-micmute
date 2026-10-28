"""
Windows microphone mute control (Core Audio via pycaw).

Target = the microphone(s) an app is recording from right now (Meet, Teams,
Discord, ... each may pick its own device, not the Windows default). When
nothing is recording (call ended), it keeps the mic(s) the last call used, then
MicMute's chosen mic, then the Windows default. Re-resolved on every poll, so a
newly opened call or plugged headset is picked up automatically.

MicMonitor owns a COM-initialised thread: it polls the real mute state (so
mutes from Teams, MicMute, etc. are picked up too) and services toggle
requests coming from other threads such as the pynput key listener.

This is an OS-level mute: the app receives silence, but its own mute button
does not change.
"""
import threading
import warnings

import comtypes
from comtypes import CLSCTX_ALL
from pycaw.pycaw import (AudioUtilities, IAudioEndpointVolume, IAudioSessionControl2,
                         IAudioSessionManager2)

POLL_SEC = 0.5

E_CAPTURE = 1
DEVICE_STATE_ACTIVE = 1
AUDIO_SESSION_STATE_ACTIVE = 1
CAPTURE_ID_PREFIX = '{0.0.1.00000000}.'
MICMUTE_REG = r"Software\MicMute"


def _volume(device):
    iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return iface.QueryInterface(IAudioEndpointVolume)


def _is_recording(device):
    mgr = device.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
    sessions = mgr.QueryInterface(IAudioSessionManager2).GetSessionEnumerator()
    for i in range(sessions.GetCount()):
        ctl = sessions.GetSession(i).QueryInterface(IAudioSessionControl2)
        if ctl.GetState() == AUDIO_SESSION_STATE_ACTIVE:
            return True
    return False


def _endpoint():
    """Windows default mic's volume interface, or None."""
    device = AudioUtilities.GetMicrophone()
    return _volume(device) if device is not None else None


def micmute_device_id():
    """Endpoint id of the mic chosen in MicMute (HKCU\\Software\\MicMute DeviceId), or None."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MICMUTE_REG) as k:
            guid, _ = winreg.QueryValueEx(k, 'DeviceId')
    except OSError:
        return None
    return CAPTURE_ID_PREFIX + '{' + guid.strip('{}') + '}' if guid else None


def targets(fallback_ids=()):
    """[(device_id, volume)] for the mics being recorded from. When nothing records,
    the first of fallback_ids that is still plugged in, else MicMute's mic, else the
    Windows default mic."""
    enum = AudioUtilities.GetDeviceEnumerator()
    active = enum.EnumAudioEndpoints(E_CAPTURE, DEVICE_STATE_ACTIVE)
    in_use, present = [], {}
    for i in range(active.GetCount()):
        device = active.Item(i)
        try:
            present[device.GetId()] = device
            if _is_recording(device):
                in_use.append((device.GetId(), _volume(device)))
        except comtypes.COMError:
            continue  # device vanished mid-enumeration
    if in_use:
        return in_use
    kept = [(i, _volume(present[i])) for i in fallback_ids if i in present]
    if kept:
        return kept
    mm = micmute_device_id()
    if mm in present:
        return [(mm, _volume(present[mm]))]
    device = AudioUtilities.GetMicrophone()
    return [(device.GetId(), _volume(device))] if device is not None else []


def device_name(device_id):
    enum = AudioUtilities.GetDeviceEnumerator()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')  # pycaw warns about unreadable optional properties
        return AudioUtilities.CreateDevice(enum.GetDevice(device_id)).FriendlyName


class MicMonitor:
    def __init__(self, on_change, poll_sec=POLL_SEC):
        """on_change(live) is called with True (a target mic is live) / False (all muted or no mic)."""
        self.on_change = on_change
        self.poll_sec = poll_sec
        self._toggle = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._live = None
        self._target_ids = None

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

    def _log_targets(self, ids):
        if ids == self._target_ids:
            return
        self._target_ids = ids
        try:
            names = ', '.join(device_name(i) for i in ids) or 'none'
        except Exception:
            names = f"{len(ids)} device(s)"
        print(f"[mic] target: {names}")

    def _run(self):
        comtypes.CoInitialize()
        try:
            while not self._stop.is_set():
                try:
                    eps = targets(self._target_ids or ())
                    self._log_targets(tuple(dev_id for dev_id, _ in eps))
                    if self._toggle.is_set():
                        self._toggle.clear()
                        if eps:
                            # Any target live -> mute all; all muted -> unmute all.
                            mute = any(not ep.GetMute() for _, ep in eps)
                            for _, ep in eps:
                                ep.SetMute(1 if mute else 0, None)
                    live = any(not ep.GetMute() for _, ep in eps)
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
