"""凭据持久化。

修复审计缺陷 C7：旧代码把 Credential 只放在内存里，每次启动都要重新扫码。

安全策略：
  - Windows 下使用 DPAPI（CryptProtectData）加密，密文只能由当前用户解开，
    无需额外第三方依赖（keyring 未安装，引入会拖累打包体积）。
  - 其他平台退化为 base64 混淆，并在文件中标记 scheme，便于日后升级。
"""

from __future__ import annotations

import base64
import ctypes
import json
import sys
from ctypes import wintypes
from typing import Any

from ..config.paths import ensure_user_data_dir
from ..utils.logger import get_logger

_log = get_logger("credential")

_CRED_FILE = "credential.dat"
_SCHEME_DPAPI = "dpapi"
_SCHEME_B64 = "base64"

# Windows 常量
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _bytes_to_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    """构造 DATA_BLOB，并返回底层缓冲以保持其生命周期。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buf


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    blob_in, _buf = _bytes_to_blob(data)
    blob_out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptProtectData 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    blob_in, _buf = _bytes_to_blob(data)
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptUnprotectData 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class CredentialStore:
    """负责 credentials 的落盘与读取（不含 bilibili_api 依赖，便于测试）。"""

    def __init__(self, path: str | None = None) -> None:
        if path is None:
            path = str(ensure_user_data_dir() / _CRED_FILE)
        self.path = path

    # ------------------------------------------------------------------ 写
    def save(self, cookies: dict[str, Any]) -> bool:
        """保存 cookie 字典，返回是否成功（失败不抛错，不阻塞登录流程）。"""
        if not cookies:
            return False
        try:
            payload = json.dumps(cookies, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            _log.warning("凭据序列化失败，跳过保存")
            return False

        scheme = _SCHEME_B64
        if _dpapi_available():
            try:
                payload = _dpapi_protect(payload)
                scheme = _SCHEME_DPAPI
            except OSError:
                _log.warning("DPAPI 加密失败，退化为混淆存储", exc_info=True)
                scheme = _SCHEME_B64
        # DPAPI 输出是任意二进制，必须 base64 后才能放进 JSON
        payload = base64.b64encode(payload)

        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"scheme": scheme, "data": payload.decode("ascii")}, fh)
            _log.info("凭据已保存到 %s（scheme=%s）", self.path, scheme)
            return True
        except OSError:
            _log.warning("凭据写入失败: %s", self.path, exc_info=True)
            return False

    # ------------------------------------------------------------------ 读
    def load(self) -> dict[str, Any] | None:
        """读取 cookie 字典；不存在或损坏时返回 None。"""
        try:
            with open(self.path, encoding="utf-8") as fh:
                wrapper = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            _log.warning("凭据文件损坏，已忽略: %s", self.path, exc_info=True)
            return None

        scheme = wrapper.get("scheme")
        raw = wrapper.get("data", "")
        try:
            # 两种 scheme 的落盘内容都是 base64（DPAPI 输出为任意二进制）
            blob = base64.b64decode(raw.encode("ascii"))
            if scheme == _SCHEME_DPAPI:
                blob = _dpapi_unprotect(blob)
            elif scheme != _SCHEME_B64:
                return None
            data = json.loads(blob.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            _log.warning("凭据解密失败，已忽略", exc_info=True)
            return None
        return data if isinstance(data, dict) else None

    def clear(self) -> None:
        """删除已保存的凭据（退出登录）。"""
        try:
            import os

            os.remove(self.path)
            _log.info("已删除本地凭据")
        except FileNotFoundError:
            pass
        except OSError:
            _log.warning("删除凭据失败", exc_info=True)
