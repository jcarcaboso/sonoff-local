"""Tests for the config flow, against the fake device."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_USER,
    SOURCE_ZEROCONF,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import sonoff_local
from custom_components.sonoff_local.const import (
    CONF_CAPTURE_PAYLOADS,
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_LOCAL_TYPE,
    CONF_POLL_INTERVAL,
    DOMAIN,
)

from .conftest import entry_for
from .fake_device import DEVICE_ID, DEVICE_KEY, SERVICE_NAME, FakeS60TPF

pytestmark = pytest.mark.usefixtures("mock_mdns")


def discovery(fake: FakeS60TPF, name: str = SERVICE_NAME) -> ZeroconfServiceInfo:
    """Return the discovery info for a fake device."""
    return ZeroconfServiceInfo(
        ip_address=ipaddress.ip_address(fake.host),
        ip_addresses=[ipaddress.ip_address(fake.host)],
        port=fake.port,
        hostname=f"eWeLink_{fake.deviceid}.local.",
        type="_ewelink._tcp.local.",
        name=name,
        properties=fake.txt(),
    )


async def start_zeroconf(hass: HomeAssistant, fake: FakeS60TPF, **kwargs: Any) -> Any:
    """Start a discovery flow."""
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=discovery(fake, **kwargs),
    )


def manual_input(fake: FakeS60TPF, **overrides: Any) -> dict[str, Any]:
    """Return a filled-in manual form."""
    return {
        CONF_HOST: fake.host,
        CONF_PORT: fake.port,
        CONF_DEVICE_ID: fake.deviceid,
        **overrides,
    }


async def start_manual(hass: HomeAssistant) -> Any:
    """Open the manual form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_discovery_of_encrypted_device_asks_for_key(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """The primary S60TPF path: the TXT record selects the encrypted flow."""
    result = await start_zeroconf(hass, device)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "device_key"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_KEY: f"  {DEVICE_KEY}\n"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Sonoff {DEVICE_ID}"
    assert result["data"] == {
        CONF_HOST: device.host,
        CONF_PORT: device.port,
        CONF_DEVICE_ID: DEVICE_ID,
        CONF_DEVICE_KEY: DEVICE_KEY,
        CONF_LOCAL_TYPE: "plug",
    }
    assert result["result"].unique_id == DEVICE_ID


async def test_setup_after_flow_respects_spacing(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """The probe and the first poll come from different clients."""
    result = await start_zeroconf(hass, device)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_KEY: DEVICE_KEY}
    )
    await hass.async_block_till_done()
    first, second = device.requests[:2]
    assert second.monotonic - first.monotonic >= 0.19


async def test_discovery_rejects_wrong_key_from_txt(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A wrong key is caught from the TXT record, without sending anything."""
    result = await start_zeroconf(hass, device)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_KEY: "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_key"}
    assert device.requests == []

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_KEY: DEVICE_KEY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_discovery_of_plain_device_needs_only_confirmation(
    hass: HomeAssistant, diy_device: FakeS60TPF
) -> None:
    """DIY mode: the TXT record selects the no-key flow."""
    result = await start_zeroconf(hass, diy_device)
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_KEY] is None
    assert diy_device.commands()[0] == "info"


async def test_confirm_reports_unreachable_device(
    hass: HomeAssistant, diy_device: FakeS60TPF
) -> None:
    """The device can vanish between discovery and confirmation."""
    result = await start_zeroconf(hass, diy_device)
    await diy_device.stop()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_discovery_ignores_other_ewelink_products(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """Other products share the service type."""
    result = await start_zeroconf(
        hass, device, name="ihost-1001000000._ewelink._tcp.local."
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_discovery_updates_address_of_configured_device(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A proven frame moves an existing entry."""
    entry = entry_for(device)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_HOST: "192.0.2.1"}
    )

    result = await start_zeroconf(hass, device)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == device.host


async def test_discovery_cannot_move_encrypted_entry_without_proof(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A spoofed announcement must not redirect a device that has a key."""
    entry = entry_for(device)
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_HOST: "192.0.2.1"}
    )

    device.devicekey = "attackers-key"
    result = await start_zeroconf(hass, device)
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.0.2.1"

    device.devicekey = None
    result = await start_zeroconf(hass, device)
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.0.2.1"


async def test_manual_encrypted(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """Manual entry works with no mDNS at all, as units without A records need."""
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        manual_input(device, **{CONF_DEVICE_KEY: DEVICE_KEY}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_KEY] == DEVICE_KEY
    assert device.commands()[0] == "getState"


async def test_manual_plain(hass: HomeAssistant, diy_device: FakeS60TPF) -> None:
    """A DIY device needs no key."""
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], manual_input(diy_device)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_KEY] is None


async def test_manual_without_key_on_encrypted_device(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """The form explains that a key is needed, then accepts one."""
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], manual_input(device)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "key_required"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], manual_input(device, **{CONF_DEVICE_KEY: DEVICE_KEY})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_manual_key_is_rejected_when_reply_unreadable(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """With no mDNS, an unreadable reply is the evidence of a wrong key."""
    device.reply_key = "other"
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], manual_input(device, **{CONF_DEVICE_KEY: DEVICE_KEY})
    )
    assert result["errors"] == {"base": "invalid_key"}


async def test_manual_wrong_key_is_rejected_when_device_cannot_read_it(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A device answering 400 with encrypt set could not read the request."""
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], manual_input(device, **{CONF_DEVICE_KEY: "wrong"})
    )
    assert result["errors"] == {"base": "invalid_key"}


async def test_txt_proof_overrides_a_rejected_probe(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A key that decrypts the TXT record is right, whatever one command replies."""
    result = await start_zeroconf(hass, device)
    device.rejects_everything = True
    with patch("custom_components.sonoff_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DEVICE_KEY: DEVICE_KEY}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_manual_uses_cached_txt_to_detect_encryption(
    hass: HomeAssistant, device: FakeS60TPF, mock_mdns: MagicMock
) -> None:
    """The mDNS cache proves a key without sending any request."""
    mock_mdns.info.return_value.properties = device.txt()
    mock_mdns.info.return_value.parsed_addresses.return_value = []
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], manual_input(device)
    )
    assert result["errors"] == {"base": "key_required"}
    assert device.requests == []

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], manual_input(device, **{CONF_DEVICE_KEY: "wrong"})
    )
    assert result["errors"] == {"base": "invalid_key"}
    assert device.requests == []


async def test_manual_drops_unneeded_key(
    hass: HomeAssistant, diy_device: FakeS60TPF, mock_mdns: MagicMock
) -> None:
    """A key for a device that announces plaintext is not stored."""
    mock_mdns.info.return_value.properties = diy_device.txt()
    mock_mdns.info.return_value.parsed_addresses.return_value = []
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        manual_input(diy_device, **{CONF_DEVICE_KEY: "not-needed"}),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_KEY] is None


async def test_manual_cannot_connect(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """An unreachable address is reported."""
    data = manual_input(device, **{CONF_DEVICE_KEY: DEVICE_KEY})
    await device.stop()
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_manual_wrong_device_id(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """A 404 means another device answered."""
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **manual_input(device, **{CONF_DEVICE_KEY: DEVICE_KEY}),
            CONF_DEVICE_ID: "ffffffffff",
        },
    )
    assert result["errors"] == {"base": "wrong_device_id"}


@pytest.mark.parametrize("value", ["short", "toolong12345", "has space!"])
async def test_manual_malformed_device_id(
    hass: HomeAssistant, device: FakeS60TPF, value: str
) -> None:
    """The ID format is checked before any network traffic."""
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**manual_input(device), CONF_DEVICE_ID: value}
    )
    assert result["errors"] == {CONF_DEVICE_ID: "invalid_device_id"}
    assert device.requests == []


async def test_manual_rejected_command_still_counts_as_reachable(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A device that rejects getState is there (SonoffLAN #1442)."""
    device.unsupported["getState"] = 400
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], manual_input(device, **{CONF_DEVICE_KEY: DEVICE_KEY})
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_manual_device_id_is_normalised(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """IDs are case-insensitive and stored in lower case."""
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **manual_input(device, **{CONF_DEVICE_KEY: DEVICE_KEY}),
            CONF_DEVICE_ID: f" {DEVICE_ID.upper()} ",
        },
    )
    assert result["data"][CONF_DEVICE_ID] == DEVICE_ID


async def test_manual_duplicate(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """A device can only be added once."""
    entry_for(device).add_to_hass(hass)
    result = await start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], manual_input(device, **{CONF_DEVICE_KEY: DEVICE_KEY})
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up an entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_reauth(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """A new key replaces the old one and the device reloads."""
    entry = entry_for(device)
    await setup_entry(hass, entry)
    old_key = device.devicekey

    device.devicekey = new_key = "a-new-key"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_DEVICE_KEY: old_key}
    )
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_KEY: new_key}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_DEVICE_KEY] == new_key


async def test_reauth_keeps_form_on_bad_key(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A bad key leaves the entry alone."""
    entry = entry_for(device)
    await setup_entry(hass, entry)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_KEY: "still-wrong"}
    )
    assert result["errors"] == {"base": "invalid_key"}
    assert entry.data[CONF_DEVICE_KEY] == DEVICE_KEY


async def test_reconfigure(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """The address can be changed by hand, as DHCP moves units with no A record."""
    entry = entry_for(device)
    await setup_entry(hass, entry)

    moved = FakeS60TPF()
    await moved.start()
    try:
        result = await entry.start_reconfigure_flow(hass)
        assert result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: moved.host,
                CONF_PORT: moved.port,
                CONF_DEVICE_KEY: DEVICE_KEY,
            },
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_PORT] == moved.port
        await hass.async_block_till_done()
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await moved.stop()


async def test_reconfigure_failure_keeps_data(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A failed change leaves the entry untouched."""
    entry = entry_for(device)
    await setup_entry(hass, entry)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: 9, CONF_DEVICE_KEY: DEVICE_KEY},
    )
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_PORT] == device.port


async def test_options(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """Saving options stores them."""
    entry = entry_for(device)
    await setup_entry(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_POLL_INTERVAL: 30, CONF_CAPTURE_PAYLOADS: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_POLL_INTERVAL: 30, CONF_CAPTURE_PAYLOADS: True}


def test_translations_match_strings() -> None:
    """The two translation files must not drift apart."""
    base = Path(sonoff_local.__file__).parent
    strings = json.loads((base / "strings.json").read_text())
    english = json.loads((base / "translations" / "en.json").read_text())
    assert strings == english


def test_every_error_has_a_translation() -> None:
    """Each error the flow can raise needs text."""
    base = Path(sonoff_local.__file__).parent
    errors = json.loads((base / "strings.json").read_text())["config"]["error"]
    assert set(errors) == {
        "cannot_connect",
        "invalid_device_id",
        "invalid_key",
        "key_required",
        "wrong_device_id",
    }
