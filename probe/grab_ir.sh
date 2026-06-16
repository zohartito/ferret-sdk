#!/usr/bin/env bash
# Grab still + short clip from the CR-Scan Ferret IR camera via ffmpeg/AVFoundation.
#
# IMPORTANT: must run from a Terminal that has Camera permission
# (System Settings -> Privacy & Security -> Camera -> enable your terminal app).
# A headless/background shell will stall on the permission gate.
#
# Note: on macOS, AVFoundation only exposes the IR stream. Depth + RGB are
# hidden (non-standard / 16-bit formats) and need libuvc/V4L2 on Linux/Windows.

set -euo pipefail
OUT="$(cd "$(dirname "$0")" && pwd)"
DEV="CR-Scan Ferret IR Camera"   # ffmpeg avfoundation accepts the device name
SIZE="${1:-1280x800}"            # native modes: 320x200, 640x400, 1280x800
FPS=30

echo "Saving to: $OUT  (size=$SIZE @ ${FPS}fps)"

# Single still frame
ffmpeg -hide_banner -loglevel warning \
  -f avfoundation -framerate "$FPS" -video_size "$SIZE" -i "$DEV" \
  -frames:v 1 -y "$OUT/ir_still.png"

# 3-second clip (lets you see whether the IR projector dot-pattern is on)
ffmpeg -hide_banner -loglevel warning \
  -f avfoundation -framerate "$FPS" -video_size "$SIZE" -i "$DEV" \
  -t 3 -y "$OUT/ir_clip.mp4"

echo "Done:"
ls -la "$OUT"/ir_still.png "$OUT"/ir_clip.mp4
