"""命名与链接提取的单元测试。"""

from datetime import datetime

from dly.core.naming import build_filename, extract_urls, safe_name


def test_extract_urls_from_share_text():
    text = "7.94 复制打开抖音，看看【测试的作品】 https://v.douyin.com/abc123/ 12/25"
    assert extract_urls(text) == ["https://v.douyin.com/abc123/"]


def test_extract_urls_dedup_and_trim():
    text = "https://a.com/x，https://a.com/x。https://b.com/y！"
    assert extract_urls(text) == ["https://a.com/x", "https://b.com/y"]


def test_extract_urls_empty():
    assert extract_urls("") == []
    assert extract_urls("没有链接") == []


def test_safe_name_strips_illegal_chars():
    assert safe_name('a/b\\c:d*e?f"g<h>i|j') == "a b c d e f g h i j"
    assert safe_name("") == "未命名"
    assert len(safe_name("x" * 100)) == 60


def test_build_filename_format():
    when = datetime(2026, 11, 12, 21, 30, 0)
    name = build_filename("douyin", "小明", "测试视频", when, ".mp4")
    assert name == "douyin_小明_测试视频_20261112_213000.mp4"


def test_build_filename_skips_empty_author():
    name = build_filename("other", "", "标题")
    assert name.startswith("other_标题_")


def test_live_folder_name_formats_platform_anchor_id():
    from dly.core.naming import live_folder_name

    assert live_folder_name("douyin", "Katie小仙女", "498526330292") == \
        "抖音-Katie小仙女-498526330292"
    # 拿不到主播号时不要留空尾巴
    assert live_folder_name("bilibili", "某主播") == "B站-某主播"
    assert live_folder_name("bilibili", "", "") == "B站"


def test_live_folder_name_sanitizes_illegal_chars():
    from dly.core.naming import live_folder_name

    name = live_folder_name("douyin", "a/b:c*d?", "123")
    assert "抖音-a b c d-123" == name


def test_live_filename_is_time_then_title():
    from datetime import datetime

    from dly.core.naming import live_filename

    when = datetime(2026, 11, 17, 12, 50, 58)
    assert live_filename("央视新闻频道正在直播", when, "ts") == \
        "20261117_125058_央视新闻频道正在直播.ts"
    assert live_filename("", when) == "20261117_125058_直播"


def test_bilibili_room_id_from_url():
    from dly.platforms.bilibili_live import room_id_from_url

    assert room_id_from_url("https://live.bilibili.com/6") == "6"
    assert room_id_from_url(
        "https://live.bilibili.com/21452505?spm_id_from=333.788") == "21452505"
    assert room_id_from_url("https://live.bilibili.com/blanc/1234") == "1234"
    # 直播首页没有房间号，应当为空（界面据此提示用户换链接）
    assert room_id_from_url("https://live.bilibili.com/?spm_id_from=333.788") == ""
