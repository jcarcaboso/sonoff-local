"""Tests for setup, entities, push, polling and availability."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_HOST, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sonoff_local import coordinator as coordinator_module
from custom_components.sonoff_local.const import (
    CONF_DEVICE_KEY,
    CONF_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.sonoff_local.coordinator import AUTH_FAILURE_LIMIT
from custom_components.sonoff_local.mdns import MdnsEvent
from custom_components.sonoff_local.protocol import parse_txt

from .conftest import entry_for
from .fake_device import DEVICE_ID, FakeS60TPF

pytestmark = pytest.mark.usefixtures("mock_mdns")

SWITCH = f"switch.sonoff_{DEVICE_ID}"
POWER = f"sensor.sonoff_{DEVICE_ID}_power"
VOLTAGE = f"sensor.sonoff_{DEVICE_ID}_voltage"
CURRENT = f"sensor.sonoff_{DEVICE_ID}_current"
ENERGY_DAY = f"sensor.sonoff_{DEVICE_ID}_energy_today"
ENERGY_MONTH = f"sensor.sonoff_{DEVICE_ID}_energy_this_month"
ENERGY_WEEK = f"sensor.sonoff_{DEVICE_ID}_energy_this_week"


async def setup(
    hass: HomeAssistant, fake: FakeS60TPF, **options: Any
) -> MockConfigEntry:
    """Set up an entry for a fake device."""
    entry = entry_for(fake, **options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def push(entry: MockConfigEntry, event: MdnsEvent) -> None:
    """Deliver an mDNS event."""
    entry.runtime_data.async_handle_mdns(event)


def push_txt(
    entry: MockConfigEntry,
    fake: FakeS60TPF,
    params: dict[str, Any] | None = None,
    **kwargs: Any,
) -> None:
    """Deliver a TXT frame from a fake device."""
    push(entry, MdnsEvent(frame=parse_txt(fake.txt(params)), **kwargs))


async def tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int = 11
) -> None:
    """Advance time past a poll."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def state_of(hass: HomeAssistant, entity_id: str) -> str:
    """Return a state, failing clearly if the entity is missing."""
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} does not exist"
    return state.state


async def test_setup_creates_s60_entities(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """Values match the table in the README, from the issue #1514 payload."""
    entry = await setup(hass, device)
    assert entry.state is ConfigEntryState.LOADED

    assert state_of(hass, SWITCH) == STATE_ON
    assert float(state_of(hass, POWER)) == 7.24
    assert float(state_of(hass, CURRENT)) == 0.05
    assert float(state_of(hass, VOLTAGE)) == 237.17
    assert float(state_of(hass, ENERGY_DAY)) == 0.02
    assert float(state_of(hass, ENERGY_MONTH)) == 1.17


async def test_sensor_metadata(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """Units and classes suit the Energy dashboard."""
    await setup(hass, device)
    power = hass.states.get(POWER)
    energy = hass.states.get(ENERGY_DAY)
    assert power is not None
    assert energy is not None
    assert power.attributes["unit_of_measurement"] == "W"
    assert power.attributes["device_class"] == "power"
    assert power.attributes["state_class"] == "measurement"
    assert energy.attributes["unit_of_measurement"] == "kWh"
    assert energy.attributes["device_class"] == "energy"
    assert energy.attributes["state_class"] == "total_increasing"


async def test_no_week_sensor_for_s60(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """UIID 190 has no weekly counter, so none is invented."""
    await setup(hass, device)
    assert hass.states.get(ENERGY_WEEK) is None


async def test_rssi_is_disabled_by_default(
    hass: HomeAssistant, device: FakeS60TPF, entity_registry: er.EntityRegistry
) -> None:
    """Signal strength is a diagnostic."""
    await setup(hass, device)
    entity = entity_registry.async_get_entity_id("sensor", DOMAIN, f"{DEVICE_ID}_rssi")
    assert entity is not None
    registered = entity_registry.async_get(entity)
    assert registered is not None
    assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_device_info(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """The device shows the firmware reported over the LAN."""

    entry = await setup(hass, device)
    registered = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, DEVICE_ID), entry.entry_id
    )
    assert registered is not None
    assert registered.manufacturer == "SONOFF"
    assert registered.sw_version == "1.2.0"
    assert registered.model == "plug"


async def test_switch_commands(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """Service calls reach the relay with the UIID 190 command."""
    await setup(hass, device)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    assert device.is_on() is False
    assert device.requests[-1].command == "switches"
    assert state_of(hass, SWITCH) == "off"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )
    assert device.is_on() is True
    assert state_of(hass, SWITCH) == "on"


async def test_switch_failure_is_reported(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A failed command raises to the caller and keeps the old state."""
    await setup(hass, device)
    device.unsupported["switches"] = 422
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
        )
    assert state_of(hass, SWITCH) == "on"


async def test_txt_push_updates_state(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """An mDNS push updates entities immediately."""
    entry = await setup(hass, device)
    push_txt(entry, device, {"switches": [{"outlet": 0, "switch": "off"}]})
    await hass.async_block_till_done()
    assert state_of(hass, SWITCH) == "off"
    # A partial frame must not erase other readings.
    assert float(state_of(hass, POWER)) == 7.24


async def test_relay_only_pushes_do_not_stop_metric_polls(
    hass: HomeAssistant, device: FakeS60TPF, freezer: FrozenDateTimeFactory
) -> None:
    """The key S60TPF case: a frame with only relay state must not suppress polling."""
    entry = await setup(hass, device)
    device.set_power(1000)
    for state in ("off", "on", "off"):
        freezer.tick(timedelta(seconds=4))
        push_txt(entry, device, {"switches": [{"outlet": 0, "switch": state}]})
        await tick(hass, freezer, 0)
    await tick(hass, freezer)
    assert float(state_of(hass, POWER)) == 10.0


async def test_metric_pushes_skip_redundant_polls(
    hass: HomeAssistant, device: FakeS60TPF, freezer: FrozenDateTimeFactory
) -> None:
    """Fresh readings by push avoid needless traffic to the device."""
    entry = await setup(hass, device)
    device.requests.clear()
    # Home Assistant schedules from a floored clock, so a 10 s interval can fire from
    # about 9 s. Pushing at 8 s stays clearly ahead of it.
    freezer.tick(timedelta(seconds=8))
    push_txt(entry, device, {"power": 2000})
    await tick(hass, freezer, 3)
    assert entry.runtime_data.stats["polls_skipped"] == 1
    assert device.requests == []
    assert float(state_of(hass, POWER)) == 20.0


async def test_new_sensor_appears_when_key_first_seen(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """Sensors follow what the device sends, not an assumed model list."""
    device.state = {
        "switches": [{"outlet": 0, "switch": "on"}],
        "fwVersion": "1.2.0",
    }
    entry = await setup(hass, device)
    assert hass.states.get(POWER) is None
    assert state_of(hass, SWITCH) == STATE_ON

    push_txt(entry, device, {"power": 1234})
    await hass.async_block_till_done()
    assert float(state_of(hass, POWER)) == 12.34

    push_txt(entry, device, {"weekKwh": 10})
    await hass.async_block_till_done()
    assert float(state_of(hass, ENERGY_WEEK)) == 0.1


async def test_entities_are_not_duplicated(
    hass: HomeAssistant, device: FakeS60TPF, entity_registry: er.EntityRegistry
) -> None:
    """Further updates must not add the same sensor again."""
    entry = await setup(hass, device)
    before = len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    push_txt(entry, device, {"power": 1})
    push_txt(entry, device, {"power": 2})
    await hass.async_block_till_done()
    after = len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    assert before == after


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (1051, 1050, 10.51),  # Small dip: ignored.
        (1051, 1041, 10.51),  # Dip of exactly 0.1: ignored.
        (1051, 1040, 10.40),  # Larger drop: a real reset.
        (1051, 3, 0.03),  # The midnight reset.
        (1051, 1052, 10.52),  # Normal growth.
    ],
)
async def test_energy_dips_are_filtered(
    hass: HomeAssistant,
    device: FakeS60TPF,
    first: int,
    second: int,
    expected: float,
) -> None:
    """Same cases as SonoffLAN's test_powr3, so a jitter is not a phantom reset."""
    entry = await setup(hass, device)
    push_txt(entry, device, {"dayKwh": first})
    await hass.async_block_till_done()
    push_txt(entry, device, {"dayKwh": second})
    await hass.async_block_till_done()
    assert float(state_of(hass, ENERGY_DAY)) == expected


async def test_unusable_reading_keeps_last_value(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A malformed value must not blank the sensor."""
    entry = await setup(hass, device)
    push_txt(entry, device, {"power": "n/a"})
    await hass.async_block_till_done()
    assert float(state_of(hass, POWER)) == 7.24


async def test_voltage_zero_is_reported(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """Zero is a real reading on these plugs (#1518), not a missing one."""
    entry = await setup(hass, device)
    push_txt(entry, device, {"voltage": 0})
    await hass.async_block_till_done()
    assert float(state_of(hass, VOLTAGE)) == 0.0


async def test_setup_retries_when_unreachable(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """An offline device delays setup rather than failing it."""
    entry = entry_for(device)
    entry.add_to_hass(hass)
    await device.stop()
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_goes_unavailable_only_after_grace(
    hass: HomeAssistant, device: FakeS60TPF, freezer: FrozenDateTimeFactory
) -> None:
    """One missed poll is tolerated, a silent device becomes unavailable."""
    await setup(hass, device)
    await device.stop()

    await tick(hass, freezer)
    assert state_of(hass, SWITCH) == STATE_ON
    await tick(hass, freezer)
    assert state_of(hass, SWITCH) == STATE_ON
    await tick(hass, freezer)
    assert state_of(hass, SWITCH) == STATE_UNAVAILABLE
    assert state_of(hass, POWER) == STATE_UNAVAILABLE


async def test_recovers_after_outage(
    hass: HomeAssistant, device: FakeS60TPF, freezer: FrozenDateTimeFactory
) -> None:
    """A TXT frame brings an unavailable device straight back."""
    entry = await setup(hass, device)
    await device.stop()
    for _ in range(3):
        await tick(hass, freezer)
    assert state_of(hass, SWITCH) == STATE_UNAVAILABLE

    push_txt(entry, device)
    await hass.async_block_till_done()
    assert state_of(hass, SWITCH) == STATE_ON


async def test_mdns_only_device_stays_available(
    hass: HomeAssistant, device: FakeS60TPF, freezer: FrozenDateTimeFactory
) -> None:
    """If HTTP is blocked but TXT flows, the device is still alive."""
    entry = await setup(hass, device)
    await device.stop()
    for raw_state in ("off", "on", "off", "on", "off"):
        push_txt(entry, device, {"switches": [{"outlet": 0, "switch": raw_state}]})
        await tick(hass, freezer)
    assert state_of(hass, SWITCH) != STATE_UNAVAILABLE


async def test_mdns_removal_is_verified(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """An expired record alone must not hide a device that still answers."""
    entry = await setup(hass, device)
    push(entry, MdnsEvent(removed=True))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state_of(hass, SWITCH) == STATE_ON


async def test_mdns_removal_of_dead_device_is_immediate(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A removal plus a failed check goes unavailable without the grace period."""
    entry = await setup(hass, device)
    await device.stop()
    push(entry, MdnsEvent(removed=True))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state_of(hass, SWITCH) == STATE_UNAVAILABLE


async def test_stale_txt_is_ignored(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """An older frame must not undo a newer state."""
    entry = await setup(hass, device)
    device.seq = 500
    newer = parse_txt(device.txt({"power": 2000}))
    device.seq = 490
    older = parse_txt(device.txt({"power": 1000}))

    push(entry, MdnsEvent(frame=newer))
    push(entry, MdnsEvent(frame=older))
    await hass.async_block_till_done()
    assert float(state_of(hass, POWER)) == 20.0


async def test_reboot_seq_reset_is_accepted(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A seq far below the last one means a reboot, so the frame counts."""
    entry = await setup(hass, device)
    device.seq = 5000
    newer = parse_txt(device.txt({"power": 2000}))
    device.seq = 1
    rebooted = parse_txt(device.txt({"power": 1000}))

    push(entry, MdnsEvent(frame=newer))
    push(entry, MdnsEvent(frame=rebooted))
    await hass.async_block_till_done()
    assert float(state_of(hass, POWER)) == 10.0


async def test_foreign_frame_is_ignored(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A frame for another device must not update this one."""
    entry = await setup(hass, device)
    other = FakeS60TPF(deviceid="ffffffffff")
    push_txt(entry, other, {"power": 99999})
    await hass.async_block_till_done()
    assert float(state_of(hass, POWER)) == 7.24
    assert entry.runtime_data.stats["txt_rejected"] == 1


async def test_malformed_plain_frame_is_ignored(
    hass: HomeAssistant, diy_device: FakeS60TPF
) -> None:
    """A broken frame changes nothing."""
    entry = await setup(hass, diy_device)
    push(entry, MdnsEvent(frame=parse_txt({"data1": "{broken", "id": DEVICE_ID})))
    await hass.async_block_till_done()
    assert entry.runtime_data.stats["txt_rejected"] == 1
    assert state_of(hass, SWITCH) == STATE_ON


async def test_address_follows_decoded_frame(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """DHCP changes are followed without losing the entry."""
    entry = await setup(hass, device)
    push_txt(entry, device, host="192.0.2.50", port=8081)
    await hass.async_block_till_done()
    assert entry.data[CONF_HOST] == "192.0.2.50"
    assert entry.runtime_data.client.host == "192.0.2.50"


async def test_addressless_frame_keeps_configured_host(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """Units with no A record still deliver state (SonoffLAN #1871)."""
    entry = await setup(hass, device)
    push_txt(entry, device, {"power": 5000}, host=None, port=8081)
    await hass.async_block_till_done()
    assert entry.data[CONF_HOST] == device.host
    assert float(state_of(hass, POWER)) == 50.0


async def test_undecodable_frame_cannot_move_address(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A spoofed announcement must not redirect commands."""
    entry = await setup(hass, device)
    device.devicekey = "attacker"
    push_txt(entry, device, {"power": 1}, host="192.0.2.66", port=8081)
    await hass.async_block_till_done()
    assert entry.data[CONF_HOST] == "127.0.0.1"
    assert float(state_of(hass, POWER)) == 7.24


async def test_plaintext_frame_cannot_downgrade_encrypted_entry(
    hass: HomeAssistant, device: FakeS60TPF, caplog: pytest.LogCaptureFixture
) -> None:
    """Anyone on the LAN can send plaintext, so it is never trusted with a key set."""
    entry = await setup(hass, device)
    key, device.devicekey = device.devicekey, None
    push_txt(entry, device, {"power": 99999}, host="192.0.2.66")
    push_txt(entry, device, {"power": 99998}, host="192.0.2.66")
    device.devicekey = key
    await hass.async_block_till_done()

    assert float(state_of(hass, POWER)) == 7.24
    assert entry.data[CONF_HOST] == "127.0.0.1"
    assert caplog.text.count("announces plaintext") == 1


async def test_wrong_key_in_frames_starts_reauth(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """Repeated undecodable frames mean the key changed."""
    entry = await setup(hass, device)
    device.devicekey = "re-paired"
    for _ in range(AUTH_FAILURE_LIMIT):
        push_txt(entry, device, {"power": 1})
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_single_bad_frame_does_not_start_reauth(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """One glitch must not ask the user for a working key."""
    entry = await setup(hass, device)
    key, device.devicekey = device.devicekey, "other"
    push_txt(entry, device, {"power": 1})
    device.devicekey = key
    push_txt(entry, device, {"power": 2000})
    await hass.async_block_till_done()
    assert hass.config_entries.flow.async_progress() == []
    assert float(state_of(hass, POWER)) == 20.0


async def test_rejected_poll_with_proven_key_does_not_start_reauth(
    hass: HomeAssistant, device: FakeS60TPF, freezer: FrozenDateTimeFactory
) -> None:
    """Commands being refused says nothing about a key the TXT records confirm."""
    entry = await setup(hass, device)
    push_txt(entry, device, {"power": 2000})
    device.rejects_everything = True
    for _ in range(AUTH_FAILURE_LIMIT + 1):
        await tick(hass, freezer)
    assert hass.config_entries.flow.async_progress() == []
    assert entry.state is ConfigEntryState.LOADED


async def test_rejected_poll_without_proven_key_starts_reauth(
    hass: HomeAssistant, device: FakeS60TPF, freezer: FrozenDateTimeFactory
) -> None:
    """With TXT unavailable, as across VLANs, the replies are the only evidence."""
    await setup(hass, device)
    device.devicekey = "re-paired"
    for _ in range(AUTH_FAILURE_LIMIT):
        await tick(hass, freezer)
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_single_unreadable_reply_does_not_start_reauth(
    hass: HomeAssistant, device: FakeS60TPF, freezer: FrozenDateTimeFactory
) -> None:
    """One corrupted reply must not take a working device offline."""
    entry = await setup(hass, device)
    device.reply_key = "other"
    await tick(hass, freezer)
    device.reply_key = None
    await tick(hass, freezer)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.config_entries.flow.async_progress() == []
    assert state_of(hass, SWITCH) == STATE_ON


async def test_repeated_unreadable_replies_start_reauth(
    hass: HomeAssistant, device: FakeS60TPF, freezer: FrozenDateTimeFactory
) -> None:
    """Persistent failure means the key really changed."""
    await setup(hass, device)
    device.reply_key = "other"
    for _ in range(AUTH_FAILURE_LIMIT):
        await tick(hass, freezer)
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_unreadable_reply_on_setup_starts_reauth(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """With nothing known, an unreadable reply goes straight to the user."""
    device.reply_key = "other"
    entry = entry_for(device)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_plain_entry_on_encrypted_device_starts_reauth(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A device re-paired after DIY mode needs a key now."""
    data = {**entry_for(device).data, CONF_DEVICE_KEY: None}
    plain = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_ID, data=data)
    plain.add_to_hass(hass)
    await hass.config_entries.async_setup(plain.entry_id)
    await hass.async_block_till_done()
    assert plain.state is ConfigEntryState.SETUP_ERROR


async def test_poll_interval_option(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """The option controls the poll and the grace period."""
    entry = await setup(hass, device, **{CONF_POLL_INTERVAL: 60})
    coordinator = entry.runtime_data
    assert coordinator.update_interval == timedelta(seconds=60)
    assert coordinator.stale_after == 180


async def test_unload(hass: HomeAssistant, device: FakeS60TPF) -> None:
    """Unloading removes the mDNS subscription."""
    entry = await setup(hass, device)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_poll_does_not_write_led_when_get_state_works(
    hass: HomeAssistant, device: FakeS60TPF, freezer: FrozenDateTimeFactory
) -> None:
    """End to end: routine polling makes no writes if the device allows it."""
    await setup(hass, device)
    for _ in range(3):
        await tick(hass, freezer)
    assert device.led_writes == 0
    assert set(device.commands()) == {"getState"}


async def test_poll_uses_led_echo_when_needed(
    hass: HomeAssistant, device: FakeS60TPF, freezer: FrozenDateTimeFactory
) -> None:
    """End to end: the upstream method works when getState gives no readings."""
    device.stateless_commands.add("getState")
    entry = await setup(hass, device)
    # Two requests in one poll wait out the spacing, which a frozen clock never does.
    entry.runtime_data.client._min_interval = 0
    push_txt(entry, device, {"sledOnline": "on", "switches": device.state["switches"]})
    await tick(hass, freezer)
    assert float(state_of(hass, POWER)) == 7.24
    assert device.led_writes >= 1
    assert device.state["sledOnline"] == "on"


async def test_entity_without_state_is_unknown(
    hass: HomeAssistant, device: FakeS60TPF
) -> None:
    """A device that has not reported its relay shows unknown, not off."""
    device.empty_replies = True
    await setup(hass, device)
    assert state_of(hass, SWITCH) == STATE_UNKNOWN


def test_constants_are_sane() -> None:
    """Guard the limits the tests rely on."""
    assert coordinator_module.STALE_POLLS >= 2
    assert AUTH_FAILURE_LIMIT >= 2
