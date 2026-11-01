"""Tests for the HTTP client against the fake device."""

from __future__ import annotations

import ast
import asyncio
import itertools
import time
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from custom_components.sonoff_local import protocol
from custom_components.sonoff_local.protocol import (
    MIN_REQUEST_INTERVAL,
    SonoffClient,
    SonoffConnectionError,
    SonoffDecryptError,
    SonoffDeviceError,
    SonoffEncryptionRequired,
    SonoffKeyRejected,
    SonoffProtocolError,
)

from .fake_device import DEVICE_ID, DEVICE_KEY, FakeS60TPF

FAST = 0.0


def encrypted_client(
    session: aiohttp.ClientSession, device: FakeS60TPF, **kwargs: Any
) -> SonoffClient:
    """Return a client for an encrypted device."""
    kwargs.setdefault("min_interval", FAST)
    return SonoffClient(
        session, device.host, DEVICE_ID, DEVICE_KEY, device.port, **kwargs
    )


def plain_client(
    session: aiohttp.ClientSession, device: FakeS60TPF, **kwargs: Any
) -> SonoffClient:
    """Return a client without a key."""
    kwargs.setdefault("min_interval", FAST)
    return SonoffClient(session, device.host, DEVICE_ID, None, device.port, **kwargs)


def test_protocol_module_does_not_import_home_assistant() -> None:
    """The client must stay usable and testable outside Home Assistant."""
    tree = ast.parse(Path(protocol.__file__).read_text())
    imported = {
        name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
        )
    }
    assert "homeassistant" not in imported


async def test_encrypted_envelope(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """Encrypted requests carry the fields SonoffLAN sends."""
    await encrypted_client(session, device).request("getState")
    body = device.requests[0].body
    assert body["deviceid"] == DEVICE_ID
    assert body["selfApikey"] == "123"
    assert body["encrypt"] is True
    assert isinstance(body["iv"], str)
    assert isinstance(body["data"], str)
    assert body["sequence"].isdecimal()
    assert list(body) == [
        "sequence",
        "deviceid",
        "selfApikey",
        "data",
        "encrypt",
        "iv",
    ]


async def test_sequence_is_unique_and_increasing(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """Sequence numbers never repeat, even within a millisecond."""
    client = encrypted_client(session, device)
    values = [int(client._next_sequence()) for _ in range(50)]
    assert values == sorted(set(values))


async def test_plain_envelope(
    session: aiohttp.ClientSession, diy_device: FakeS60TPF
) -> None:
    """Plain requests send data as an object, with no encryption fields."""
    await plain_client(session, diy_device).request("info")
    body = diy_device.requests[0].body
    assert body["data"] == {}
    assert body["deviceid"] == DEVICE_ID
    assert "iv" not in body
    assert "encrypt" not in body


async def test_encrypted_response_is_decrypted(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """State from an encrypted reply is readable."""
    response = await encrypted_client(session, device).request("getState")
    assert response.encrypted is True
    assert response.params["voltage"] == 23717
    assert response.seq == device.seq


async def test_s60_switch_command(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """UIID 190 uses /zeroconf/switches with operSide, not /zeroconf/switch."""
    state = await encrypted_client(session, device).async_set_switch(
        False, device.state
    )
    request = device.requests[-1]
    assert request.command == "switches"
    assert request.params == {
        "switches": [{"outlet": 0, "switch": "off"}],
        "operSide": 1,
    }
    assert device.is_on() is False
    assert state["switches"] == [{"outlet": 0, "switch": "off"}]


async def test_switch_shape_defaults_to_switches_when_encrypted(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """With no state known yet, an encrypted device gets the S60TPF command."""
    await encrypted_client(session, device).async_set_switch(True, {})
    assert device.requests[-1].command == "switches"


async def test_compact_json_reaches_device(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """The fake ignores spaced JSON, as firmware 1.3.1 does, so this must actuate."""
    await encrypted_client(session, device).async_set_switch(False, device.state)
    assert device.is_on() is False


async def test_diy_switch_command(
    session: aiohttp.ClientSession, diy_device: FakeS60TPF
) -> None:
    """DIY plugs use the documented command."""
    state = await plain_client(session, diy_device).async_set_switch(
        False, diy_device.state
    )
    request = diy_device.requests[-1]
    assert request.command == "switch"
    assert request.params == {"switch": "off"}
    assert state["switch"] == "off"


async def test_switch_with_empty_reply_is_optimistic(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """Not every device echoes state, so success alone must update the relay."""
    device.empty_replies = True
    state = await encrypted_client(session, device).async_set_switch(
        True, {"switches": [{"outlet": 0, "switch": "off"}]}
    )
    assert state == {"switches": [{"outlet": 0, "switch": "on"}]}


@pytest.mark.parametrize("code", [400, 401])
async def test_plain_request_to_encrypted_device(
    session: aiohttp.ClientSession, code: int
) -> None:
    """Both the documented 401 and the observed 400 mean a key is needed."""
    fake = FakeS60TPF(plain_error=code)
    await fake.start()
    try:
        with pytest.raises(SonoffEncryptionRequired):
            await plain_client(session, fake).request("getState")
    finally:
        await fake.stop()


async def test_encrypted_reply_without_key(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """If a device accepts a plain request but encrypts the reply, ask for a key."""
    device.devicekey = None  # Accept the plain request.
    device_reply = device._reply

    def encrypted_reply(payload: dict[str, Any], *, encrypted: bool) -> Any:
        device.devicekey = DEVICE_KEY
        try:
            return device_reply(payload, encrypted=True)
        finally:
            device.devicekey = None

    device._reply = encrypted_reply  # type: ignore[method-assign]
    with pytest.raises(SonoffEncryptionRequired):
        await plain_client(session, device).request("getState")


async def test_unreadable_reply_is_a_decrypt_error(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """A reply that does not decrypt means the configured key is wrong."""
    device.reply_key = "a-different-key"
    with pytest.raises(SonoffDecryptError):
        await encrypted_client(session, device).request("getState")


async def test_wrong_key_is_reported_when_nothing_else_works(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """A device that cannot read our requests points at the key."""
    device.devicekey = "a-different-key"
    client = encrypted_client(session, device)
    with pytest.raises(SonoffKeyRejected):
        await client.request("getState")
    with pytest.raises(SonoffKeyRejected):
        await client.async_read_state({"sledOnline": "on"})


async def test_one_rejected_command_is_not_proof_of_a_wrong_key(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """If another command works, the key is fine."""
    device.key_rejected_commands.add("getState")
    state = await encrypted_client(session, device).async_read_state(
        {"sledOnline": "on"}
    )
    assert state["power"] == 724


async def test_empty_http_404_counts_as_alive(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """T6 answers getState with an empty 404 (SonoffLAN #1907): alive, not offline."""
    device.http_404_commands.add("getState")
    client = encrypted_client(session, device)
    assert await client.async_read_state({}) == {}
    state = await client.async_read_state({"sledOnline": "on"})
    assert state["voltage"] == 23717


async def test_html_reply_counts_as_alive(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """Older firmware answers getState with HTML (SonoffLAN #1442)."""
    device.garbage_replies = True
    assert await encrypted_client(session, device).async_read_state({}) == {}


@pytest.mark.parametrize("code", [404, 422, 400, 503])
async def test_device_errors(
    session: aiohttp.ClientSession, device: FakeS60TPF, code: int
) -> None:
    """Other error codes surface with their value."""
    device.unsupported["switches"] = code
    with pytest.raises(SonoffDeviceError) as err:
        await encrypted_client(session, device).request("switches", {"x": 1})
    assert err.value.code == code


async def test_wrong_device_id(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """A device ID mismatch gives 404."""
    client = SonoffClient(
        session, device.host, "ffffffffff", DEVICE_KEY, device.port, min_interval=FAST
    )
    with pytest.raises(SonoffDeviceError) as err:
        await client.request("getState")
    assert err.value.code == 404


async def test_non_json_reply(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """Garbage is a protocol error, not a crash."""
    device.garbage_replies = True
    with pytest.raises(SonoffProtocolError):
        await encrypted_client(session, device).request("getState")


async def test_connection_refused(session: aiohttp.ClientSession) -> None:
    """Connection failures are wrapped and not retried."""
    fake = FakeS60TPF()
    await fake.start()
    port = fake.port
    await fake.stop()
    client = SonoffClient(session, "127.0.0.1", DEVICE_ID, DEVICE_KEY, port)
    start = time.monotonic()
    with pytest.raises(SonoffConnectionError):
        await client.request("getState")
    assert time.monotonic() - start < MIN_REQUEST_INTERVAL


async def test_timeout(session: aiohttp.ClientSession, device: FakeS60TPF) -> None:
    """A silent device times out."""
    device.delay = 0.5
    client = encrypted_client(session, device, timeout=0.05)
    with pytest.raises(SonoffConnectionError):
        await client.request("getState")


async def test_retries_connection_reset(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """A busy device resets connections, and retrying must recover."""
    device.reset_next = 2
    response = await encrypted_client(session, device).request("getState")
    assert response.params["power"] == 724
    assert len(device.requests) == 3


async def test_gives_up_after_bounded_retries(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """Retries are bounded."""
    device.reset_next = 99
    with pytest.raises(SonoffConnectionError):
        await encrypted_client(session, device).request("getState")
    assert len(device.requests) == protocol.MAX_ATTEMPTS


async def test_requests_are_spaced(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """Concurrent requests must still arrive at least 200 ms apart."""
    client = encrypted_client(session, device, min_interval=MIN_REQUEST_INTERVAL)
    await asyncio.gather(*(client.request("getState") for _ in range(4)))
    times = [request.monotonic for request in device.requests]
    gaps = [later - earlier for earlier, later in itertools.pairwise(times)]
    assert len(gaps) == 3
    # The server records arrival, so allow for scheduling jitter of a few ms.
    assert min(gaps) >= MIN_REQUEST_INTERVAL - 0.01


async def test_spacing_survives_a_new_client(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """The limit belongs to the device, not to one client object.

    Found by running a real server: the config flow's probe and the entry's first poll
    use separate clients, and reached the plug 19 ms apart.
    """
    await encrypted_client(session, device, min_interval=MIN_REQUEST_INTERVAL).request(
        "getState"
    )
    await encrypted_client(session, device, min_interval=MIN_REQUEST_INTERVAL).request(
        "getState"
    )
    first, second = device.requests
    assert second.monotonic - first.monotonic >= MIN_REQUEST_INTERVAL - 0.01


async def test_concurrent_clients_are_spaced(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """A reconfigure probe can run alongside the coordinator's poll."""
    clients = [
        encrypted_client(session, device, min_interval=MIN_REQUEST_INTERVAL)
        for _ in range(3)
    ]
    await asyncio.gather(*(client.request("getState") for client in clients))
    times = sorted(request.monotonic for request in device.requests)
    assert min(b - a for a, b in itertools.pairwise(times)) >= (
        MIN_REQUEST_INTERVAL - 0.01
    )


def test_spacing_table_stays_bounded() -> None:
    """Addresses that come and go must not accumulate for ever."""
    protocol._next_request_at.clear()
    for number in range(protocol._MAX_TRACKED_DEVICES + 50):
        protocol._reserve_request_slot(
            (f"192.0.2.{number}", 8081), 0.2, 1000.0 + number
        )
    assert len(protocol._next_request_at) <= protocol._MAX_TRACKED_DEVICES


async def test_retries_are_spaced(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """A retry is a request too."""
    device.reset_next = 1
    client = encrypted_client(session, device, min_interval=MIN_REQUEST_INTERVAL)
    await client.request("getState")
    first, second = device.requests
    assert second.monotonic - first.monotonic >= MIN_REQUEST_INTERVAL - 0.01


async def test_read_prefers_get_state_over_led_write(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """If getState returns readings, the LED must never be written."""
    client = encrypted_client(session, device)
    state = await client.async_read_state(device.state)
    assert state["power"] == 724
    assert device.commands() == ["getState"]
    assert device.led_writes == 0
    assert client.poll_command == "getState"


async def test_read_falls_back_to_led_echo(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """If getState gives no readings, the upstream LED method is used."""
    device.stateless_commands.add("getState")
    client = encrypted_client(session, device)
    state = await client.async_read_state(device.state)
    assert device.commands() == ["getState", "sledOnline"]
    assert state["voltage"] == 23717
    assert client.poll_command == "sledOnline"

    device.requests.clear()
    await client.async_read_state(device.state)
    assert device.commands() == ["sledOnline"]


@pytest.mark.parametrize("led", ["on", "off"])
async def test_led_echo_never_changes_led(
    session: aiohttp.ClientSession, device: FakeS60TPF, led: str
) -> None:
    """The echo sends back exactly the value the device reported."""
    device.state["sledOnline"] = led
    device.stateless_commands.add("getState")
    await encrypted_client(session, device).async_read_state({"sledOnline": led})
    assert device.requests[-1].params == {"sledOnline": led}
    assert device.state["sledOnline"] == led


async def test_read_without_known_led_does_not_guess(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """With the LED state unknown, no value may be written."""
    device.stateless_commands.add("getState")
    client = encrypted_client(session, device)
    assert await client.async_read_state({}) == {}
    assert device.commands() == ["getState"]
    assert device.led_writes == 0
    assert client.poll_command is None  # Retried once the LED state is known.


async def test_get_state_error_counts_as_alive(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """A device rejecting getState is reachable (SonoffLAN #1442)."""
    device.unsupported["getState"] = 400
    assert await encrypted_client(session, device).async_read_state({}) == {}


async def test_get_state_timeout_falls_back(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """A timeout on getState alone must not read as offline."""

    original = device._handle

    async def slow_get_state(request: Any) -> Any:
        if request.match_info["command"] == "getState":
            await asyncio.sleep(0.3)
        return await original(request)

    device._handle = slow_get_state  # type: ignore[method-assign]
    await device.stop()
    await device.start()
    client = encrypted_client(session, device, timeout=0.1)
    state = await client.async_read_state({"sledOnline": "on"})
    assert state["power"] == 724
    assert client.poll_command == "sledOnline"


async def test_unmetered_device_stops_writing_led(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """A device with no readings must not get an LED write on every poll."""
    device.state = {
        "switches": [{"outlet": 0, "switch": "on"}],
        "sledOnline": "on",
        "fwVersion": "1.2.0",
    }
    client = encrypted_client(session, device)
    await client.async_read_state(device.state)
    assert client.poll_command == "getState"
    device.requests.clear()
    await client.async_read_state(device.state)
    assert device.commands() == ["getState"]


async def test_plain_read_uses_documented_info(
    session: aiohttp.ClientSession, diy_device: FakeS60TPF
) -> None:
    """Without a key, the documented command is tried first."""
    state = await plain_client(session, diy_device).async_read_state({})
    assert diy_device.commands()[0] == "info"
    assert state["fwVersion"] == "3.5.0"


async def test_plain_device_settles_on_one_poll_command(
    session: aiohttp.ClientSession, diy_device: FakeS60TPF
) -> None:
    """A DIY device must not get two requests on every poll."""
    client = plain_client(session, diy_device)
    await client.async_read_state({})
    assert client.poll_command == "info"
    diy_device.requests.clear()
    await client.async_read_state({})
    assert diy_device.commands() == ["info"]


async def test_encrypted_device_keeps_looking_until_led_is_known(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """Readings must not be given up on just because the LED state is not known yet.

    The S60TPF case: getState gives relay state only, and sledOnline arrives later by
    TXT. Settling on getState early would lose the metrics permanently.
    """
    device.stateless_commands.add("sledOnline")
    device.state = {"switches": [{"outlet": 0, "switch": "on"}]}
    client = encrypted_client(session, device)
    await client.async_read_state({})
    assert client.poll_command is None

    device.stateless_commands.clear()
    device.stateless_commands.add("getState")
    device.state = {"power": 724, "sledOnline": "on"}
    state = await client.async_read_state({"sledOnline": "on"})
    assert state["power"] == 724
    assert client.poll_command == "sledOnline"


async def test_unreachable_read_raises(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """If nothing answers, the failure reaches the caller."""
    client = encrypted_client(session, device)
    await device.stop()
    with pytest.raises(SonoffConnectionError):
        await client.async_read_state({"sledOnline": "on"})


async def test_unencrypted_reply_ignored_when_key_configured(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """Anyone on the LAN can answer in plaintext, so it must not become state."""
    device.empty_replies = False
    original = device._reply

    def plain_reply(payload: dict[str, Any], *, encrypted: bool) -> Any:
        return original({"power": 999999}, encrypted=False)

    device._reply = plain_reply  # type: ignore[method-assign]
    client = encrypted_client(session, device)
    assert await client.async_read_state({}) == {}


async def test_observer_receives_traffic(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """Every stage is observable for the capture feature."""
    seen: list[str] = []
    client = encrypted_client(
        session, device, observer=lambda kind, _payload: seen.append(kind)
    )
    await client.request("getState")
    assert seen == ["tx", "rx", "rx_decrypted"]


async def test_observer_sees_errors(
    session: aiohttp.ClientSession, device: FakeS60TPF
) -> None:
    """Failures are captured too."""
    seen: list[str] = []
    client = encrypted_client(
        session, device, observer=lambda kind, _payload: seen.append(kind)
    )
    await device.stop()
    with pytest.raises(SonoffConnectionError):
        await client.request("getState")
    assert seen == ["tx", "error"]


async def test_set_address(session: aiohttp.ClientSession, device: FakeS60TPF) -> None:
    """A new address takes effect on the next request."""
    client = SonoffClient(session, "192.0.2.1", DEVICE_ID, DEVICE_KEY, 1)
    client.set_address(device.host, device.port)
    assert (await client.request("getState")).params
