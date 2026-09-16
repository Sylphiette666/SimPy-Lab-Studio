"""Opt-in Windows user-scoped DPAPI storage; never fall back to plaintext."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import sys
from ctypes import wintypes

_PREFIX = "dpapi-user-v1:"
_HEADER = b"SimPyLabStudioKey1\0"


class CredentialProtectionError(Exception):
    """A sanitized failure suitable for the local settings UI."""


def persistence_available() -> bool:
    return sys.platform == "win32"


def _crypt(data: bytes, context: bytes, *, decrypt: bool) -> bytes:
    if not persistence_available():
        raise CredentialProtectionError("本机加密保存仅支持 Windows；可使用仅本次运行模式。")

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    source = ctypes.create_string_buffer(data)
    entropy = ctypes.create_string_buffer(context)
    incoming = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    optional = Blob(len(context), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    kernel = None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        # UI_FORBIDDEN only: deliberately do not use LOCAL_MACHINE scope.
        if not function(ctypes.byref(incoming), None, ctypes.byref(optional), None, None,
                        0x1, ctypes.byref(outgoing)):
            raise CredentialProtectionError("Windows 无法保护或读取此密钥，请重新填写后重试。")
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    except (OSError, AttributeError):
        raise CredentialProtectionError("Windows 密钥保护不可用；配置未更改。") from None
    finally:
        ctypes.memset(source, 0, ctypes.sizeof(source))
        if outgoing.pbData and kernel is not None:
            ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
            kernel.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))


def _context(profile_id: str, endpoint: str) -> bytes:
    # Moving a ciphertext to another profile or service must not reuse its key.
    value = json.dumps(["SimPy Lab Studio credential v1", profile_id, endpoint])
    return hashlib.sha256(value.encode("utf-8")).digest()


def protect_secret(secret: str, profile_id: str, endpoint: str) -> str:
    if not secret or len(secret) > 4096:
        raise CredentialProtectionError("请填写有效密钥后再选择本机加密保存。")
    encrypted = _crypt(_HEADER + secret.encode("utf-8"), _context(profile_id, endpoint),
                       decrypt=False)
    return _PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect_secret(value: str, profile_id: str, endpoint: str) -> str:
    try:
        if not value.startswith(_PREFIX) or len(value) > 65536:
            raise ValueError
        encrypted = base64.b64decode(value[len(_PREFIX):], validate=True)
        plain = _crypt(encrypted, _context(profile_id, endpoint), decrypt=True)
        if not plain.startswith(_HEADER):
            raise ValueError
        secret = plain[len(_HEADER):].decode("utf-8")
        if not secret or len(secret) > 4096:
            raise ValueError
        return secret
    except (ValueError, UnicodeError):
        raise CredentialProtectionError("已保存的密钥无法读取，请重新填写或移除。") from None
