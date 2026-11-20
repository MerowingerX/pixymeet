# pixyMeet

Steuerung der EMEET PIXY Webcam unter Linux, gedacht als Videocall-Kamera auf einem Raspberry Pi.

- `pixycontrol.py` – GUI: Vorschau, Pan/Tilt/Zoom (Tastatur, Gamepad, Presets), Modus (manuell / AI-Tracking / Privacy), Gesten, Mikrofon (stumm, Lautstärke, Pegel, Mithören)
- `pixyhid.py` – HID-Steuerkanal der Kamera, auch als CLI: `pixyhid.py status`, `pixyhid.py mode standard|tracking|privacy`
- `pixymic.py` – Mikrofonsteuerung über `pactl`
- `70-emeet-pixy.rules` – udev-Regel für Benutzerzugriff auf den HID-Kanal

## Einrichtung

```
sudo cp 70-emeet-pixy.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
cp pixycontrol.desktop ~/.local/share/applications/   # Exec-Pfad ggf. anpassen
```

Abhängigkeiten: Python 3, PyQt6 (mit QtMultimedia), numpy, `pactl` (PipeWire oder PulseAudio).

## Hinweise

- Pan/Tilt/Zoom laufen über Standard-UVC-Controls. Zoom: Hardware-Maximum 1,5×.
- Modusumschaltung (Tracking an/aus) wirkt nur, solange ein Programm das Videobild offen hat.
- Nur ein Programm kann das Videobild gleichzeitig öffnen – während eines Calls hat die Call-Software es.

## Quellen

Die Beschreibung des HID-Protokolls stammt aus öffentlicher Dokumentation dieser Projekte:

- [nille/omarchy-emeet-pixy](https://github.com/nille/omarchy-emeet-pixy) – `docs/PROTOCOL.md` (MIT-Lizenz)
- [Romonaga/PixyPilot](https://github.com/Romonaga/PixyPilot) – `docs/EMEET_PIXY_HID_REFERENCE.md`

Es wurde kein Code aus diesen Projekten übernommen. Kein offizielles EMEET-Produkt; EMEET und PIXY sind Marken ihrer Inhaber.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
