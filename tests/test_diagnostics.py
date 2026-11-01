"""Tests for diagnostics and payload capture."""

from __future__ import annotations

import json
import logging

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sonoff_local.const import (
    CONF_CAPTURE_PAYLOADS,
)
from custom_components.sonoff_local.coordinator import CAPTURE_SIZE
from custom_components.sonoff_local.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.sonoff_local.mdns import MdnsEvent
from custom_components.sonoff_local.protocol import SonoffConnectionError, parse_txt

from .conftest import entry_for
from .fake_device import DEVICE_ID, DEVICE_KEY, FakeS60TPF

pytestmark = pytest.mark.usefixtures("mock_mdns")


async def setup(
    hass: HomeAssistant, device: FakeS60TPF, **opts: bool
) -> MockConfigEntry:
    """Set up an entry."""
    entry = entry_for(device, **opts)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def exercise(
    hass: HomeAssistant, entry: MockConfigEntry, device: FakeS60TPF
) -> None:
    """Generate every kind of frame."""
    entry.runtime_data.async_handle_mdns(MdnsEvent(frame=parse_txt(device.txt())))
    await entry.runtime_data.async_set_switch(False)
    entry.runtime_data.async_handle_mdns(MdnsEvent(removed=True))
    await hass.async_block_till_done(wait_background_tasks=True)


def assert_no_secrets(text: str, device: FakeS60TPF) -> None:
    """Fail if anything sensitive appears."""
    for secret in (
        DEVICE_KEY,
        DEVICE_ID,
        device.host,
        str(device.state["ssid"]),
        str(device.state["bssid"]),
        str(device.state["staMac"]),
    ):
        assert secret not in text, f"leaked {secret}"


async def test_capture_contains_useful_frames(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """Captured frames carry the raw readings needed to fix parsing."""
    entry = await setup(hass, device, **{CONF_CAPTURE_PAYLOADS: True})
    await exercise(hass, entry, device)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    frames = diagnostics["device"]["frames"]
    kinds = {frame["kind"] for frame in frames}
    assert {"tx", "rx", "rx_decrypted", "txt", "txt_decoded", "txt_removed"} <= kinds

    decoded = next(frame for frame in frames if frame["kind"] == "rx_decrypted")
    assert decoded["params"]["voltage"] == 23717

    txt = next(frame for frame in frames if frame["kind"] == "txt")
    assert txt["txt"]["type"] == "plug"
    assert txt["txt"]["encrypt"] == "true"
    assert diagnostics["device"]["raw_metrics"]["voltage"] == 23717
    assert diagnostics["device"]["seconds_since_metrics"] is not None
    assert diagnostics["device"]["poll_command"] == "getState"


async def test_diagnostics_never_contain_secrets(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """The whole file is safe to attach to a public issue."""
    entry = await setup(hass, device, **{CONF_CAPTURE_PAYLOADS: True})
    await exercise(hass, entry, device)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert_no_secrets(json.dumps(diagnostics), device)


async def test_debug_log_never_contains_secrets(
    hass: HomeAssistant, device: FakeS60TPF, caplog: pytest.LogCaptureFixture
) -> None:
    """Enabling debug logging must not write secrets to the log."""
    caplog.set_level(logging.DEBUG, logger="custom_components.sonoff_local")
    entry = await setup(hass, device)
    await exercise(hass, entry, device)
    assert "Frame eeda" in caplog.text
    integration_log = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.sonoff_local")
    )
    assert DEVICE_KEY not in integration_log
    assert str(device.state["ssid"]) not in integration_log
    assert str(device.state["bssid"]) not in integration_log


async def test_failed_request_capture_hides_address(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """aiohttp's error text contains the address, so only the type is stored."""
    entry = await setup(hass, device, **{CONF_CAPTURE_PAYLOADS: True})
    await device.stop()
    with pytest.raises(SonoffConnectionError):
        await entry.runtime_data.client.request("getState")
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    errors = [f for f in diagnostics["device"]["frames"] if f["kind"] == "error"]
    assert errors
    assert errors[0]["error"] == "SonoffConnectionError"
    assert device.host not in json.dumps(errors)


async def test_capture_is_off_by_default(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """Nothing accumulates unless asked for."""
    entry = await setup(hass, device)
    await exercise(hass, entry, device)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["device"]["frames"] == []
    assert diagnostics["device"]["capture_enabled"] is False


async def test_capture_is_bounded(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """Capture cannot grow without limit."""
    entry = await setup(hass, device, **{CONF_CAPTURE_PAYLOADS: True})
    for number in range(CAPTURE_SIZE + 25):
        entry.runtime_data.record("tx", {"number": number})
    assert len(entry.runtime_data.frames) == CAPTURE_SIZE
