"""统一配置管理：YAML 文件 + 环境变量覆盖。

约定：
- 配置文件查找顺序：--config 指定 > ./config.yaml > ~/.media-toolkit/config.yaml
- 环境变量前缀 MTK_，例如 MTK_OUTPUT_DIR、MTK_PROXY
- 配置里严禁存放 Cookie 明文，Cookie 一律走 cookies 模块动态获取
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

DEFAULT_CONFIG_PATHS = [
    Path("config.yaml"),
    Path.home() / ".media-toolkit" / "config.yaml",
]


@dataclass
class CookieConfig:
    browser: str = "edge"            # chrome / edge / firefox / brave
    cookies_file: str = ""           # Netscape 格式 cookies.txt 路径


@dataclass
class RecordConfig:
    format: str = "ts"
    poll_interval: int = 60
    max_retries: int = 20
    segment_minutes: int = 0
    rooms: List[str] = field(default_factory=list)


@dataclass
class AppConfig:
    output_dir: str = "./downloads"
    quality: str = "best"
    proxy: str = ""
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    cookies: CookieConfig = field(default_factory=CookieConfig)
    record: RecordConfig = field(default_factory=RecordConfig)


def _find_config_file(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        return p
    for p in DEFAULT_CONFIG_PATHS:
        if p.is_file():
            return p
    return None


def load_config(explicit_path: Optional[str] = None) -> AppConfig:
    """加载配置；文件不存在时返回默认配置。环境变量优先级最高。"""
    cfg = AppConfig()
    path = _find_config_file(explicit_path)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        cfg.output_dir = raw.get("output_dir", cfg.output_dir)
        cfg.quality = raw.get("quality", cfg.quality)
        cfg.proxy = raw.get("proxy", cfg.proxy)
        cfg.ffmpeg_path = raw.get("ffmpeg_path", cfg.ffmpeg_path)
        cfg.ffprobe_path = raw.get("ffprobe_path", cfg.ffprobe_path)
        ck = raw.get("cookies") or {}
        cfg.cookies = CookieConfig(
            browser=ck.get("browser", "edge"),
            cookies_file=ck.get("cookies_file", "") or "",
        )
        rc = raw.get("record") or {}
        cfg.record = RecordConfig(
            format=rc.get("format", "ts"),
            poll_interval=int(rc.get("poll_interval", 60)),
            max_retries=int(rc.get("max_retries", 20)),
            segment_minutes=int(rc.get("segment_minutes", 0)),
            rooms=[r for r in (rc.get("rooms") or [])
                   if isinstance(r, str) and r.strip() and not r.strip().startswith("#")],
        )

    # 环境变量覆盖
    cfg.output_dir = os.environ.get("MTK_OUTPUT_DIR", cfg.output_dir)
    cfg.quality = os.environ.get("MTK_QUALITY", cfg.quality)
    cfg.proxy = os.environ.get("MTK_PROXY", cfg.proxy)
    cfg.cookies.browser = os.environ.get("MTK_COOKIE_BROWSER", cfg.cookies.browser)
    cfg.cookies.cookies_file = os.environ.get("MTK_COOKIES_FILE", cfg.cookies.cookies_file)
    return cfg
