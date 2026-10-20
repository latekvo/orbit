"""Send one control command to agent/phone.py: ``ctl.py SOCKET JSON``."""

import socket
import sys

s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
s.sendall(sys.argv[2].encode() + b"\n")
print(s.makefile().readline().strip())
