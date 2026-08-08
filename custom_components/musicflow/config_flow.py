"""MusicFlow 配置流程:手动填写 + Zeroconf 自动发现 + 重新认证。"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import MusicFlowAuthError, MusicFlowClient, MusicFlowError
from .const import (
    CONF_API_KEY,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_url(raw: str) -> str:
    """把用户输入补全成带 scheme、去掉尾斜杠、缺端口时补默认端口的 URL。"""
    url = raw.strip().rstrip("/")
    if not url:
        return url
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    if parsed.port is None and parsed.scheme == "http" and parsed.hostname:
        url = f"http://{parsed.hostname}:{DEFAULT_PORT}"
    return url


class MusicFlowConfigFlow(ConfigFlow, domain=DOMAIN):
    """MusicFlow 配置流程。"""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_url: str | None = None
        self._discovered_name: str | None = None

    # ==================== 通用校验 ====================
    async def _async_validate(
        self, url: str, api_key: str, verify_ssl: bool
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """返回 (errors, user_info)。errors 为空表示校验通过。"""
        session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)
        client = MusicFlowClient(session, url, api_key)
        try:
            info = await client.async_verify()
        except MusicFlowAuthError:
            return {"base": "invalid_auth"}, {}
        except MusicFlowError as err:
            _LOGGER.debug("MusicFlow 连通性校验失败: %s", err)
            return {"base": "cannot_connect"}, {}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("MusicFlow 连通性校验出现未预期异常")
            return {"base": "unknown"}, {}
        return {}, info

    def _schema(self, url_default: str = "") -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_URL, default=url_default): str,
                vol.Required(CONF_API_KEY): str,
                vol.Optional(CONF_VERIFY_SSL, default=True): bool,
            }
        )

    # ==================== 手动配置 ====================
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        url_default = ""
        if user_input is not None:
            url = _normalize_url(user_input[CONF_URL])
            url_default = url
            api_key = user_input[CONF_API_KEY].strip()
            verify_ssl = user_input.get(CONF_VERIFY_SSL, True)
            errors, info = await self._async_validate(url, api_key, verify_ssl)
            if not errors:
                # 手动添加时没有 mDNS uuid,退而用 URL 去重
                await self.async_set_unique_id(url, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._title(info, url),
                    data={
                        CONF_URL: url,
                        CONF_API_KEY: api_key,
                        CONF_VERIFY_SSL: verify_ssl,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(url_default),
            errors=errors,
        )

    # ==================== Zeroconf 发现 ====================
    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """后端 services/discovery/mdns.ts 广播 `_musicflow._tcp.local.`,
        TXT 里带 version 与 uuid。uuid 持久化在 DATA_DIR/.server-uuid,
        用它做 unique_id 才能在 IP 变化后仍然认得出是同一台。
        """
        props = discovery_info.properties or {}
        uuid = _prop(props, "uuid")
        host = discovery_info.host
        port = discovery_info.port or DEFAULT_PORT
        url = f"http://{host}:{port}"

        if uuid:
            await self.async_set_unique_id(uuid)
            # IP/端口漂移时顺手更新已存在的条目
            self._abort_if_unique_id_configured(updates={CONF_URL: url})
        else:
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()

        self._discovered_url = url
        self._discovered_name = discovery_info.name.partition(".")[0] or "MusicFlow"
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """发现后只需补一个 API Key。"""
        errors: dict[str, str] = {}
        url = self._discovered_url or ""
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            verify_ssl = user_input.get(CONF_VERIFY_SSL, True)
            errors, info = await self._async_validate(url, api_key, verify_ssl)
            if not errors:
                return self.async_create_entry(
                    title=self._title(info, url, self._discovered_name),
                    data={
                        CONF_URL: url,
                        CONF_API_KEY: api_key,
                        CONF_VERIFY_SSL: verify_ssl,
                    },
                )

        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={
                "name": self._discovered_name or "MusicFlow",
                "url": url,
            },
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): str,
                    vol.Optional(CONF_VERIFY_SSL, default=True): bool,
                }
            ),
            errors=errors,
        )

    # ==================== 重新认证 ====================
    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """API Key 失效时重新填写。"""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        url = entry.data[CONF_URL]
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            verify_ssl = entry.data.get(CONF_VERIFY_SSL, True)
            errors, _info = await self._async_validate(url, api_key, verify_ssl)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"url": url},
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )

    # ==================== 工具 ====================
    @staticmethod
    def _title(info: dict[str, Any], url: str, name: str | None = None) -> str:
        base = name or "MusicFlow"
        username = info.get("username") or (info.get("user") or {}).get("username")
        return f"{base} ({username})" if username else f"{base} ({url})"


def _prop(props: dict, key: str) -> str:
    """读取 TXT 记录。HA 各版本可能给 str 键或 bytes 键,两种都试。"""
    value = props.get(key)
    if value is None:
        value = props.get(key.encode())
    if isinstance(value, bytes):
        return value.decode("utf-8", "ignore").strip()
    return str(value).strip() if value else ""
