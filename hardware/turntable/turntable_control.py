"""Host-side controller for the Ferret motorized turntable.

Talks to the ESP32 firmware (firmware/turntable.ino) over a USB-serial link.
The wire protocol is line-oriented ASCII, one command per line terminated by
"\n"; the firmware replies with a single line per command:

    "R<degrees>\n"   rotate by a signed angle (e.g. "R90", "R-45.5")
    "S<rpm>\n"       set rotation speed in RPM (e.g. "S5")
    "H\n"            home / zero the logical angle
    "P\n"            query current logical angle ("P12.5\n")

The firmware acknowledges each command with "OK ...\n" on success or
"ERR ...\n" on failure; rotations stay quiet until motion completes, then ack.

The controller is hardware-free for testing: it writes to and reads from any
file-like object exposing ``write(bytes)`` / ``readline() -> bytes``. In
production that object is a :class:`serial.Serial`; in tests it is a fake.
"""

from __future__ import annotations

from dataclasses import dataclass

# Defaults chosen for a NEMA17 + TMC2209 turntable; mirrored in the firmware.
DEFAULT_BAUD = 115200
DEFAULT_SPEED_RPM = 5.0
ACK_PREFIX = "OK"
ERR_PREFIX = "ERR"


class TurntableError(RuntimeError):
    """Raised when the firmware reports an error or sends no acknowledgement."""


@dataclass
class TurntableController:
    """Drives the turntable firmware over a serial transport.

    Parameters
    ----------
    transport:
        Any object with ``write(bytes) -> int`` and ``readline() -> bytes``.
        Normally a :class:`serial.Serial`; a fake in tests.
    expect_ack:
        When True (default) every command waits for and validates an "OK"
        reply. Set False for fire-and-forget on a one-way link.
    """

    transport: object
    expect_ack: bool = True
    angle: float = 0.0

    @classmethod
    def open_serial(
        cls,
        port: str,
        baud: int = DEFAULT_BAUD,
        timeout: float = 30.0,
        **kwargs: object,
    ) -> "TurntableController":
        """Open a real pyserial port and wrap it. Imported lazily so the
        module stays importable (and unit-testable) without pyserial."""
        import serial  # local import: hardware-only dependency

        port_obj = serial.Serial(port, baud, timeout=timeout, **kwargs)
        return cls(transport=port_obj)

    # --- command API ------------------------------------------------------

    def rotate(self, degrees: float) -> None:
        """Rotate the platter by a signed angle in degrees and update the
        tracked logical angle. Positive is counter-clockwise (firmware-defined)."""
        self._send(f"R{_fmt(degrees)}")
        self.angle = _wrap360(self.angle + degrees)

    def set_speed(self, rpm: float) -> None:
        """Set rotation speed in revolutions per minute (must be > 0)."""
        if rpm <= 0:
            raise ValueError(f"rpm must be positive, got {rpm}")
        self._send(f"S{_fmt(rpm)}")

    def home(self) -> None:
        """Reset the logical angle to zero on both host and firmware."""
        self._send("H")
        self.angle = 0.0

    def query_angle(self) -> float:
        """Ask the firmware for its current logical angle (degrees)."""
        reply = self._send("P", want_value=True)
        return float(reply)

    def step_sequence(self, steps: int) -> list:
        """Yield the absolute angle after each of ``steps`` equal rotations
        that together complete one full 360 deg turn. Useful for turntable
        scans: 12 steps -> 30 deg each. Returns the list of angles visited."""
        if steps <= 0:
            raise ValueError(f"steps must be positive, got {steps}")
        increment = 360.0 / steps
        visited = []
        for _ in range(steps):
            self.rotate(increment)
            visited.append(self.angle)
        return visited

    def close(self) -> None:
        """Close the underlying transport if it supports close()."""
        closer = getattr(self.transport, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> "TurntableController":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- transport plumbing ----------------------------------------------

    def _send(self, command: str, want_value: bool = False) -> str:
        """Write ``command + '\\n'`` and, if acks are expected, read and
        validate one reply line. Returns the reply payload (text after the
        OK prefix) or "" when acks are disabled."""
        self.transport.write((command + "\n").encode("ascii"))
        if not self.expect_ack:
            return ""

        raw = self.transport.readline()
        line = raw.decode("ascii", errors="replace").strip()
        if not line:
            raise TurntableError(f"no acknowledgement for command {command!r}")
        if line.startswith(ERR_PREFIX):
            raise TurntableError(f"firmware error for {command!r}: {line}")
        if not line.startswith(ACK_PREFIX):
            raise TurntableError(f"unexpected reply for {command!r}: {line!r}")

        payload = line[len(ACK_PREFIX):].strip()
        if want_value and not payload:
            raise TurntableError(f"expected a value in reply, got {line!r}")
        return payload


def _fmt(value: float) -> str:
    """Format a number compactly: integers without a trailing ".0"."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _wrap360(angle: float) -> float:
    """Normalize an angle into the [0, 360) range."""
    return angle % 360.0


if __name__ == "__main__":
    # Smoke test against an in-memory fake (no hardware needed).
    class _Fake:
        def __init__(self) -> None:
            self.written: list[bytes] = []

        def write(self, data: bytes) -> int:
            self.written.append(data)
            return len(data)

        def readline(self) -> bytes:
            return b"OK\n"

    fake = _Fake()
    ctrl = TurntableController(transport=fake)
    ctrl.rotate(90)
    assert fake.written[-1] == b"R90\n", fake.written
    assert ctrl.angle == 90.0
    ctrl.rotate(-45.5)
    assert fake.written[-1] == b"R-45.5\n", fake.written
    assert ctrl.angle == 44.5
    ctrl.set_speed(5)
    assert fake.written[-1] == b"S5\n", fake.written
    ctrl.home()
    assert fake.written[-1] == b"H\n", fake.written
    assert ctrl.angle == 0.0
    print("turntable_control self-test OK:", [b.decode().strip() for b in fake.written])
