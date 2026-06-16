# Ferret Motorized Turntable

Automated rotation stage for 360-degree Ferret scans. An ESP32 spins a
NEMA17 stepper (via a TMC2209 or A4988 driver) under a 200 mm platter; the
host pipeline commands precise angles over USB serial or, optionally, MQTT.

## Files

| File | Role |
|------|------|
| `firmware/turntable.ino` | ESP32 firmware: serial + optional MQTT, microstepped motion |
| `turntable_control.py` | Python host controller (`TurntableController`) over pyserial |
| `turntable.scad` / `turntable.stl` | Printable platter (built by the hardware-CAD agent) |
| `../../tests/test_turntable.py` | Unit tests (fake serial, no hardware) |

## Serial protocol

115200 baud, 8N1. One command per line, terminated by `\n`. Every command is
acknowledged with `OK ...` or `ERR ...`.

| Command | Meaning | Example | Ack |
|---------|---------|---------|-----|
| `R<deg>` | Rotate by a signed angle | `R90`, `R-45.5` | `OK R 90.00 angle=90.00` |
| `S<rpm>` | Set rotation speed (RPM) | `S5` | `OK S 5.00` |
| `H` | Home / zero the logical angle | `H` | `OK H angle=0.00` |
| `P` | Print current angle | `P` | `OK 90.00` |

Positive angle = counter-clockwise (flip `INVERT_DIR` in the firmware if your
build is mirrored). A rotation blocks until the move completes, then acks.

## Python host usage

```python
from turntable_control import TurntableController

# Real hardware:
ctrl = TurntableController.open_serial("/dev/tty.usbserial-0001")
ctrl.set_speed(5)
ctrl.home()
for angle in ctrl.step_sequence(12):   # 12 stops, 30 deg each
    capture_frame()                    # your Ferret depth grab here
ctrl.close()
```

`step_sequence(n)` performs `n` equal rotations that sum to one full turn and
returns the list of absolute angles visited — the natural loop for a
turntable scan.

## Wiring (ESP32 DevKitC + TMC2209/A4988)

```
ESP32        Driver (TMC2209 / A4988)        Motor / Power
-----------  ------------------------------  ------------------------
GPIO26  -->  STEP
GPIO25  -->  DIR
GPIO27  -->  EN  (active-LOW: LOW = enabled)
GND     -->  GND  (logic ground, shared)
                  VMOT  <-- 12-24 V motor supply (+)
                  GND   <-- motor supply (-)  [common with ESP32 GND]
                  1A 1B 2A 2B  --> NEMA17 coils (A1/A2, B1/B2)
                  VDD/IOREF  <-- 3.3 V from ESP32 (driver logic, if required)
```

Notes:
- Add a 100 uF (or larger) electrolytic across `VMOT`/`GND` close to the
  driver to absorb back-EMF. Connect it before powering the motor supply.
- Set the driver current limit (Vref pot) to the NEMA17 rated current
  (typically ~1.0-1.5 A) before running, or the motor will overheat / stall.
- `MICROSTEPS` in the firmware must match the driver's microstep configuration
  (MS pins for A4988; UART or MS1/MS2 for TMC2209). Default is 16.
- Never hot-plug the motor while `VMOT` is live — it can destroy the driver.
- If your platter is belt/gear driven, set `GEAR_RATIO` in the firmware so
  reported angles stay correct.

## Flashing

Arduino IDE (or arduino-cli):

1. Install ESP32 board support: Boards Manager -> "esp32 by Espressif".
2. Select your board (e.g. "ESP32 Dev Module") and the serial port.
3. Open `firmware/turntable.ino`, verify, and upload.
4. Open Serial Monitor at 115200 baud; you should see `OK ready ferret-turntable`.
5. Type `R90` + Enter to test a quarter turn.

arduino-cli example:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/
arduino-cli upload  --fqbn esp32:esp32:esp32 -p /dev/tty.usbserial-0001 firmware/
```

## Optional MQTT control (Bambu-style topics)

Set `#define ENABLE_MQTT 1` in `turntable.ino`, fill in the WiFi/broker
constants, and install the **PubSubClient** library. The firmware then mirrors
the serial API over MQTT:

| Topic | Direction | Payload |
|-------|-----------|---------|
| `ferret/turntable/rotate` | subscribe | degrees, e.g. `30` |
| `ferret/turntable/speed`  | subscribe | rpm, e.g. `5` |
| `ferret/turntable/status` | publish (retained) | `{"angle":90.00,"rpm":5.00}` |

This matches the namespaced `ferret/turntable/*` layout used elsewhere in the
Bambu MQTT workflow, so the same broker and tooling apply.

## Testing

```bash
.venv/bin/python -m pytest tests/test_turntable.py -v
```

The suite drives a fake serial object (records bytes, replays canned acks) so
it runs with no board and no `pyserial` port. It asserts the exact wire bytes
for each command (e.g. `rotate(90)` writes `b"R90\n"`), angle tracking with
360-degree wraparound, speed validation, and error handling.

## Verification status

- **Verified offline:** Python controller logic and the full serial protocol,
  via 14 passing unit tests against a fake serial transport. The `__main__`
  self-test in `turntable_control.py` also passes.
- **NOT verified (needs hardware):** firmware compilation/flash on a real
  ESP32, stepper motion accuracy, driver current tuning, and MQTT connectivity.
  No board was attached this session.
