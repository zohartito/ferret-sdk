"""Deterministic substitute for private depth captures; NOT scanner evidence."""

import numpy as np


def make_raw_depth() -> np.ndarray:
    """Return an 800x640 Y16 sloped plane near 600 mm, with invalid sentinels.

    Values are raw uint16 (0.2 mm per unit). A bounded rectangular surface and
    zero background exercise masking and projection without private scan data.
    """
    rows, cols = np.indices((800, 640))
    raw = (3000 + rows // 8 + cols // 8).astype("<u2")
    raw[(rows < 100) | (rows >= 700) | (cols < 80) | (cols >= 560)] = 0
    raw[400, 320] = 65535
    return raw
