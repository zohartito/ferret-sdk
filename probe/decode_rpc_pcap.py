#!/usr/bin/env python3
"""
Decode a tcpdump capture of the CrealityScan GUI <-> RPCServer daemon handshake.

CAPTURE (run while CrealityScan is actively streaming depth):
    sudo tcpdump -i lo0 -s 0 -w /tmp/rpc.pcap \
        '(tcp port 10010 or tcp port 10011 or tcp port 10012 or tcp port 10013)'

Then (from the repo root): .venv/bin/python probe/decode_rpc_pcap.py /tmp/rpc.pcap

Requires third-party msgpack from requirements-lock.txt. The pcap parsing itself
uses the stdlib (no scapy/dpkt needed), reassembles
each TCP half-stream by (src,dst) port, and:
  - For the CONTROL port (10010): msgpack-unpacks the raw stream in BOTH
    directions and prints every RPC request/response with method + params,
    so you can read off the exact call sequence and argument shapes.
  - For the DATA ports (10011/10012/10013): slices the stream into 248-byte
    packets and dumps the first packet's header bytes (hex) so you can locate
    the magic word (expect 0x4321 somewhere), payload-size field, frame id,
    and packet/offset index. 1280x800 Y16 depth = 2,048,000 bytes/frame, which
    is 1280x800x2; reassemble by concatenating payload slices until you have a
    full frame, then np.frombuffer(buf, '<u2').reshape(800, 1280).
"""
import sys
import struct
from collections import defaultdict

import msgpack

LINKTYPE_NULL = 0      # macOS lo0 in pcap: 4-byte BSD loopback family header
LINKTYPE_LOOP = 108
CONTROL_PORTS = {10010}
DATA_PORTS = {10011, 10012, 10013}


def read_pcap(path):
    """Yield raw link-layer frames from a classic .pcap (not pcapng)."""
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"):
            le = magic == b"\xd4\xc3\xb2\xa1"
        else:
            raise SystemExit("not a classic pcap (pcapng? re-capture with tcpdump -w)")
        endian = "<" if le else ">"
        f.read(20)  # rest of global header
        while True:
            hdr = f.read(16)
            if len(hdr) < 16:
                return
            _, _, incl_len, _ = struct.unpack(endian + "IIII", hdr)
            yield f.read(incl_len)


def parse_loopback_ipv4_tcp(frame):
    """Return (src_port, dst_port, tcp_payload) for IPv4/TCP over BSD loopback."""
    if len(frame) < 4:
        return None
    # BSD loopback: 4-byte address family (2 = AF_INET), host byte order.
    fam = struct.unpack("=I", frame[:4])[0]
    ip = frame[4:]
    if fam != 2 or len(ip) < 20:
        return None
    ihl = (ip[0] & 0x0F) * 4
    if ip[9] != 6:  # protocol 6 = TCP
        return None
    tcp = ip[ihl:]
    if len(tcp) < 20:
        return None
    src_port, dst_port = struct.unpack(">HH", tcp[:4])
    data_off = (tcp[12] >> 4) * 4
    return src_port, dst_port, tcp[data_off:]


def main(path):
    # reassemble each direction's byte stream keyed by (src_port, dst_port)
    streams = defaultdict(bytearray)
    for frame in read_pcap(path):
        parsed = parse_loopback_ipv4_tcp(frame)
        if not parsed:
            continue
        src, dst, payload = parsed
        if payload and (src in CONTROL_PORTS | DATA_PORTS
                        or dst in CONTROL_PORTS | DATA_PORTS):
            key = (src, dst)
            if len(streams[key]) > 8_000_000:
                continue  # cap every stream — only need the first responses/packets
            streams[key] += payload

    for (src, dst), data in streams.items():
        if src in CONTROL_PORTS or dst in CONTROL_PORTS:
            direction = "daemon->gui" if src in CONTROL_PORTS else "gui->daemon"
            print(f"\n=== CONTROL {src}->{dst} ({direction}) {len(data)} bytes ===")
            unp = msgpack.Unpacker(raw=True, strict_map_key=False,
                                   max_buffer_size=64 * 1024 * 1024)
            unp.feed(bytes(data))
            for obj in unp:
                if isinstance(obj, (list, tuple)) and obj:
                    if obj[0] == 0 and len(obj) >= 4:
                        print(f"  REQ  id={obj[1]} {obj[2]}({obj[3]!r})")
                    elif obj[0] == 1 and len(obj) >= 4:
                        res = obj[3]
                        if isinstance(res, (bytes, bytearray)):
                            summ = f"<bytes len={len(res)} head={bytes(res[:24]).hex(' ')}>"
                        elif isinstance(res, (list, tuple)):
                            summ = "[" + ", ".join(
                                (f"<bytes {len(x)} head={bytes(x[:16]).hex(' ')}>"
                                 if isinstance(x, (bytes, bytearray)) else repr(x)[:48])
                                for x in res) + "]"
                        else:
                            summ = repr(res)[:200]
                        print(f"  RESP id={obj[1]} err={obj[2]!r} result={summ}")
                    elif obj[0] == 2:
                        print(f"  NOTIFY {obj[1]}({obj[2]!r})")
                    else:
                        print(f"  obj={repr(obj)[:160]}")
        else:
            direction = "daemon->gui" if src in DATA_PORTS else "gui->daemon"
            print(f"\n=== DATA {src}->{dst} ({direction}) {len(data)} bytes ===")
            # slice into 248-byte packets; dump the first header
            if len(data) >= 0xF8:
                first = bytes(data[:0xF8])
                print("  first 248-byte packet header (hex, 16/line):")
                for i in range(0, min(64, len(first)), 16):
                    print("   ", first[i:i + 16].hex(" "))
                # hunt for the 0x4321 magic in either endianness
                for off in range(0, len(first) - 1):
                    le16 = struct.unpack_from("<H", first, off)[0]
                    be16 = struct.unpack_from(">H", first, off)[0]
                    if 0x4321 in (le16, be16):
                        print(f"  magic 0x4321 at offset {off} "
                              f"(LE={le16:#06x} BE={be16:#06x})")
                print(f"  total packets (~248B each): {len(data) // 0xF8}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: decode_rpc_pcap.py /tmp/rpc.pcap")
    main(sys.argv[1])
