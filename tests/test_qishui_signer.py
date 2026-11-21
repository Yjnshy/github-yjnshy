"""汽水 PC 签名桥接测试。

签名依赖用户本机 node + 汽水客户端 bdms.node，不是所有环境都有，
所以核心逻辑（URL 参数、stub、身份缓存）总是测；真实签名用 skipif 守卫。
"""
import json

import pytest

from dly.core import qishui_signer as qs


def test_pc_track_v2_url_params():
    ident = {"device_id": "123456", "iid": "123457", "fp": "123456"}
    url = qs.pc_track_v2_url(ident)
    assert url.startswith("https://api.qishui.com/luna/pc/track_v2?")
    for key in ("aid=386088", "app_name=luna_pc", "device_platform=windows",
                "device_id=123456", "iid=123457", "fp=123456"):
        assert key in url, key


def test_md5_stub():
    # "{}" 的 MD5 = 99914b932bd37a50b983c5e7c90ae93b
    assert qs.md5_stub(b"{}") == "99914B932BD37A50B983C5E7C90AE93B"


def test_device_identity_cache(tmp_path):
    f = tmp_path / "identity.json"
    a = qs.device_identity(f)
    assert a["device_id"] and a["iid"] and a["fp"]
    b = qs.device_identity(f)
    assert b == a, "身份应被缓存，两次一致"


@pytest.mark.skipif(not qs.signer_available(),
                    reason="本机无 node 或汽水客户端 bdms.node")
def test_real_signature_roundtrip():
    """有客户端的机器上验证真实签名能出 X-Helios / X-Medusa。"""
    ident = qs.device_identity()
    url = qs.pc_track_v2_url(ident)
    stub = qs.md5_stub(b'{"track_id":"7414310889190836241"}')
    headers = {
        "Host": "api.qishui.com",
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
        "X-SS-STUB": stub,
    }
    signed = qs.sign_request(url, headers)
    assert signed.get("X-Helios"), signed
    assert signed.get("X-Medusa"), signed


def test_ttml_to_lrc():
    from dly.core.qishui_import import _ttml_to_lrc
    content = "[21040,3190]<0,300,0>每<300,280,0>当<580,290,0>乌\n[25640,3190]<0,300,0>也<300,280,0>代"
    lrc = _ttml_to_lrc(content)
    assert "[00:21.04]每当乌" in lrc, lrc
    assert "[00:25.64]也代" in lrc, lrc


@pytest.mark.skipif(not qs.signer_available(),
                    reason="本机无 node 或汽水客户端 bdms.node")
def test_full_version_uses_signing(tmp_path):
    """有客户端+登录态时能取到完整版（真实网络，需 sessionid 环境变量）。"""
    import os
    import pytest as _pytest
    sid = os.environ.get("QISHUI_TEST_SESSIONID", "").strip()
    if not sid:
        _pytest.skip("未提供 QISHUI_TEST_SESSIONID")
    from dly.core.qishui_import import full_version
    info = full_version("7414310889190836241", sid)
    assert info and info["audio_url"], info
    assert info["duration"] > 200, info
    assert info["lyric"]


def test_qishui_aes_key():
    """PlayAuth(spade) 解出 16 字节 AES key。"""
    from dly.core.qishui_import import qishui_aes_key
    # 从真实 GetPlayInfo 抓到的 hi_res PlayAuth
    key = qishui_aes_key("orws9le6L/ZluCrAVroe9FOyKs5hiRv3Vroq9mW5N8RJvgGOjg==")
    assert len(key) == 32, key
    bytes.fromhex(key)  # 合法 hex
