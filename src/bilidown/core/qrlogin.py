#架构：扫码会话经官方轮询接口取登录结果；票据交换单独请求并从URL/Set-Cookie恢复凭据
"""扫码登录会话：请求官方端点并自行解析响应

用库提供的通用``Api``客户端请求官方端点，按标准查询串规则解析，
解析失败时把原始响应记进日志
"""

from __future__ import annotations  #延迟解析类型标注

#依赖：异步轮询、二维码缓冲、登录URL及响应解析
import asyncio  #在线程中执行同步换票请求
import enum  #定义扫码状态枚举
import io  #承载二维码PNG内存缓冲
import json  #解码字符串形式的接口响应
import re  #抹除日志中的敏感查询参数
import urllib.parse  #分离登录URL查询参数
from dataclasses import dataclass, field  #存放一次轮询结果
from typing import Any  #容纳接口的不定形响应

from bilibili_api.utils.network import Api, Credential  #请求官方接口并构造凭据
from bilibili_api.utils.utils import get_api  #查询登录端点配置

from ..utils.logger import get_logger  #获取扫码日志器
from .network import create_session  #复用统一代理设置

_log = get_logger("qrlogin")  #记录扫码诊断信息

#模块常量：轮询状态码与需要过滤的非Cookie参数
_LOGIN_API = get_api("login")  #获取登录API描述

#与bilibili_api.login_v2.QrCodeLoginEvents的取值保持一致
_CODE_SCAN = 86101  #等待扫描
_CODE_CONF = 86090  #等待确认
_CODE_TIMEOUT = 86038  #二维码过期

#不属于cookie的字段
_NON_COOKIE_KEYS = frozenset(  #排除URL中非凭据字段
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

#枚举：对外统一扫码的等待、确认、超时与成功状态
class QrState(enum.Enum):
    """扫码登录状态，取值与库内的QrCodeLoginEvents相同"""

    SCAN = "scan"  #尚待扫描
    CONF = "confirm"  #已扫描待确认
    TIMEOUT = "timeout"  #二维码过期
    DONE = "done"  #已获得登录凭据
    ERROR = "error"  #轮询或解析失败


#模型：保留一次轮询的状态、Cookie和原始响应
@dataclass(slots=True)  #限制实例属性集合
class QrPollResult:
    """一次轮询的结果（保留原始响应以便诊断与解析）"""

    state: QrState  #本次扫码状态
    cookies: dict[str, str] = field(default_factory=dict)  #已解析的Cookie
    message: str = ""  #提供给调用方的错误信息
    raw: dict[str, Any] = field(default_factory=dict)  #保留接口响应

    #属性：便于调用方辨别错误结果
    @property  #用属性访问状态判断
    def okay(self) -> bool:  #报告轮询是否无错误
        return self.state is not QrState.ERROR  #其他状态均允许继续处理


#函数：从URL原始查询串提取未解码的登录Cookie
def parse_login_cookies(raw_url: str, refresh_token: str = "") -> dict[str, str]:
    """从crossDomain登录URL中解析出cookie（不做百分号解码）

    按``&``切分、在第一个``=``处分割，不触碰值的编码
    """
    result: dict[str, str] = {}  #收集原始编码的查询值
    if not raw_url:  #空URL不包含凭据
        return result  #返回空集合

    try:  #URL解析可能因括号等格式失败
        parsed = urllib.parse.urlsplit(raw_url)  #拆分查询串和片段
    except ValueError:  #无法识别URL
        _log.warning("登录返回的 url 无法解析: %r", raw_url[:120])  #记录诊断片段
        return result  #避免使用无效URL

    #query与fragment都解析（跨域登录链接有时把参数放在fragment里）
    chunks = [c for c in (parsed.query, parsed.fragment) if c]  #收集非空参数区
    if not chunks and "?" in raw_url:  #标准解析没有识别查询串
        #兜底：url不是标准结构时，直接取第一个 ? 之后的部分
        chunks = [raw_url.split("?", 1)[1]]  #取问号后的原始串

    for chunk in chunks:  #依次解析query和fragment
        for pair in chunk.split("&"):  #按参数分隔符拆分
            if not pair or "=" not in pair:  #跳过没有键值形式的参数
                continue  #处理下一项
            key, _, value = pair.partition("=")  #只在第一个等号处分割
            if not key or not value:  #忽略空键与空值
                continue  #保留其他参数
            if key.endswith("__ckMd5") or key in _NON_COOKIE_KEYS:  #过滤非Cookie字段
                continue  #防止混入业务参数
            result.setdefault(key, value)  #保留首次取得的原始值

    #cookie名称大小写在不同渠道间可能不一致，统一成库期望的写法
    canonical = {"sessdata": "SESSDATA", "bili_jct": "bili_jct", "dedeuserid": "DedeUserID"}  #定义规范键名
    for key in list(result):  #遍历副本以允许改名
        want = canonical.get(key.lower())  #查找大小写对应的键名
        if want and key != want:  #只修正非规范写法
            result.setdefault(want, result.pop(key))  #保留规范名的现有值

    if refresh_token and not result.get("ac_time_value"):  #有刷新令牌但未从链接得到
        result["ac_time_value"] = refresh_token  #补全刷新令牌
    return result  #返回原始编码的登录参数


#函数：把已提取Cookie转成第三方凭据对象
def build_credential(cookies: dict[str, str]) -> Credential | None:
    """把cookie字典转成Credential；缺少SESSDATA时返回None"""
    sessdata = cookies.get("SESSDATA")  #提取必要的登录会话值
    if not sessdata:  #不能创建匿名凭据
        return None  #通知调用方缺失必需字段
    return Credential(  #按库要求组合已有Cookie
        sessdata=sessdata,
        bili_jct=cookies.get("bili_jct") or None,
        buvid3=cookies.get("buvid3") or None,
        buvid4=cookies.get("buvid4") or None,
        dedeuserid=cookies.get("DedeUserID") or None,
        ac_time_value=cookies.get("ac_time_value") or None,
    )


#官方域名白名单：票据换票只允许访问这些域
_OFFICIAL_DOMAINS = ("bilibili.com", "biligame.com")  #覆盖主站与Passport域


#函数：校验URL是否为https且主机属于官方域
def _is_official_url(url: str) -> bool:
    """仅允许https协议且主机为bilibili.com/biligame.com及其子域"""
    try:  #防括号等造成解析异常
        parts = urllib.parse.urlsplit(url)  #拆分协议与主机
    except ValueError:  #无法识别的URL
        return False  #一律拒绝
    if parts.scheme != "https":  #拒绝明文或其他协议
        return False  #不进入换票路径
    host = (parts.hostname or "").lower()  #归一化主机名
    return any(host == d or host.endswith("." + d) for d in _OFFICIAL_DOMAINS)  #校验域白名单


#函数：抹掉文本里URL的查询串与片段，避免ticket/SESSDATA进日志
def _strip_query(text: str) -> str:
    """把文本中的 ?查询串 与 #片段 替换为省略标记"""
    return re.sub(r"([?#])[^\s\"']*", r"\1…", str(text))  #截断敏感参数区


#函数：通过ticket参数区分换票链接与直出Cookie链接
def is_ticket_url(url: str) -> bool:
    """判断登录返回的链接是否为「票据换票」链接

    这类链接里没有cookie，必须请求它（跟随302），由服务端用Set-Cookie下发
    """
    if not url or "ticket=" not in url:  #空链接或无换票参数
        return False  #不进入换票路径
    if not _is_official_url(url):  #非官方域或协议不安全
        _log.warning("票据链接目标不在官方域白名单，已拒绝: %s", _strip_query(url)[:120])  #记录脱敏后的目标
        return False  #拒绝访问非预期目标
    return "SESSDATA" not in url  #已附登录Cookie则不再换票


#只接受真正的登录cookie，避免把buvid3/b_nut等匿名标识误当凭据
_CREDENTIAL_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4")  #登录相关键名


#函数：从会话Cookie容器挑选登录凭据字段
def _collect_credential_cookies(
    jar: Any, start: dict[str, str] | None = None
) -> dict[str, str]:
    """从cookie jar（或普通dict）中挑出登录相关的cookie"""
    result: dict[str, str] = dict(start or {})  #继承已解析的参数
    try:  #Cookie jar也可能是不支持items的对象
        items = list(jar.items())  #读取响应中的键值对
    except (AttributeError, TypeError):  #非映射容器
        return result  #保留现有结果

    #按小写名建索引，兼容渠道间的大小写差异
    lowered: dict[str, str] = {}  #维护大小写无关索引
    for key, value in items:  #遍历容器Cookie
        if value:  #排除空字段
            lowered.setdefault(str(key).lower(), str(value))  #首值优先

    for want in _CREDENTIAL_KEYS:  #只提取登录字段
        if not result.get(want):  #已有字段保持不变
            value = lowered.get(want.lower())  #匹配不区分大小写的键
            if value:  #有效值才写入
                result[want] = value  #补齐Cookie
    result.pop("DedeUserID__ckMd5", None)  #去掉非登录校验项
    return result  #返回汇总的登录Cookie


#函数：访问票据地址并合并重定向后的凭据Cookie
def exchange_ticket(ticket_url: str, refresh_token: str = "") -> dict[str, str]:
    """请求票据链接并跟随302，从Set-Cookie中取出登录凭据

    SESSDATA / bili_jct / DedeUserID由该链接响应头的Set-Cookie下发；
    函数会阻塞，调用方需放到工作线程（poll内部已用asyncio.to_thread）
    """
    import requests  #noqa: PLC0415 - 与慢启动解耦

    from ..config.settings import DOWNLOAD_TIMEOUT  #获取换票请求超时

    cookies: dict[str, str] = {}  #合并多个Cookie来源
    #统一的会话策略：默认绕过失效的系统代理
    if not _is_official_url(ticket_url):  #请求前再做一次白名单防御
        _log.warning("换票目标不在官方域白名单，已拒绝: %s", _strip_query(ticket_url)[:120])  #仅记录脱敏目标
        return cookies  #返回空结果
    session = create_session()  #建立统一代理策略的会话
    try:  #确保最终关闭会话
        response = session.get(  #访问换票URL并跟随重定向
            ticket_url,
            headers={"Referer": "https://www.bilibili.com/"},
            timeout=DOWNLOAD_TIMEOUT,
            allow_redirects=True,
        )
        #来源1：最终落地URL的查询串（部分渠道仍然挂cookie）
        cookies.update(parse_login_cookies(str(response.url or ""), refresh_token))  #从落地URL读取参数
        #来源2/3：本次响应的Set-Cookie与整个会话cookie
        cookies.update(_collect_credential_cookies(response.cookies, cookies))  #读取当前响应
        cookies.update(_collect_credential_cookies(session.cookies, cookies))  #读取跨重定向会话

        if not cookies.get("SESSDATA"):  #未拿到登录所需Cookie
            _log.error(  #记录换票失败细节（URL已脱敏）
                "换票请求完成但没有拿到 SESSDATA。status=%s，最终 URL=%s，"
                "响应 Set-Cookie 名称=%s",
                response.status_code,
                _strip_query(str(response.url))[:120],
                sorted({c.name for c in response.cookies}),
            )
    except requests.RequestException as exc:  #HTTP请求失败
        _log.warning("换取登录 cookie 失败（网络）: %s", _strip_query(exc))  #输出脱敏后的网络错误
    finally:  #网络成功或失败均需释放会话
        session.close()  #关闭连接池

    if refresh_token and not cookies.get("ac_time_value"):  #响应未携带刷新令牌
        cookies["ac_time_value"] = refresh_token  #使用轮询返回的令牌
    return cookies  #返回可用于登录的Cookie


#类：持有二维码状态并在登录成功后构造可持久化凭据
class LoginSession:
    """一次扫码登录会话（自己持有二维码密钥与解析结果）

    生命周期::

        session = LoginSession()
        await session.generate()          # 拿到二维码PNG
        while True:
            result = await session.poll() # 轮询
            ...
    """

    #方法：初始化一次登录会话的密钥和结果容器
    def __init__(self) -> None:
        self._qrcode_key = ""  #尚未申请的轮询密钥
        self._qr_link = ""  #用于扫码的登录链接
        self._png: bytes = b""  #二维码PNG缓存
        self._cookies: dict[str, str] = {}  #已登录时保存Cookie

    #------------------------------------------------------------- 属性
    @property  #只读二维码数据
    def png(self) -> bytes:  #提供二维码PNG
        return self._png  #返回上次生成的内容

    @property  #只读扫码URL
    def link(self) -> str:  #提供二维码链接
        return self._qr_link  #返回官方扫码地址

    @property  #避免调用方直接修改内部字典
    def cookies(self) -> dict[str, str]:  #提供Cookie快照
        return dict(self._cookies)  #返回副本

    @property  #只读密钥有效性
    def has_key(self) -> bool:  #检查是否已获取轮询密钥
        return bool(self._qrcode_key)  #空密钥表示尚未生成

    #------------------------------------------------------------- 生成
    #方法：调用二维码接口并渲染PNG
    async def generate(self) -> bytes:
        """请求二维码与登录密钥，返回二维码PNG字节"""
        api = _LOGIN_API["qrcode"]["web"]["get_qrcode_and_token"]  #选取二维码端点
        data = await Api(credential=Credential(), **api).result  #请求二维码凭据

        self._qr_link = str(_get(data, "url"))  #保存扫码目标
        self._qrcode_key = str(_get(data, "qrcode_key"))  #保存轮询密钥
        if not self._qrcode_key:  #不能轮询缺失密钥的二维码
            raise RuntimeError("登录接口未返回 qrcode_key")  #反馈接口异常

        #复用库的图片渲染（纯本地操作，不涉及网络解析）
        import qrcode  #noqa: PLC0415 - 延迟导入，避免影响启动时间

        qr = qrcode.QRCode()  #创建二维码绘制器
        qr.add_data(self._qr_link)  #编入扫码URL
        image = qr.make_image()  #渲染二维码图像
        buffer = io.BytesIO()  #分配内存输出流
        image.save(buffer, format="PNG")  #保存PNG字节
        self._png = buffer.getvalue()  #缓存返回图像
        return self._png  #将PNG交给界面

    #------------------------------------------------------------- 轮询
    #方法：请求扫码轮询端点并规范化响应
    async def _query_poll_endpoint(self) -> dict[str, Any]:
        """请求 /qrcode/poll并返回data字典（独立成方法，便于测试替身）"""
        api = _LOGIN_API["qrcode"]["web"]["get_events"]  #读取轮询端点配置
        params = dict(api.get("params") or {})  #复制默认查询参数
        params["qrcode_key"] = self._qrcode_key  #指向当前扫码会话
        raw = (  #通过官方接口轮询状态
            await Api(
                url=api["url"],
                method=api["method"],
                verify=api.get("verify", False),
                credential=Credential(),
            )
            .update_params(**params)
            .result
        )
        return _as_dict(raw)  #统一为字典结构

    #方法：区分扫码状态；成功时从URL或票据交换取得Cookie
    async def poll(self) -> QrPollResult:
        """查询一次扫码状态；成功时完成「换票」取得cookie"""
        if not self._qrcode_key:  #尚未生成可轮询二维码
            return QrPollResult(QrState.ERROR, message="尚未生成二维码")  #报告前置条件失败

        try:  #捕获本次状态请求异常
            data = await self._query_poll_endpoint()  #取得官方轮询响应
        except Exception as exc:  #noqa: BLE001 - 交给调用方决定是否重试
            _log.warning("扫码状态查询失败: %r", exc)  #留下异常信息
            return QrPollResult(QrState.ERROR, message=str(exc))  #提供重试判断依据

        code = data.get("code")  #读取扫码状态码
        refresh_token = str(data.get("refresh_token") or "")  #获取刷新令牌
        login_url = str(data.get("url") or "")  #获取结果链接

        if code == _CODE_SCAN:  #等待手机扫码
            return QrPollResult(QrState.SCAN, raw=data)  #继续轮询
        if code == _CODE_CONF:  #等待手机确认
            return QrPollResult(QrState.CONF, raw=data)  #继续轮询
        if code == _CODE_TIMEOUT:  #二维码超过有效期
            return QrPollResult(QrState.TIMEOUT, raw=data)  #通知重新生成
        if code != 0:  #未知状态码不按成功处理
            _log.warning("扫码轮询返回未知状态码: %r", code)  #记录未识别的状态
            return QrPollResult(QrState.ERROR, message=f"未知扫码状态: {code}", raw=data)  #明确报告异常

        #第一步：有些渠道会把cookie直接挂在url的查询串上
        cookies = parse_login_cookies(login_url, refresh_token)  #首先解析直出Cookie

        #第二步：url只带ticket时请求它，由服务端用302 + Set-Cookie下发凭据
        if not cookies.get("SESSDATA") and is_ticket_url(login_url):  #需要票据换取登录Cookie
            _log.info("登录返回的是 ticket 链接，开始换取 cookie…")  #记录换票开始
            cookies = await asyncio.to_thread(exchange_ticket, login_url, refresh_token)  #在线程中请求票据

        if not cookies.get("SESSDATA"):  #两种获取方式均失败
            #关键诊断：把原始响应写进日志，便于定位接口变化（URL只留主机与长度）
            _log.error(  #输出未取到凭据的响应摘要
                "扫码成功但未解析出 SESSDATA。响应键=%s，url 长度=%s，url 目标=%s",
                sorted(data),
                len(login_url),
                _strip_query(login_url)[:120],
            )
            return QrPollResult(  #返回缺失凭据的诊断结果
                QrState.ERROR,
                message="登录成功但未能取得凭据（SESSDATA 缺失）",
                raw=data,
            )

        self._cookies = cookies  #缓存成功取得的登录信息
        _log.info(  #输出凭据字段存在性信息
            "登录凭据获取成功：SESSDATA=%s 字符，bili_jct=%s，DedeUserID=%s",
            len(cookies.get("SESSDATA", "")),
            "有" if cookies.get("bili_jct") else "无",
            cookies.get("DedeUserID") or "无",
        )
        return QrPollResult(QrState.DONE, cookies=cookies, raw=data)  #返回成功结果

    #方法：从已确认的Cookie构建正式凭据
    def credential(self) -> Credential:
        """用已解析的cookie构造Credential（失败抛异常）"""
        cred = build_credential(self._cookies)  #将本次Cookie组合成凭据
        if cred is None:  #未拿到完整凭据
            raise RuntimeError("尚未取得可用的登录凭据")  #拒绝空凭据
        return cred  #交给调用方持久化


#函数：容错读取接口字典的字段
def _get(data: Any, key: str, default: Any = "") -> Any:
    return data.get(key, default) if isinstance(data, dict) else default  #仅字典支持字段查找


#函数：兼容dict、JSON字符串和字节响应
def _as_dict(raw: Any) -> dict[str, Any]:
    """``Api.result`` 一般已是dict；但以bytes/str返回时需要自行解码"""
    if isinstance(raw, dict):  #已是预期响应结构
        return raw  #原样返回接口字典
    if isinstance(raw, (bytes, bytearray)):  #二进制JSON响应
        try:  #捕获无效编码及JSON
            parsed = json.loads(bytes(raw).decode("utf-8"))  #解码UTF-8 JSON
        except (ValueError, UnicodeDecodeError):  #非法响应
            return {}  #以空字典表明无法解析
        return parsed if isinstance(parsed, dict) else {}  #仅接受对象
    if isinstance(raw, str):  #文本JSON响应
        try:  #捕获非法JSON
            parsed = json.loads(raw)  #解析文本内容
        except ValueError:  #无法解码
            return {}  #防止向上透出格式错误
        return parsed if isinstance(parsed, dict) else {}  #排除非对象
    return {}  #未知响应类型视为无数据
