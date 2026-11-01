"""Config flow for Sonoff Local."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_CAPTURE_PAYLOADS,
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_LOCAL_TYPE,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .mdns import async_lookup_txt
from .protocol import (
    COMMAND_GET_STATE,
    COMMAND_INFO,
    DEFAULT_PORT,
    DEVICE_ID_RE,
    ERROR_NOT_FOUND,
    SonoffClient,
    SonoffConnectionError,
    SonoffDecryptError,
    SonoffDeviceError,
    SonoffEncryptionRequired,
    SonoffError,
    SonoffKeyRejected,
    TxtFrame,
    decode_txt,
    parse_device_id,
    parse_txt,
)

_LOGGER = logging.getLogger(__name__)

_KEY_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_PORT_FIELD = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))


def _clean_key(user_input: dict[str, Any]) -> str | None:
    return (user_input.get(CONF_DEVICE_KEY) or "").strip() or None


def _frame_matches_entry(entry: ConfigEntry, frame: TxtFrame) -> bool:
    """Return True if a frame is proven to come from this entry's device."""
    if not (key := entry.data.get(CONF_DEVICE_KEY)):
        return True
    if not frame.encrypted:
        return False
    try:
        decode_txt(frame, key)
    except SonoffError:
        return False
    return True


class SonoffLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a device, discovered or by address."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing known."""
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._deviceid: str | None = None
        self._frame: TxtFrame | None = None
        self._local_type: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return SonoffOptionsFlow()

    async def _async_probe(
        self,
        host: str,
        port: int,
        deviceid: str,
        devicekey: str | None,
    ) -> tuple[str | None, str | None]:
        """Check a device can be used. Returns ``(error, key_to_store)``.

        The mDNS record decides the mode when one is available, and it also proves a
        key without sending anything. Without one, the mode is what the user entered
        and the reply to one request is the only evidence.
        """
        frame = self._frame or await async_lookup_txt(self.hass, deviceid)
        key_proven = False
        if frame is not None:
            self._local_type = frame.localtype
            if frame.encrypted and devicekey is None:
                return "key_required", None
            if not frame.encrypted:
                devicekey = None  # Unused by this device, so do not store it.
            elif frame.data:
                try:
                    decode_txt(frame, devicekey)
                except SonoffDecryptError:
                    return "invalid_key", None
                key_proven = True

        client = SonoffClient(
            async_get_clientsession(self.hass), host, deviceid, devicekey, port
        )
        try:
            await client.request(COMMAND_GET_STATE if devicekey else COMMAND_INFO)
        except SonoffEncryptionRequired:
            return "key_required", None
        except SonoffKeyRejected:
            if not key_proven:
                return "invalid_key", None
        except SonoffDecryptError:
            return "invalid_key", None
        except SonoffConnectionError:
            return "cannot_connect", None
        except SonoffDeviceError as err:
            if err.code == ERROR_NOT_FOUND:
                return "wrong_device_id", None
            # Rejecting this one command still shows the device is there.
            _LOGGER.debug("Probe of %s rejected: %s", deviceid, err)
        except SonoffError:
            _LOGGER.debug("Unusable reply probing %s", deviceid, exc_info=True)
        return None, devicekey

    def _create_entry(self, devicekey: str | None) -> ConfigFlowResult:
        assert self._host is not None
        assert self._deviceid is not None
        return self.async_create_entry(
            title=f"Sonoff {self._deviceid}",
            data={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_DEVICE_ID: self._deviceid,
                CONF_DEVICE_KEY: devicekey,
                CONF_LOCAL_TYPE: self._local_type,
            },
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a discovered device."""
        deviceid = parse_device_id(discovery_info.name)
        if deviceid is None:
            return self.async_abort(reason="not_supported")

        frame = parse_txt(discovery_info.properties)
        port = discovery_info.port or DEFAULT_PORT
        entry = await self.async_set_unique_id(deviceid)
        if entry is not None and not _frame_matches_entry(entry, frame):
            return self.async_abort(reason="already_configured")
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: discovery_info.host, CONF_PORT: port}
        )

        self._host = discovery_info.host
        self._port = port
        self._deviceid = deviceid
        self._frame = frame
        self._local_type = frame.localtype
        self.context["title_placeholders"] = {"name": deviceid}

        if frame.encrypted:
            return await self.async_step_device_key()
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered device that needs no key."""
        assert self._host is not None
        assert self._deviceid is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            error, key = await self._async_probe(
                self._host, self._port, self._deviceid, None
            )
            if error is None:
                return self._create_entry(key)
            errors["base"] = error

        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"device_id": self._deviceid},
            errors=errors,
        )

    async def async_step_device_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the key of a discovered encrypted device."""
        assert self._host is not None
        assert self._deviceid is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            error, key = await self._async_probe(
                self._host, self._port, self._deviceid, _clean_key(user_input)
            )
            if error is None:
                return self._create_entry(key)
            errors["base"] = error

        return self.async_show_form(
            step_id="device_key",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE_KEY): _KEY_SELECTOR}),
            description_placeholders={"device_id": self._deviceid},
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a device by address.

        This is also the only way in for units that never send an address record:
        Home Assistant's discovery ignores those (SonoffLAN #1871).
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            deviceid = user_input[CONF_DEVICE_ID].strip().lower()
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            if not DEVICE_ID_RE.match(deviceid):
                errors[CONF_DEVICE_ID] = "invalid_device_id"
            else:
                await self.async_set_unique_id(deviceid)
                self._abort_if_unique_id_configured()
                error, key = await self._async_probe(
                    host, port, deviceid, _clean_key(user_input)
                )
                if error is None:
                    self._host = host
                    self._port = port
                    self._deviceid = deviceid
                    return self._create_entry(key)
                errors["base"] = error

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): _PORT_FIELD,
                vol.Required(CONF_DEVICE_ID): str,
                vol.Optional(CONF_DEVICE_KEY): _KEY_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start again with a new key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new key, or none if the device now runs without one."""
        entry = self._get_reauth_entry()
        deviceid = entry.data[CONF_DEVICE_ID]
        errors: dict[str, str] = {}
        if user_input is not None:
            error, key = await self._async_probe(
                entry.data[CONF_HOST],
                entry.data.get(CONF_PORT, DEFAULT_PORT),
                deviceid,
                _clean_key(user_input),
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_DEVICE_KEY: key}
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_DEVICE_KEY): _KEY_SELECTOR}),
            description_placeholders={"device_id": deviceid},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address or key of an existing device."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            error, key = await self._async_probe(
                host, port, entry.data[CONF_DEVICE_ID], _clean_key(user_input)
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_DEVICE_KEY: key,
                    },
                )
            errors["base"] = error

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT): _PORT_FIELD,
                vol.Optional(CONF_DEVICE_KEY): _KEY_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or dict(entry.data)
            ),
            description_placeholders={"device_id": entry.data[CONF_DEVICE_ID]},
            errors=errors,
        )


class SonoffOptionsFlow(OptionsFlowWithReload):
    """Polling and capture settings. Saving reloads the device."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
                    CONF_CAPTURE_PAYLOADS: user_input[CONF_CAPTURE_PAYLOADS],
                }
            )

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_POLL_INTERVAL,
                        max=MAX_POLL_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_CAPTURE_PAYLOADS,
                    default=options.get(CONF_CAPTURE_PAYLOADS, False),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
