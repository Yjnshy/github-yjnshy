"""统一配置管理：YAML 文件 + 环境变量覆盖。

约定：
- 配置文件查找顺序：--config 指定 > ./config.yaml > ~/.dly/config.yaml
- 环境变量前缀 DLY_，例如 DLY_OUTPUT_DIR、DLY_PROXY
- 配置里严禁存放 Cookie 明文，Cookie 一律走 cookies 模块动态获取
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from dly.core import paths

DEFAULT_CONFIG_PATHS = [
    Path("config.yaml"),
    Path.home() / ".dly" / "config.yaml",
]


@dataclass
class CookieConfig:
    browser: str = "chrome"            # chrome / edge / firefox / brave
    cookies_file: str = ""           # Netscape 格式 cookies.txt 路径
    manual_cookie: str = ""          # 设置页直接粘贴的 Cookie 请求头
    cache_days: int = 7              # 提取成功后的本地缓存天数，0 表示不缓存


@dataclass
class RecordConfig:
    format: str = "ts"
    poll_interval: int = 60
    max_retries: int = 20
    segment_minutes: int = 0
    fps: int = 0                      # 0 = 原帧率直通（-c copy，不转码）
    rooms: List[str] = field(default_factory=list)


@dataclass
class UiConfig:
    """界面偏好：设置页可改，前端也会用 localStorage 先渲染避免闪白。"""

    theme: str = "system"             # system / light / dark
    sound: bool = True                # 任务完成提示音
    log_level: str = "info"           # 界面日志最低显示级别 info / warn / error


@dataclass
class AppConfig:
    output_dir: str = ""              # 空 = 按运行方式推导默认目录
    quality: str = "best"
    fps: int = 0                      # 下载完成后转码到的帧率，0 = 不转码
    keep_original: bool = False       # 转码后是否保留原片
    proxy: str = ""
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    cookies: CookieConfig = field(default_factory=CookieConfig)
    record: RecordConfig = field(default_factory=RecordConfig)
    ui: UiConfig = field(default_factory=UiConfig)


def _default_config_paths() -> List[Path]:
    """配置文件搜索顺序：当前目录 > 程序目录（打包后为 exe 同级）> 用户目录。"""
    candidates = list(DEFAULT_CONFIG_PATHS)
    local = paths.app_dir() / "config.yaml"
    if local not in candidates:
        candidates.insert(1, local)
    return candidates


def _find_config_file(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"配置文件不存在: {p}")
        return p
    for p in _default_config_paths():
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
        cfg.fps = int(raw.get("fps", cfg.fps) or 0)
        cfg.keep_original = bool(raw.get("keep_original", cfg.keep_original))
        cfg.proxy = raw.get("proxy", cfg.proxy)
        cfg.ffmpeg_path = raw.get("ffmpeg_path", cfg.ffmpeg_path)
        cfg.ffprobe_path = raw.get("ffprobe_path", cfg.ffprobe_path)
        ck = raw.get("cookies") or {}
        cfg.cookies = CookieConfig(
            browser=ck.get("browser", "chrome"),
            cookies_file=ck.get("cookies_file", "") or "",
            manual_cookie=ck.get("manual_cookie", "") or "",
            cache_days=int(ck.get("cache_days", 7)),
        )
        rc = raw.get("record") or {}
        cfg.record = RecordConfig(
            format=rc.get("format", "ts"),
            poll_interval=int(rc.get("poll_interval", 60)),
            max_retries=int(rc.get("max_retries", 20)),
            segment_minutes=int(rc.get("segment_minutes", 0)),
            fps=int(rc.get("fps", 0) or 0),
            rooms=[r for r in (rc.get("rooms") or [])
                   if isinstance(r, str) and r.strip() and not r.strip().startswith("#")],
        )
        ui = raw.get("ui") or {}
        cfg.ui = UiConfig(
            theme=ui.get("theme", "system")
            if ui.get("theme") in ("system", "light", "dark") else "system",
            sound=bool(ui.get("sound", True)),
            log_level=ui.get("log_level", "info")
            if ui.get("log_level") in ("info", "warn", "error") else "info",
        )

    # 环境变量覆盖
    cfg.output_dir = os.environ.get("DLY_OUTPUT_DIR", cfg.output_dir)
    cfg.quality = os.environ.get("DLY_QUALITY", cfg.quality)
    cfg.fps = int(os.environ.get("DLY_FPS", cfg.fps) or 0)
    cfg.proxy = os.environ.get("DLY_PROXY", cfg.proxy)
    cfg.cookies.browser = os.environ.get("DLY_COOKIE_BROWSER", cfg.cookies.browser)
    cfg.cookies.cookies_file = os.environ.get("DLY_COOKIES_FILE", cfg.cookies.cookies_file)

    # 路径收敛：没配就用推导出的默认目录；配了相对路径则按程序目录解析，
    # 否则双击启动（工作目录可能是 system32）会把文件下到莫名其妙的地方
    cfg.output_dir = (paths.resolve_path(cfg.output_dir) if cfg.output_dir
                      else str(paths.default_output_dir()))
    return cfg


# ---------------------------------------------------------------- 设置页持久化
#
# 界面把配置读成一个扁平字典，保存时只覆盖白名单里的键，其余（proxy、
# ffmpeg_path、cookies_file、max_retries 等）原样保留。
# 用户在设置页粘贴的 Cookie 明文只写进本机 config.yaml（该文件已 gitignore，
# 不会入库、不上传、不进 zip），且界面展示时只回显到本地。

QUALITY_CHOICES = ("best", "1080", "720", "480", "audio", "mp3")
BROWSER_CHOICES = ("chrome", "edge", "firefox", "brave")
FORMAT_CHOICES = ("ts", "mp4", "mkv", "flv")
THEME_CHOICES = ("system", "light", "dark")
LOG_LEVEL_CHOICES = ("info", "warn", "error")


def settings_from_config(cfg: AppConfig) -> Dict[str, Any]:
    """把 AppConfig 摊平成设置页 / 前端默认值用的字典。"""
    return {
        "output_dir": cfg.output_dir,
        "quality": cfg.quality,
        "fps": cfg.fps,
        "keep_original": cfg.keep_original,
        "browser": cfg.cookies.browser,
        "cookies_file": cfg.cookies.cookies_file,
        "manual_cookie": cfg.cookies.manual_cookie,
        "record_format": cfg.record.format,
        "segment_minutes": cfg.record.segment_minutes,
        "poll_interval": cfg.record.poll_interval,
        "record_fps": cfg.record.fps,
        "rooms": list(cfg.record.rooms),
        "theme": cfg.ui.theme,
        "sound": cfg.ui.sound,
        "log_level": cfg.ui.log_level,
    }


def config_file_path() -> Path:
    """返回设置应该写到哪个文件：优先正在生效的那个，否则选一个可写位置。

    一律返回绝对路径。``./config.yaml`` 这种相对路径依赖进程工作目录，
    换一种启动方式（双击 exe / 快捷方式 / 命令行）就可能指到不同文件，
    界面上也没法告诉用户究竟改了哪一份。
    """
    found = _find_config_file(None)
    if found:
        return found.resolve()
    user = Path.home() / ".dly" / "config.yaml"
    for candidate in (paths.app_dir() / "config.yaml", user):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            probe = candidate.parent / ".dly_write_probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return candidate.resolve()
        except OSError:
            continue
    return user.resolve()            # 实在不可写时仍给出路径，由调用方报错


def _choice(value: Any, choices: tuple, default: str) -> str:
    text = str(value or "").strip()
    return text if text in choices else default


def _number(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _section(raw: Dict[str, Any], name: str) -> Dict[str, Any]:
    current = raw.get(name)
    return dict(current) if isinstance(current, dict) else {}


def save_settings(data: Dict[str, Any], path: Optional[str] = None) -> Path:
    """局部更新 config.yaml 并原子写回；返回实际写入的路径。

    未提供的键保持原样。非法值按默认值规整而不是直接报错，避免界面上
    一个手滑的输入把整个配置文件写坏。
    """
    target = (Path(path) if path else config_file_path()).resolve()
    raw: Dict[str, Any] = {}
    if target.is_file():
        try:
            loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, yaml.YAMLError):
            raw = {}

    cfg = load_config(str(target)) if target.is_file() else AppConfig()
    current = settings_from_config(cfg)

    if "output_dir" in data:
        raw["output_dir"] = str(data["output_dir"] or "").strip()[:500]
    if "quality" in data:
        raw["quality"] = _choice(data["quality"], QUALITY_CHOICES,
                                 current["quality"])
    if "fps" in data:
        raw["fps"] = _number(data["fps"], 0, 120, 0)
    if "keep_original" in data:
        raw["keep_original"] = bool(data["keep_original"])
    if "browser" in data:
        cookies = _section(raw, "cookies")
        cookies["browser"] = _choice(data["browser"], BROWSER_CHOICES,
                                     current["browser"])
        raw["cookies"] = cookies
    if "cookies_file" in data:
        # 只存用户选中的路径，文件本身绝不入库 / 上传
        value = str(data["cookies_file"] or "").strip()[:500]
        if value and not Path(value).is_file():
            value = ""          # 路径失效就清空，避免界面显示一个假文件
        cookies = _section(raw, "cookies")
        cookies["cookies_file"] = value
        raw["cookies"] = cookies
    if "manual_cookie" in data:
        # 只存本机 config.yaml；允许较长的整段 Cookie 请求头
        value = str(data["manual_cookie"] or "").strip()[:20000]
        cookies = _section(raw, "cookies")
        cookies["manual_cookie"] = value
        raw["cookies"] = cookies

    record = _section(raw, "record")
    if "record_format" in data:
        record["format"] = _choice(data["record_format"], FORMAT_CHOICES,
                                   current["record_format"])
    if "segment_minutes" in data:
        record["segment_minutes"] = _number(data["segment_minutes"],
                                            0, 1440, 0)
    if "poll_interval" in data:
        record["poll_interval"] = _number(data["poll_interval"], 10, 3600, 60)
    if "record_fps" in data:
        record["fps"] = _number(data["record_fps"], 0, 120, 0)
    if "rooms" in data:
        rooms = data["rooms"]
        if isinstance(rooms, str):
            rooms = rooms.splitlines()
        cleaned = []
        for item in rooms if isinstance(rooms, (list, tuple)) else []:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text and not text.startswith("#") and len(text) <= 500:
                cleaned.append(text)
        record["rooms"] = cleaned[:200]
    if record:
        raw["record"] = record

    ui = _section(raw, "ui")
    if "theme" in data:
        ui["theme"] = _choice(data["theme"], THEME_CHOICES, "system")
    if "sound" in data:
        ui["sound"] = bool(data["sound"])
    if "log_level" in data:
        ui["log_level"] = _choice(data["log_level"], LOG_LEVEL_CHOICES, "info")
    if ui:
        raw["ui"] = ui

    target.parent.mkdir(parents=True, exist_ok=True)
    text = ("# 帅羊羊DLY 设置（界面设置页维护，所有配置项的值都会保留）\n"
            + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return target
