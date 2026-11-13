#架构：同步门面经后台事件循环调用B站API；纯解析函数、搜索数据模型与流选择器分离
"""B站API客户端封装

设计要点：
  - **UI层不接触asyncio**：对外只暴露同步接口，内部提交到BackgroundLoop
  - 纯函数（parse_bv / sanitize_filename）与网络调用分离，便于单元测试
"""

from __future__ import annotations  #延迟求值类型标注

#依赖：文本解析、数据模型及B站网络调用
import re  #校验编号与净化文件名
from collections.abc import Callable, Sequence  #描述回调及分页数据
from dataclasses import dataclass, field  #存放搜索和流选择结果
from typing import Any  #接纳接口不定形值

from bilibili_api import search, user, video  #调用视频、搜索和账号接口
from bilibili_api.utils.network import Credential  #管理第三方登录凭据

from ..config.settings import SEARCH_PAGE_SIZE  #控制搜索分页数量
from ..utils.logger import get_logger  #获取日志器
from .bridge import BackgroundLoop  #将协程送入后台循环
from .credential import CredentialStore  #保存和恢复登录凭据
from .models import DownloadKind, VideoItem, VideoPage  #共享下载数据模型
from .qrlogin import LoginSession, QrState  #实现扫码登录状态机

_log = get_logger("client")  #建立客户端日志器

#---------------------------------------------------------------- 纯函数部分
#模块分组：输入解析、文件名净化、接口数值和流地址提取不执行网络请求

#与库内部校验保持一致：BV + 10位 [A-Za-z0-9]
_BV_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")  #编译完整BV号模式

#Windows非法文件名字符 + 控制字符
_ILLEGAL_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')  #识别Windows非法字符
_RESERVED_NAMES = {  #列举Windows保留设备名
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_STEM_LENGTH = 80  #限制视频标题主干长度


#函数：从裸编号或输入文本提取BV号
def parse_bv(text: str) -> str | None:
    """从用户输入中解析BV号

    支持整条链接（https://www.bilibili.com/video/BV1xx411c7mD/?p=1）与裸BV号
    """
    if not text:  #空输入不包含编号
        return None  #提示调用方解析失败
    #大小写不敏感匹配完整十位主体，匹配结果统一前缀为大写BV
    match = re.search(r"(?<![0-9A-Za-z])bv([0-9A-Za-z]{10})(?![0-9A-Za-z])", text.strip(), re.IGNORECASE)  #提取十位主体
    return f"BV{match.group(1)}" if match else None  #统一前缀或返回空值


#函数：粗略识别输入中是否包含HTTP链接
def looks_like_link(text: str) -> bool:
    return "http://" in text or "https://" in text  #粗略识别链接片段


#函数：将标题净化成目标平台的文件名主干
def sanitize_filename(name: str, *, max_length: int = _MAX_STEM_LENGTH) -> str:
    """把视频标题净化成安全的文件名主干

    - 换行/制表等控制字符先转成空格，再折叠连续空白
    - 其余Windows非法字符替换为下划线
    """
    cleaned = (name or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")  #空白控制符转空格
    cleaned = _ILLEGAL_FS_CHARS.sub("_", cleaned)  #替换非法字符
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")  #折叠空白及结尾点
    if len(cleaned) > max_length:  #需要限制主干长度
        cleaned = cleaned[:max_length].rstrip(" .")  #截断并清除尾部空格和点
    #按首个点前的设备名判断，CON.txt等带扩展名的保留名同样会被避开
    if cleaned.split(".", 1)[0].upper() in _RESERVED_NAMES:  #首段匹配设备名
        cleaned = f"_{cleaned}"  #添加前缀避开保留名
    return cleaned or "untitled"  #对空标题使用兜底文件名


#函数：兼容多种时长表示，统一返回秒数
def parse_duration(value: Any) -> int:
    """把各种形态的时长解析成秒数，解析不了就返回0

    支持的形态：

    - ``469``           纯秒数
    - ``"7:49"``        ``"分:秒"`` 字符串
    - ``"1:07:49"``     ``"时:分:秒"``
    - ``"469"``         数字字符串
    """
    if value is None or isinstance(value, bool):  #排除无效和布尔输入
        return 0  #不把布尔值当秒数
    if isinstance(value, (int, float)):  #数值无需拆分
        return int(value)  #转换成整数秒
    text = str(value).strip()  #兼容文本时间
    if not text:  #空白输入
        return 0  #使用零时长
    if ":" in text:  #分或时分秒格式
        total = 0  #累计换算的秒数
        for part in text.split(":"):  #从左至右解析每段
            part = part.strip()  #去除段前后空格
            if not part.isdigit():  #非整数段不可换算
                return 0  #返回无效时长
            total = total * 60 + int(part)  #逐段乘六十累加
        return total  #返回换算秒数
    try:  #尝试纯数字和千分位文本
        return int(float(text.replace(",", "")))  #转成整数秒
    except (TypeError, ValueError):  #无法转换为数值
        return 0  #无效时长视为零


#函数：将数量、单位与高亮文本转换成整数
def parse_count(value: Any) -> int:
    """把播放量/弹幕数等解析成整数，解析不了就返回0

    兼容 ``12345``、``"12345"``、``"1,234"``、``"12.3万"``、``"1.2亿"``，
    以及接口偶尔返回的带高亮标签的字符串
    """
    if value is None or isinstance(value, bool):  #空值和布尔值不是数量
        return 0  #返回安全默认值
    if isinstance(value, (int, float)):  #数字可直接截取整数部分
        return int(value)  #返回整数计数
    text = _clean_html(str(value)).strip()  #去掉搜索高亮标签
    if not text:  #空白计数字段
        return 0  #视为零

    multiplier = 1  #默认无中文单位
    for suffix, factor in (("万", 10_000), ("亿", 100_000_000)):  #识别单位倍率
        if text.endswith(suffix):  #匹配末尾单位
            multiplier = factor  #记录缩放因子
            text = text[: -len(suffix)]  #去除中文单位
            break  #已选中对应单位
    text = text.replace(",", "").strip()  #清理千分位与空格
    try:  #数量可能含小数
        return int(float(text) * multiplier)  #转换成整数总量
    except (TypeError, ValueError):  #数字部分不合法
        return 0  #回退零值


#函数：兼容两种DASH主流地址字段名
def _stream_url(stream: dict[str, Any]) -> str:
    """取流的下载地址，兼容baseUrl / base_url两种写法"""
    return str(stream.get("baseUrl") or stream.get("base_url") or "")  #统一流地址类型


#函数：将DASH备用地址转换为字符串列表
def _stream_backups(stream: dict[str, Any]) -> list[str]:
    """取备用地址列表（B站dash的backupUrl可能是列表，也可能是单个字符串）"""
    raw = stream.get("backupUrl") or stream.get("backup_url") or []  #兼容两种字段名
    if isinstance(raw, str):  #单一备用地址
        return [raw] if raw else []  #空地址不加入列表
    if isinstance(raw, (list, tuple)):  #多条备用地址
        return [str(url) for url in raw if url]  #跳过空地址
    return []  #不支持的字段形态


#函数：按编码前缀归类流格式
def codecs_to_kind(codecs: Any) -> str:
    """把dash里的 ``codecs`` 字符串归类为 ``avc`` / ``hev`` / ``av01`` / ``other``

    真实取值形如 ``avc1.64001F``、``hvc1.1.6.L120.90``、
    ``av01.0.08M.08.0.110.01.01.01.0``
    """
    text = str(codecs or "").lower()  #规范化编码名
    if text.startswith("avc") or text.startswith("h264"):  #H.264编码
        return "avc"  #返回AVC类别
    if text.startswith("hev") or text.startswith("hvc") or text.startswith("h265"):  #H.265编码
        return "hev"  #返回HEVC类别
    if text.startswith("av01") or text.startswith("av1"):  #AV1编码
        return "av01"  #返回AV1类别
    return "other"  #兜底未知编码


#编码偏好：同档位下AVC(H.264)优先
_CODEC_PREFERENCE = ("avc", "hev", "av01", "other")  #同档位编码优先顺序

#qn（清晰度id）→ (界面标签, 权限要求)。权限要求："" 免费 / "login" 需登录 / "vip" 需大会员
#依据B站playurl的通行规则归纳；具体视频可能没有某一档，下载时会自动回退
QUALITY_TABLE: dict[int, tuple[str, str]] = {  #清晰度对应显示名与访问条件
    127: ("8K 超高清", "vip"),
    126: ("杜比视界", "vip"),
    125: ("HDR 真彩", "vip"),
    120: ("4K 超清", "vip"),
    116: ("1080P 60帧", "vip"),
    112: ("1080P 高码率", "vip"),
    80: ("1080P 高清", "login"),
    74: ("720P 60帧", "login"),
    64: ("720P 高清", "login"),
    32: ("480P 清晰", ""),
    16: ("360P 流畅", ""),
}

#从高到低，用于界面下拉与「自动」档位的兜底
QUALITY_ORDER: tuple[int, ...] = tuple(sorted(QUALITY_TABLE, reverse=True))  #按质量从高到低


#函数：查询清晰度档位的访问权限
def quality_permission(qn: int) -> str:
    return QUALITY_TABLE.get(qn, ("", ""))[1]  #未知档位默认无权限提示


#函数：格式化清晰度档位及权限提示
def quality_label(qn: int) -> str:
    base = QUALITY_TABLE.get(qn, (f"qn{qn}", ""))[0]  #获取界面显示名
    need = quality_permission(qn)  #获取该档位权限
    if need == "login":  #需登录才能访问
        return f"{base}（需登录）"  #显示登录条件
    if need == "vip":  #需会员才能访问
        return f"{base}（需大会员）"  #显示会员条件
    return base  #免费档位只显示名称


#函数：优先按清晰度、编码及带宽挑选DASH音视频流
def select_streams_from_dash(
    data: dict[str, Any],
    kind: DownloadKind = DownloadKind.VIDEO,
    qn: int = 0,
    audio_qn: int = 0,
) -> StreamSelection:
    """从 ``get_download_url`` 的响应里挑选音视频流

    挑选规则：
      1. **先按清晰度**（``id``，即qn）取最高档；
      2. 同一档内**按编码偏好**选（AVC优先）；
      3. 再按带宽取该编码下最高的
    """
    dash = (data or {}).get("dash") or {}  #提取DASH音视频信息
    select = StreamSelection()  #初始化返回流组合

    audios = [s for s in (dash.get("audio") or []) if isinstance(s, dict)]  #过滤异常音频项
    if audios:  #存在音频流
        pool = audios  #默认在所有音频中挑选
        if audio_qn:  #用户指定音频档位
            exact = [s for s in audios if parse_count(s.get("id")) == audio_qn]  #查找精确档
            if exact:  #指定档位可用
                pool = exact  #限定候选集合
        best_audio = max(  #优先音质档位再比较带宽
            pool, key=lambda s: (parse_count(s.get("id")), parse_count(s.get("bandwidth")))
        )
        select.audio_url = _stream_url(best_audio)  #保存音频主地址
        select.audio_backups = _stream_backups(best_audio)  #保存备用CDN
        select.audio_label = f"id{parse_count(best_audio.get('id'))}"  #标注音频档位

    if kind is DownloadKind.VIDEO:  #视频下载需选视频流
        videos = [  #排除非字典或无主地址的条目
            s
            for s in (dash.get("video") or [])
            if isinstance(s, dict) and _stream_url(s)
        ]
        if videos:  #有可用视频流
            pool = videos  #默认使用全部档位
            if qn:  #用户选定目标清晰度
                #用户指定了清晰度：优先精确命中；没有该档时退到不高于目标的最高档
                exact = [s for s in videos if parse_count(s.get("id")) == qn]  #精确查找档位
                if exact:  #指定档位存在
                    pool = exact  #限制候选到指定档
                else:  #指定档位不可用
                    lower = [s for s in videos if parse_count(s.get("id")) <= qn]  #查找较低档
                    pool = lower or videos  #无低档时退回全部流
            best = max(  #清晰度优先，其次编码和带宽
                pool,
                key=lambda s: (
                    parse_count(s.get("id")),  #清晰度优先
                    -_CODEC_PREFERENCE.index(codecs_to_kind(s.get("codecs"))),  #AVC优先
                    parse_count(s.get("bandwidth")),  #同档内取高码率
                ),
            )
            select.video_url = _stream_url(best)  #保存视频主地址
            select.video_backups = _stream_backups(best)  #保存视频备用CDN
            quality = parse_count(best.get("id"))  #读取实际获取的画质档位
            select.quality_label = f"{quality_label(quality)}·{codecs_to_kind(best.get('codecs'))}"  #标注画质及编码

    if not select.video_url and not select.audio_url:  #完全未得到媒体流
        raise ClientError("未能解析出可用的音视频流，可能是会员/地区限制")  #报告访问限制
    if kind is DownloadKind.VIDEO and not select.video_url:  #视频任务缺失视频流
        raise ClientError("未能解析出可用的视频流（可能只提供试看片段）")  #禁止仅音频视频任务
    if not select.audio_url:  #所有下载类型均要求音频
        raise ClientError("未能解析出可用的音频流")  #报告音频不可用
    return select  #返回完整媒体流选择


#---------------------------------------------------------------- 数据模型
#模型分组：承载搜索结果、选出的流及用户可读异常


@dataclass(slots=True)  #限制搜索结果属性
class SearchResult:  #搜索结果及分页信息
    """一次搜索的结果"""

    items: list[VideoItem] = field(default_factory=list)  #返回的视频条目
    page: int = 1  #当前页码
    keyword: str = ""  #本次搜索词

    #方法：让结果对象可直接获取条目数
    def __len__(self) -> int:  #实现容器长度协议
        return len(self.items)  #返回视频数量


@dataclass(slots=True)  #固定流组合字段
class StreamSelection:  #音视频流地址与备用地址
    """挑选好的音视频流地址

    ``*_backups``是B站dash响应里自带的备用CDN地址，主地址失败时可回退
    """

    video_url: str = ""  #选中视频主地址
    audio_url: str = ""  #选中音频主地址
    quality_label: str = ""  #视频档位说明
    audio_label: str = ""  #音频档位说明
    video_backups: list[str] = field(default_factory=list)  #视频备用地址
    audio_backups: list[str] = field(default_factory=list)  #音频备用地址

    #属性：是否已经选出视频流
    @property  #提供只读便捷判断
    def has_video(self) -> bool:  #检查视频主地址
        return bool(self.video_url)  #有地址视为有视频流


class ClientError(RuntimeError):  #面向调用方的网络业务异常
    """面向用户的可读错误"""


#---------------------------------------------------------------- 客户端
#类分组：BiliClient将登录、搜索、分P和下载地址调用统一交给后台事件循环


class BiliClient:  #将异步B站API封装为同步客户端
    """bilibili_api的同步门面

    所有网络方法都是同步的，内部通过BackgroundLoop提交协程并等待结果
    因此**不得在Tk主线程调用**，否则仍会阻塞界面
    """

    #方法：初始化后台循环、凭据存储和扫码会话
    def __init__(  #创建客户端门面
        self,
        bridge: BackgroundLoop | None = None,
        store: CredentialStore | None = None,
    ) -> None:
        self._bridge = bridge or BackgroundLoop.instance()  #复用默认后台事件循环
        self._store = store or CredentialStore()  #复用或创建本地凭据存储
        self.credential: Credential | None = None  #暂无登录凭据
        self._qr: LoginSession | None = None  #暂无扫码会话

    #------------------------------------------------------------ 凭据管理
    #方法：从本地恢复上次登录的Cookie
    def restore_credential(self) -> bool:  #恢复凭据但不校验远端有效性
        """启动时尝试恢复上次登录，返回是否恢复成功（不代表仍然有效）"""
        cookies = self._store.load()  #读取本地Cookie
        if not cookies:  #未保存可用记录
            return False  #标识未恢复
        try:  #兼容损坏或不完整的Cookie
            self.credential = Credential.from_cookies(cookies)  #构造第三方凭据
        except Exception:  #noqa: BLE001
            _log.warning("恢复凭据失败", exc_info=True)  #留存异常信息
            return False  #恢复失败
        _log.info("已从本地恢复登录凭据")  #记录恢复状态
        return True  #标识已恢复

    #方法：将当前登录Cookie写入本地
    def save_credential(self) -> None:  #持久化已获取凭据
        if self.credential is None:  #没有登录凭据
            return  #无需保存
        try:  #存储可能因磁盘或权限失败
            self._store.save(self.credential.get_cookies())  #提取并保存Cookie
        except Exception:  #noqa: BLE001
            _log.warning("保存凭据时出错", exc_info=True)  #仅记录持久化失败

    #方法：清空内存和本地凭据
    def logout(self) -> None:  #退出当前登录
        self.credential = None  #先移除内存凭据
        self._store.clear()  #清理保存的Cookie

    #属性：是否保有登录凭据，不代表远端凭据仍有效
    @property  #暴露只读登录状态
    def logged_in(self) -> bool:  #检查内存中是否存在凭据
        return self.credential is not None  #凭据存在即视为已登录

    #方法：通过远端接口校验当前凭据
    def validate_credential(self) -> bool | None:
        """同步校验凭据是否仍然有效

        返回值区分三种结果：
          - ``True``  远端确认凭据有效；
          - ``False`` 远端明确判定凭据无效，调用方可以安全登出；
          - ``None``  超时/网络等暂时失败，无法判定，调用方**不应**据此删除凭据
        """
        if self.credential is None:  #没有待校验凭据
            return False  #无登录状态
        try:  #执行远端验证
            return bool(self._bridge.run_sync(self.credential.check_valid(), timeout=20))  #返回有效标记
        except Exception:  #noqa: BLE001
            _log.info("凭据校验出现暂时性失败", exc_info=True)  #记录校验异常
            return None  #暂时失败不代表无效

    #---------------------------------------------------------------- 登录
    #方法：创建扫码会话并生成二维码
    def login_qrcode_start(self) -> bytes:  #获取扫码图片字节
        """生成二维码，返回PNG字节流"""
        self._qr = LoginSession()  #初始化新的扫码会话
        return self._bridge.run_sync(self._qr.generate(), timeout=30)  #同步等待图片

    #方法：查询一次扫码会话状态
    def login_qrcode_poll(self) -> QrState:  #返回扫码状态枚举
        """查询一次扫码状态"""
        if self._qr is None:  #尚无扫码会话
            raise ClientError("尚未生成二维码")  #要求先生成二维码
        result = self._bridge.run_sync(self._qr.poll(), timeout=30)  #同步查询状态
        if result.state is QrState.ERROR:  #轮询错误视为可恢复
            #网络抖动等可恢复错误：返回SCAN让上层继续轮询，只记日志
            _log.warning("扫码轮询出现可恢复错误: %s", result.message)  #留下异常原因
            return QrState.SCAN  #继续轮询
        return result.state  #返回实际状态

    #方法：从扫码结果获取凭据并尝试持久化
    def login_qrcode_credential(self) -> Credential:  #完成扫码登录
        """登录成功后取凭据并持久化，解析失败时抛出可读错误"""
        if self._qr is None:  #缺少扫码会话
            raise ClientError("尚未生成二维码")  #提示先生成二维码
        try:  #获取扫码端已解析的凭据
            cred = self._qr.credential()  #读取登录凭据
        except RuntimeError as exc:  #尚未取得有效登录信息
            raise ClientError(str(exc)) from exc  #转换为可读错误
        self.credential = cred  #更新当前账号凭据
        self.save_credential()  #尝试写入本地存储
        return cred  #返回登录凭据

    #------------------------------------------------------------ 用户信息
    #方法：请求当前登录用户资料
    def fetch_self_info(self) -> dict[str, Any]:  #获取账号信息
        """获取登录用户信息（异步失败只记日志，由调用方决定UI表现）"""
        if self.credential is None:  #账号尚未登录
            raise ClientError("未登录")  #拒绝无凭据请求
        return self._bridge.run_sync(user.get_self_info(credential=self.credential), timeout=30)  #同步获取资料

    #---------------------------------------------------------------- 搜索
    #方法：通过搜索端点批量映射视频摘要
    def search_videos(self, keyword: str, page: int = 1) -> SearchResult:
        """关键词搜索，cid在真正下载时再解析（见ensure_cid）"""
        keyword = keyword.strip()  #清理搜索关键词首尾空格
        if not keyword:  #搜索词不能为空
            raise ClientError("请输入搜索关键词")  #提示填写关键词
        try:  #调用B站搜索接口
            raw = self._bridge.run_sync(  #同步等待搜索结果
                search.search_by_type(  #按视频类型搜索
                    keyword=keyword,  #传入关键词
                    search_type=search.SearchObjectType.VIDEO,  #限定视频结果
                    page=page,  #指定搜索页码
                    page_size=SEARCH_PAGE_SIZE,  #使用配置页大小
                ),
                timeout=45,  #限制等待时间
            )
        except Exception as exc:  #noqa: BLE001
            raise ClientError(f"搜索失败：{exc}") from exc  #转换为客户端错误

        items: list[VideoItem] = []  #累积解析后条目
        skipped = 0  #统计坏数据项
        for entry in raw.get("result", []) or []:  #遍历搜索结果
            try:  #隔离单条数据解析错误
                bvid = entry.get("bvid")  #读取视频编号
                if not bvid:  #编号为空无法定位视频
                    continue  #跳过当前结果
                items.append(  #构造列表摘要
                    VideoItem(  #初始化视频模型
                        bvid=bvid,  #写入BV编号
                        cid=0,  #延迟解析
                        title=_clean_html(entry.get("title", "")),  #去掉标题高亮标签
                        author=entry.get("author", ""),  #读取作者昵称
                        #duration/play等字段形态不一，统一走健壮解析
                        play=parse_count(entry.get("play")),  #解析播放量
                        danmaku=parse_count(entry.get("danmaku")),  #解析弹幕量
                        duration=parse_duration(entry.get("duration")),  #解析视频时长
                        cover=_normalize_pic(entry.get("pic", "")),  #补全搜索封面地址
                    )
                )
            except Exception:  #noqa: BLE001 - 单条脏数据不应拖垮整页结果
                skipped += 1  #记录跳过数量
                _log.debug("跳过无法解析的搜索结果项: %r", entry, exc_info=True)  #记录异常条目
        if skipped:  #存在无法解析项目
            _log.warning("搜索结果中有 %d 条无法解析，已跳过", skipped)  #汇报数据缺失数量
        return SearchResult(items=items, page=page, keyword=keyword)  #返回分页结果

    #方法：按BV号请求详情并解析首页CID
    def fetch_by_bvid(self, bvid: str) -> VideoItem:
        """按BV号取单条视频信息，cid从 ``pages[0]["cid"]`` 提取"""
        try:  #请求视频详情数据
            info = self._bridge.run_sync(  #同步等待详情
                video.Video(bvid=bvid, credential=self.credential).get_info(), timeout=30  #获取基本信息
            )
        except Exception as exc:  #noqa: BLE001
            raise ClientError(f"获取视频信息失败：{exc}") from exc  #提供统一错误类型

        pages: Sequence[dict[str, Any]] = info.get("pages") or []  #读取分P数据
        if not pages:  #详情中没有分P
            raise ClientError("该视频没有可用分 P")  #拒绝无页面视频
        cid = parse_count(pages[0].get("cid"))  #解析首页CID
        if not cid:  #首页CID缺失或无效
            raise ClientError("该视频的分 P 信息缺少 cid")  #报告响应数据问题
        stat = info.get("stat") or {}  #读取播放统计
        owner = info.get("owner") or {}  #读取作者资料
        return VideoItem(  #组装完整视频条目
            bvid=info.get("bvid") or bvid,  #优先使用接口返回编号
            cid=cid,  #使用首页CID
            title=_clean_html(info.get("title", "")),  #去除标题标签
            author=owner.get("name", ""),  #设置作者名称
            play=parse_count(stat.get("view")),  #解析播放次数
            danmaku=parse_count(stat.get("danmaku")),  #解析弹幕数量
            duration=parse_duration(info.get("duration")),  #解析总时长
            cover=info.get("pic", ""),  #使用详情封面地址
        )

    #方法：同步补齐延迟加载的视频CID
    def ensure_cid(self, item: VideoItem) -> int:
        """确保item.cid可用（搜索结果里为0时再请求一次）"""
        if item.cid:  #已有缓存CID
            return item.cid  #直接复用
        return self._bridge.run_sync(self.ensure_cid_async(item), timeout=30)  #同步等待补全

    #方法：在已有后台循环内异步补齐CID
    async def ensure_cid_async(self, item: VideoItem) -> int:
        """ensure_cid的协程版本（供后台loop内部使用，避免同loop死锁）"""
        if not item.cid:  #需要请求首页CID
            item.cid = await video.Video(  #通过详情接口读取CID
                bvid=item.bvid, credential=self.credential  #带上账号凭据
            ).get_cid(0)  #明确请求第1 P
        return item.cid  #返回可用CID

    #------------------------------------------------------------ 分P列表
    #方法：同步查询分P数据
    def fetch_pages(self, bvid: str) -> list[VideoPage]:  #供同步调用方使用
        """取某视频的全部分P（单P视频也会返回1条）"""
        return self._bridge.run_sync(self.fetch_pages_async(bvid), timeout=30)  #调度查询协程

    #方法：异步请求并转换视频分P列表
    async def fetch_pages_async(self, bvid: str) -> list[VideoPage]:  #返回序号化的分P信息
        try:  #查询分P接口
            raw = await video.Video(bvid=bvid, credential=self.credential).get_pages()  #获取原始列表
        except Exception as exc:  #noqa: BLE001
            raise ClientError(f"获取分 P 列表失败：{exc}") from exc  #统一错误类型
        pages: list[VideoPage] = []  #创建结果列表
        for idx, entry in enumerate(raw or [], start=1):  #从1开始编号
            if not isinstance(entry, dict):  #忽略非字典项
                continue  #处理下一项
            pages.append(  #转换为内部数据模型
                VideoPage(  #建立单个分P
                    index=idx,  #保存页序号
                    cid=parse_count(entry.get("cid")),  #解析内容编号
                    title=_clean_html(entry.get("part") or ""),  #清理分P标题
                    duration=parse_duration(entry.get("duration")),  #解析分段时长
                )
            )
        return pages  #返回已解析列表

    #方法：获取指定页CID；指定非第1 P拿不到时明确报错，仅首页允许回退
    async def _cid_for_page(self, item: VideoItem, page_index: int) -> int:
        """解析指定分P的cid；非第1 P缺失时抛错，第1 P退回item.cid补全"""
        if not item.pages:  #还未缓存分P信息
            try:  #尝试获取全部页面
                item.pages = await self.fetch_pages_async(item.bvid)  #缓存列表
            except ClientError:  #请求失败时无法定位页
                item.pages = []  #清空并继续兜底逻辑
        for page in item.pages:  #查找指定序号
            if page.index == page_index and page.cid:  #仅接受有效CID
                return page.cid  #返回目标页面编号
        if page_index != 1:  #指定非第1 P时不再静默回退，避免下载到错误分P
            raise ClientError(f"未找到第 {page_index} P 的 CID（共 {len(item.pages)} 个分 P）")  #明确报错
        return await self.ensure_cid_async(item)  #仅首页允许回退补全

    #------------------------------------------------------------ 下载地址
    #方法：同步封装流选择并转发到后台循环
    def select_streams(
        self,
        item: VideoItem,
        kind: DownloadKind = DownloadKind.VIDEO,
        qn: int = 0,
        page_index: int = 1,
        audio_qn: int = 0,
    ) -> StreamSelection:
        """挑选合适的音视频流（同步封装）"""
        return self._bridge.run_sync(  #等待后台任务结束
            self.select_streams_async(item, kind, qn, page_index, audio_qn), timeout=60  #转发所有筛选参数
        )

    #方法：按指定CID请求播放地址并选择音视频流
    async def select_streams_async(
        self,
        item: VideoItem,
        kind: DownloadKind = DownloadKind.VIDEO,
        qn: int = 0,
        page_index: int = 1,
        audio_qn: int = 0,
    ) -> StreamSelection:
        """挑选合适的音视频流，交给本地选择器并优先AVC编码

        ``qn`` 为用户指定清晰度（0=自动取最高）；``page_index`` 指定分P
        """
        cid = await self._cid_for_page(item, page_index)  #获取目标页CID
        try:  #查询指定页面播放地址
            data = await video.Video(  #创建视频接口客户端
                bvid=item.bvid, credential=self.credential  #附带当前凭据
            ).get_download_url(cid=cid)  #请求DASH音视频数据
        except Exception as exc:  #noqa: BLE001
            raise ClientError(f"获取下载地址失败：{exc}") from exc  #转换接口异常

        #首选本地选择器，异常时退回库的检测器
        try:  #优先使用本地DASH选择逻辑
            return select_streams_from_dash(data, kind, qn, audio_qn)  #返回兼容性优先的流
        except ClientError:  #业务限制或无可用流
            raise  #保留明确错误
        except Exception:  #noqa: BLE001 - 解析异常则退回库的检测器
            _log.warning("内置流选择失败，尝试库检测器", exc_info=True)  #记录选择器故障

        try:  #使用第三方库的检测器兜底
            detecter = video.VideoDownloadURLDataDetecter(data)  #初始化流解析器
            streams = detecter.detect_best_streams(codecs=[video.VideoCodecs.AVC])  #优先AVC编码
        except Exception:  #noqa: BLE001
            _log.warning("库检测器同样失败，改用最简单的带宽选择", exc_info=True)  #记录检测器错误
            return self._fallback_streams(data, kind)  #退回带宽排序

        select = StreamSelection()  #创建结果对象
        for stream in streams or []:  #遍历库返回的流
            if isinstance(stream, video.VideoStreamDownloadURL) and not select.video_url:  #取首个视频流
                select.video_url = stream.url  #保存视频地址
                select.quality_label = getattr(stream.video_quality, "name", "") or ""  #保存画质名
            elif isinstance(stream, video.AudioStreamDownloadURL) and not select.audio_url:  #取首个音频流
                select.audio_url = stream.url  #保存音频地址
                select.audio_label = getattr(stream.audio_quality, "name", "") or ""  #保存音质名

        if not select.video_url and not select.audio_url:  #检测器未产生可用流
            return self._fallback_streams(data, kind)  #尝试最后兜底
        return select  #返回检测到的流

    @staticmethod  #此兜底不依赖客户端状态
    #方法：主流选择器异常时退回带宽排序
    def _fallback_streams(data: dict[str, Any], kind: DownloadKind) -> StreamSelection:
        """最后兜底：完全不挑编码，只取带宽最大者（保证「至少能下」）"""
        dash = (data or {}).get("dash") or {}  #提取DASH流信息
        select = StreamSelection()  #初始化返回对象
        audios = dash.get("audio") or []  #读取音频候选
        videos = dash.get("video") or []  #读取视频候选
        if audios:  #存在音频候选
            best_audio = max(audios, key=lambda x: x.get("bandwidth", 0))  #选择带宽最大音频
            select.audio_url = _stream_url(best_audio)  #保存音频地址
        if videos and kind is DownloadKind.VIDEO:  #视频任务且有视频候选
            best_video = max(videos, key=lambda x: x.get("bandwidth", 0))  #选择带宽最大视频
            select.video_url = _stream_url(best_video)  #保存视频地址
        if not select.video_url and not select.audio_url:  #没有任何有效流地址
            raise ClientError("未能解析出可用的音视频流，可能是会员/地区限制")  #报告解析失败
        return select  #返回兜底流选择

    #---------------------------------------------------------- 异步提交封装
    #方法：为UI提交协程工厂并分派回调
    def submit(
        self,
        factory: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """给UI用的通用异步入口：factory()必须返回协程

        例如：client.submit(lambda: ..., on_success=...)
        这里保留原始协程对象由bridge调度
        """
        coro = factory()  #由调用方创建协程对象
        self._bridge.submit(coro, on_success=on_success, on_error=on_error)  #调度并注册结果回调


#函数：将接口返回的封面地址规范为可直接请求的URL
def _normalize_pic(url: Any) -> str:
    """搜索接口的 ``pic`` 常为协议相对地址（``//i0.hdslb.com/...``），补全为https"""
    text = str(url or "").strip()  #兼容非字符串输入
    if text.startswith("//"):  #缺少协议部分
        return f"https:{text}"  #补齐https协议
    return text  #返回原地址


#函数：从接口标题中过滤HTML高亮标签
def _clean_html(text: str) -> str:  #返回去标签后的纯文本
    """去掉搜索结果里用于高亮的<em>等标签"""
    return re.sub(r"<[^>]+>", "", text or "")  #删除标签片段
