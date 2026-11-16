"""Cookie 管理器。

获取优先级（从高到低）：
1. 环境变量 DLY_COOKIES_FILE 或配置里的 cookies.txt（Netscape 格式）
2. 本地缓存（~/.dly/cookie_cache.json，默认 7 天）
3. 浏览器自动提取（复用 yt-dlp 内置的浏览器 Cookie 解密，
   browser-cookie3 兜底）

缓存的意义：部分浏览器（Chrome/Edge）运行时会锁住 Cookie 数据库，
提取不到。缓存一次后 7 天内免关浏览器，过期再重新提取。

安全约定：
- Cookie 只在内存、系统临时目录和用户主目录的缓存文件中流转，
  绝不写入项目目录，绝不上传
- 缓存文件权限设为仅当前用户可读（非 Windows）
- 提供给 yt-dlp 的 Netscape 文件创建在系统临时目录，用完即删
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

from dly.core.config import CookieConfig

logger = logging.getLogger(__name__)

# 域名别名：平台 -> 需要提取 Cookie 的域名
PLATFORM_DOMAINS: Dict[str, List[str]] = {
    "douyin": ["douyin.com"],
    "bilibili": ["bilibili.com"],
    "xiaohongshu": ["xiaohongshu.com"],
}

# 这些平台没有 Cookie 就基本不可用，取不到时值得告警；
# 其余平台（如 B 站）匿名也能访问，取不到只记调试日志
COOKIE_REQUIRED = {"douyin", "xiaohongshu"}


def _explain_cookie_error(message: str, browser: str) -> str:
    """把 yt-dlp 的原始报错翻译成用户能照做的建议。"""
    text = message.lower()
    if ("decrypt with dpapi" in text or "app-bound" in text
            or "could not decrypt" in text or "no key found" in text):
        return (f"{browser} 的 Cookie 受系统级加密（App-Bound Encryption / v20）保护，"
                "Windows 不允许第三方程序解密，这是浏览器厂商的安全限制，"
                "程序无法绕过。请在那台电脑的浏览器里装 Cookie 导出扩展"
                "（如「Cookie-Editor」），登录后导出 Netscape 格式的 cookies.txt，"
                "再到设置页点「选择 cookies.txt」导入即可。")
    if "could not copy" in text:
        return (f"{browser} 正在运行并锁住了 Cookie 数据库，"
                "请完全退出该浏览器（任务管理器确认无残留进程）后重试。")
    if "could not find" in text and "database" in text:
        return f"未找到 {browser} 的 Cookie 数据库（可能未安装或从未使用）。"
    return ""


class _QuietLogger:
    """吞掉 yt-dlp 的内部日志。

    yt-dlp 默认把 "Could not copy Chrome cookie database" 之类的 ERROR
    直接写到 stderr，探测 Cookie 时会污染命令行输出，这里全部静音；
    真正的异常仍会以 Python 异常形式抛出，不会丢信息。
    """

    def debug(self, msg: str) -> None:  # noqa: D102
        pass

    def info(self, msg: str) -> None:  # noqa: D102
        pass

    def warning(self, msg: str) -> None:  # noqa: D102
        pass

    def error(self, msg: str) -> None:  # noqa: D102
        pass


class CookieManager:
    """按需为各平台提供 Cookie 请求头。"""

    CACHE_PATH = Path.home() / ".dly" / "cookie_cache.json"

    def __init__(self, config: CookieConfig, use_cache: bool = True):
        self._config = config
        self._use_cache = use_cache and getattr(config, "cache_days", 0) > 0
        self._cache: Dict[str, str] = {}
        # 失败状态也缓存：无人值守模式下每轮都要查一次平台状态，
        # 没有负缓存会反复解密浏览器数据库（很慢）并刷屏告警
        self._failed: Dict[str, str] = {}
        self._temp_files: List[str] = []
        self.last_error = ""   # 最近一次取 Cookie 的可读原因，供界面日志展示

    # ---- 对外接口 ----

    def get_header(self, platform: str) -> str:
        """获取某平台的 Cookie 请求头字符串，获取失败返回空串。"""
        if platform in self._cache:
            return self._cache[platform]
        if platform in self._failed:
            # 本次进程内已确认取不到，直接返回，避免重复解密浏览器数据库
            logger.debug("[%s] Cookie 不可用（已缓存失败状态）", platform)
            return ""

        self.last_error = ""
        header = (self._from_file(platform)
                  or self._from_disk_cache(platform)
                  or self._from_browser(platform))
        if header:
            self._cache[platform] = header
            logger.debug("[%s] Cookie 已就绪（%d 字符）", platform, len(header))
        else:
            self._failed[platform] = "浏览器与 cookies 文件均未取到"
            if platform not in COOKIE_REQUIRED:
                logger.debug("[%s] 未取到 Cookie，该平台匿名可访问，继续", platform)
                return header
            logger.warning(
                "[%s] 未能获取 Cookie：%s\n"
                "  1. 浏览器里没有登录该平台（先登录再试）\n"
                "  2. 浏览器正在运行，锁住了 Cookie 数据库（完全退出浏览器）\n"
                "  3. Edge/新版 Chrome 的 App-Bound 加密无法被第三方解密"
                "（在设置页选择 cookies.txt 导入）\n"
                "（本次运行内不再重试，如需重试请执行 dly cache --clear 或重启）",
                platform, self.last_error or "浏览器与 cookies 文件均未取到")
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

    def _from_disk_cache(self, platform: str) -> str:
        """读取本地缓存（未过期则复用，避免每次都去翻浏览器数据库）。"""
        if not self._use_cache or not self.CACHE_PATH.is_file():
            return ""
        try:
            data = json.loads(self.CACHE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        entry = data.get(platform) or {}
        if not entry.get("header") or entry.get("expires", 0) < time.time():
            return ""
        logger.info("[%s] 使用本地 Cookie 缓存（剩余 %d 天）", platform,
                    max(0, int((entry["expires"] - time.time()) // 86400)))
        return entry["header"]

    def _save_disk_cache(self, platform: str, header: str) -> None:
        if not self._use_cache:
            return
        try:
            self.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if self.CACHE_PATH.is_file():
                data = json.loads(self.CACHE_PATH.read_text(encoding="utf-8"))
            data[platform] = {
                "header": header,
                "expires": int(time.time()) + self._config.cache_days * 86400,
            }
            self.CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False),
                                       encoding="utf-8")
            if os.name != "nt":
                os.chmod(self.CACHE_PATH, 0o600)
            logger.debug("[%s] Cookie 已缓存 %d 天", platform,
                         self._config.cache_days)
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("写入 Cookie 缓存失败: %s", exc)

    def clear_cache(self) -> None:
        """清空本地 Cookie 缓存（缓存失效时需要）。"""
        self._cache.clear()
        self._failed.clear()
        try:
            self.CACHE_PATH.unlink(missing_ok=True)
            logger.info("本地 Cookie 缓存已清除")
        except OSError as exc:
            logger.warning("清除缓存失败: %s", exc)

    def _from_browser(self, platform: str) -> str:
        domains = PLATFORM_DOMAINS.get(platform)
        if not domains:
            return ""
        domain = domains[0]
        # 配置的浏览器优先，其余浏览器依次兜底（用户可能登录在别的浏览器里）
        preferred = (self._config.browser or "edge").lower()
        browsers = [preferred] + [b for b in ("edge", "chrome", "firefox", "brave")
                                  if b != preferred]
        errors = []
        for browser in browsers:
            header, err = self._via_ytdlp(browser, domain)
            if header:
                logger.info("[%s] 已从 %s 浏览器提取 Cookie", platform, browser)
                self._save_disk_cache(platform, header)
                return header
            if err:
                errors.append(err)
        fallback, err = self._via_browser_cookie3(preferred, domain)
        if fallback:
            return fallback
        if err:
            errors.append(err)
        # 同类报错只留一条，优先展示能指导用户操作的建议
        hints = [e for e in errors if _explain_cookie_error(e, preferred)]
        self.last_error = (hints[0] if hints else
                           (errors[0] if errors else "浏览器与 cookies 文件均未取到"))
        if hints:
            self.last_error = _explain_cookie_error(hints[0], preferred)
        return ""

    @staticmethod
    def _via_ytdlp(browser: str, domain: str) -> tuple:
        """复用 yt-dlp 内置的浏览器 Cookie 解密；返回 (cookie头, 错误文本)。"""
        try:
            from yt_dlp import YoutubeDL
        except ImportError:
            return "", ""
        try:
            with YoutubeDL({
                "quiet": True,
                "no_warnings": True,
                "logger": _QuietLogger(),
                "cookiesfrombrowser": (browser,),
            }) as ydl:
                jar = ydl.cookiejar
        except Exception as exc:
            message = str(exc)
            logger.debug("[%s] Cookie 提取失败: %s", browser, message)
            return "", message
        items = [
            f"{c.name}={c.value}" for c in jar
            if domain.lstrip(".") in (c.domain or "")
        ]
        return "; ".join(items), ""

    @staticmethod
    def _via_browser_cookie3(browser: str, domain: str) -> tuple:
        try:
            import browser_cookie3  # type: ignore
        except ImportError:
            return "", ""
        loader = getattr(browser_cookie3, browser, None)
        if loader is None:
            return "", ""
        try:
            jar: http.cookiejar.CookieJar = loader(domain_name=domain)
        except Exception as exc:
            message = str(exc)
            logger.debug("browser-cookie3 提取失败: %s", message)
            return "", message
        return "; ".join(f"{c.name}={c.value}" for c in jar), ""
