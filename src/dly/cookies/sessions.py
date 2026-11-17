"""站点登录会话：程序内登录窗口拿到的 Cookie，按平台存在本地。

为什么单独存一份，而不是继续用「读浏览器 Cookie」:

Edge / 新版 Chrome 对 Cookie 加了 App-Bound Encryption，密钥绑定在浏览器
进程上，第三方程序解不开（这不是能绕过的问题，是浏览器厂商的安全设计）。
所以真正的解法是让用户在**我们自己的窗口里**登录一次，WebView2 直接把
会话 Cookie 交给程序——不涉及解密，也不碰用户的浏览器数据。

存储位置：``~/.dly/login_sessions.json``，仅当前用户可读。
文件里只有名值对，不含密码；退出登录即删除对应条目。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

def _default_store() -> Path:
    """登录会话的默认落点。

    优先程序目录：打包后它一定可写（config.yaml 也放这里），而实测某些机器
    上冻结进程写 ``%LOCALAPPDATA%\\DLY`` 会被系统拒绝（PermissionError 13）。
    拿不到程序目录（比如装在 Program Files）时退回 AppData / 用户目录。
    """
    if getattr(sys, "frozen", False):
        try:
            return Path(sys.executable).resolve().parent / "login_sessions.json"
        except OSError:               # pragma: no cover 极端环境
            pass
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "DLY" / "login_sessions.json"
    return Path.home() / ".dly" / "login_sessions.json"


STORE_PATH = _default_store()

# 站点展示名：界面上只用这些名字，不出现技术术语
SITE_NAMES: Dict[str, str] = {
    "bilibili": "B 站",
    "douyin": "抖音",
    "xiaohongshu": "小红书",
}


def site_name(platform: str) -> str:
    return SITE_NAMES.get(platform, platform)


def _fallbacks() -> "list[Path]":
    """主位置写不进去时的备选落点（装在只读目录、策略受限等极端情况）。"""
    out = []
    base = os.environ.get("LOCALAPPDATA")
    if base:
        out.append(Path(base) / "DLY" / STORE_PATH.name)
    out.append(Path.home() / ".dly" / STORE_PATH.name)
    if getattr(sys, "frozen", False):
        try:
            out.append(Path(sys.executable).resolve().parent / STORE_PATH.name)
        except OSError:               # pragma: no cover
            pass
    return [p for p in out if p != STORE_PATH]


# 真正在用的文件：写入失败切到备选后记住它，读的时候优先看它
_active: Optional[Path] = None
_active_for: Optional[Path] = None      # 记录当时的 STORE_PATH，换位置就失效


def current_path() -> Path:
    """当前实际使用的会话文件（诊断与设置页展示用）。"""
    if _active is not None and _active_for == STORE_PATH:
        return _active
    return STORE_PATH


def _read() -> Dict[str, dict]:
    candidates = [current_path()] + _fallbacks()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _write(data: Dict[str, dict]) -> None:
    """写入会话文件；主位置不可写时依次尝试备选落点。"""
    global _active, _active_for
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    last_exc: Optional[OSError] = None
    for path in [current_path()] + _fallbacks():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
            if os.name != "nt":
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            _active = path
            _active_for = STORE_PATH
            return
        except OSError as exc:
            last_exc = exc
            logger.debug("写入 %s 失败：%s", path, exc)
    if last_exc is not None:
        raise last_exc


def save(platform: str, header: str, account: str = "") -> bool:
    """保存某站点的登录会话，成功返回 True。"""
    header = (header or "").strip()
    if not header:
        return False
    data = _read()
    data[platform] = {
        "header": header,
        "saved_at": int(time.time()),
        "account": account,
    }
    try:
        _write(data)
        logger.info("已保存 %s 的登录状态（%d 字符）→ %s", site_name(platform),
                    len(header), current_path())
        return True
    except OSError as exc:
        logger.warning("保存登录状态失败: %s", exc)
        return False


def get_header(platform: str) -> str:
    entry = _read().get(platform) or {}
    return entry.get("header") or ""


def clear(platform: str) -> bool:
    """退出登录：删掉该站点的会话。"""
    data = _read()
    if platform not in data:
        return False
    data.pop(platform, None)
    try:
        _write(data)
        return True
    except OSError as exc:
        logger.warning("清除登录状态失败: %s", exc)
        return False


def get(platform: str) -> Optional[dict]:
    return _read().get(platform)


def status() -> List[dict]:
    """给界面用的状态列表（绝不含 Cookie 值）。"""
    data = _read()
    now = int(time.time())
    out = []
    for platform, name in SITE_NAMES.items():
        entry = data.get(platform) or {}
        saved_at = int(entry.get("saved_at") or 0)
        out.append({
            "platform": platform,
            "name": name,
            "logged_in": bool(entry.get("header")),
            "saved_at": saved_at,
            "age_hours": round((now - saved_at) / 3600, 1) if saved_at else None,
            "account": entry.get("account") or "",
        })
    return out
