"""MusicFlow 配置流程:手动输入 + Zeroconf 自动发现。"""
from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.zeroconf import ZeroconfServiceInfo
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_API_KEY, CONF_URL, DEFAULT_PORT, DOMAIN, ZEROCONF_TYPE

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_API_KEY): str,
    }
)


async def _test_connection(session: aiohttp.ClientSession, url: str, api_key: str) -> bool:
    """验证 MusicFlow 连通性与 API Key。"""
    try:
        async with session.get(
            f"{url.rstrip('/')}/api/v1/users/me",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


class MusicFlowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """MusicFlow 配置流程。"""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """手动配置入口。"""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip("/")
            api_key = user_input[CONF_API_KEY]
            session = aiohttp.ClientSession()
            try:
                if not await _test_connection(session, url, api_key):
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=f"MusicFlow ({url})",
                        data={CONF_URL: url, CONF_API_KEY: api_key},
                    )
            finally:
                await session.close()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> FlowResult:
        """Zeroconf 自动发现:后端广播 _musicflow._tcp.local. 被捕获。"""
        props = discovery_info.properties
        uuid = props.get(b"uuid", b"").decode("ascii", "ignore")
        if uuid:
            await self.async_set_unique_id(uuid)
            self._abort_if_unique_id_configured()

        port = discovery_info.port or DEFAULT_PORT
        url = f"http://{discovery_info.host}:{port}"
        self.context[CONF_URL] = url
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """发现后让用户输入 api_key 确认。"""
        errors: dict[str, str] = {}
        url = self.context.get(CONF_URL, "")
        if user_input is not None:
            api_key = user_input[CONF_API_KEY]
            session = aiohttp.ClientSession()
            try:
                if not await _test_connection(session, url, api_key):
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=f"MusicFlow ({url})",
                        data={CONF_URL: url, CONF_API_KEY: api_key},
                    )
            finally:
                await session.close()

        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={"url": url},
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )
