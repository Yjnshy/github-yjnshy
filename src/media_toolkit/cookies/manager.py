"""Cookie 管理器。

获取优先级（从高到低）：
1. 环境变量 MTK_COOKIES_FILE 或配置里的 cookies.txt（Netscape 格式）
2. 浏览器自动提取（复用 yt-dlp 内置的浏览器 Cookie 解密，
   browser-cookie3 兜底）

安全约定：
- Cookie 只在内存和系统临时目录中流转，绝不写入项目目录，绝不上传
- 提供给 yt-dlp 的 Netscape 文件创建在系统临时目录，用完即删
"""

from __future__ import annotations

import http.cookiejar
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from media_toolkit.core.config import CookieConfig

logger = logging.getLogger(__name__)

# 域名别名：平台 -> 需要提取 Cookie 的域名
PLATFORM_DOMAINS: Dict[str, List[str]] = {
    "douyin": ["douyin.com"],
    "bilibili": ["bilibili.com"],
    "xiaohongshu": ["xiaohongshu.com"],
}


class CookieManager:
    """按需为各平台提供 Cookie 请求头。"""

    def __init__(self, config: CookieConfig):
        self._config = config
        self._cache: Dict[str, str] = {}
        self._temp_files: List[str] = []

    # ---- 对外接口 ----

    def get_header(self, platform: str) -> str:
        """获取某平台的 Cookie 请求头字符串，获取失败返回空串。"""
        if platform in self._cache:
            return self._cache[platform]

        header = self._from_file(platform) or self._from_browser(platform)
        if header:
            self._cache[platform] = header
            logger.debug("[%s] Cookie 已就绪（%d 字符）", platform, len(header))
        else:
            logger.warning(
                "[%s] 未能获取 Cookie：请在浏览器中登录该平台，"
                "或在 config.yaml 中配置 cookies.cookies_file", platform)
        return header

    def write_netscape_file(self, platform: str) -> Optional[str]:
        """把 Cookie 写成 yt-dlp 可读的 Netscape 临时文件，返回路径。"""
        header = self.get_header(platform)
        if not header:
            return None
        domain = PLATFORM_DOMAINS.get(platform, [""])[0]
        try:
            fd, path = tempfile.mkstemp(prefix="mtk_cookies_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("# Netscape HTTP Cookie File\n")
                for part in header.split(";"):
                    part = part.strip()
                    if not part or "=" not in part:
                        continue
                    name, value = part.split("=", 1)
                    f.write(f".{domain}\tTRUE\t/\tTRUE\t0\t"
                            f"{name.strip()}\t{value.strip()}\n")
            self._temp_files.append(path)
            return path
        except OSError as exc:
            logger.warning("写入 Cookie 临时文件失败: %s", exc)
            return None

    def cleanup(self) -> None:
        """删除所有临时 Cookie 文件。"""
        for path in self._temp_files:
            try:
                os.remove(path)
            except OSError:
                pass
        self._temp_files.clear()

    # ---- 内部实现 ----

    def _from_file(self, platform: str) -> str:
        path = self._config.cookies_file
        if not path or not Path(path).is_file():
            return ""
        domain = PLATFORM_DOMAINS.get(platform, [""])[0]
        items = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 7 and domain in parts[0]:
                        items.append(f"{parts[5]}={parts[6]}")
        except OSError:
            return ""
        return "; ".join(items)

    def _from_browser(self, platform: str) -> str:
        domains = PLATFORM_DOMAINS.get(platform)
        if not domains:
            return ""
        browser = (self._config.browser or "edge").lower()
        return self._via_ytdlp(browser, domains[0]) or \
            self._via_browser_cookie3(browser, domains[0])

    @staticmethod
    def _via_ytdlp(browser: str, domain: str) -> str:
        """复用 yt-dlp 内置的浏览器 Cookie 解密（支持新版 Chrome 加密）。"""
        try:
            from yt_dlp import YoutubeDL
        except ImportError:
            return ""
        try:
            with YoutubeDL({
                "quiet": True,
                "no_warnings": True,
                "cookiesfrombrowser": (browser,),
            }) as ydl:
                jar = ydl.cookiejar
        except Exception as exc:
            logger.debug("yt-dlp 浏览器 Cookie 提取失败: %s", exc)
            return ""
        items = [
            f"{c.name}={c.value}" for c in jar
            if domain.lstrip(".") in (c.domain or "")
        ]
        return "; ".join(items)

    @staticmethod
    def _via_browser_cookie3(browser: str, domain: str) -> str:
        try:
            import browser_cookie3  # type: ignore
        except ImportError:
            return ""
        loader = getattr(browser_cookie3, browser, None)
        if loader is None:
            return ""
        try:
            jar: http.cookiejar.CookieJar = loader(domain_name=domain)
        except Exception as exc:
            logger.debug("browser-cookie3 提取失败: %s", exc)
            return ""
        return "; ".join(f"{c.name}={c.value}" for c in jar)
