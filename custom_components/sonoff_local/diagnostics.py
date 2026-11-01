"""Diagnostics download.

Turn on "Capture payloads" in the integration's options, use the device for a while,
then download diagnostics. The file holds redacted TXT and HTTP frames, which is what
is needed to correct the parsing for a real device. Keys, Wi-Fi details and addresses
are removed when a frame is captured, so they never enter the buffer.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_ID, CONF_DEVICE_KEY
from .coordinator import SonoffConfigEntry

_TO_REDACT = {CONF_DEVICE_ID, CONF_DEVICE_KEY, CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SonoffConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a device."""
    return {
        "config": async_redact_data(dict(entry.data), _TO_REDACT),
        "options": dict(entry.options),
        "device": entry.runtime_data.diagnostics(),
    }
