"""Local secret storage (Windows DPAPI, macOS Keychain) for the media-engine keys such as Groq."""

from __future__ import annotations

import base64
import ctypes
import os
import shlex
import subprocess
import sys
import uuid
from ctypes import wintypes
from typing import Any


DPAPI_ENTROPY = b"MathProblemBank.AIAgent.v1"
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    if not data:
        return _DataBlob(0, None), None
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


_KEYCHAIN_SERVICE = "MathProblemBank.APIKeys"
_KEYCHAIN_PREFIX = "macos-keychain:"


def _keychain_protect(text: str) -> str:
    account = uuid.uuid4().hex
    command = shlex.join(["add-generic-password", "-U", "-s", _KEYCHAIN_SERVICE,
                          "-a", account, "-w", text])
    result = subprocess.run(["/usr/bin/security", "-i"], input=command + "\n",
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("无法写入 macOS 钥匙串；请解锁登录钥匙串或使用环境变量。")
    # security -i can exit successfully after a failed subcommand. Verify without
    # reading or printing the stored password.
    check = subprocess.run(["/usr/bin/security", "find-generic-password", "-s",
                            _KEYCHAIN_SERVICE, "-a", account], capture_output=True, timeout=30)
    if check.returncode:
        raise RuntimeError("macOS 钥匙串写入后验证失败；请使用环境变量。")
    return _KEYCHAIN_PREFIX + account


def _keychain_unprotect(encoded: str) -> str:
    if not encoded.startswith(_KEYCHAIN_PREFIX):
        raise RuntimeError("该密钥由原 Windows 用户加密，请在 Mac 的 AI 设置中重新填写，或使用环境变量。")
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s",
                             _KEYCHAIN_SERVICE, "-a", encoded[len(_KEYCHAIN_PREFIX):], "-w"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("无法读取 macOS 钥匙串密钥，请在 AI 设置中重新填写。")
    return result.stdout.rstrip("\r\n")


def _delete_local_secret(encoded: str) -> None:
    if sys.platform == "darwin" and encoded.startswith(_KEYCHAIN_PREFIX):
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-s",
                        _KEYCHAIN_SERVICE, "-a", encoded[len(_KEYCHAIN_PREFIX):]],
                       capture_output=True, timeout=30)


def local_secret_available(encoded: str) -> bool:
    if not encoded:
        return False
    return encoded.startswith(_KEYCHAIN_PREFIX) if sys.platform == "darwin" else not encoded.startswith(_KEYCHAIN_PREFIX)


def _dpapi_protect(text: str) -> str:
    if sys.platform == "darwin":
        return _keychain_protect(text)
    if os.name != "nt":
        raise RuntimeError("API Key 的本地加密保存仅支持 Windows；请改用环境变量。")
    input_blob, input_buffer = _blob(text.encode("utf-8"))
    entropy_blob, entropy_buffer = _blob(DPAPI_ENTROPY)
    output_blob = _DataBlob()
    result = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "MathProblemBank AI Agent",
        ctypes.byref(entropy_blob),
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    _ = input_buffer, entropy_buffer
    if not result:
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _dpapi_unprotect(encoded: str) -> str:
    if sys.platform == "darwin":
        return _keychain_unprotect(encoded)
    if os.name != "nt":
        raise RuntimeError("API Key 的本地解密仅支持 Windows；请改用环境变量。")
    encrypted = base64.b64decode(encoded.encode("ascii"), validate=True)
    input_blob, input_buffer = _blob(encrypted)
    entropy_blob, entropy_buffer = _blob(DPAPI_ENTROPY)
    output_blob = _DataBlob()
    result = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    _ = input_buffer, entropy_buffer
    if not result:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)

