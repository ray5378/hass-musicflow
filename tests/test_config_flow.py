"""config_flow.py 单测（简体中文）。

锁死对象：手动配置 / Zeroconf 自动发现 / 重新认证三条入口，
以及 URL 归一化、TXT 记录读取、标题生成等纯函数工具。
校验阶段用假 MusicFlowClient 顶替真实网络。
"""

from __future__ import annotations

import dataclasses
from ipaddress import ip_address
from typing import Any

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.musicflow import config_flow
from custom_components.musicflow.api import MusicFlowAuthError, MusicFlowError
from custom_components.musicflow.const import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DOMAIN,
)


# ==================== 假客户端 ====================
class _FakeClient:
    """顶替 config_flow 里的 MusicFlowClient，只实现 async_verify。"""

    result: Any = {"username": "alice"}
    error: BaseException | None = None
    instances: list["_FakeClient"] = []

    def __init__(self, session, url, username, password) -> None:
        self.session = session
        self.url = url
        self.username = username
        self.password = password
        _FakeClient.instances.append(self)

    async def async_verify(self) -> dict:
        if _FakeClient.error is not None:
            raise _FakeClient.error
        return _FakeClient.result


@pytest.fixture
def flow_client(monkeypatch):
    _FakeClient.result = {"username": "alice"}
    _FakeClient.error = None
    _FakeClient.instances = []
    monkeypatch.setattr(config_flow, "MusicFlowClient", _FakeClient)
    monkeypatch.setattr(
        config_flow, "async_get_clientsession", lambda hass, verify_ssl=True: object()
    )
    return _FakeClient


def _zc_info(
    host: str = "192.168.1.5",
    port: int = DEFAULT_PORT,
    name: str = "MusicFlow._musicflow._tcp.local.",
    properties: dict | None = None,
):
    """构造真实的 ZeroconfServiceInfo（按当前 HA 版本的 dataclass 字段动态填充）。"""
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

    values = {
        "ip_address": ip_address("192.168.1.5"),
        "ip_addresses": [ip_address("192.168.1.5")],
        "port": port,
        "hostname": host,
        "host": host,
        "type": "_musicflow._tcp.local.",
        "name": name,
        "properties": dict(properties or {}),
        "homekit_compatible": False,
    }
    kwargs = {
        f.name: values[f.name]
        for f in dataclasses.fields(ZeroconfServiceInfo)
        if f.name in values
    }
    return ZeroconfServiceInfo(**kwargs)


# ==================== 纯函数工具 ====================
def test_normalize_url():
    assert config_flow._normalize_url("  mf.local/  ") == f"http://mf.local:{DEFAULT_PORT}"
    assert config_flow._normalize_url("http://mf.local:8080/") == "http://mf.local:8080"
    assert config_flow._normalize_url("http://mf.local") == f"http://mf.local:{DEFAULT_PORT}"
    assert config_flow._normalize_url("https://mf.local") == "https://mf.local"
    # 空输入原样返回
    assert config_flow._normalize_url("   ") == ""


def test_prop_reads_str_bytes_and_missing():
    assert config_flow._prop({"uuid": "abc"}, "uuid") == "abc"
    assert config_flow._prop({b"uuid": b"abc"}, "uuid") == "abc"
    assert config_flow._prop({"uuid": b"abc"}, "uuid") == "abc"
    assert config_flow._prop({}, "uuid") == ""


def test_title():
    assert config_flow.MusicFlowConfigFlow._title({"username": "alice"}, "u") == "MusicFlow (alice)"
    assert (
        config_flow.MusicFlowConfigFlow._title({"user": {"username": "bob"}}, "u")
        == "MusicFlow (bob)"
    )
    assert config_flow.MusicFlowConfigFlow._title({}, "http://x") == "MusicFlow (http://x)"
    assert config_flow.MusicFlowConfigFlow._title({"username": "alice"}, "u", "客厅") == "客厅 (alice)"
    assert config_flow.MusicFlowConfigFlow._title({}, "u", "客厅") == "客厅 (u)"


def test_version_constant():
    assert config_flow.MusicFlowConfigFlow.VERSION == 2
    # 领域由 `class MusicFlowConfigFlow(ConfigFlow, domain=DOMAIN)` 注册进 HA 的
    # HANDLERS；ConfigFlow.__init_subclass__ 只做注册，不会在类上留 `domain` 属性，
    # 所以这里锁定注册结果本身。
    assert config_entries.HANDLERS.get(DOMAIN) is config_flow.MusicFlowConfigFlow


# ==================== 手动配置 ====================
async def test_user_flow_shows_form(hass, flow_client):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_flow_success(hass, flow_client):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: "mf.local",
            CONF_USERNAME: "  alice  ",
            CONF_PASSWORD: "secret",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "MusicFlow (alice)"
    assert result["data"][CONF_URL] == f"http://mf.local:{DEFAULT_PORT}"
    assert result["data"][CONF_USERNAME] == "alice"  # strip 掉空白
    assert result["data"][CONF_VERIFY_SSL] is True  # 缺省开启


async def test_user_flow_invalid_auth(hass, flow_client):
    flow_client.error = MusicFlowAuthError("bad")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "mf.local", CONF_USERNAME: "alice", CONF_PASSWORD: "bad"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass, flow_client):
    flow_client.error = MusicFlowError("down")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "mf.local", CONF_USERNAME: "alice", CONF_PASSWORD: "p"},
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_unknown_error(hass, flow_client):
    flow_client.error = ValueError("boom")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "mf.local", CONF_USERNAME: "alice", CONF_PASSWORD: "p"},
    )
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_duplicate_aborts(hass, flow_client):
    existing = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=f"http://mf.local:{DEFAULT_PORT}",
        data={
            CONF_URL: f"http://mf.local:{DEFAULT_PORT}",
            CONF_USERNAME: "alice",
            CONF_PASSWORD: "secret",
        },
    )
    existing.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "mf.local", CONF_USERNAME: "alice", CONF_PASSWORD: "secret"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ==================== Zeroconf 发现 ====================
async def test_zeroconf_flow_with_uuid(hass, flow_client):
    info = _zc_info(properties={"uuid": "server-uuid-1", "version": "1"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "alice", CONF_PASSWORD: "secret"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == f"http://192.168.1.5:{DEFAULT_PORT}"


async def test_zeroconf_flow_without_uuid_uses_url(hass, flow_client):
    info = _zc_info(properties={})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info
    )
    assert result["step_id"] == "zeroconf_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "alice", CONF_PASSWORD: "secret"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_zeroconf_flow_updates_existing_entry(hass, flow_client):
    # 已有条目（uuid 相同）→ 顺手更新 URL（IP 漂移场景）
    existing = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="server-uuid-1",
        data={
            CONF_URL: "http://old:46400",
            CONF_USERNAME: "alice",
            CONF_PASSWORD: "secret",
        },
    )
    existing.add_to_hass(hass)
    info = _zc_info(properties={"uuid": "server-uuid-1"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert existing.data[CONF_URL] == f"http://192.168.1.5:{DEFAULT_PORT}"


async def test_zeroconf_confirm_error_path(hass, flow_client):
    flow_client.error = MusicFlowError("down")
    info = _zc_info(properties={"uuid": "server-uuid-2"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "alice", CONF_PASSWORD: "p"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


# ==================== 重新认证 ====================
async def test_reauth_flow_success(hass, flow_client):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="server-uuid-3",
        data={
            CONF_URL: "http://mf.local:46400",
            CONF_USERNAME: "alice",
            CONF_PASSWORD: "old",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "alice", CONF_PASSWORD: "new"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_flow_invalid_auth(hass, flow_client):
    flow_client.error = MusicFlowAuthError("bad")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="server-uuid-4",
        data={
            CONF_URL: "http://mf.local:46400",
            CONF_USERNAME: "alice",
            CONF_PASSWORD: "old",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "alice", CONF_PASSWORD: "bad"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_confirm_unknown_error(hass, flow_client):
    flow_client.error = ValueError("boom")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="server-uuid-5",
        data={
            CONF_URL: "http://mf.local:46400",
            CONF_USERNAME: "alice",
            CONF_PASSWORD: "old",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "alice", CONF_PASSWORD: "p"},
    )
    assert result["errors"] == {"base": "unknown"}


async def test_reauth_confirm_without_input_shows_form(hass, flow_client):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="server-uuid-6",
        data={
            CONF_URL: "http://mf.local:46400",
            CONF_USERNAME: "alice",
            CONF_PASSWORD: "old",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    # HA 会在流程自带的占位符之外额外注入 {"name": entry.title}
    placeholders = result["description_placeholders"]
    assert placeholders["url"] == "http://mf.local:46400"
    assert placeholders["name"] == entry.title
