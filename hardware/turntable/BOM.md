# Turntable BOM

Bill of materials for the Ferret SDK 3D-scanning turntable. Two build tiers:
a **passive** (hand-turned) version and a **motorized** (ESP32 + stepper)
version driven by `turntable_control.py` over serial.

The platter itself (`turntable.scad` -> `artifacts/turntable.stl`) is shared by
both builds: 200 mm dia, 12 mm tall, 100 mm bearing recess on the underside,
raised outer ledge to retain a printed marker ring, flat matte top.

---

## Shared / printed parts

| Qty | Part | Spec / notes |
|----:|------|--------------|
| 1 | Turntable platter | Print `turntable.stl`. PETG or PLA, ~0.2 mm layers, 3 walls, 15-20% infill. Sand or use a textured PEI plate for the matte top (kills specular glare during scanning). |
| 1 | Marker ring (optional) | Printed flat ring, OD 200 mm / ID 188 mm, ~1.5 mm thick, drops into the raised ledge. Print in matte black, then add ArUco/coded markers (printed sticker label) for pose tracking. |

---

## Passive (hand-turned) build

| Qty | Part | Spec / notes |
|----:|------|--------------|
| 1 | Lazy-susan bearing | **100 mm (4 in)** square or round turntable bearing, steel ball-race. Seats in the underside recess. |
| 1 | Base plate / disc | Any flat rigid base >= 200 mm (3 mm plywood, MDF, or a second printed disc) to mount the bearing's lower race. |
| 8 | M4 wood/self-tapping screws | Fix bearing races to platter underside and base plate (4 per race; check your bearing's hole pattern). |
| 4 | Rubber feet | Stick-on bumpers under the base plate (anti-slip, vibration damping). |

Cost: ~US$8-15 depending on bearing source.

---

## Motorized build (ESP32 + stepper)

Adds closed-ish positioning so `turntable_control.py` can issue `R<deg>`
commands. Pairs with `firmware/turntable.ino`.

| Qty | Part | Spec / notes |
|----:|------|--------------|
| 1 | Lazy-susan bearing | Same 100 mm bearing as passive build (carries the radial/axial load so the motor only provides torque, not support). |
| 1 | ESP32 dev board | Any ESP32-WROOM-32 dev kit. USB serial to host; optional Wi-Fi/MQTT. |
| 1 | NEMA 17 stepper | 1.8 deg/step (200 steps/rev), >= 0.4 Nm holding torque. 42 mm frame. |
| 1 | Stepper driver | **TMC2209** (quiet, sensorless, UART) preferred; **A4988/DRV8825** acceptable. Mount on a heatsink. |
| 1 | Driver carrier / shield | CNC-shield or perfboard; set Vref per motor current. |
| 1 | Power supply | 12 V (or 24 V) >= 2 A for the motor rail. Do NOT power NEMA 17 from ESP32 5 V. |
| 1 | Buck converter | 12 V -> 5 V for the ESP32 if running off the motor PSU (e.g. mini-360). |
| 1 | Drive coupling | GT2 timing belt + pulleys (motor 20 T, platter large ring) **or** a direct 8 mm shaft + set-screw hub through the bearing center. Belt reduction improves angular resolution. |
| 1 | Motor mount | Printed NEMA 17 bracket fixed to the base plate, aligned to the platter axis. |
| - | Dupont wires | Driver STEP/DIR/EN to ESP32 GPIO; motor 4-wire to driver A1/A2/B1/B2. |
| 1 | Optional: photo-interrupter / hall sensor | Home/index switch for absolute zero on power-up. |
| 4 | Rubber feet | Anti-slip under base plate. |

Cost: ~US$30-55 depending on driver and PSU.

### Wiring quick reference (ESP32 <-> TMC2209/A4988)

| Signal | ESP32 GPIO (example) | Driver pin |
|--------|----------------------|-----------|
| STEP   | GPIO 26              | STEP      |
| DIR    | GPIO 25              | DIR       |
| ENABLE | GPIO 27              | EN (active low) |
| GND    | GND                  | GND (logic) |

Motor coils to A1/A2 (coil A) and B1/B2 (coil B); confirm pairs with a
multimeter (continuity = same coil). Motor PSU 12 V -> VM/GND on the driver.

### Serial protocol

Host -> ESP32, newline-terminated ASCII:

- `R<deg>` rotate relative by `<deg>` degrees (e.g. `R15`, `R-90`)
- `H` home to index (if sensor fitted)
- `?` query status

ESP32 replies `OK` when a move completes (consumed by `turntable_control.py`).
