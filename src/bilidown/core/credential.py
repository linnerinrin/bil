#架构：凭据存储负责Cookie序列化和DPAPI保护，调用方只接收保存结果与加载字典
"""凭据持久化

安全策略：
  - Windows下使用DPAPI（CryptProtectData）加密，密文只能由当前用户解开
  - 其他平台退化为base64混淆，并在文件中标记scheme
"""

from __future__ import annotations  #延迟解释类型标注

#模块依赖：处理编码、系统加密、JSON与存储路径
import base64  #将密文字节转成可存储文本
import ctypes  #调用Windows DPAPI
import json  #序列化凭据包装对象
import os  #原子替换与删除凭据文件
import sys  #判断运行平台
from ctypes import wintypes  #定义Windows API结构字段
from typing import Any  #表示外部Cookie值

from ..config.paths import ensure_user_data_dir  #获取用户数据目录
from ..utils.logger import get_logger  #获取模块日志器

_log = get_logger("credential")  #初始化日志器

_CRED_FILE = "credential.dat"  #持久化文件名
_SCHEME_DPAPI = "dpapi"  #Windows用户级加密方案
_SCHEME_B64 = "base64"  #可逆编码方案

#Windows常量
_CRYPTPROTECT_UI_FORBIDDEN = 0x01  #禁止DPAPI弹出交互窗口


#结构：Windows DPAPI输入输出字节缓冲描述符
class _DataBlob(ctypes.Structure):  #对接原生DATA_BLOB
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]  #长度与指针


#函数：创建DPAPI数据结构并保留底层缓冲引用
def _bytes_to_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:  #包装待处理字节
    """构造DATA_BLOB，并返回底层缓冲以保持其生命周期"""
    buf = ctypes.create_string_buffer(data, len(data))  #分配持有数据的缓冲区
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))  #填入指针
    return blob, buf  #防止调用期间缓冲区被回收


#函数：判断当前平台是否支持Windows DPAPI
def _dpapi_available() -> bool:  #检测系统类型
    return sys.platform == "win32"  #仅Windows启用


#函数：调用DPAPI加密数据并释放系统内存
def _dpapi_protect(data: bytes) -> bytes:  #保护Cookie数据
    crypt32 = ctypes.windll.crypt32  #type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  #type: ignore[attr-defined]

    blob_in, _buf = _bytes_to_blob(data)  #保持输入缓冲有效
    blob_out = _DataBlob()  #接收加密后的字节
    ok = crypt32.CryptProtectData(  #调用当前用户加密接口
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:  #DPAPI报告失败
        raise OSError("CryptProtectData 失败")  #交给保存层处理
    try:  #确保返回前释放Windows内存
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)  #拷贝密文字节
    finally:  #无论复制成功与否都释放
        kernel32.LocalFree(blob_out.pbData)  #释放DPAPI分配的内存


#函数：用当前Windows用户身份解密DPAPI数据
def _dpapi_unprotect(data: bytes) -> bytes:  #解开保存的密文
    crypt32 = ctypes.windll.crypt32  #type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  #type: ignore[attr-defined]

    blob_in, _buf = _bytes_to_blob(data)  #绑定输入密文缓冲
    blob_out = _DataBlob()  #接收解密输出
    ok = crypt32.CryptUnprotectData(  #调用当前用户解密接口
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:  #解密接口报错
        raise OSError("CryptUnprotectData 失败")  #让读取层忽略不可解凭据
    try:  #保护系统内存释放
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)  #复制明文字节
    finally:  #保证释放输出缓冲
        kernel32.LocalFree(blob_out.pbData)  #归还系统内存


#类：封装Cookie文件保存、恢复及退出登录删除
class CredentialStore:  #管理单个本地凭据文件
    """负责credentials的落盘与读取（不含bilibili_api依赖，便于测试）"""

    #方法：初始化凭据文件位置
    def __init__(self, path: str | None = None) -> None:  #允许测试指定路径
        if path is None:  #未指定时使用用户目录
            path = str(ensure_user_data_dir() / _CRED_FILE)  #生成默认路径
        self.path = path  #保存后续读写目标

    #------------------------------------------------------------------ 写
    #方法：编码Cookie，按平台保护后写入文件
    def save(self, cookies: dict[str, Any]) -> bool:  #返回落盘是否成功
        """保存cookie字典，返回是否成功（失败不抛错，不阻塞登录流程）"""
        if not cookies:  #空凭据无需写入
            return False  #明确报告未保存
        try:  #Cookie值可能不能序列化
            payload = json.dumps(cookies, ensure_ascii=False).encode("utf-8")  #编码为UTF-8
        except (TypeError, ValueError):  #捕获JSON编码错误
            _log.warning("凭据序列化失败，跳过保存")  #告知持久化失败
            return False  #保留内存登录态

        scheme = _SCHEME_B64  #默认使用可逆编码
        if _dpapi_available():  #Windows优先使用系统加密
            try:  #DPAPI可能因系统服务或权限失败
                payload = _dpapi_protect(payload)  #加密序列化凭据
                scheme = _SCHEME_DPAPI  #标记密文格式
            except OSError:  #加密失败时不降级保存明文
                _log.warning("DPAPI 加密失败，拒绝以可逆方式保存凭据", exc_info=True)  #告知持久化失败
                return False  #返回明确失败结果，保留内存登录态
        #DPAPI输出是任意二进制，必须base64后才能放进JSON
        payload = base64.b64encode(payload)  #编码为JSON可保存文本

        tmp_path = f"{self.path}.tmp"  #同目录临时文件保证原子替换可用
        try:  #写入临时文件可能中途失败
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)  #确保配置目录存在
            with open(tmp_path, "w", encoding="utf-8") as fh:  #先写临时副本
                json.dump({"scheme": scheme, "data": payload.decode("ascii")}, fh)  #写包装格式
            os.replace(tmp_path, self.path)  #原子覆盖旧凭据
            _log.info("凭据已保存到 %s（scheme=%s）", self.path, scheme)  #记录格式
            return True  #成功完成写入
        except OSError:  #文件系统写入或替换失败
            _log.warning("凭据写入失败: %s", self.path, exc_info=True)  #记录异常
            try:  #尽量清理残留的临时文件
                os.remove(tmp_path)  #删除半成品临时文件
            except OSError:  #清理失败不影响返回结果
                pass  #临时文件残留可接受
            return False  #调用方可继续保持登录

    #------------------------------------------------------------------ 读
    #方法：读取包装格式并还原Cookie字典
    def load(self) -> dict[str, Any] | None:  #缺失或损坏时返回空值
        """读取cookie字典；不存在或损坏时返回None"""
        try:  #解析持久化JSON
            with open(self.path, encoding="utf-8") as fh:  #以文本方式打开
                wrapper = json.load(fh)  #读取包装对象
        except FileNotFoundError:  #首次启动没有文件
            return None  #告知无本地凭据
        except (OSError, ValueError):  #文件不可读或JSON非法
            _log.warning("凭据文件损坏，已忽略: %s", self.path, exc_info=True)  #告警
            return None  #忽略坏文件

        if not isinstance(wrapper, dict):  #合法JSON也可能是数组/数字/null
            _log.warning("凭据文件根类型不是对象，已忽略: %s", self.path)  #告警坏结构
            return None  #不继续解析
        scheme = wrapper.get("scheme")  #查询编码方案
        raw = wrapper.get("data", "")  #查询文本载荷
        if not isinstance(scheme, str) or not isinstance(raw, str):  #字段类型必须都是字符串
            _log.warning("凭据文件字段类型不符，已忽略: %s", self.path)  #告警坏字段
            return None  #不尝试解码
        try:  #解码及解析凭据内容
            #两种scheme的落盘内容都是base64
            blob = base64.b64decode(raw.encode("ascii"))  #从文本恢复字节
            if scheme == _SCHEME_DPAPI:  #Windows加密格式
                blob = _dpapi_unprotect(blob)  #按当前用户解密
            elif scheme != _SCHEME_B64:  #未知格式
                return None  #不尝试错误解码
            data = json.loads(blob.decode("utf-8"))  #解码Cookie JSON
        except (OSError, ValueError, UnicodeDecodeError):  #捕获已知解码错误
            _log.warning("凭据解密失败，已忽略", exc_info=True)  #忽略损坏数据
            return None  #不恢复不完整凭据
        return data if isinstance(data, dict) else None  #仅接受Cookie字典

    #方法：退出登录时删除磁盘上的凭据
    def clear(self) -> bool:  #向调用方返回删除是否成功
        """删除已保存的凭据（退出登录），返回是否确实删除"""
        try:  #删除可能遇到占用或权限问题
            os.remove(self.path)  #删除落盘凭据
            _log.info("已删除本地凭据")  #记录成功删除
            return True  #报告删除成功
        except FileNotFoundError:  #已经不存在
            return True  #目标已消失视为成功
        except OSError:  #文件仍可能存在
            _log.warning("删除凭据失败", exc_info=True)  #留下诊断信息
            return False  #告知调用方旧凭据可能残留
