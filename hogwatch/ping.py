"""ICMP ping through the Windows IcmpSendEcho API.

Why not ping.exe: spawning a process every 2 seconds for 4 targets is wasteful,
and parsing its output is locale-dependent. IcmpSendEcho needs no admin rights
and reports the kernel-measured round-trip time, which (unlike timing the call
from Python) isn't inflated when this process is busy.
"""

from __future__ import annotations

import ctypes
import socket
import struct
import threading
from ctypes import wintypes

_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)

IP_SUCCESS = 0
IP_REQ_TIMED_OUT = 11010
IP_TTL_EXPIRED_TRANSIT = 11013


class IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.c_void_p),
    ]


class ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [
        ("Address", ctypes.c_uint32),
        ("Status", ctypes.c_uint32),
        ("RoundTripTime", ctypes.c_uint32),
        ("DataSize", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort),
        ("Data", ctypes.c_void_p),
        ("Options", IP_OPTION_INFORMATION),
    ]


_IcmpCreateFile = _iphlpapi.IcmpCreateFile
_IcmpCreateFile.restype = wintypes.HANDLE
_IcmpSendEcho = _iphlpapi.IcmpSendEcho
_IcmpSendEcho.argtypes = [
    wintypes.HANDLE, ctypes.c_uint32, ctypes.c_void_p, wintypes.WORD,
    ctypes.POINTER(IP_OPTION_INFORMATION), ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
]
_IcmpSendEcho.restype = wintypes.DWORD

_PAYLOAD = b"hogwatch-latency-probe-32-bytes!"
_local = threading.local()


def _handle():
    """One ICMP handle per thread; the docs don't promise handles are thread-safe."""
    h = getattr(_local, "h", None)
    if h is None:
        h = _IcmpCreateFile()
        _local.h = h
    return h


def ping(ip: str, timeout_ms: int = 1000, ttl: int = 128) -> tuple[float | None, int, str | None]:
    """Send one echo request.

    Returns (rtt_ms or None if lost, status code, address that replied).
    With a small ttl, the reply comes from the router at that hop instead
    (status IP_TTL_EXPIRED_TRANSIT) -- that's how we discover and time the ISP
    gateway, since AT&T gateways ignore pings sent directly to them.
    """
    try:
        dest = struct.unpack("<I", socket.inet_aton(ip))[0]
    except OSError:
        return None, -1, None
    reply_size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(_PAYLOAD) + 512
    buf = ctypes.create_string_buffer(reply_size)
    opts = IP_OPTION_INFORMATION(Ttl=ttl)
    n = _IcmpSendEcho(_handle(), dest, _PAYLOAD, len(_PAYLOAD), ctypes.byref(opts), buf, reply_size, timeout_ms)
    reply = ICMP_ECHO_REPLY.from_buffer(buf)
    status = reply.Status if n else ctypes.get_last_error()
    src = socket.inet_ntoa(struct.pack("<I", reply.Address)) if reply.Address else None
    if n and status in (IP_SUCCESS, IP_TTL_EXPIRED_TRANSIT):
        return float(reply.RoundTripTime), status, src
    return None, status, src


def hop(n: int, toward: str = "1.1.1.1") -> str | None:
    """Return the router at hop `n` on the way to `toward` (1 = your router, 2 = the next one)."""
    for _ in range(3):
        _, status, src = ping(toward, timeout_ms=1000, ttl=n)
        if src and status in (IP_TTL_EXPIRED_TRANSIT, IP_SUCCESS):
            return src
    return None
