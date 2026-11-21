"""汽水音乐 PC 端请求签名（x-helios / x-medusa）。

会员/整曲流走 ``POST /luna/pc/track_v2``，服务端只认官方原生安全组件
``bdms.node``（底层 mssdk/metasecml.dll）逐请求生成的签名头，缺签名时返回
HTTP 200 + 空 body，公开的 SEO 端点无论登录与否都只给试听片段。

本模块不破解任何加密算法，而是"借用"用户本机已安装的汽水 PC 客户端自带的
``bdms.node`` 算签名：
  1. 自动探测客户端目录与 bdms.node（含非标准安装路径）；
  2. 起一个短命 node 进程执行内置 JS 桥接垫片；
  3. 请求描述走 stdin，签好的头走 stdout，进程随用随起、不占端口。

协议参考 sodam / libresoda 的 mssdk-bridge（AGPL-3.0）与
music-lib/soda 的 qishui-bridge.cjs，此处为独立实现。

仅用于个人对自己已合法访问的内容做离线备份，不传播、不商用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# bdms.node：init({deviceId}) + generateHttpSignatureHeaders(url, headerLines)
# headerLines 形如 "Host\r\napi.qishui.com\r\nUser-Agent\r\n..."，
# 返回按 \r\n 成对排列的 "头名\r\n值\r\n..."。stdin 收请求描述，stdout 出 {"headers":{...}}。
_BRIDGE_JS = r"""
const fs = require('fs');
let input = '';
process.stdin.on('data', c => { input += c; });
process.stdin.on('end', () => {
  try {
    const req = JSON.parse(input || '{}');
    const bdmsPath = String(process.env.QISHUI_BDMS_PATH || '').trim();
    if (!bdmsPath || !fs.existsSync(bdmsPath)) throw new Error('bdms.node 不存在');
    const deviceId = String(process.env.QISHUI_DEVICE_ID || '').trim();
    if (!deviceId) throw new Error('缺少 deviceId');
    const bdms = require(bdmsPath);
    bdms.init({ deviceId: deviceId });
    const src = req.headers || {};
    const lines = Object.keys(src)
      .filter(n => n && src[n] !== undefined && src[n] !== null)
      .map(n => n + '\r\n' + String(src[n]))
      .join('\r\n');
    const raw = String(bdms.generateHttpSignatureHeaders(String(req.url || ''), lines) || '');
    const fields = raw.split('\r\n').filter(x => x.trim());
    const headers = {};
    for (let i = 0; i + 1 < fields.length; i += 2) headers[fields[i]] = fields[i + 1];
    if (!headers['X-Helios'] || !headers['X-Medusa']) {
      throw new Error('签名不完整');
    }
    process.stdout.write(JSON.stringify({ headers: headers }));
  } catch (e) {
    process.stderr.write(String((e && e.message) || e));
    process.exit(1);
  }
});
"""

# 常见的客户端安装位置（不含具体盘符，靠 _drive_roots() 展开所有盘）
_CLIENT_SUBPATHS = [
    r"Program Files (x86)\qishuiyiinyue\Soda Music",
    r"Program Files\qishuiyiinyue\Soda Music",
    r"Program Files (x86)\Soda Music",
    r"Program Files\Soda Music",
    r"qishuiyiinyue\Soda Music",
    r"Soda Music",
]
_VERSION_DIR_RE = re.compile(r"^\d+\.\d+\.\d+$")
_BDMS_REL = Path("resources") / "app.asar.unpacked" / "bdms.node"


def _drive_roots() -> list[Path]:
    """所有可用盘符根（Windows 从 A-Z 探，非 Windows 只给 /）。"""
    if not sys.platform.startswith("win"):
        return [Path("/")]
    import string
    roots = []
    for letter in string.ascii_uppercase:
        p = Path(f"{letter}:\\")
        try:
            if p.is_dir():
                roots.append(p)
        except OSError:
            pass
    return roots


def _uninstall_paths() -> list[Path]:
    """从注册表卸载项读汽水安装目录（最通用，不猜盘符/路径）。"""
    out: list[Path] = []
    if not sys.platform.startswith("win"):
        return out
    try:
        import winreg
    except ImportError:
        return out
    hives = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, sub in hives:
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        i = 0
        while True:
            try:
                child = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            try:
                ck = winreg.OpenKey(key, child)
                name = winreg.QueryValueEx(ck, "DisplayName")[0]
                if re.search(r"汽水|Soda|qishui", str(name), re.I):
                    for field in ("InstallLocation", "DisplayIcon"):
                        try:
                            val = str(winreg.QueryValueEx(ck, field)[0]).strip('"')
                        except OSError:
                            continue
                        if not val:
                            continue
                        p = Path(val)
                        # DisplayIcon 常是 exe 路径，取所在目录
                        if p.suffix.lower() in (".exe",):
                            p = p.parent
                        # 可能指到版本子目录，向上找到含 resources 的根
                        if p.is_dir():
                            out.append(p)
                            for parent in list(p.parents)[:2]:
                                out.append(parent)
            except OSError:
                continue
    return out


def _versioned_client(root: Path) -> Optional[Path]:
    """给定安装根，返回含 bdms.node 的版本目录（版本号最大的优先）。"""
    if not root.is_dir():
        return None
    versions = [d for d in root.iterdir()
                if d.is_dir() and _VERSION_DIR_RE.match(d.name)]
    candidates = sorted(versions, key=lambda d: [int(x) for x in d.name.split(".")]) or [root]
    for d in reversed(candidates):
        if (d / _BDMS_REL).is_file():
            return d
    return None


def find_client_dir() -> Optional[Path]:
    """探测汽水 PC 客户端版本目录（含 bdms.node 的那一层）。

    顺序：环境变量 > 注册表卸载项 > LOCALAPPDATA > 各盘常见路径。
    全部按「含 bdms.node」校验，绝不写死某个盘符。
    """
    env = os.environ.get("QISHUI_CLIENT_DIR", "").strip()
    roots: list[Path] = [Path(env)] if env else []
    roots += _uninstall_paths()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        roots += [Path(local) / "Programs" / "Soda Music",
                  Path(local) / "Programs" / "汽水音乐"]
    # 各盘常见路径兜底
    for drive in _drive_roots():
        for sub in _CLIENT_SUBPATHS:
            roots.append(drive / sub)

    # 先直接命中带 bdms.node 的目录
    for root in roots:
        found = _versioned_client(root)
        if found:
            return found
    return None


def find_bdms() -> Optional[Path]:
    env = os.environ.get("QISHUI_BDMS_PATH", "").strip()
    if env and Path(env).is_file():
        return Path(env)
    client = find_client_dir()
    if client:
        p = client / "resources" / "app.asar.unpacked" / "bdms.node"
        if p.is_file():
            return p
    return None


def find_node() -> Optional[str]:
    """找 JS 运行时：环境变量 > 程序自带 node > 系统 PATH 的 node。

    打包版会随附一个 node.exe（bin/node.exe），这样用户电脑无需另装 Node.js。
    注意：客户端自带的 Electron（SodaMusic.exe + ELECTRON_RUN_AS_NODE=1）实测
    加载 bdms.node 会崩溃，不能当回退用，故不采用。
    """
    env = os.environ.get("QISHUI_NODE", "").strip()
    if env and Path(env).is_file():
        return env
    # 程序自带 node（打包版在资源目录 bin/ 下；源码运行在我方环境也可指定）
    try:
        from dly.core import paths
        for directory in paths.resource_dirs():
            for rel in ("node.exe", "bin/node.exe", "bin/node"):
                p = Path(directory) / rel
                if p.is_file():
                    return str(p)
    except Exception:
        pass
    return shutil.which("node")


def signer_available() -> bool:
    """本机是否具备签名条件（JS 运行时 + bdms.node）。"""
    return bool(find_node() and find_bdms())


def device_identity(cache_file: Optional[Path] = None) -> Dict[str, str]:
    """取（并缓存）设备身份 device_id / iid / fp。"""
    if cache_file is None:
        cache_file = Path(os.environ.get("QISHUI_IDENTITY_FILE", "")) if os.environ.get(
            "QISHUI_IDENTITY_FILE") else None
    if cache_file is None:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        cache_file = base / "DLY" / "qishui_signer_identity.json"
    if cache_file.is_file():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if data.get("device_id"):
                return {"device_id": str(data["device_id"]),
                        "iid": str(data.get("iid") or ""),
                        "fp": str(data.get("fp") or data["device_id"])}
        except (OSError, ValueError):
            pass
    seed = int(time.time() * 1000)
    ident = {"device_id": f"{seed}{int(time.time() * 1000) % 1000:03d}",
             "iid": str(seed + 1), "fp": ""}
    ident["fp"] = ident["device_id"]
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(ident, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return ident


PC_PARAMS_BASE = {
    "aid": "386088", "app_name": "luna_pc", "region": "cn",
    "geo_region": "cn", "os_region": "cn", "sim_region": "",
    "cdid": "", "version_name": "3.3.0", "version_code": "30030000",
    "channel": "official", "build_mode": "master", "network_carrier": "",
    "ac": "wifi", "tz_name": "Asia/Shanghai", "resolution": "",
    "device_platform": "windows", "device_type": "Windows",
    "os_version": "Windows 11",
}


def pc_track_v2_url(ident: Dict[str, str]) -> str:
    import urllib.parse
    params = dict(PC_PARAMS_BASE)
    params.update({"device_id": ident["device_id"],
                   "iid": ident.get("iid") or ident["device_id"],
                   "fp": ident.get("fp") or ident["device_id"]})
    return "https://api.qishui.com/luna/pc/track_v2?" + urllib.parse.urlencode(params)


def sign_request(url: str, headers: Dict[str, str]) -> Dict[str, str]:
    """给一次请求算签名头。失败抛 RuntimeError。

    返回需要合并进请求的头（通常是 X-Helios / X-Medusa）。
    """
    node = find_node()
    bdms = find_bdms()
    if not node or not bdms:
        raise RuntimeError("未找到签名依赖（Node.js 或汽水客户端 bdms.node）")
    ident = device_identity()
    payload = json.dumps({"url": url, "method": "POST", "headers": headers})
    env = dict(os.environ)
    # 用户机器可能设了 NODE_OPTIONS（注入 --require 等），会让签名子进程加载失败；
    # 签名桥接不依赖任何外部 node 配置，清掉它避免污染。
    env.pop("NODE_OPTIONS", None)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    env["QISHUI_BDMS_PATH"] = str(bdms)
    env["QISHUI_DEVICE_ID"] = ident["device_id"]
    try:
        proc = subprocess.run(
            [node, "-e", _BRIDGE_JS], input=payload.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=25,
            creationflags=0x08000000,  # CREATE_NO_WINDOW，不闪控制台
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"签名进程启动失败：{exc}") from exc
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip() or "未知错误"
        raise RuntimeError(f"汽水签名失败：{msg[:200]}")
    try:
        out = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RuntimeError("汽水签名输出解析失败") from exc
    signed = {str(k): str(v) for k, v in (out.get("headers") or {}).items()}
    if not signed.get("X-Helios") or not signed.get("X-Medusa"):
        raise RuntimeError("汽水签名不完整")
    return signed


def md5_stub(body: bytes) -> str:
    """App 端 X-SS-STUB = body 的 MD5 大写十六进制。"""
    return hashlib.md5(body).hexdigest().upper()
