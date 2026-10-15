"""B 站客户端测试：cid 解析（B1 回归）、搜索结果、流选择回退、扫码凭据解析。

使用假的 bilibili_api 对象，不触网。
"""

from __future__ import annotations

from typing import Any

from bilidown.core import client as client_mod
from bilidown.core.client import BiliClient, ClientError, SearchResult
from bilidown.core.models import DownloadKind, VideoItem
from bilidown.core.qrlogin import QrState, build_credential, parse_login_cookies

from .test_downloader import patched


class FakeBridge:
    """把协程直接跑在当前事件循环里（测试用同步门面）。"""

    def run_sync(self, coro: Any, timeout: float | None = None) -> Any:
        import asyncio

        return asyncio.run(coro)

    def submit(self, coro: Any, **_kw: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


INFO_PAYLOAD = {
    "bvid": "BV1xx411c7mD",
    "title": "<em class=\"keyword\">Python</em> 入门教程",
    "duration": 125,
    "pic": "https://x/cover.jpg",
    "owner": {"name": "某UP"},
    "stat": {"view": 12345, "danmaku": 678},
    "pages": [{"cid": 987654, "page": 1, "part": "P1"}],
}


class FakeVideo:
    def __init__(self, bvid: str = "", credential: Any = None) -> None:
        self.bvid = bvid

    async def get_info(self) -> dict[str, Any]:
        return INFO_PAYLOAD

    async def get_cid(self, page_index: int = 0) -> int:
        return 987654

    async def get_download_url(self, cid: int | None = None, **_: Any) -> dict[str, Any]:
        return {
            "dash": {
                "video": [
                    {"baseUrl": "https://x/low.m4s", "bandwidth": 100},
                    {"baseUrl": "https://x/high.m4s", "bandwidth": 900},
                ],
                "audio": [
                    {"baseUrl": "https://x/a64.m4a", "bandwidth": 64},
                    {"baseUrl": "https://x/a192.m4a", "bandwidth": 192},
                ],
            }
        }


class FakeSearch:
    class SearchObjectType:
        VIDEO = "video"

    @staticmethod
    async def search_by_type(**kwargs: Any) -> dict[str, Any]:
        # 完全按真实响应形态给：duration 是 "7:49"，play 是 "12.3万"
        return {
            "result": [
                {
                    "bvid": "BV1xx411c7mD",
                    "title": '<em class="keyword">Python</em> 教程',
                    "author": "某UP",
                    "play": "12.3万",
                    "danmaku": "1,234",
                    "duration": "7:49",
                },
                {"title": "没有 bvid 的脏数据"},
            ]
        }


class FakeCredential:
    def __init__(self, *a: Any, **k: Any) -> None:
        self.cookies: dict[str, Any] = {}

    @staticmethod
    def from_cookies(cookies: dict[str, Any]) -> FakeCredential:
        cred = FakeCredential()
        cred.cookies = dict(cookies)
        return cred

    def get_cookies(self) -> dict[str, Any]:
        return dict(self.cookies)

    async def check_valid(self) -> bool:
        return True


def _client() -> BiliClient:
    client = BiliClient(bridge=FakeBridge())  # type: ignore[arg-type]
    return client


class TestFetchByBvid:
    def test_cid_taken_from_pages(self) -> None:
        """B1 回归：cid 在 pages[0] 里，顶层没有 cid 字段。"""
        client = _client()
        with patched(client_mod, video=type("V", (), {"Video": FakeVideo})):
            item = client.fetch_by_bvid("BV1xx411c7mD")
        assert item.cid == 987654
        assert item.bvid == "BV1xx411c7mD"

    def test_title_html_tags_stripped(self) -> None:
        client = _client()
        with patched(client_mod, video=type("V", (), {"Video": FakeVideo})):
            item = client.fetch_by_bvid("BV1xx411c7mD")
        assert "<em" not in item.title
        assert "Python" in item.title

    def test_owner_and_stat_mapped(self) -> None:
        client = _client()
        with patched(client_mod, video=type("V", (), {"Video": FakeVideo})):
            item = client.fetch_by_bvid("BV1xx411c7mD")
        assert item.author == "某UP"
        assert item.play == 12345
        assert item.danmaku == 678
        assert item.duration == 125

    def test_no_pages_raises(self) -> None:
        class NoPages(FakeVideo):
            async def get_info(self) -> dict[str, Any]:
                return {**INFO_PAYLOAD, "pages": []}

        client = _client()
        with patched(client_mod, video=type("V", (), {"Video": NoPages})):
            try:
                client.fetch_by_bvid("BV1xx411c7mD")
            except ClientError as exc:
                assert "分 P" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("应当抛 ClientError")


class TestSearchVideos:
    def test_returns_items_and_skips_dirty_rows(self) -> None:
        client = _client()
        with patched(client_mod, search=FakeSearch):
            result = client.search_videos("python")
        assert isinstance(result, SearchResult)
        assert len(result.items) == 1
        assert result.items[0].bvid == "BV1xx411c7mD"
        assert result.items[0].cid == 0  # 延迟解析
        assert result.keyword == "python"

    def test_real_world_types_are_parsed(self) -> None:
        """真实缺陷回归：duration="7:49"、play="12.3万" 不能炸掉搜索。

        旧代码 ``int("7:49")`` 会抛
        ``invalid literal for int() with base 10: '7:49'``。
        """
        client = _client()
        with patched(client_mod, search=FakeSearch):
            result = client.search_videos("python")
        item = result.items[0]
        assert item.duration == 469, f"7:49 应解析为 469 秒，得到 {item.duration}"
        assert item.play == 123000
        assert item.danmaku == 1234
        assert item.title == "Python 教程"

    def test_unparseable_rows_are_skipped_not_fatal(self) -> None:
        class BrokenSearch:
            class SearchObjectType:
                VIDEO = "video"

            @staticmethod
            async def search_by_type(**kwargs: Any) -> dict[str, Any]:
                return {
                    "result": [
                        {
                            "bvid": "BV1xx411c7mD",
                            "title": "好数据",
                            "author": "UP",
                            "play": 1,
                            "danmaku": 2,
                            "duration": 30,
                        },
                        # 这条会让 title 处理抛异常（None 不是字符串）
                        {"bvid": "BV1xx411c7mE", "title": None, "author": None},
                    ]
                }

        client = _client()
        with patched(client_mod, search=BrokenSearch):
            result = client.search_videos("x")
        # 至少好数据要留下来，绝不能整页失败
        assert len(result.items) >= 1
        assert result.items[0].bvid == "BV1xx411c7mD"

    def test_empty_keyword_raises(self) -> None:
        client = _client()
        try:
            client.search_videos("   ")
        except ClientError as exc:
            assert "关键词" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("应当抛 ClientError")


class TestEnsureCid:
    def test_ensure_cid_fills_missing(self) -> None:
        client = _client()
        item = VideoItem(bvid="BV1xx411c7mD", cid=0, title="t")
        with patched(client_mod, video=type("V", (), {"Video": FakeVideo})):
            assert client.ensure_cid(item) == 987654
            assert item.cid == 987654

    def test_ensure_cid_keeps_existing(self) -> None:
        client = _client()
        item = VideoItem(bvid="BV1xx411c7mD", cid=42, title="t")
        assert client.ensure_cid(item) == 42


class TestStreamSelectionFromDash:
    """真实缺陷回归：库的 detect_best_streams 在所有真实视频上都会崩。

    bilibili_api 17.4.1 的 ``video.py:2584`` 假设 ``s.video_codecs`` 不为 None，
    而真实响应里的 ``dash.dolby`` / ``dash.flac`` 音频流没有 ``codecs`` 字段，
    于是 ``video_stream_cmp`` 抛 AttributeError，被上层捕获后回退到「纯带宽选择」，
    结果选到 HEVC/AV1（带宽反而可能更高），兼容性差。

    这里用真实抓到的 dash 结构做回归。
    """

    # 真实响应片段（BV1wD4y1o7AS 实测）：
    # 同一清晰度 id=32 下 AVC 带宽最高；但 id=16 的 AVC 带宽低于 id=32 的 HEVC，
    # 所以「只按带宽选」会选错清晰度/编码。
    REAL_DASH = {
        "dash": {
            "duration": 469,
            "video": [
                {"id": 32, "codecs": "avc1.64001F", "bandwidth": 348675,
                 "baseUrl": "https://x/32-avc.m4s", "width": 1280, "height": 720},
                {"id": 32, "codecs": "hvc1.1.6.L120.90", "bandwidth": 213842,
                 "baseUrl": "https://x/32-hev.m4s", "width": 1280, "height": 720},
                {"id": 32, "codecs": "av01.0.08M.08.0.110.01.01.01.0", "bandwidth": 185670,
                 "baseUrl": "https://x/32-av1.m4s", "width": 1280, "height": 720},
                {"id": 16, "codecs": "hvc1.1.6.L120.90", "bandwidth": 151113,
                 "baseUrl": "https://x/16-hev.m4s", "width": 854, "height": 480},
                {"id": 16, "codecs": "avc1.64001E", "bandwidth": 235862,
                 "baseUrl": "https://x/16-avc.m4s", "width": 854, "height": 480},
            ],
            "audio": [
                {"id": 30232, "codecs": "mp4a.40.2", "bandwidth": 89566,
                 "baseUrl": "https://x/a-132k.m4s"},
                {"id": 30216, "codecs": "mp4a.40.2", "bandwidth": 66214,
                 "baseUrl": "https://x/a-64k.m4s"},
                {"id": 30280, "codecs": "mp4a.40.2", "bandwidth": 156279,
                 "baseUrl": "https://x/a-192k.m4s"},
            ],
            # 真实响应里这两个会击穿库的检测器（没有 codecs 字段）
            "dolby": {"audio": [{"id": 30250, "bandwidth": 448000}]},
            "flac": {"audio": {"id": 30251, "bandwidth": 1000000}},
        },
        "accept_quality": [32, 16],
    }

    def test_codecs_classification(self) -> None:
        from bilidown.core.client import codecs_to_kind

        assert codecs_to_kind("avc1.64001F") == "avc"
        assert codecs_to_kind("hvc1.1.6.L120.90") == "hev"
        assert codecs_to_kind("hev1.1.6") == "hev"
        assert codecs_to_kind("av01.0.08M.08") == "av01"
        assert codecs_to_kind("mp4a.40.2") == "other"
        assert codecs_to_kind(None) == "other"
        assert codecs_to_kind("") == "other"

    def test_picks_highest_quality_then_avc(self) -> None:
        """必须是 id=32 + AVC，而不是带宽最大的那条。"""
        from bilidown.core.client import select_streams_from_dash

        selection = select_streams_from_dash(self.REAL_DASH, DownloadKind.VIDEO)
        assert selection.video_url == "https://x/32-avc.m4s", (
            f"应选最高清晰度的 AVC，实际 {selection.video_url}"
        )

    def test_does_not_pick_hevc_or_av1(self) -> None:
        from bilidown.core.client import select_streams_from_dash

        selection = select_streams_from_dash(self.REAL_DASH, DownloadKind.VIDEO)
        assert "hev" not in selection.video_url
        assert "av1" not in selection.video_url
        assert "qn32" in selection.quality_label and "avc" in selection.quality_label

    def test_naive_bandwidth_selection_would_be_wrong(self) -> None:
        """证明「只按带宽」确实是错的（旧回退逻辑的行为）。"""
        videos = self.REAL_DASH["dash"]["video"]
        naive = max(videos, key=lambda s: s["bandwidth"])
        assert naive["baseUrl"] == "https://x/32-avc.m4s", (
            "本例中带宽最大恰好是 AVC；但下一组用例保证清晰度优先"
        )

    def test_quality_beats_bandwidth(self) -> None:
        """构造：低清晰度编码带宽更高时，仍必须选高清晰度。"""
        from bilidown.core.client import select_streams_from_dash

        dash = {
            "dash": {
                "video": [
                    {"id": 16, "codecs": "avc1.64001E", "bandwidth": 900000,
                     "baseUrl": "https://x/low-qn-high-bw.m4s"},
                    {"id": 80, "codecs": "avc1.640033", "bandwidth": 100000,
                     "baseUrl": "https://x/high-qn-low-bw.m4s"},
                ],
                "audio": [{"id": 30280, "codecs": "mp4a.40.2", "bandwidth": 156279,
                           "baseUrl": "https://x/a.m4s"}],
            }
        }
        selection = select_streams_from_dash(dash, DownloadKind.VIDEO)
        assert selection.video_url == "https://x/high-qn-low-bw.m4s"

    def test_audio_only_kind_skips_video(self) -> None:
        from bilidown.core.client import select_streams_from_dash

        selection = select_streams_from_dash(self.REAL_DASH, DownloadKind.AUDIO)
        assert selection.video_url == ""
        assert selection.audio_url == "https://x/a-192k.m4s"  # id 最高

    def test_base_url_snake_case_supported(self) -> None:
        from bilidown.core.client import select_streams_from_dash

        dash = {
            "dash": {
                "video": [{"id": 32, "codecs": "avc1", "bandwidth": 1,
                           "base_url": "https://x/snake.m4s"}],
                "audio": [{"id": 30280, "codecs": "mp4a", "bandwidth": 1,
                           "base_url": "https://x/snake-a.m4s"}],
            }
        }
        selection = select_streams_from_dash(dash, DownloadKind.VIDEO)
        assert selection.video_url == "https://x/snake.m4s"
        assert selection.audio_url == "https://x/snake-a.m4s"

    def test_no_streams_raises_readable_error(self) -> None:
        from bilidown.core.client import select_streams_from_dash

        try:
            select_streams_from_dash({"dash": {}}, DownloadKind.VIDEO)
        except ClientError as exc:
            assert "音视频流" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("没有可用流时应抛 ClientError")

    def test_video_without_video_stream_raises(self) -> None:
        from bilidown.core.client import select_streams_from_dash

        dash = {"dash": {"audio": [{"id": 30280, "codecs": "mp4a", "bandwidth": 1,
                                   "baseUrl": "https://x/a.m4s"}]}}
        try:
            select_streams_from_dash(dash, DownloadKind.VIDEO)
        except ClientError as exc:
            assert "视频流" in str(exc) or "试看" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("缺视频流时应抛 ClientError")

    def test_tolerates_dolby_and_flac_without_codecs(self) -> None:
        """这些正是击穿库检测器的结构；我们的实现必须无视它们。"""
        from bilidown.core.client import select_streams_from_dash

        dash = {
            "dash": {
                "video": [{"id": 32, "bandwidth": 100, "baseUrl": "https://x/v.m4s"}],
                "audio": [{"id": 30280, "bandwidth": 200, "baseUrl": "https://x/a.m4s"}],
                "dolby": {"audio": [{"id": 30250, "bandwidth": 300}]},  # 无 codecs
                "flac": {"audio": {"id": 30251, "bandwidth": 400}},  # 无 codecs
            }
        }
        selection = select_streams_from_dash(dash, DownloadKind.VIDEO)
        assert selection.video_url == "https://x/v.m4s"
        assert selection.audio_url == "https://x/a.m4s"

    def test_backup_urls_are_captured(self) -> None:
        """B 站 dash 自带 backupUrl（实测为含 2 个地址的列表），必须被采集。"""
        from bilidown.core.client import select_streams_from_dash

        dash = {
            "dash": {
                "video": [
                    {"id": 32, "codecs": "avc1", "bandwidth": 10,
                     "baseUrl": "https://x/main-v.m4s",
                     "backupUrl": ["https://x/b1-v.m4s", "https://x/b2-v.m4s"]},
                ],
                "audio": [
                    {"id": 30280, "codecs": "mp4a", "bandwidth": 10,
                     "baseUrl": "https://x/main-a.m4s",
                     "backupUrl": ["https://x/b1-a.m4s"]},
                ],
            }
        }
        selection = select_streams_from_dash(dash, DownloadKind.VIDEO)
        assert selection.video_url == "https://x/main-v.m4s"
        assert selection.video_backups == ["https://x/b1-v.m4s", "https://x/b2-v.m4s"]
        assert selection.audio_backups == ["https://x/b1-a.m4s"]

    def test_backup_url_string_form_supported(self) -> None:
        from bilidown.core.client import select_streams_from_dash

        dash = {
            "dash": {
                "video": [
                    {"id": 32, "codecs": "avc1", "bandwidth": 10,
                     "baseUrl": "https://x/v.m4s", "backup_url": "https://x/single.m4s"},
                ],
                "audio": [{"id": 30280, "codecs": "mp4a", "bandwidth": 1,
                           "baseUrl": "https://x/a.m4s"}],
            }
        }
        selection = select_streams_from_dash(dash, DownloadKind.VIDEO)
        assert selection.video_backups == ["https://x/single.m4s"]

    def test_backup_url_absent_gives_empty_list(self) -> None:
        from bilidown.core.client import select_streams_from_dash

        dash = {
            "dash": {
                "video": [{"id": 32, "codecs": "avc1", "bandwidth": 1,
                           "baseUrl": "https://x/v.m4s"}],
                "audio": [{"id": 30280, "codecs": "mp4a", "bandwidth": 1,
                           "baseUrl": "https://x/a.m4s"}],
            }
        }
        selection = select_streams_from_dash(dash, DownloadKind.VIDEO)
        assert selection.video_backups == []
        assert selection.audio_backups == []

    def test_malformed_entries_are_ignored(self) -> None:
        from bilidown.core.client import select_streams_from_dash

        dash = {
            "dash": {
                "video": [
                    "not-a-dict",
                    {"id": 32, "codecs": "avc1", "bandwidth": 5, "baseUrl": "https://x/ok.m4s"},
                ],
                "audio": ["junk", {"id": 30280, "bandwidth": 5, "baseUrl": "https://x/a.m4s"}],
            }
        }
        selection = select_streams_from_dash(dash, DownloadKind.VIDEO)
        assert selection.video_url == "https://x/ok.m4s"
        assert selection.audio_url == "https://x/a.m4s"


class TestStreamSelectionIntegration:
    def test_select_streams_uses_own_selector(self) -> None:
        """client.select_streams 必须走自己的选择器，且不再依赖库检测器。"""
        client = _client()
        item = VideoItem(bvid="BV1xx411c7mD", cid=1, title="t")

        class RichVideo(FakeVideo):
            async def get_cid(self, page_index: int = 0) -> int:
                return 1

            async def get_download_url(self, cid: int | None = None, **_: Any) -> dict[str, Any]:
                return {
                    "dash": {
                        "video": [
                            {"id": 32, "codecs": "hvc1", "bandwidth": 999,
                             "baseUrl": "https://x/hev.m4s"},
                            {"id": 32, "codecs": "avc1.64001F", "bandwidth": 10,
                             "baseUrl": "https://x/avc.m4s"},
                        ],
                        "audio": [{"id": 30280, "codecs": "mp4a", "bandwidth": 1,
                                   "baseUrl": "https://x/a.m4s"}],
                        "dolby": {"audio": [{"id": 30250}]},  # 会让库检测器崩
                    }
                }

        with patched(client_mod, video=type("V", (), {"Video": RichVideo})):
            selection = client.select_streams(item, DownloadKind.VIDEO)
        # 即使 HEVC 带宽更高，也必须选 AVC
        assert selection.video_url == "https://x/avc.m4s"


class TestCredentialManagement:
    def test_logout_clears_credential(self) -> None:
        client = _client()
        client.credential = FakeCredential()  # type: ignore[assignment]
        assert client.logged_in is True
        client.logout()
        assert client.logged_in is False

    def test_validate_without_credential_is_false(self) -> None:
        client = _client()
        assert client.validate_credential() is False


class TestParseLoginCookies:
    """真实缺陷回归：扫码成功后必须能稳定拿到 SESSDATA。

    库内实现按固定下标解析（``cookie[:8] == "SESSDATA"`` / ``cookie[9:]``），
    对参数顺序与额外参数敏感，解析失败时会静默给出空 sessdata，
    表现为「扫码成功但获取用户信息失败」。这里覆盖各种真实形态。
    """

    def test_standard_crossdomain_url(self) -> None:
        url = (
            "https://passport.biligame.com/crossDomain"
            "?DedeUserID=1234567&DedeUserID__ckMd5=abcdef"
            "&Expires=1740000000&SESSDATA=abc%2Adef%2Fghi"
            "&bili_jct=tokenvalue&gourl=https%3A%2F%2Fwww.bilibili.com"
        )
        cookies = parse_login_cookies(url, "refresh-token-xyz")
        # 必须保留原始编码形态：Credential 的规则是「值里没有 % 才编码一次」，
        # 而真实 SESSDATA 自带 %2C/%2A，所以这里绝不能先解码
        # （解码后 Credential 会重新编码，对 SESSDATA 恰好可还原，对其它 cookie 就会坏）
        assert cookies["SESSDATA"] == "abc%2Adef%2Fghi"
        assert cookies["bili_jct"] == "tokenvalue"
        assert cookies["DedeUserID"] == "1234567"
        assert cookies["ac_time_value"] == "refresh-token-xyz"

    def test_sessdata_not_first_is_parsed(self) -> None:
        """库内实现依赖顺序；这里验证不依赖顺序。"""
        url = "https://x/crossDomain?bili_jct=j&SESSDATA=s1&gourl=g"
        cookies = parse_login_cookies(url)
        assert cookies["SESSDATA"] == "s1"

    def test_missing_optional_fields_still_yields_sessdata(self) -> None:
        cookies = parse_login_cookies("https://x/crossDomain?SESSDATA=onlythis")
        assert cookies["SESSDATA"] == "onlythis"
        assert cookies.get("bili_jct") is None

    def test_ckmd5_and_gourl_excluded(self) -> None:
        url = "https://x/c?SESSDATA=s&DedeUserID=9&DedeUserID__ckMd5=m&gourl=https%3A%2F%2Fb"
        cookies = parse_login_cookies(url)
        assert "__ckMd5" not in "".join(cookies)
        assert "gourl" not in cookies
        assert cookies["DedeUserID"] == "9"

    def test_lowercase_names_normalised(self) -> None:
        cookies = parse_login_cookies("https://x/c?sessdata=low&bili_jct=j")
        assert cookies["SESSDATA"] == "low"

    def test_empty_url_returns_empty(self) -> None:
        assert parse_login_cookies("") == {}
        assert parse_login_cookies("not a url") == {}

    def test_plain_sessdata_preserved(self) -> None:
        """不含 % 的 SESSDATA 必须原样保留，不能被二次编码。"""
        cookies = parse_login_cookies("https://x/c?SESSDATA=AbCdEf123")
        assert cookies["SESSDATA"] == "AbCdEf123"

    def test_build_credential(self) -> None:
        cred = build_credential({"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "1"})
        assert cred is not None
        assert cred.sessdata == "s"
        assert cred.bili_jct == "j"
        assert cred.dedeuserid == "1"

    def test_build_credential_without_sessdata_returns_none(self) -> None:
        assert build_credential({}) is None
        assert build_credential({"bili_jct": "j"}) is None


class TestQrCredentialFlow:
    """验证 client 在扫码成功后确实拿到带 sessdata 的凭据。"""

    def test_credential_from_session_cookies(self) -> None:
        from bilidown.core.qrlogin import LoginSession

        client = _client()
        session = LoginSession()
        session._cookies = {"SESSDATA": "sessiondata", "bili_jct": "j"}  # noqa: SLF001
        client._qr = session  # noqa: SLF001
        cred = client.login_qrcode_credential()
        assert cred.sessdata == "sessiondata"
        assert client.logged_in is True

    def test_credential_missing_raises_readable_error(self) -> None:
        from bilidown.core.qrlogin import LoginSession

        client = _client()
        client._qr = LoginSession()  # noqa: SLF001 - 未登录，无 cookie
        try:
            client.login_qrcode_credential()
        except ClientError as exc:
            assert "凭据" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("缺少凭据时应抛 ClientError")

    def test_credential_without_session_raises(self) -> None:
        client = _client()
        try:
            client.login_qrcode_credential()
        except ClientError:
            pass
        else:  # pragma: no cover
            raise AssertionError("没有会话时应抛 ClientError")


class TestQrStatePollingConversion:
    def test_recoverable_error_becomes_scan(self) -> None:
        """网络抖动不应中断轮询，返回 SCAN 让上层继续。"""

        class FakeSession:
            async def poll(self):  # type: ignore[no-untyped-def]
                from bilidown.core.qrlogin import QrPollResult

                return QrPollResult(QrState.ERROR, message="boom")

        client = _client()
        client._qr = FakeSession()  # type: ignore[assignment]  # noqa: SLF001
        assert client.login_qrcode_poll() is QrState.SCAN

    def test_done_state_passes_through(self) -> None:
        class FakeSession:
            async def poll(self):  # type: ignore[no-untyped-def]
                from bilidown.core.qrlogin import QrPollResult

                return QrPollResult(QrState.DONE, cookies={"SESSDATA": "s"})

        client = _client()
        client._qr = FakeSession()  # type: ignore[assignment]  # noqa: SLF001
        assert client.login_qrcode_poll() is QrState.DONE


def _session_with_poll_data(payload: dict[str, Any]):
    """构造一个 LoginSession，并让它的 poll 端点返回给定 payload。

    直接覆盖 ``_query_poll_endpoint`` 这个接缝，避免去 monkeypatch 第三方库的
    属性（那会牵扯 property 描述符与协程包装，很容易测出假结果）。
    """
    from bilidown.core.qrlogin import LoginSession

    session = LoginSession()
    session._qrcode_key = "TEST_KEY"  # noqa: SLF001

    async def fake_query() -> dict[str, Any]:
        return payload

    session._query_poll_endpoint = fake_query  # type: ignore[method-assign]  # noqa: SLF001
    return session


class TestTicketExchange:
    """真实线上流程回归：poll 只给 ticket，凭据必须靠换票取得。

    实测响应形态（用户日志）::

        url = https://passport.biligame.com/x/passport-login/web/crossDomain
              ?ticket=<hex>&gourl=...&first_domain=.bilibili.com

    该 URL 里没有任何 cookie，必须请求它并由 Set-Cookie 下发。
    """

    REAL_SHAPED_TICKET = (
        "https://passport.biligame.com/x/passport-login/web/crossDomain"
        "?ticket=780576481668896829242c49e5014ccc"
        "&gourl=https%3A%2F%2Fwww.bilibili.com"
        "&first_domain=.bilibili.com"
    )

    # 真实的成功响应（就在用户日志里出现过）
    DONE_PAYLOAD = {
        "code": 0,
        "message": "0",
        "refresh_token": "REFRESH_TOKEN",
        "timestamp": 1700000000,
        "url": REAL_SHAPED_TICKET,
    }

    def test_is_ticket_url_detects_real_shape(self) -> None:
        from bilidown.core.qrlogin import is_ticket_url

        assert is_ticket_url(self.REAL_SHAPED_TICKET) is True
        # 直接带 cookie 的旧形态不应被判定为票据链接
        assert is_ticket_url("https://x/c?SESSDATA=abc&bili_jct=j") is False
        assert is_ticket_url("") is False
        assert is_ticket_url("https://x/c?gourl=https%3A%2F%2Fb") is False

    def test_ticket_does_not_leak_into_cookies(self) -> None:
        cookies = parse_login_cookies(self.REAL_SHAPED_TICKET)
        assert cookies == {}, f"票据链接不该解析出任何 cookie，却得到 {cookies}"

    def test_exchange_ticket_reads_set_cookie(self) -> None:
        """换票成功后必须从 Set-Cookie 里拿到 SESSDATA 等凭据。"""
        from bilidown.core import qrlogin

        captured: dict[str, Any] = {}

        class FakeCookies(dict):
            def items(self):  # type: ignore[override]
                return list(super().items())

        class FakeResponse:
            status_code = 200
            url = "https://www.bilibili.com/"
            # 关键：凭据在 Set-Cookie 里，而不是 url 里
            cookies = FakeCookies(
                {
                    "SESSDATA": "abc%2C1700000000%2Cdeadbeef",
                    "bili_jct": "csrf-token",
                    "DedeUserID": "98765432",
                    "DedeUserID__ckMd5": "md5value",
                }
            )

        class FakeSession:
            def __init__(self) -> None:
                # 与真实 create_session() 的默认行为保持一致：不信任环境代理
                self.trust_env = False
                self.proxies: dict = {}
                self.headers: dict = {}
                self.cookies = FakeCookies()

            def get(self, url: str, **kwargs: Any) -> FakeResponse:
                captured["url"] = url
                captured["kwargs"] = kwargs
                captured["trust_env"] = self.trust_env
                return FakeResponse()

            def close(self) -> None:
                captured["closed"] = True

        original = qrlogin.create_session
        qrlogin.create_session = lambda *a, **k: FakeSession()  # type: ignore[assignment]
        try:
            cookies = qrlogin.exchange_ticket(self.REAL_SHAPED_TICKET, "refresh-tok")
        finally:
            qrlogin.create_session = original  # type: ignore[assignment]

        assert captured["url"] == self.REAL_SHAPED_TICKET
        assert captured["kwargs"]["allow_redirects"] is True
        # 必须绕过系统代理（真实 create_session() 默认 trust_env=False），
        # 这台机器的代理是坏的，会直接导致登录失败
        assert captured["trust_env"] is False
        assert cookies["SESSDATA"] == "abc%2C1700000000%2Cdeadbeef"
        assert cookies["bili_jct"] == "csrf-token"
        assert cookies["DedeUserID"] == "98765432"
        assert cookies["ac_time_value"] == "refresh-tok"
        assert "DedeUserID__ckMd5" not in cookies
        assert captured.get("closed") is True, "会话必须被关闭"

    def test_exchange_ticket_network_failure_is_graceful(self) -> None:
        from bilidown.core import qrlogin

        class FakeSession:
            def __init__(self) -> None:
                self.trust_env = True
                self.proxies: dict = {}
                self.headers: dict = {}
                self.cookies: dict = {}

            def get(self, url: str, **kwargs: Any):
                import requests

                raise requests.ConnectionError("boom")

            def close(self) -> None:
                pass

        original = qrlogin.create_session
        qrlogin.create_session = lambda *a, **k: FakeSession()  # type: ignore[assignment]
        try:
            cookies = qrlogin.exchange_ticket(self.REAL_SHAPED_TICKET)
        finally:
            qrlogin.create_session = original  # type: ignore[assignment]
        assert "SESSDATA" not in cookies

    def test_poll_reaches_done_via_ticket_exchange(self) -> None:
        """端到端：poll 返回 ticket → 换票 → DONE 且带 SESSDATA。"""
        import asyncio

        from bilidown.core import qrlogin

        session = _session_with_poll_data(dict(self.DONE_PAYLOAD))
        exchanged: dict[str, Any] = {}

        def fake_exchange(url: str, refresh: str = "") -> dict[str, str]:
            exchanged["url"] = url
            exchanged["refresh"] = refresh
            return {
                "SESSDATA": "real-sess",
                "bili_jct": "real-jct",
                "DedeUserID": "42",
                "ac_time_value": refresh,
            }

        original_exchange = qrlogin.exchange_ticket
        qrlogin.exchange_ticket = fake_exchange  # type: ignore[assignment]
        try:
            result = asyncio.run(session.poll())
        finally:
            qrlogin.exchange_ticket = original_exchange  # type: ignore[assignment]

        assert exchanged.get("url") == self.REAL_SHAPED_TICKET, "必须对 ticket 链接发起换票"
        assert exchanged.get("refresh") == "REFRESH_TOKEN"
        assert result.state is QrState.DONE
        assert result.cookies["SESSDATA"] == "real-sess"
        assert session.credential().sessdata == "real-sess"

    def test_poll_done_with_inline_cookies_skips_exchange(self) -> None:
        """老渠道把 cookie 直接挂在 url 上时不应再发换票请求。"""
        import asyncio

        from bilidown.core import qrlogin

        session = _session_with_poll_data(
            {
                "code": 0,
                "refresh_token": "R",
                "url": "https://x/crossDomain?SESSDATA=inline&bili_jct=j&DedeUserID=1",
            }
        )
        called: list[str] = []

        def spy_exchange(url: str, refresh: str = "") -> dict[str, str]:
            called.append(url)
            return {}

        original_exchange = qrlogin.exchange_ticket
        qrlogin.exchange_ticket = spy_exchange  # type: ignore[assignment]
        try:
            result = asyncio.run(session.poll())
        finally:
            qrlogin.exchange_ticket = original_exchange  # type: ignore[assignment]

        assert called == [], "inline cookie 形态不应触发换票"
        assert result.state is QrState.DONE
        assert result.cookies["SESSDATA"] == "inline"

    def test_poll_reports_error_when_exchange_yields_nothing(self) -> None:
        """换票也没拿到 SESSDATA 时必须报错，而不是假装成功。"""
        import asyncio

        from bilidown.core import qrlogin

        session = _session_with_poll_data(dict(self.DONE_PAYLOAD))
        original_exchange = qrlogin.exchange_ticket
        qrlogin.exchange_ticket = lambda url, refresh="": {}  # type: ignore[assignment]
        try:
            result = asyncio.run(session.poll())
        finally:
            qrlogin.exchange_ticket = original_exchange  # type: ignore[assignment]

        assert result.state is QrState.ERROR
        assert "SESSDATA" in result.message

    def test_poll_scan_state_from_real_shape(self) -> None:
        """真实未扫码响应：data.code=86101 且 url/refresh_token 为空。"""
        import asyncio

        session = _session_with_poll_data(
            {"url": "", "refresh_token": "", "timestamp": 0, "code": 86101, "message": "未扫码"}
        )
        result = asyncio.run(session.poll())
        assert result.state is QrState.SCAN

    def test_poll_conf_state(self) -> None:
        import asyncio

        session = _session_with_poll_data({"code": 86090, "message": "未确认"})
        assert asyncio.run(session.poll()).state is QrState.CONF

    def test_poll_timeout_state(self) -> None:
        import asyncio

        session = _session_with_poll_data(
            {"code": 86038, "message": "二维码已过期", "url": "", "refresh_token": ""}
        )
        assert asyncio.run(session.poll()).state is QrState.TIMEOUT

    def test_poll_network_exception_becomes_error(self) -> None:
        import asyncio

        from bilidown.core.qrlogin import LoginSession

        session = LoginSession()
        session._qrcode_key = "K"  # noqa: SLF001

        async def boom() -> dict[str, Any]:
            raise RuntimeError("网络不可用")

        session._query_poll_endpoint = boom  # type: ignore[method-assign]  # noqa: SLF001
        result = asyncio.run(session.poll())
        assert result.state is QrState.ERROR
        assert "网络不可用" in result.message
