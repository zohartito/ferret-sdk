"""Unit tests for the turntable host controller.

All tests run against a fake serial transport (no board, no pyserial port).
The fake records every byte written and serves canned reply lines so we can
assert the exact wire protocol the firmware will see.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

# Load turntable_control.py from hardware/turntable/ without making the
# hardware directory an importable package. The module is registered in
# sys.modules before execution so @dataclass can resolve its own forward
# reference annotations during class creation.
_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "hardware"
    / "turntable"
    / "turntable_control.py"
)
_spec = importlib.util.spec_from_file_location("turntable_control", _MODULE_PATH)
turntable_control = importlib.util.module_from_spec(_spec)
sys.modules["turntable_control"] = turntable_control
_spec.loader.exec_module(turntable_control)

TurntableController = turntable_control.TurntableController
TurntableError = turntable_control.TurntableError


class FakeSerial:
    """Minimal stand-in for serial.Serial: records writes, replays replies."""

    def __init__(self, replies=None):
        self.written = []
        # Default: every command acknowledged with bare "OK".
        self._replies = list(replies) if replies is not None else None
        self.closed = False

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def readline(self) -> bytes:
        if self._replies is None:
            return b"OK\n"
        return self._replies.pop(0)

    def close(self) -> None:
        self.closed = True

    @property
    def last(self) -> bytes:
        return self.written[-1]


def test_rotate_writes_r90_bytes():
    """The core spec assertion: rotate(90) emits exactly b'R90\\n'."""
    fake = FakeSerial()
    ctrl = TurntableController(transport=fake)

    ctrl.rotate(90)

    assert fake.last == b"R90\n"


def test_rotate_negative_and_fractional():
    fake = FakeSerial()
    ctrl = TurntableController(transport=fake)

    ctrl.rotate(-45.5)

    assert fake.last == b"R-45.5\n"


def test_rotate_tracks_logical_angle_with_wrap():
    fake = FakeSerial()
    ctrl = TurntableController(transport=fake)

    ctrl.rotate(270)
    assert ctrl.angle == 270.0
    ctrl.rotate(180)  # 450 -> wraps to 90
    assert ctrl.angle == pytest.approx(90.0)


def test_set_speed_writes_s_command():
    fake = FakeSerial()
    ctrl = TurntableController(transport=fake)

    ctrl.set_speed(5)

    assert fake.last == b"S5\n"


def test_set_speed_rejects_nonpositive():
    ctrl = TurntableController(transport=FakeSerial())
    with pytest.raises(ValueError):
        ctrl.set_speed(0)
    with pytest.raises(ValueError):
        ctrl.set_speed(-3)


def test_home_writes_h_and_zeros_angle():
    fake = FakeSerial()
    ctrl = TurntableController(transport=fake)
    ctrl.rotate(90)

    ctrl.home()

    assert fake.last == b"H\n"
    assert ctrl.angle == 0.0


def test_query_angle_parses_payload():
    fake = FakeSerial(replies=[b"OK 12.5\n"])
    ctrl = TurntableController(transport=fake)

    assert ctrl.query_angle() == pytest.approx(12.5)
    assert fake.last == b"P\n"


def test_step_sequence_twelve_steps():
    fake = FakeSerial()
    ctrl = TurntableController(transport=fake)

    visited = list(ctrl.step_sequence(12))

    # 12 commands written, each a 30 deg rotation.
    rotate_cmds = [w for w in fake.written if w.startswith(b"R")]
    assert len(rotate_cmds) == 12
    assert rotate_cmds[0] == b"R30\n"
    assert visited[0] == pytest.approx(30.0)
    assert visited[-1] == pytest.approx(0.0)  # full turn wraps back to 0


def test_step_sequence_rejects_nonpositive():
    ctrl = TurntableController(transport=FakeSerial())
    with pytest.raises(ValueError):
        ctrl.step_sequence(0)


def test_step_sequence_interleaves_moves_and_captures():
    fake = FakeSerial()
    ctrl = TurntableController(transport=fake)
    captured = []
    for index, angle in enumerate(ctrl.step_sequence(12), start=1):
        assert len(fake.written) == index
        assert ctrl.angle == pytest.approx(angle)
        captured.append(ctrl.angle)
    assert len(set(captured)) == 12


def test_error_reply_raises():
    fake = FakeSerial(replies=[b"ERR bad command\n"])
    ctrl = TurntableController(transport=fake)
    with pytest.raises(TurntableError):
        ctrl.rotate(90)


def test_empty_reply_raises():
    fake = FakeSerial(replies=[b"\n"])
    ctrl = TurntableController(transport=fake)
    with pytest.raises(TurntableError):
        ctrl.rotate(90)


def test_unexpected_reply_raises():
    fake = FakeSerial(replies=[b"WAT\n"])
    ctrl = TurntableController(transport=fake)
    with pytest.raises(TurntableError):
        ctrl.rotate(90)


def test_fire_and_forget_skips_ack():
    """With expect_ack=False, no reply is read and no error is raised even
    though the fake would have served one."""
    fake = FakeSerial(replies=[])  # readline would IndexError if called
    ctrl = TurntableController(transport=fake, expect_ack=False)

    ctrl.rotate(90)  # must not call readline()

    assert fake.last == b"R90\n"


def test_context_manager_closes_transport():
    fake = FakeSerial()
    with TurntableController(transport=fake) as ctrl:
        ctrl.rotate(90)
    assert fake.closed is True
