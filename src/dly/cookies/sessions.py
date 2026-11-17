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
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

STORE_PATH = Path.home() / ".dly" / "login_sessions.json"

# 站点展示名：界面上只用这些名字，不出现技术术语
SITE_NAMES: Dict[str, str] = {
    "bilibili": "B 站",
    "douyin": "抖音",
    "xiaohongshu": "小红书",
}


def site_name(platform: str) -> str:
    return SITE_NAMES.get(platform, platform)


def _read() -> Dict[str, dict]:
    if not STORE_PATH.is_file():
        return {}
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: Dict[str, dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, STORE_PATH)
    if os.name != "nt":
        try:
            os.chmod(STORE_PATH, 0o600)
        except OSError:
            pass


def save(platform: str, header: str, account: str = "") -> None:
    """保存某站点的登录会话。"""
    header = (header or "").strip()
    if not header:
        return
    data = _read()
    data[platform] = {
        "header": header,
        "saved_at": int(time.time()),
        "account": account,
    }
    try:
        _write(data)
        logger.info("已保存 %s 的登录状态（%d 字符）", site_name(platform),
                    len(header))
    except OSError as exc:
        logger.warning("保存登录状态失败: %s", exc)


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
