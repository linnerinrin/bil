"""扫码登录会话：自己调用官方端点并健壮解析，不依赖库的 cookie 解析。

背景（真实环境暴露的缺陷）
--------------------------
``bilibili_api`` 17.4.1 的 ``QrCodeLogin.check_state`` 在登录成功时这样解析 cookie::

    cookies_list = cred_url.split("?")[1].split("&")
    for cookie in cookies_list:
        if cookie[:8] == "SESSDATA":
            sessdata = cookie[9:]
        ...

它按**固定下标**取值，对参数顺序、URL 编码、额外参数都很敏感。一旦匹配不上，
它会静默产出一个 ``sessdata`` 为空的 ``Credential``，最终表现为::

    获取用户信息失败: Credential 类未提供 sessdata 或者为空

本模块的做法：用库提供的通用 ``Api`` 客户端直接请求同一批官方端点，
自己按标准查询串规则解析，并在解析失败时把原始响应记进日志。
**不修改也不 monkeypatch 第三方库**，只是不再使用它那段脆弱的解析逻辑。
"""

from __future__ import annotations

import asyncio
import enum
import io
import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from bilibili_api.utils.network import Api, Credential
from bilibili_api.utils.utils import get_api

from ..utils.logger import get_logger
from .network import create_session

_log = get_logger("qrlogin")

_LOGIN_API = get_api("login")

# 与 bilibili_api.login_v2.QrCodeLoginEvents 的取值保持一致
_CODE_SCAN = 86101
_CODE_CONF = 86090
_CODE_TIMEOUT = 86038

# 不属于 cookie 的字段
_NON_COOKIE_KEYS = frozenset(
    {
        "gourl",
        "Expires",
        "expires",
        "source",
        "url",
        "refresh_token",
        "code",
        "message",
        "ts",
        "ticket",
        "first_domain",
    }
)

class QrState(enum.Enum):
    """扫码登录状态，取值与库内的 QrCodeLoginEvents 相同。"""

    SCAN = "scan"
    CONF = "confirm"
    TIMEOUT = "timeout"
    DONE = "done"
    ERROR = "error"


@dataclass(slots=True)
class QrPollResult:
    """一次轮询的结果（保留原始响应以便诊断与解析）。"""

    state: QrState
    cookies: dict[str, str] = field(default_factory=dict)
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def okay(self) -> bool:
        return self.state is not QrState.ERROR


def parse_login_cookies(raw_url: str, refresh_token: str = "") -> dict[str, str]:
    """从 crossDomain 登录 URL 中解析出 cookie（**不做百分号解码**）。

    为什么必须保留原始编码形态
    --------------------------
    ``Credential.__init__`` 的规则是::

        sessdata if sessdata.find("%") != -1 else urllib.parse.quote(sessdata)

    即「值里**没有** ``%`` 就编码一次」。而真实 SESSDATA 本身就带 ``%2C`` / ``%2A``
    这类转义，所以必须把**原始编码串**原样交给它；若我们先解码成 ``,`` ``*``，
    它就会重新编码，虽然对 SESSDATA 恰好互相抵消，但这不是可以依赖的巧合
    （对其它含特殊字符的 cookie 就会坏掉）。

    因此这里不用 ``parse_qsl``（它会解码），改为手工按 ``&`` 切分、在第一个
    ``=`` 处分割，不触碰值的编码。
    """
    result: dict[str, str] = {}
    if not raw_url:
        return result

    try:
        parsed = urllib.parse.urlsplit(raw_url)
    except ValueError:
        _log.warning("登录返回的 url 无法解析: %r", raw_url[:120])
        return result

    # query 与 fragment 都解析（跨域登录链接有时把参数放在 fragment 里）
    chunks = [c for c in (parsed.query, parsed.fragment) if c]
    if not chunks and "?" in raw_url:
        # 兜底：url 不是标准结构时，直接取第一个 ? 之后的部分
        chunks = [raw_url.split("?", 1)[1]]

    for chunk in chunks:
        for pair in chunk.split("&"):
            if not pair or "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            if not key or not value:
                continue
            if key.endswith("__ckMd5") or key in _NON_COOKIE_KEYS:
                continue
            result.setdefault(key, value)

    # cookie 名称大小写在不同渠道间可能不一致，统一成库期望的写法
    canonical = {"sessdata": "SESSDATA", "bili_jct": "bili_jct", "dedeuserid": "DedeUserID"}
    for key in list(result):
        want = canonical.get(key.lower())
        if want and key != want:
            result.setdefault(want, result.pop(key))

    if refresh_token and not result.get("ac_time_value"):
        result["ac_time_value"] = refresh_token
    return result


def build_credential(cookies: dict[str, str]) -> Credential | None:
    """把 cookie 字典转成 Credential；缺少 SESSDATA 时返回 None。"""
    sessdata = cookies.get("SESSDATA")
    if not sessdata:
        return None
    return Credential(
        sessdata=sessdata,
        bili_jct=cookies.get("bili_jct") or None,
        buvid3=cookies.get("buvid3") or None,
        buvid4=cookies.get("buvid4") or None,
        dedeuserid=cookies.get("DedeUserID") or None,
        ac_time_value=cookies.get("ac_time_value") or None,
    )


def is_ticket_url(url: str) -> bool:
    """判断登录返回的链接是否只是「票据换票」链接（当前线上主流程）。

    线上实测形态::

        https://passport.biligame.com/x/passport-login/web/crossDomain
            ?ticket=<hex>&gourl=https%3A%2F%2Fwww.bilibili.com&first_domain=.bilibili.com

    这类链接里**没有** cookie，必须请求它（跟随 302），由服务端用 Set-Cookie 下发。
    """
    if not url or "ticket=" not in url:
        return False
    return "SESSDATA" not in url


# 只接受真正的登录 cookie，避免把 buvid3/b_nut 等匿名标识误当凭据
_CREDENTIAL_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4")


def _collect_credential_cookies(
    jar: Any, start: dict[str, str] | None = None
) -> dict[str, str]:
    """从 cookie jar（或普通 dict）中挑出登录相关的 cookie。

    requests 的 ``response.cookies`` / ``session.cookies`` 取到的值**已是解码后的
    明文**，正好是 ``Credential`` 期望的形态，不需要再做处理。
    """
    result: dict[str, str] = dict(start or {})
    try:
        items = list(jar.items())
    except (AttributeError, TypeError):
        return result

    # 按小写名建索引，兼容渠道间的大小写差异
    lowered: dict[str, str] = {}
    for key, value in items:
        if value:
            lowered.setdefault(str(key).lower(), str(value))

    for want in _CREDENTIAL_KEYS:
        if not result.get(want):
            value = lowered.get(want.lower())
            if value:
                result[want] = value
    result.pop("DedeUserID__ckMd5", None)
    return result


def exchange_ticket(ticket_url: str, refresh_token: str = "") -> dict[str, str]:
    """请求票据链接并跟随 302，从 Set-Cookie 中取出登录凭据。

    这是当前线上的登录方式：poll 接口只返回一个 ``ticket`` 链接，
    真正的 SESSDATA / bili_jct / DedeUserID 由该链接响应头的 Set-Cookie 下发。
    （这也是旧版库会失败的根因：它只在 url 查询串里找 cookie，而那里面没有。）

    该函数会阻塞，调用方需放到工作线程（poll 内部已用 asyncio.to_thread）。
    """
    import requests  # noqa: PLC0415 - 与慢启动解耦

    from ..config.settings import DOWNLOAD_TIMEOUT

    cookies: dict[str, str] = {}
    # 统一的会话策略：默认绕过失效的系统代理
    session = create_session()
    try:
        response = session.get(
            ticket_url,
            headers={"Referer": "https://www.bilibili.com/"},
            timeout=DOWNLOAD_TIMEOUT,
            allow_redirects=True,
        )
        # 来源 1：最终落地 URL 的查询串（部分渠道仍然挂 cookie）
        cookies.update(parse_login_cookies(str(response.url or ""), refresh_token))
        # 来源 2/3：本次响应的 Set-Cookie 与整个会话 cookie
        cookies.update(_collect_credential_cookies(response.cookies, cookies))
        cookies.update(_collect_credential_cookies(session.cookies, cookies))

        if not cookies.get("SESSDATA"):
            _log.error(
                "换票请求完成但没有拿到 SESSDATA。status=%s，最终 URL=%s，"
                "响应 Set-Cookie 名称=%s",
                response.status_code,
                str(response.url)[:120],
                sorted({c.name for c in response.cookies}),
            )
    except requests.RequestException as exc:
        _log.warning("换取登录 cookie 失败（网络）: %r", exc)
    finally:
        session.close()

    if refresh_token and not cookies.get("ac_time_value"):
        cookies["ac_time_value"] = refresh_token
    return cookies


class LoginSession:
    """一次扫码登录会话（自己持有二维码密钥与解析结果）。

    生命周期::

        session = LoginSession()
        await session.generate()          # 拿到二维码 PNG
        while True:
            result = await session.poll() # 轮询
            ...
    """

    def __init__(self) -> None:
        self._qrcode_key = ""
        self._qr_link = ""
        self._png: bytes = b""
        self._cookies: dict[str, str] = {}

    # ------------------------------------------------------------- 属性
    @property
    def png(self) -> bytes:
        return self._png

    @property
    def link(self) -> str:
        return self._qr_link

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    @property
    def has_key(self) -> bool:
        return bool(self._qrcode_key)

    # ------------------------------------------------------------- 生成
    async def generate(self) -> bytes:
        """请求二维码与登录密钥，返回二维码 PNG 字节。"""
        api = _LOGIN_API["qrcode"]["web"]["get_qrcode_and_token"]
        data = await Api(credential=Credential(), **api).result

        self._qr_link = str(_get(data, "url"))
        self._qrcode_key = str(_get(data, "qrcode_key"))
        if not self._qrcode_key:
            raise RuntimeError("登录接口未返回 qrcode_key")

        # 复用库的图片渲染（纯本地操作，不涉及网络解析）
        import qrcode  # noqa: PLC0415 - 延迟导入，避免影响启动时间

        qr = qrcode.QRCode()
        qr.add_data(self._qr_link)
        image = qr.make_image()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self._png = buffer.getvalue()
        return self._png

    # ------------------------------------------------------------- 轮询
    async def _query_poll_endpoint(self) -> dict[str, Any]:
        """请求 /qrcode/poll 并返回 data 字典（独立成方法，便于测试替身）。"""
        api = _LOGIN_API["qrcode"]["web"]["get_events"]
        params = dict(api.get("params") or {})
        params["qrcode_key"] = self._qrcode_key
        raw = (
            await Api(
                url=api["url"],
                method=api["method"],
                verify=api.get("verify", False),
                credential=Credential(),
            )
            .update_params(**params)
            .result
        )
        return _as_dict(raw)

    async def poll(self) -> QrPollResult:
        """查询一次扫码状态；成功时完成「换票」取得 cookie。"""
        if not self._qrcode_key:
            return QrPollResult(QrState.ERROR, message="尚未生成二维码")

        try:
            data = await self._query_poll_endpoint()
        except Exception as exc:  # noqa: BLE001 - 交给调用方决定是否重试
            _log.warning("扫码状态查询失败: %r", exc)
            return QrPollResult(QrState.ERROR, message=str(exc))

        code = data.get("code")
        refresh_token = str(data.get("refresh_token") or "")
        login_url = str(data.get("url") or "")

        if code == _CODE_SCAN:
            return QrPollResult(QrState.SCAN, raw=data)
        if code == _CODE_CONF:
            return QrPollResult(QrState.CONF, raw=data)
        if code == _CODE_TIMEOUT:
            return QrPollResult(QrState.TIMEOUT, raw=data)

        # 到达这里说明已登录成功（B 站以 data.code == 0 表示成功）。
        # 第一步：有些渠道会把 cookie 直接挂在 url 的查询串上。
        cookies = parse_login_cookies(login_url, refresh_token)

        # 第二步（当前线上主流程）：url 只带一个 ticket，必须请求它，
        # 由服务端用 302 + Set-Cookie 下发 SESSDATA / bili_jct / DedeUserID。
        if not cookies.get("SESSDATA") and is_ticket_url(login_url):
            _log.info("登录返回的是 ticket 链接，开始换取 cookie…")
            cookies = await asyncio.to_thread(exchange_ticket, login_url, refresh_token)

        if not cookies.get("SESSDATA"):
            # 关键诊断：把原始响应写进日志，便于定位接口变化
            _log.error(
                "扫码成功但未解析出 SESSDATA。响应键=%s，url 长度=%s，url 前 240 字符=%r",
                sorted(data),
                len(login_url),
                login_url[:240],
            )
            return QrPollResult(
                QrState.ERROR,
                message="登录成功但未能取得凭据（SESSDATA 缺失）",
                raw=data,
            )

        self._cookies = cookies
        _log.info(
            "登录凭据获取成功：SESSDATA=%s 字符，bili_jct=%s，DedeUserID=%s",
            len(cookies.get("SESSDATA", "")),
            "有" if cookies.get("bili_jct") else "无",
            cookies.get("DedeUserID") or "无",
        )
        return QrPollResult(QrState.DONE, cookies=cookies, raw=data)

    def credential(self) -> Credential:
        """用已解析的 cookie 构造 Credential（失败抛异常）。"""
        cred = build_credential(self._cookies)
        if cred is None:
            raise RuntimeError("尚未取得可用的登录凭据")
        return cred


def _get(data: Any, key: str, default: Any = "") -> Any:
    return data.get(key, default) if isinstance(data, dict) else default


def _as_dict(raw: Any) -> dict[str, Any]:
    """``Api.result`` 一般已是 dict；但以 bytes/str 返回时需要自行解码。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            parsed = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
