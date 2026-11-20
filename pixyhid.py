#!/usr/bin/env python3
"""HID-Steuerkanal der EMEET PIXY (Tracking, Privacy, Gesten, Audio).

Protokoll nach nille/omarchy-emeet-pixy (docs/PROTOCOL.md, MIT) und
Romonaga/PixyPilot (docs/EMEET_PIXY_HID_REFERENCE.md):
  32-Byte-Reports, Report-ID 0x09
  Aufbau: 09 GG AA BB 00 LL 00 LL <payload>   (Laenge doppelt)
  Abfragen: schreiben, dann Antwort mit gleichen Bytes 1..3 lesen
            (Byte 1 der Antwort mit 0x1f maskieren).
  Der erste Write auf einem frisch geoeffneten fd geht verloren.

CLI:
  pixyhid.py status
  pixyhid.py mode standard|tracking|privacy
  pixyhid.py gesture on|off
  pixyhid.py audio nc|live|original
"""

import os
import select
import sys
import time
from pathlib import Path

REPORT_ID = 0x09
REPORT_LEN = 32
GAP = 0.025          # Pause zwischen Reports
QUERY_TIMEOUT = 0.6
RETRIES = 4

MODES = {"standard": 0x00, "tracking": 0x01, "privacy": 0x02}
MODE_NAMES = {v: k for k, v in MODES.items()}
AUDIO = {"nc": 0x01, "live": 0x02, "original": 0x03}
AUDIO_NAMES = {v: k for k, v in AUDIO.items()}


def find_hidraw():
    """Sucht den hidraw-Knoten der PIXY (Usage-Page 0x83)."""
    for h in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        dev = os.path.realpath(h / "device")
        if "328F:00C0" not in dev.upper():
            continue
        try:
            desc = (h / "device" / "report_descriptor").read_bytes()
        except OSError:
            continue
        if b"\x05\x83\x09\x83" in desc:
            return f"/dev/{h.name}"
    return None


def build(group, a, b, payload=b""):
    n = len(payload)
    if n:
        data = bytes([REPORT_ID, group, a, b, 0, n, 0, n]) + bytes(payload)
    else:
        data = bytes([REPORT_ID, group, a, b])
    return data.ljust(REPORT_LEN, b"\0")


class PixyHID:
    def __init__(self, path=None):
        self.path = path or find_hidraw()
        if not self.path:
            raise OSError("EMEET PIXY HID-Knoten nicht gefunden")
        self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        self._last = 0.0
        # erster Write wird von der Kamera verschluckt -> harmlose Abfrage vorweg
        self._write(build(0x01, 0x01, 0x01))
        self._drain()

    def close(self):
        os.close(self.fd)

    # --- low level ---
    def _write(self, data):
        wait = GAP - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        os.write(self.fd, data)
        self._last = time.monotonic()

    def _drain(self):
        time.sleep(GAP)
        while select.select([self.fd], [], [], 0)[0]:
            try:
                os.read(self.fd, 64)
            except BlockingIOError:
                break

    def query(self, group, a, b, payload=b""):
        """Schreibt Anfrage, liefert passende Antwort (bytes) oder None."""
        req = build(group, a, b, payload)
        for _ in range(RETRIES):
            self._drain()
            self._write(req)
            end = time.monotonic() + QUERY_TIMEOUT
            while (left := end - time.monotonic()) > 0:
                if not select.select([self.fd], [], [], left)[0]:
                    break
                try:
                    resp = os.read(self.fd, 64)
                except BlockingIOError:
                    continue
                if len(resp) >= 4 and resp[0] == REPORT_ID and (resp[1] & 0x1F) == group \
                        and resp[2] == a and resp[3] == b:
                    return resp
        return None

    def send(self, group, a, b, payload=b""):
        self._write(build(group, a, b, payload))

    # --- Modus (Standard / Tracking / Privacy) ---
    def get_mode(self):
        r = self.query(0x01, 0x01, 0x01)
        return r[8] if r and len(r) > 8 else None

    def set_mode(self, name):
        target = MODES[name]
        cur = self.get_mode()
        if cur == MODES["privacy"] and target == MODES["tracking"]:
            self._set_mode_raw(MODES["standard"])   # direkt Privacy->Tracking wird ignoriert
        for _ in range(2):
            got = self._set_mode_raw(target)
            if got == target:
                return got
        return got

    def _set_mode_raw(self, value):
        self.send(0x01, 0x01, 0x00, bytes([value]))
        return self.get_mode()   # Abfrage danach "committet" den Wert

    # --- Gesten ---
    def get_gesture(self):
        r = self.query(0x04, 0x02, 0x01, b"\x02")
        return bool(r[9]) if r and len(r) > 9 else None

    def set_gesture(self, on):
        self.send(0x04, 0x02, 0x00, bytes([0x02, 1 if on else 0]))
        return self.get_gesture()

    # --- Audio-DSP ---
    def get_audio(self):
        r = self.query(0x05, 0x00, 0x04)
        return r[8] if r and len(r) > 8 else None

    def set_audio(self, name):
        self.send(0x05, 0x00, 0x03, bytes([AUDIO[name]]))
        return self.get_audio()


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    hid = PixyHID()
    try:
        cmd = argv[0]
        if cmd == "status":
            m, g, a = hid.get_mode(), hid.get_gesture(), hid.get_audio()
            print(f"Geraet:  {hid.path}")
            print(f"Modus:   {MODE_NAMES.get(m, m)}")
            print(f"Gesten:  {g}")
            print(f"Audio:   {AUDIO_NAMES.get(a, a)}")
        elif cmd == "mode" and len(argv) > 1 and argv[1] in MODES:
            got = hid.set_mode(argv[1])
            print("Modus jetzt:", MODE_NAMES.get(got, got))
            if got != MODES[argv[1]]:
                print("Hinweis: Standard/Tracking greift nur, solange ein Programm das Videobild offen hat.")
        elif cmd == "gesture" and len(argv) > 1 and argv[1] in ("on", "off"):
            print("Gesten jetzt:", hid.set_gesture(argv[1] == "on"))
        elif cmd == "audio" and len(argv) > 1 and argv[1] in AUDIO:
            got = hid.set_audio(argv[1])
            print("Audio jetzt:", AUDIO_NAMES.get(got, got))
        else:
            print(__doc__)
            return 2
    finally:
        hid.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
