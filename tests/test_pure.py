"""纯函数测试：BV 解析、文件名净化、容量格式化、时长/计数解析。

这些测试不依赖网络、不依赖 Tk，是回归防护的第一道防线。
"""

from __future__ import annotations

from bilidown.core.client import (
    human_size,
    parse_bv,
    parse_count,
    parse_duration,
    sanitize_filename,
)


class TestParseBv:
    def test_bare_bvid(self) -> None:
        assert parse_bv("BV1xx411c7mD") == "BV1xx411c7mD"

    def test_full_url(self) -> None:
        url = "https://www.bilibili.com/video/BV1xx411c7mD/?p=1&t=3"
        assert parse_bv(url) == "BV1xx411c7mD"

    def test_url_with_spaces(self) -> None:
        assert parse_bv("  https://b23.tv/BV1xx411c7mD  ") == "BV1xx411c7mD"

    def test_none_for_keyword(self) -> None:
        assert parse_bv("python 教程") is None

    def test_none_for_empty(self) -> None:
        assert parse_bv("") is None
        assert parse_bv("   ") is None

    def test_rejects_wrong_length(self) -> None:
        """回归审计缺陷 B11：旧正则 {10,} 会放过长度不为 12 的串。"""
        assert parse_bv("BV1234567890") == "BV1234567890"  # 恰好 12 位，合法
        assert parse_bv("BV123") is None

    def test_lowercase_prefix_rejected(self) -> None:
        # B 站 BV 号前缀固定为大写 BV
        assert parse_bv("bv1xx411c7mD") is None


class TestSanitizeFilename:
    def test_windows_illegal_chars(self) -> None:
        assert sanitize_filename('a<b>c:d/e\\f|g?h*i"j') == "a_b_c_d_e_f_g_h_i_j"

    def test_control_characters(self) -> None:
        assert sanitize_filename("a\nb\tc\rd") == "a b c d"

    def test_trailing_dots_and_spaces_stripped(self) -> None:
        assert sanitize_filename("文件名...   ") == "文件名"

    def test_reserved_device_names(self) -> None:
        """Windows 保留名 CON/PRN/NUL 等不能做文件名。"""
        assert sanitize_filename("CON") == "_CON"
        assert sanitize_filename("com1") == "_com1"

    def test_max_length(self) -> None:
        result = sanitize_filename("x" * 300)
        assert len(result) == 80

    def test_empty_falls_back(self) -> None:
        assert sanitize_filename("") == "untitled"
        assert sanitize_filename("   ") == "untitled"

    def test_chinese_preserved(self) -> None:
        assert sanitize_filename("【教程】Python 入门") == "【教程】Python 入门"


class TestHumanSize:
    def test_zero(self) -> None:
        assert human_size(0) == "0B"

    def test_bytes(self) -> None:
        assert human_size(512) == "512B"

    def test_kilobytes(self) -> None:
        assert human_size(1536) == "1.5KB"

    def test_megabytes(self) -> None:
        assert human_size(5 * 1024 * 1024) == "5.0MB"

    def test_gigabytes(self) -> None:
        assert human_size(2.5 * 1024**3) == "2.5GB"


class TestParseDuration:
    """真实缺陷回归：搜索接口的 duration 是 "7:49" 这样的字符串。

    旧代码直接 ``int(entry["duration"])`` 会抛
    ``invalid literal for int() with base 10: '7:49'``，
    表现为界面上「搜索失败：invalid literal for int() with base 10: '7:49'」。
    """

    def test_exact_failing_value(self) -> None:
        """截图里那个具体的报错值。"""
        assert parse_duration("7:49") == 7 * 60 + 49

    def test_plain_seconds_int(self) -> None:
        assert parse_duration(469) == 469

    def test_plain_seconds_string(self) -> None:
        assert parse_duration("469") == 469

    def test_float_seconds(self) -> None:
        assert parse_duration(469.8) == 469
        assert parse_duration("469.8") == 469

    def test_hours_minutes_seconds(self) -> None:
        assert parse_duration("1:07:49") == 3600 + 7 * 60 + 49
        assert parse_duration("2:00:00") == 7200

    def test_zero_and_empty(self) -> None:
        assert parse_duration(0) == 0
        assert parse_duration("") == 0
        assert parse_duration(None) == 0
        assert parse_duration("0:00") == 0

    def test_garbage_returns_zero_not_raise(self) -> None:
        for bad in ("abc", "--:--", "7:4x", [], {}, "1.2万", True, False):
            assert parse_duration(bad) == 0, f"{bad!r} 应返回 0 而不是抛异常"


class TestParseCount:
    """播放量/弹幕数在不同端点上可能是 int、数字字符串或带单位的中文串。"""

    def test_int(self) -> None:
        assert parse_count(12345) == 12345

    def test_numeric_string(self) -> None:
        assert parse_count("12345") == 12345

    def test_thousand_separator(self) -> None:
        assert parse_count("1,234") == 1234

    def test_chinese_wan(self) -> None:
        assert parse_count("12.3万") == 123000

    def test_chinese_yi(self) -> None:
        assert parse_count("1.5亿") == 150_000_000

    def test_html_tags_stripped(self) -> None:
        assert parse_count('<em class="keyword">123</em>') == 123

    def test_zero_and_none(self) -> None:
        assert parse_count(0) == 0
        assert parse_count(None) == 0
        assert parse_count("") == 0

    def test_garbage_returns_zero_not_raise(self) -> None:
        for bad in ("abc", "--", [], {}, True, "7:49"):
            assert parse_count(bad) == 0, f"{bad!r} 应返回 0 而不是抛异常"


class TestSearchRowRobustness:
    """整行解析必须容错：一条脏数据不能拖垮整页搜索结果。"""

    def test_realistic_search_entry_with_time_string(self) -> None:
        from bilidown.core.client import _clean_html  # noqa: PLC0415

        entry = {
            "bvid": "BV1xx411c7mD",
            "title": '<em class="keyword">Python</em> 教程',
            "author": "某UP",
            "play": "12.3万",
            "danmaku": "1,234",
            "duration": "7:49",
        }
        assert _clean_html(entry["title"]) == "Python 教程"
        assert parse_count(entry["play"]) == 123000
        assert parse_count(entry["danmaku"]) == 1234
        assert parse_duration(entry["duration"]) == 469
