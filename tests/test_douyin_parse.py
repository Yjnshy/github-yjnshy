"""抖音解析纯函数的离线单元测试（不依赖网络）。"""

from dly.platforms.douyin import (
    _image_score, find_item, pick_image_urls,
)


def test_find_item_nested():
    data = {"loader": {"video": {"detail": {"item_list": [{"desc": "标题"}]}}}}
    assert find_item(data) == {"desc": "标题"}


def test_find_item_missing():
    assert find_item({"a": {"b": []}}) is None
    assert find_item({}) is None


def test_pick_image_urls_prefers_clean_original():
    item = {
        "images": [
            {
                "url_list": [
                    "https://p3.douyinpic.com/img/origin~tplv-resize:1080:1080.webp",
                    "https://p3.douyinpic.com/img/origin.jpeg",
                ],
                "download_url_list": [
                    "https://p3.douyinpic.com/img/watermark_download.jpeg",
                ],
            }
        ]
    }
    urls = pick_image_urls(item)
    assert urls == ["https://p3.douyinpic.com/img/origin.jpeg"]


def test_pick_image_urls_skips_watermarked():
    item = {"images": [{"url_list": ["https://x.com/watermark_1.jpg"],
                        "download_url_list": []}]}
    assert pick_image_urls(item) == []


def test_image_score_prefers_original_jpeg():
    original = "https://p3.douyinpic.com/img/origin.jpeg"
    resized = "https://p3.douyinpic.com/img/x~tplv-resize:720:720.webp"
    assert _image_score(original) > _image_score(resized)
