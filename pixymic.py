"""Mikrofon der EMEET PIXY: Stumm, Lautstaerke, Pegel, Mithoeren.

Laeuft ueber pactl (PipeWire-Pulse oder PulseAudio), Pegelanzeige ueber QtMultimedia.
"""

import subprocess

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

MATCH = "EMEET_PIXY"


def pactl(*args):
    r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=3)
    return r.stdout.strip() if r.returncode == 0 else None


def find_source():
    out = pactl("list", "short", "sources") or ""
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and MATCH in parts[1] and ".monitor" not in parts[1]:
            return parts[1]
    return None


class MicControl(QObject):
    state = pyqtSignal(bool, int, bool)   # stumm, lautstaerke %, mithoeren aktiv
    level = pyqtSignal(float)             # Spitzenpegel 0..1

    def __init__(self):
        super().__init__()
        self.source = find_source()
        self.loop_module = None
        self._audio = None
        self._io = None
        self.poll = QTimer(self)
        self.poll.timeout.connect(self.refresh)
        self.poll.start(2000)
        self._start_meter()

    @property
    def ok(self):
        return self.source is not None

    # --- pactl ---
    def refresh(self):
        if not self.source:
            self.source = find_source()
            if not self.source:
                return
        mute = pactl("get-source-mute", self.source)
        vol = pactl("get-source-volume", self.source)
        if mute is None or vol is None:
            self.source = None
            return
        muted = mute.endswith("yes")
        pct = 0
        for tok in vol.split():
            if tok.endswith("%"):
                pct = int(tok[:-1])
                break
        self.state.emit(muted, pct, self._loop_active())

    def set_mute(self, on):
        if self.source:
            pactl("set-source-mute", self.source, "1" if on else "0")
            self.refresh()

    def set_volume(self, pct):
        if self.source:
            pactl("set-source-volume", self.source, f"{int(pct)}%")

    # --- Mithoeren (Loopback auf Standard-Ausgang) ---
    def _loop_active(self):
        out = pactl("list", "short", "modules") or ""
        for line in out.splitlines():
            if "module-loopback" in line and self.source and self.source in line:
                self.loop_module = line.split("\t")[0]
                return True
        self.loop_module = None
        return False

    def set_monitor(self, on):
        if not self.source:
            return
        if on and not self._loop_active():
            pactl("load-module", "module-loopback", f"source={self.source}", "latency_msec=30")
        elif not on:
            while self._loop_active():
                pactl("unload-module", self.loop_module)
        self.refresh()

    # --- Pegel ---
    def _start_meter(self):
        dev = next((d for d in QMediaDevices.audioInputs() if "PIXY" in d.description().upper()), None)
        if dev is None:
            return
        fmt = QAudioFormat()
        fmt.setSampleRate(48000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not dev.isFormatSupported(fmt):
            fmt = dev.preferredFormat()
        self._fmt = fmt
        self._audio = QAudioSource(dev, fmt, self)
        self._audio.setBufferSize(4800)
        self._io = self._audio.start()
        if self._io:
            self._io.readyRead.connect(self._read)

    def _read(self):
        data = self._io.readAll().data()
        if not data:
            return
        sf = self._fmt.sampleFormat()
        if sf == QAudioFormat.SampleFormat.Int16:
            a = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        elif sf == QAudioFormat.SampleFormat.Int32:
            a = np.frombuffer(data[: len(data) // 4 * 4], dtype=np.int32).astype(np.float32) / 2147483648.0
        elif sf == QAudioFormat.SampleFormat.Float:
            a = np.frombuffer(data[: len(data) // 4 * 4], dtype=np.float32)
        else:
            return
        if a.size:
            self.level.emit(float(np.abs(a).max()))

    def stop(self):
        if self._audio:
            self._audio.stop()
