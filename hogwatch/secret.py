"""Encrypt small secrets (the email app password) with Windows DPAPI.

DPAPI ties the ciphertext to this Windows user account: another account, or a
copy of the file on another PC, can't decrypt it, and there's no key file to
manage or leak.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

CRYPTPROTECT_UI_FORBIDDEN = 0x1
# Extra entropy so other programs running as the same user can't decrypt it by
# accident with a plain CryptUnprotectData call.
_ENTROPY = b"HogWatch email password v1"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_CryptProtectData = _crypt32.CryptProtectData
_CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
_CryptProtectData.restype = wintypes.BOOL
_CryptUnprotectData = _crypt32.CryptUnprotectData
_CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
_CryptUnprotectData.restype = wintypes.BOOL
_LocalFree = _kernel32.LocalFree
_LocalFree.argtypes = [ctypes.c_void_p]


def _blob(data: bytes):
    """A DATA_BLOB over `data`, plus the buffer that must stay alive while it's used."""
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _take(out: DATA_BLOB) -> bytes:
    """Copy the bytes out of a Windows-allocated blob and free it."""
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(plain: str) -> bytes:
    """Encrypt `plain` for the current Windows user."""
    inp, _keep1 = _blob(plain.encode("utf-8"))
    ent, _keep2 = _blob(_ENTROPY)
    out = DATA_BLOB()
    if not _CryptProtectData(ctypes.byref(inp), "HogWatch", ctypes.byref(ent), None, None,
                             CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "Windows couldn't encrypt the password")
    return _take(out)


def unprotect(cipher: bytes) -> str:
    """Decrypt bytes from protect(); fails for other users or other PCs."""
    inp, _keep1 = _blob(cipher)
    ent, _keep2 = _blob(_ENTROPY)
    out = DATA_BLOB()
    if not _CryptUnprotectData(ctypes.byref(inp), None, ctypes.byref(ent), None, None,
                               CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error(), "Windows couldn't decrypt the saved password")
    return _take(out).decode("utf-8")
