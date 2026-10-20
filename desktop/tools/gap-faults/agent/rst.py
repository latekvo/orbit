"""Forge a TCP RST on an established connection, as a middlebox (AP, NAT, firewall) would.

    rst.py SRC_IP SRC_PORT DST_IP     # run on the SRC host; resets SRC:SRC_PORT <-> DST

Sniffs the next segment SRC -> DST (AF_PACKET) to learn the sequence numbers, then sends
SRC -> DST a RST with the exact next sequence number, so DST's kernel accepts it.
"""

import socket
import struct
import sys
import time

src, sport, dst = sys.argv[1], int(sys.argv[2]), sys.argv[3]


def csum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


sniff = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
sniff.settimeout(30)
while True:
    frame, addr = sniff.recvfrom(65535)
    if addr[2] != socket.PACKET_OUTGOING or frame[12:14] != b"\x08\x00":
        continue
    ip = frame[14:]
    ihl = (ip[0] & 15) * 4
    if ip[9] != 6 or socket.inet_ntoa(ip[12:16]) != src or socket.inet_ntoa(ip[16:20]) != dst:
        continue
    tcp = ip[ihl:]
    sp, dp, seq, ack = struct.unpack("!HHII", tcp[:12])
    if sp != sport:
        continue
    doff = (tcp[12] >> 4) * 4
    plen = struct.unpack("!H", ip[2:4])[0] - ihl - doff
    flags = tcp[13]
    nxt = (seq + plen + (1 if flags & 0x01 else 0)) & 0xFFFFFFFF
    break

time.sleep(0.05)  # let the sniffed segment arrive first
th = struct.pack("!HHIIBBHHH", sport, dp, nxt, ack, 5 << 4, 0x14, 0, 0, 0)  # RST|ACK
pseudo = socket.inet_aton(src) + socket.inet_aton(dst) + struct.pack("!BBH", 0, 6, len(th))
th = th[:16] + struct.pack("!H", csum(pseudo + th)) + th[18:]
iph = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(th), 0, 0x4000, 64, 6, 0, socket.inet_aton(src), socket.inet_aton(dst))
iph = iph[:10] + struct.pack("!H", csum(iph)) + iph[12:]
raw = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
raw.sendto(iph + th, (dst, 0))
print(f"RST {src}:{sport} -> {dst}:{dp} seq={nxt} ack={ack}")
