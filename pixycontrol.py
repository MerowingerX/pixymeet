#!/usr/bin/env python3
"""PixyControl - Pan/Tilt/Zoom-Steuerung fuer die EMEET PIXY unter Linux.

Steuerung:
  Pfeiltasten / WASD   schwenken / neigen (Shift = schnell)
  + / -   oder  E / Q  Zoom
  Leertaste / Pos1     Mitte
  1..6                 Preset anfahren
  Strg+1..6            aktuelle Position als Preset speichern
  P                    Foto speichern

Gamepad (Logitech F310, DirectInput-Modus, Schalter hinten auf "D"):
  linker Stick         schwenken / neigen
  rechter Stick hoch/runter  Zoom
  Steuerkreuz          feine Schritte
  LB / RB              Zoom raus / rein
  RT gehalten          schnell
  A                    Mitte
  X / B / Y            Preset 1 / 2 / 3
  Start                Foto
  Back                 AI-Tracking an/aus
  LT                   Mikrofon stumm an/aus

Modus (AI-Tracking / Privacy / Gesten) laeuft ueber den HID-Kanal, siehe pixyhid.py.
Manuelle Bewegung schaltet AI-Tracking automatisch ab.
"""

import array
import fcntl
import json
import os
import select
import struct
import sys
import threading
import time
from pathlib import Path

import pixyhid
import pixymic

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtMultimedia import QCamera, QImageCapture, QMediaCaptureSession, QMediaDevices
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QApplication, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMainWindow,
    QProgressBar, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget,
)

# ---------------------------------------------------------------- V4L2 ----

VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
VIDIOC_QUERYCTRL = 0xC0445624

CID_PAN = 0x009A0908
CID_TILT = 0x009A0909
CID_ZOOM = 0x009A090D

ARCSEC = 3600  # pan/tilt werden in Bogensekunden angegeben


def find_pixy():
    """Liefert (Pfad, Name) des ersten Video-Capture-Knotens der PIXY."""
    base = Path("/sys/class/video4linux")
    nodes = sorted(base.glob("video*"), key=lambda p: int(p.name[5:]))
    for node in nodes:
        try:
            name = (node / "name").read_text().strip()
            index = int((node / "index").read_text().strip())
        except (OSError, ValueError):
            continue
        if "PIXY" in name.upper() and index == 0:
            return f"/dev/{node.name}", name
    return None, None


class V4L2Controls:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def query(self, cid):
        buf = bytearray(struct.pack("II32siiiiI2I", cid, 0, b"", 0, 0, 0, 0, 0, 0, 0))
        fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, buf)
        _, _, _, mn, mx, step, default, _, _, _ = struct.unpack("II32siiiiI2I", buf)
        return mn, mx, step, default

    def get(self, cid):
        buf = bytearray(struct.pack("Ii", cid, 0))
        fcntl.ioctl(self.fd, VIDIOC_G_CTRL, buf)
        return struct.unpack("Ii", buf)[1]

    def set(self, cid, value):
        fcntl.ioctl(self.fd, VIDIOC_S_CTRL, bytearray(struct.pack("Ii", cid, int(value))))

    def close(self):
        os.close(self.fd)


# ------------------------------------------------------------- Gamepad ----

JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80
JSIOCGNAME_64 = 0x80006A13 + (0x10000 * 64)


class GamepadReader(QThread):
    """Liest /dev/input/js0 (Linux-Joystick-API), mit Hotplug."""

    axis = pyqtSignal(int, float)      # Achse, Wert -1..1
    button = pyqtSignal(int, bool)     # Taste, gedrueckt
    status = pyqtSignal(str)

    def __init__(self, path="/dev/input/js0"):
        super().__init__()
        self.path = path
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running:
            try:
                f = open(self.path, "rb", buffering=0)
            except OSError:
                self.status.emit("Gamepad: nicht gefunden")
                self._sleep(2.0)
                continue
            try:
                name = array.array("B", [0] * 64)
                fcntl.ioctl(f, JSIOCGNAME_64, name)
                self.status.emit("Gamepad: " + name.tobytes().rstrip(b"\0").decode(errors="replace"))
            except OSError:
                self.status.emit("Gamepad: verbunden")
            try:
                while self._running:
                    r, _, _ = select.select([f], [], [], 0.2)
                    if not r:
                        continue
                    data = f.read(8)
                    if not data or len(data) < 8:
                        raise OSError("getrennt")
                    _, value, etype, number = struct.unpack("IhBB", data)
                    if etype & JS_EVENT_AXIS:
                        self.axis.emit(number, max(-1.0, value / 32767.0))
                    elif etype & JS_EVENT_BUTTON:
                        self.button.emit(number, bool(value))
            except OSError:
                self.status.emit("Gamepad: getrennt")
            finally:
                f.close()

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(0.1)


# F310 im DirectInput-Modus
AX_LX, AX_LY, AX_RX, AX_RY, AX_HATX, AX_HATY = range(6)
BTN_X, BTN_A, BTN_B, BTN_Y, BTN_LB, BTN_RB, BTN_LT, BTN_RT, BTN_BACK, BTN_START = range(10)
DEADZONE = 0.15


def deadzone(v):
    if abs(v) < DEADZONE:
        return 0.0
    sign = 1.0 if v > 0 else -1.0
    return sign * ((abs(v) - DEADZONE) / (1.0 - DEADZONE)) ** 1.6  # feiner um die Mitte


# ------------------------------------------------------------- Steuerung ----

PRESET_FILE = Path.home() / ".config" / "pixycontrol" / "presets.json"
SNAP_DIR = Path.home() / "Bilder" / "PixyControl"
TICK_MS = 50  # 20 Hz


class PTZ(QObject):
    """Haelt Soll-Position, integriert Geschwindigkeiten und schreibt an die Kamera."""

    changed = pyqtSignal(float, float, float)  # pan°, tilt°, zoom-faktor
    moved = pyqtSignal()                       # manuelle Bewegung angefordert

    def __init__(self, ctrls):
        super().__init__()
        self.c = ctrls
        pmn, pmx, *_ = ctrls.query(CID_PAN)
        tmn, tmx, *_ = ctrls.query(CID_TILT)
        zmn, zmx, *_ = ctrls.query(CID_ZOOM)
        self.pan_range = (pmn / ARCSEC, pmx / ARCSEC)
        self.tilt_range = (tmn / ARCSEC, tmx / ARCSEC)
        self.zoom_range = (zmn, zmx)
        self.pan = ctrls.get(CID_PAN) / ARCSEC
        self.tilt = ctrls.get(CID_TILT) / ARCSEC
        self.zoom = float(ctrls.get(CID_ZOOM))
        self._sent = (None, None, None)
        # Geschwindigkeitsquellen: Name -> (vpan, vtilt, vzoom), Werte -1..1
        self.sources = {}
        self.speed = 40.0     # Grad pro Sekunde bei Vollausschlag
        self.fast = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

    def set_source(self, name, vpan=0.0, vtilt=0.0, vzoom=0.0):
        if vpan or vtilt or vzoom:
            self.sources[name] = (vpan, vtilt, vzoom)
        else:
            self.sources.pop(name, None)

    def step(self, dpan=0.0, dtilt=0.0, dzoom=0.0):
        self.goto(self.pan + dpan, self.tilt + dtilt, self.zoom + dzoom)

    def goto(self, pan, tilt, zoom=None):
        self.pan = clamp(pan, *self.pan_range)
        self.tilt = clamp(tilt, *self.tilt_range)
        if zoom is not None:
            self.zoom = clamp(zoom, *self.zoom_range)
        self.moved.emit()
        self._send()

    def center(self):
        self.goto(0.0, 0.0, self.zoom_range[0])

    def _tick(self):
        if not self.sources:
            return
        vp = sum(s[0] for s in self.sources.values())
        vt = sum(s[1] for s in self.sources.values())
        vz = sum(s[2] for s in self.sources.values())
        dt = TICK_MS / 1000.0
        k = self.speed * (2.5 if self.fast else 1.0) * dt
        zk = (self.zoom_range[1] - self.zoom_range[0]) * 0.6 * dt
        self.goto(self.pan + vp * k, self.tilt + vt * k, self.zoom + vz * zk)

    def _send(self):
        pan = int(round(self.pan)) * ARCSEC
        tilt = int(round(self.tilt)) * ARCSEC
        zoom = int(round(self.zoom))
        try:
            if pan != self._sent[0]:
                self.c.set(CID_PAN, pan)
            if tilt != self._sent[1]:
                self.c.set(CID_TILT, tilt)
            if zoom != self._sent[2]:
                self.c.set(CID_ZOOM, zoom)
        except OSError as e:
            print("Steuerfehler:", e, file=sys.stderr)
        self._sent = (pan, tilt, zoom)
        self.changed.emit(self.pan, self.tilt, self.zoom / 100.0)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class HidWorker(QObject):
    """Fuehrt HID-Befehle im Hintergrund aus (Antworten dauern bis ~1 s)."""

    state = pyqtSignal(object, object)   # modus (int|None), gesten (bool|None)
    error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.hid = None
        self.lock = threading.Lock()
        try:
            self.hid = pixyhid.PixyHID()
        except OSError as e:
            self.error.emit(f"HID nicht verfuegbar: {e}")
            print("HID nicht verfuegbar:", e, file=sys.stderr)
        self.poll = QTimer(self)
        self.poll.timeout.connect(self.refresh)
        self.poll.start(3000)

    @property
    def ok(self):
        return self.hid is not None

    def _run(self, fn, wait=True):
        if not self.hid:
            return

        def job():
            if not self.lock.acquire(blocking=wait):
                return  # Abfrage laeuft schon - Poll ueberspringen
            try:
                fn()
                self.state.emit(self.hid.get_mode(), self.hid.get_gesture())
            except OSError as e:
                self.error.emit(f"HID-Fehler: {e}")
            finally:
                self.lock.release()

        threading.Thread(target=job, daemon=True).start()

    def refresh(self):
        self._run(lambda: None, wait=False)

    def set_mode(self, name):
        self._run(lambda: self.hid.set_mode(name))

    def set_gesture(self, on):
        self._run(lambda: self.hid.set_gesture(on))


# ------------------------------------------------------------------ GUI ----

class HoldButton(QPushButton):
    """Knopf, der waehrend des Gedrueckthaltens eine Bewegung erzeugt."""

    def __init__(self, text, ptz, vpan=0.0, vtilt=0.0, vzoom=0.0):
        super().__init__(text)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumSize(56, 48)
        name = f"btn-{id(self)}"
        self.pressed.connect(lambda: ptz.set_source(name, vpan, vtilt, vzoom))
        self.released.connect(lambda: ptz.set_source(name))


class MainWindow(QMainWindow):
    MOVE_KEYS = {
        Qt.Key.Key_Left: (-1, 0, 0), Qt.Key.Key_A: (-1, 0, 0),
        Qt.Key.Key_Right: (1, 0, 0), Qt.Key.Key_D: (1, 0, 0),
        Qt.Key.Key_Up: (0, 1, 0), Qt.Key.Key_W: (0, 1, 0),
        Qt.Key.Key_Down: (0, -1, 0), Qt.Key.Key_S: (0, -1, 0),
        Qt.Key.Key_Plus: (0, 0, 1), Qt.Key.Key_E: (0, 0, 1),
        Qt.Key.Key_Minus: (0, 0, -1), Qt.Key.Key_Q: (0, 0, -1),
    }

    def __init__(self, dev_path, dev_name, ptz):
        super().__init__()
        self.ptz = ptz
        self.keys_down = set()
        self.pad_axes = [0.0] * 8
        self.pad_zoom_btn = 0.0
        self.presets = self._load_presets()
        self.setWindowTitle(f"PixyControl – {dev_name} ({dev_path})")
        self.resize(1280, 720)

        # Vorschau
        self.video = QVideoWidget()
        self.video.setMinimumSize(640, 360)
        self.session = QMediaCaptureSession()
        self.camera = None
        self.capture = QImageCapture()
        self.session.setImageCapture(self.capture)
        self.session.setVideoOutput(self.video)
        self.capture.imageSaved.connect(lambda _id, path: self.statusBar().showMessage(f"Foto gespeichert: {path}", 5000))
        self.capture.errorOccurred.connect(lambda _id, _e, msg: self.statusBar().showMessage(f"Foto-Fehler: {msg}", 5000))
        self._start_camera(dev_path)

        # Seitenleiste
        side = QVBoxLayout()
        self.pos_label = QLabel()
        self.pos_label.setStyleSheet("font-family: monospace; font-size: 14px;")
        side.addWidget(self.pos_label)

        # Modus (HID)
        self.mode = None
        self.hid = HidWorker()
        mode_box = QGroupBox("Modus (T = Tracking an/aus)")
        ml = QGridLayout(mode_box)
        self.mode_buttons = {}
        for col, (name, label) in enumerate((("standard", "Manuell"), ("tracking", "AI-Tracking"),
                                             ("privacy", "Privacy"))):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setMinimumHeight(36)
            b.clicked.connect(lambda _=False, n=name: self.set_mode(n))
            ml.addWidget(b, 0, col)
            self.mode_buttons[name] = b
        self.gesture_btn = QPushButton("Gestensteuerung")
        self.gesture_btn.setCheckable(True)
        self.gesture_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.gesture_btn.clicked.connect(lambda on: self.hid.set_gesture(on))
        ml.addWidget(self.gesture_btn, 1, 0, 1, 3)
        side.addWidget(mode_box)
        self.hid.state.connect(self._hid_state)
        self.hid.error.connect(lambda msg: self.statusBar().showMessage(msg, 5000))
        if not self.hid.ok:
            mode_box.setEnabled(False)
            mode_box.setTitle("Modus (HID nicht verfügbar – udev-Regel?)")
        ptz.moved.connect(self._manual_move)

        # Mikrofon
        self.mic = pixymic.MicControl()
        mic_box = QGroupBox("Mikrofon (M = stumm)")
        mg = QGridLayout(mic_box)
        self.mute_btn = QPushButton("🎤 an")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mute_btn.setMinimumHeight(36)
        self.mute_btn.clicked.connect(self.mic.set_mute)
        mg.addWidget(self.mute_btn, 0, 0)
        self.monitor_btn = QPushButton("🎧 Mithören")
        self.monitor_btn.setCheckable(True)
        self.monitor_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.monitor_btn.setMinimumHeight(36)
        self.monitor_btn.setToolTip("Mikrofon auf den Standard-Ausgang (Kopfhörer) legen")
        self.monitor_btn.clicked.connect(self.mic.set_monitor)
        mg.addWidget(self.monitor_btn, 0, 1)
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 150)
        self.vol.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.vol_label = QLabel("–")
        self.vol_label.setMinimumWidth(42)
        self.vol.valueChanged.connect(self._vol_changed)
        mg.addWidget(self.vol, 1, 0)
        mg.addWidget(self.vol_label, 1, 1)
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setMaximumHeight(10)
        mg.addWidget(self.meter, 2, 0, 1, 2)
        self._meter_val = 0.0
        side.addWidget(mic_box)
        self.mic.state.connect(self._mic_state)
        self.mic.level.connect(self._mic_level)
        if not self.mic.ok:
            mic_box.setEnabled(False)
            mic_box.setTitle("Mikrofon (nicht gefunden)")
        self.mic.refresh()

        move_box = QGroupBox("Bewegung")
        grid = QGridLayout(move_box)
        grid.addWidget(HoldButton("↖", ptz, -0.7, 0.7), 0, 0)
        grid.addWidget(HoldButton("↑", ptz, 0, 1), 0, 1)
        grid.addWidget(HoldButton("↗", ptz, 0.7, 0.7), 0, 2)
        grid.addWidget(HoldButton("←", ptz, -1, 0), 1, 0)
        center = QPushButton("●")
        center.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        center.setMinimumSize(56, 48)
        center.setToolTip("Mitte (Leertaste)")
        center.clicked.connect(ptz.center)
        grid.addWidget(center, 1, 1)
        grid.addWidget(HoldButton("→", ptz, 1, 0), 1, 2)
        grid.addWidget(HoldButton("↙", ptz, -0.7, -0.7), 2, 0)
        grid.addWidget(HoldButton("↓", ptz, 0, -1), 2, 1)
        grid.addWidget(HoldButton("↘", ptz, 0.7, -0.7), 2, 2)
        side.addWidget(move_box)

        zoom_box = QGroupBox("Zoom")
        zl = QHBoxLayout(zoom_box)
        zl.addWidget(HoldButton("−", ptz, vzoom=-1))
        zl.addWidget(HoldButton("+", ptz, vzoom=1))
        side.addWidget(zoom_box)

        speed_box = QGroupBox("Geschwindigkeit")
        sl = QHBoxLayout(speed_box)
        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(5, 90)
        self.speed.setValue(int(ptz.speed))
        self.speed.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.speed_label = QLabel()
        self.speed.valueChanged.connect(self._speed_changed)
        sl.addWidget(self.speed)
        sl.addWidget(self.speed_label)
        self._speed_changed(self.speed.value())
        side.addWidget(speed_box)

        preset_box = QGroupBox("Presets (Klick = anfahren, Rechtsklick = speichern)")
        pl = QGridLayout(preset_box)
        self.preset_buttons = []
        for i in range(6):
            b = QPushButton()
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setMinimumHeight(40)
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.clicked.connect(lambda _=False, n=i: self.recall_preset(n))
            b.customContextMenuRequested.connect(lambda _p, n=i: self.save_preset(n))
            pl.addWidget(b, i // 3, i % 3)
            self.preset_buttons.append(b)
        self._refresh_presets()
        side.addWidget(preset_box)

        snap = QPushButton("📷  Foto (P)")
        snap.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        snap.setMinimumHeight(40)
        snap.clicked.connect(self.snapshot)
        side.addWidget(snap)

        self.pad_label = QLabel("Gamepad: …")
        side.addWidget(self.pad_label)
        help_label = QLabel(
            "Tasten: Pfeile/WASD bewegen · Shift schnell · +/− bzw. E/Q Zoom\n"
            "Leertaste Mitte · 1–6 Preset · Strg+1–6 speichern · P Foto · T Tracking · M stumm")
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: gray; font-size: 11px;")
        side.addWidget(help_label)
        side.addStretch(1)

        panel = QWidget()
        panel.setLayout(side)
        scroll = QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setFixedWidth(340)
        root = QWidget()
        rl = QHBoxLayout(root)
        rl.addWidget(self.video, 1)
        rl.addWidget(scroll)
        self.setCentralWidget(root)

        # Kuerzel
        for i in range(6):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self, activated=lambda n=i: self.save_preset(n))
        QShortcut(QKeySequence("P"), self, activated=self.snapshot)

        ptz.changed.connect(self._show_pos)
        self._show_pos(ptz.pan, ptz.tilt, ptz.zoom / 100.0)

        # Gamepad
        self.pad = GamepadReader()
        self.pad.axis.connect(self._pad_axis)
        self.pad.button.connect(self._pad_button)
        self.pad.status.connect(self.pad_label.setText)
        self.pad.start()

    # --- Kamera ---
    def _start_camera(self, dev_path):
        dev = next((d for d in QMediaDevices.videoInputs() if bytes(d.id()).decode() == dev_path), None)
        if dev is None:
            self.statusBar().showMessage("Vorschau: Kamera nicht in Qt gefunden", 0)
            return
        self.camera = QCamera(dev)
        fmts = [f for f in dev.videoFormats() if f.resolution().width() == 1920 and f.resolution().height() == 1080]
        if fmts:
            self.camera.setCameraFormat(max(fmts, key=lambda f: f.maxFrameRate()))
        self.camera.errorOccurred.connect(
            lambda _e, msg: self.statusBar().showMessage(
                f"Vorschau-Fehler: {msg} – ist die Kamera in einem anderen Programm offen?", 0))
        self.session.setCamera(self.camera)
        self.camera.start()

    def snapshot(self):
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        if self.capture.isReadyForCapture():
            self.capture.captureToFile(str(SNAP_DIR / time.strftime("pixy_%Y%m%d_%H%M%S.jpg")))

    # --- Anzeige ---
    def _show_pos(self, pan, tilt, zoom):
        self.pos_label.setText(f"Schwenk {pan:+6.0f}°\nNeigung {tilt:+6.0f}°\nZoom    {zoom:5.2f}×")

    def _speed_changed(self, v):
        self.ptz.speed = float(v)
        self.speed_label.setText(f"{v}°/s")

    # --- Modus ---
    def set_mode(self, name):
        self.mode = pixyhid.MODES[name]
        self._show_mode()
        self.hid.set_mode(name)

    def toggle_tracking(self):
        self.set_mode("standard" if self.mode == pixyhid.MODES["tracking"] else "tracking")

    def _manual_move(self):
        # manuelle Bewegung schaltet Tracking ab, sonst dreht die Kamera zurueck
        if self.mode == pixyhid.MODES["tracking"]:
            self.set_mode("standard")
            self.statusBar().showMessage("AI-Tracking aus – manuelle Steuerung", 3000)

    def _hid_state(self, mode, gesture):
        if mode in pixyhid.MODE_NAMES:
            self.mode = mode
        self._show_mode()
        if gesture is not None:
            self.gesture_btn.setChecked(gesture)

    def _show_mode(self):
        for name, b in self.mode_buttons.items():
            b.setChecked(self.mode == pixyhid.MODES[name])

    # --- Mikrofon ---
    def _mic_state(self, muted, pct, monitor):
        self.mute_btn.setChecked(muted)
        self.mute_btn.setText("🔇 stumm" if muted else "🎤 an")
        self.mute_btn.setStyleSheet("background:#c0392b;color:white;" if muted else "")
        self.monitor_btn.setChecked(monitor)
        if not self.vol.isSliderDown():
            self.vol.blockSignals(True)
            self.vol.setValue(pct)
            self.vol.blockSignals(False)
            self.vol_label.setText(f"{pct}%")

    def _vol_changed(self, v):
        self.vol_label.setText(f"{v}%")
        self.mic.set_volume(v)

    def _mic_level(self, peak):
        import math
        db = 20 * math.log10(peak) if peak > 1e-5 else -60.0
        val = max(0.0, min(100.0, (db + 60) / 60 * 100))
        self._meter_val = max(val, self._meter_val - 4)   # langsam abfallen
        self.meter.setValue(int(self._meter_val))

    def toggle_mute(self):
        self.mic.set_mute(not self.mute_btn.isChecked())

    # --- Presets ---
    def _load_presets(self):
        try:
            data = json.loads(PRESET_FILE.read_text())
            return {int(k): v for k, v in data.items()}
        except (OSError, ValueError):
            return {}

    def _refresh_presets(self):
        for i, b in enumerate(self.preset_buttons):
            p = self.presets.get(i)
            b.setText(f"{i + 1}\n{p['pan']:+.0f}° {p['tilt']:+.0f}°" if p else f"{i + 1}\n–")

    def save_preset(self, n):
        self.presets[n] = {"pan": self.ptz.pan, "tilt": self.ptz.tilt, "zoom": self.ptz.zoom}
        PRESET_FILE.parent.mkdir(parents=True, exist_ok=True)
        PRESET_FILE.write_text(json.dumps(self.presets, indent=2))
        self._refresh_presets()
        self.statusBar().showMessage(f"Preset {n + 1} gespeichert", 3000)

    def recall_preset(self, n):
        p = self.presets.get(n)
        if p:
            self.ptz.goto(p["pan"], p["tilt"], p["zoom"])
        else:
            self.statusBar().showMessage(f"Preset {n + 1} ist leer – Rechtsklick oder Strg+{n + 1} speichert", 3000)

    # --- Tastatur ---
    def keyPressEvent(self, e):
        if e.isAutoRepeat():
            return
        k = e.key()
        if k in self.MOVE_KEYS:
            self.keys_down.add(k)
            self._update_keys(e.modifiers())
        elif k in (Qt.Key.Key_Space, Qt.Key.Key_Home):
            self.ptz.center()
        elif k == Qt.Key.Key_T:
            self.toggle_tracking()
        elif k == Qt.Key.Key_M:
            self.toggle_mute()
        elif Qt.Key.Key_1 <= k <= Qt.Key.Key_6 and not (e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.recall_preset(k - Qt.Key.Key_1)
        elif k == Qt.Key.Key_Shift:
            self._update_keys(e.modifiers())
        else:
            super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.isAutoRepeat():
            return
        self.keys_down.discard(e.key())
        self._update_keys(e.modifiers())

    def _update_keys(self, mods):
        vp = sum(self.MOVE_KEYS[k][0] for k in self.keys_down)
        vt = sum(self.MOVE_KEYS[k][1] for k in self.keys_down)
        vz = sum(self.MOVE_KEYS[k][2] for k in self.keys_down)
        mult = 2.5 if mods & Qt.KeyboardModifier.ShiftModifier else 1.0
        self.ptz.set_source("keys", clamp(vp, -1, 1) * mult, clamp(vt, -1, 1) * mult, clamp(vz, -1, 1))

    def focusOutEvent(self, e):
        self.keys_down.clear()
        self.ptz.set_source("keys")
        super().focusOutEvent(e)

    # --- Gamepad ---
    def _pad_axis(self, n, v):
        if n >= len(self.pad_axes):
            return
        self.pad_axes[n] = v
        if n == AX_HATX and v != 0:
            self.ptz.step(dpan=1 if v > 0 else -1)
        elif n == AX_HATY and v != 0:
            self.ptz.step(dtilt=-1 if v > 0 else 1)
        self.ptz.set_source(
            "pad",
            deadzone(self.pad_axes[AX_LX]),
            -deadzone(self.pad_axes[AX_LY]),
            clamp(-deadzone(self.pad_axes[AX_RY]) + self.pad_zoom_btn, -1, 1),
        )

    def _pad_button(self, n, pressed):
        if n in (BTN_LB, BTN_RB):
            self.pad_zoom_btn = (1.0 if n == BTN_RB else -1.0) if pressed else 0.0
            self._pad_axis(AX_RY, self.pad_axes[AX_RY])
        elif n == BTN_RT:
            self.ptz.fast = pressed
        elif n == BTN_LT:
            if pressed:
                self.toggle_mute()
        elif not pressed:
            return
        elif n == BTN_A:
            self.ptz.center()
        elif n == BTN_X:
            self.recall_preset(0)
        elif n == BTN_B:
            self.recall_preset(1)
        elif n == BTN_Y:
            self.recall_preset(2)
        elif n == BTN_START:
            self.snapshot()
        elif n == BTN_BACK:
            self.toggle_tracking()

    def closeEvent(self, e):
        self.pad.stop()
        self.pad.wait(1000)
        if self.camera:
            self.camera.stop()
        self.mic.stop()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PixyControl")
    dev_path, dev_name = (sys.argv[1], sys.argv[1]) if len(sys.argv) > 1 else find_pixy()
    if not dev_path:
        print("EMEET PIXY nicht gefunden. Geraet als Argument angeben, z.B. /dev/video2", file=sys.stderr)
        sys.exit(1)
    try:
        ctrls = V4L2Controls(dev_path)
        ptz = PTZ(ctrls)
    except OSError as e:
        print(f"Kann {dev_path} nicht oeffnen: {e}", file=sys.stderr)
        sys.exit(1)
    win = MainWindow(dev_path, dev_name, ptz)
    win.show()
    code = app.exec()
    ctrls.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
