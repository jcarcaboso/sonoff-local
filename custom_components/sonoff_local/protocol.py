"""Async LAN client for Sonoff devices.

This module deliberately imports nothing from Home Assistant, so the wire format
can be tested on its own. ``tests/test_protocol_isolation.py`` enforces that.

Primary target: S60TPF (UIID 190, LAN type ``plug``) in encrypted LAN mode. Nothing
here has been verified on real hardware. The behaviour comes from Sonoff's DIY-mode
documents and from reading AlexxIT/SonoffLAN (MIT), commit ``08135f0``. No code is
shared with that project at runtime.

Wire format
-----------
``POST http://<host>:<port>/zeroconf/<command>`` with a JSON body::

    {"sequence": "<ms>", "deviceid": "<id>", "selfApikey": "123", "data": {...}}

In encrypted mode ``data`` becomes base64 AES-128-CBC ciphertext (PKCS7 padding, the
key is ``md5(devicekey)``) and the envelope gains ``"encrypt": true`` and ``"iv"``.
JSON must be compact: firmware 1.3.1 on some models replies ``error: 0`` to a payload
with spaces and then does nothing (SonoffLAN PR #1884).

Metric mapping (UIID 190)
-------------------------
======== ========== ===== ============================ =======
LAN key  Entity     Unit  Raw example (issue #1514)    Scale
======== ========== ===== ============================ =======
power    power      W     724 -> 7.24 W                x 0.01
current  current    A     5 -> 0.05 A                  x 0.01
voltage  voltage    V     23717 -> 237.17 V            x 0.01
dayKwh   energy_day kWh   2 -> 0.02 kWh                x 0.01
monthKwh energy_mon kWh   117 -> 1.17 kWh              x 0.01
======== ========== ===== ============================ =======

UIID 190 has no week or year counter in SonoffLAN's device spec, and no captured
payload contains one. ``weekKwh`` and ``yearKwh`` are mapped for other models and
only produce an entity if the device actually sends them.

Other models report the same keys differently: POWR2 (UIID 32) sends decimal strings
such as ``"234.20"`` that are already in volts. ``scale_metric`` covers both forms.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

SERVICE_TYPE: Final = "_ewelink._tcp.local."
DEFAULT_PORT: Final = 8081
# Sonoff's protocol document requires at least 200 ms between HTTP requests.
MIN_REQUEST_INTERVAL: Final = 0.2
# Some firmware takes about 1 s to answer (SonoffLAN #1822), so stay well above that.
REQUEST_TIMEOUT: Final = 5.0
# The device's HTTP server handles one request at a time and resets the connection
# of anything that overlaps. Upstream retries; we retry a bounded number of times.
MAX_ATTEMPTS: Final = 3

ERROR_OK: Final = 0
ERROR_BAD_REQUEST: Final = 400
ERROR_ENCRYPTION_REQUIRED: Final = 401
ERROR_NOT_FOUND: Final = 404
ERROR_INVALID_PARAM: Final = 422

COMMAND_GET_STATE: Final = "getState"
COMMAND_INFO: Final = "info"
COMMAND_SLED_ONLINE: Final = "sledOnline"
COMMAND_SWITCH: Final = "switch"
COMMAND_SWITCHES: Final = "switches"

# Keys whose presence shows a payload carries live readings.
METRIC_KEYS: Final = frozenset(
    {"power", "current", "voltage", "dayKwh", "weekKwh", "monthKwh", "yearKwh"}
)

_SERVICE_NAME_RE: Final = re.compile(r"^ewelink_([0-9a-z]{10})\.", re.IGNORECASE)
DEVICE_ID_RE: Final = re.compile(r"^[0-9a-z]{10}$", re.IGNORECASE)
_TXT_DATA_KEYS: Final = ("data1", "data2", "data3", "data4")
_INTEGER_STRING_RE: Final = re.compile(r"^-?\d+$")

_REDACTED: Final = "**REDACTED**"
# Matched as substrings so that a key firmware adds later fails safe: hiding a reading
# by accident is a far smaller problem than publishing a password.
_SECRET_FRAGMENTS: Final = ("key", "pass", "secret", "token", "ssid", "mac", "bindinfo")
_SECRET_KEYS: Final = frozenset({"host", "ip"})
_DEVICE_ID_KEYS: Final = frozenset({"deviceid", "id"})
_OPAQUE_KEYS: Final = frozenset({"data", "iv", *_TXT_DATA_KEYS})

Observer = Callable[[str, dict[str, Any]], None]


class SonoffError(Exception):
    """Base class for protocol errors."""


class SonoffConnectionError(SonoffError):
    """The device could not be reached or did not answer in time."""


class SonoffEncryptionRequired(SonoffError):
    """The device expects encrypted requests but no devicekey is configured."""


class SonoffDecryptError(SonoffError):
    """A payload could not be decrypted, which usually means a wrong devicekey."""


class SonoffKeyRejected(SonoffDecryptError):
    """The device could not read an encrypted request, which suggests a wrong key.

    An unsupported command does not look like this: real devices answer those with
    HTML or an empty HTTP 404 (SonoffLAN #1442, #1907), which is a protocol error. Doc
    error 400 means the body could not be parsed, and for a device reporting
    ``encrypt: true`` that points at the key. It is still indirect evidence, so
    ``async_read_state`` only reports it if nothing else worked.
    """


class SonoffProtocolError(SonoffError):
    """The device answered with something that is not a valid response."""


class SonoffDeviceError(SonoffError):
    """The device answered with a non-zero error code."""

    def __init__(self, code: int) -> None:
        """Store the error code."""
        super().__init__(f"Device returned error {code}")
        self.code = code


def _aes_key(devicekey: str) -> bytes:
    """Derive the AES key. MD5 is mandated by the device protocol."""
    return hashlib.md5(devicekey.encode(), usedforsecurity=False).digest()


def encode_json(value: Mapping[str, Any]) -> bytes:
    """Serialise compactly, which some firmware requires."""
    return json.dumps(value, separators=(",", ":")).encode()


def encrypt_params(params: Mapping[str, Any], devicekey: str) -> tuple[str, str]:
    """Return base64 ``(ciphertext, iv)`` for ``params``."""
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(encode_json(params)) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_aes_key(devicekey)), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode(), base64.b64encode(iv).decode()


def decrypt_bytes(data: str, iv: str, devicekey: str) -> bytes:
    """Decrypt to raw plaintext. Raises ``ValueError`` or ``TypeError`` on bad input."""
    decryptor = Cipher(
        algorithms.AES(_aes_key(devicekey)), modes.CBC(base64.b64decode(iv))
    ).decryptor()
    padded = decryptor.update(base64.b64decode(data)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def decrypt_params(data: str, iv: str, devicekey: str) -> dict[str, Any]:
    """Decrypt a payload. Raises ``SonoffDecryptError`` if it cannot be read."""
    if not data:  # Some devices send an empty body with an iv (SonoffLAN local.py).
        return {}
    try:
        # Some firmware leaves stray 0x02 bytes after the JSON (SonoffLAN #1160).
        params = json.loads(decrypt_bytes(data, iv, devicekey).rstrip(b"\x02"))
    except (TypeError, ValueError) as err:
        raise SonoffDecryptError("Payload could not be decrypted") from err
    if not isinstance(params, dict):
        raise SonoffDecryptError("Decrypted payload is not an object")
    return params


def parse_device_id(service_name: str) -> str | None:
    """Return the device ID from a service name, or None if it is not a Sonoff.

    Other products share ``_ewelink._tcp`` (``ihost-...``, ``zbbridgeu-...``) and are
    skipped. Case varies by model (``eWeLink_`` and ``eWelink_``).
    """
    if match := _SERVICE_NAME_RE.match(service_name):
        return match.group(1).lower()
    return None


def _as_text(value: Any) -> str | None:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class TxtFrame:
    """A decoded mDNS TXT record."""

    deviceid: str | None
    localtype: str | None
    seq: int | None
    encrypted: bool
    iv: str | None
    data: str
    raw: dict[str, str] = field(repr=False)


def parse_txt(properties: Mapping[Any, Any]) -> TxtFrame:
    """Parse TXT properties, given as bytes (zeroconf) or strings (Home Assistant).

    The flag is compared by value: some devices publish ``encrypt=false``, and the
    mere presence of the key does not mean the payload is encrypted (SonoffLAN #1886).
    """
    raw = {
        key: value
        for key, value in ((_as_text(k), _as_text(v)) for k, v in properties.items())
        if key is not None and value is not None
    }
    if (flag := raw.get("encrypt")) is not None:
        encrypted = flag.strip().lower() in ("true", "1")
    else:
        encrypted = "iv" in raw

    seq_text = raw.get("seq", "")
    return TxtFrame(
        deviceid=(raw["id"].lower() if "id" in raw else None),
        localtype=raw.get("type"),
        seq=int(seq_text) if seq_text.isdecimal() else None,
        encrypted=encrypted,
        iv=raw.get("iv"),
        data="".join(raw.get(key, "") for key in _TXT_DATA_KEYS),
        raw=raw,
    )


def decode_txt(frame: TxtFrame, devicekey: str | None) -> dict[str, Any]:
    """Return the state carried by a TXT frame."""
    if frame.encrypted:
        if not devicekey:
            raise SonoffEncryptionRequired("Encrypted TXT record but no devicekey")
        if not frame.iv:
            raise SonoffDecryptError("Encrypted TXT record without iv")
        return decrypt_params(frame.data, frame.iv, devicekey)
    if not frame.data:
        return {}
    try:
        params = json.loads(frame.data)
    except ValueError as err:
        raise SonoffProtocolError("TXT record is not valid JSON") from err
    if not isinstance(params, dict):
        raise SonoffProtocolError("TXT payload is not an object")
    return params


def scale_metric(value: Any) -> float | None:
    """Convert a raw metric to its unit, or None if it is unusable.

    Integers (or integer strings) are hundredths of the unit, as sent by UIID 190.
    Decimal values are already in the unit, as sent by UIID 32.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return round(value * 0.01, 2)
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            return None
        if number != number or number in (float("inf"), float("-inf")):
            return None
        if _INTEGER_STRING_RE.match(text):
            return round(number * 0.01, 2)
        return round(number, 2)
    return None


def switch_is_on(state: Mapping[str, Any]) -> bool | None:
    """Return the relay state from either state shape."""
    switches = state.get("switches")
    if isinstance(switches, list):
        for channel in switches:
            if isinstance(channel, dict) and channel.get("outlet") == 0:
                return _on_off(channel.get("switch"))
        return None
    return _on_off(state.get("switch"))


def _on_off(value: Any) -> bool | None:
    return {"on": True, "off": False}.get(value) if isinstance(value, str) else None


def signal_strength(state: Mapping[str, Any]) -> int | None:
    """Return RSSI in dBm: ``rssi`` on UIID 190, ``signalStrength`` in DIY mode."""
    for key in ("rssi", "signalStrength"):
        value = state.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def mask_device_id(deviceid: str) -> str:
    """Keep only the last four characters of a device ID."""
    return f"******{deviceid[-4:]}" if len(deviceid) > 4 else _REDACTED


def redact(value: Any) -> Any:
    """Return a copy that is safe to log or attach to a bug report.

    Removes keys, Wi-Fi details and network addresses at any depth, masks device IDs
    and replaces opaque ciphertext with its length. Readings and other unknown keys
    are kept on purpose: they are the point of capturing a payload.
    """
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.lower()
            if lowered in _SECRET_KEYS or any(
                fragment in lowered for fragment in _SECRET_FRAGMENTS
            ):
                result[name] = _REDACTED
            elif lowered in _DEVICE_ID_KEYS and isinstance(item, str):
                result[name] = mask_device_id(item)
            elif (lowered in _OPAQUE_KEYS and isinstance(item, str)) or (
                lowered == "data" and isinstance(item, str)
            ):
                result[name] = f"<{len(item)} chars>"
            else:
                result[name] = redact(item)
        return result
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class DeviceResponse:
    """A decoded HTTP response."""

    params: dict[str, Any]
    seq: int | None
    encrypted: bool


class SonoffClient:
    """Talks to one device. One instance must be used per device."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        deviceid: str,
        devicekey: str | None = None,
        port: int = DEFAULT_PORT,
        *,
        min_interval: float = MIN_REQUEST_INTERVAL,
        timeout: float = REQUEST_TIMEOUT,
        observer: Observer | None = None,
    ) -> None:
        """Create a client. An empty ``devicekey`` selects plaintext (DIY) mode."""
        self._session = session
        self.host = host
        self.port = port
        self.deviceid = deviceid
        self._devicekey = devicekey or None
        self._min_interval = min_interval
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._observer = observer
        self._lock = asyncio.Lock()
        self._not_before = 0.0
        self._sequence = 0
        self._poll_command: str | None = None

    @property
    def encrypted(self) -> bool:
        """Return True when requests are encrypted."""
        return self._devicekey is not None

    @property
    def poll_command(self) -> str | None:
        """Return the command that proved able to return readings, if any."""
        return self._poll_command

    def set_address(self, host: str, port: int) -> None:
        """Use a new address, for example after a DHCP change."""
        self.host = host
        self.port = port

    def _observe(self, kind: str, payload: dict[str, Any]) -> None:
        if self._observer is not None:
            self._observer(kind, payload)

    def _next_sequence(self) -> str:
        """Return a unique, increasing millisecond counter."""
        self._sequence = max(time.time_ns() // 1_000_000, self._sequence + 1)
        return str(self._sequence)

    def _build_body(self, params: Mapping[str, Any]) -> bytes:
        body: dict[str, Any] = {
            "sequence": self._next_sequence(),
            "deviceid": self.deviceid,
            "selfApikey": "123",  # Fixed placeholder, as used by SonoffLAN.
        }
        if self._devicekey is not None:
            # Field order matches SonoffLAN, in case a device's parser cares.
            ciphertext, iv = encrypt_params(params, self._devicekey)
            body |= {"data": ciphertext, "encrypt": True, "iv": iv}
        else:
            body["data"] = dict(params)
        return encode_json(body)

    async def _post(self, url: str, body: bytes) -> Any:
        """POST once and return the decoded JSON."""
        async with self._session.post(
            url,
            data=body,
            headers={"Connection": "close", "Content-Type": "application/json"},
            timeout=self._timeout,
        ) as response:
            return await response.json(content_type=None)

    async def _exchange(self, command: str, body: bytes) -> Any:
        """Send a request, keeping requests spaced and retrying connection resets."""
        url = f"http://{self.host}:{self.port}/zeroconf/{command}"
        loop = asyncio.get_running_loop()
        async with self._lock:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if (delay := self._not_before - loop.time()) > 0:
                    await asyncio.sleep(delay)
                try:
                    return await self._post(url, body)
                except TimeoutError as err:
                    raise SonoffConnectionError(f"Timeout on {command}") from err
                except ValueError as err:  # Includes invalid JSON.
                    raise SonoffProtocolError(f"Bad reply to {command}") from err
                except aiohttp.ClientConnectorError as err:
                    raise SonoffConnectionError(str(err)) from err
                except aiohttp.ClientError as err:
                    if not _is_busy_error(err) or attempt == MAX_ATTEMPTS:
                        raise SonoffConnectionError(str(err)) from err
                    _LOGGER.debug("Device busy on %s, attempt %s", command, attempt)
                finally:
                    self._not_before = loop.time() + self._min_interval
        raise AssertionError("unreachable")  # pragma: no cover

    async def request(
        self, command: str, params: Mapping[str, Any] | None = None
    ) -> DeviceResponse:
        """Send a command and return the decoded response."""
        params = dict(params or {})
        self._observe("tx", {"command": command, "params": params})
        try:
            reply = await self._exchange(command, self._build_body(params))
        except SonoffError as err:
            # The type only: aiohttp's messages contain the device's address.
            self._observe("error", {"command": command, "error": type(err).__name__})
            raise
        if not isinstance(reply, dict):
            raise SonoffProtocolError(f"Reply to {command} is not an object")

        self._observe("rx", {"command": command, "reply": reply})
        code = reply.get("error", ERROR_OK)
        encrypted = "iv" in reply
        if code != ERROR_OK:
            # Encrypted devices answered 400 rather than the documented 401 to plain
            # requests (SonoffLAN #975), flagging themselves with "encrypt": true.
            if code == ERROR_ENCRYPTION_REQUIRED or (
                code == ERROR_BAD_REQUEST
                and reply.get("encrypt")
                and self._devicekey is None
            ):
                raise SonoffEncryptionRequired(f"Device returned error {code}")
            if code == ERROR_BAD_REQUEST and reply.get("encrypt"):
                raise SonoffKeyRejected("Device could not read the encrypted request")
            raise SonoffDeviceError(code)

        data = reply.get("data")
        if encrypted and isinstance(data, str):
            if self._devicekey is None:
                raise SonoffEncryptionRequired("Encrypted reply but no devicekey")
            params = decrypt_params(data, str(reply["iv"]), self._devicekey)
            self._observe("rx_decrypted", {"command": command, "params": params})
        elif isinstance(data, dict):
            params = data
        else:
            params = {}
        seq = reply.get("seq")
        return DeviceResponse(
            params=params,
            seq=seq if isinstance(seq, int) else None,
            encrypted=encrypted,
        )

    def _poll_candidates(
        self, state: Mapping[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """List the ways to read state, least invasive first.

        There is no state query in the non-DIY protocol. SonoffLAN re-sends the
        current ``sledOnline`` value to provoke a reply, which is a write. So
        ``getState`` is tried first, because DEVICES.md says the S60TPF answers any
        LAN request with its state. ``sledOnline`` is only ever sent with the value
        the device reported, so the LED setting never changes.
        """
        candidates: list[tuple[str, dict[str, Any]]] = []
        if self._devicekey is None:
            candidates.append((COMMAND_INFO, {}))
        candidates.append((COMMAND_GET_STATE, {}))
        led = state.get(COMMAND_SLED_ONLINE)
        if led in ("on", "off"):
            candidates.append((COMMAND_SLED_ONLINE, {COMMAND_SLED_ONLINE: led}))
        if self._poll_command is not None:
            for candidate in candidates:
                if candidate[0] == self._poll_command:
                    return [candidate]
            self._poll_command = None
        return candidates

    async def async_read_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Read current state, returning whatever the device reported.

        ``state`` is the last known state. A device error means the device is alive
        but rejected that command, so the next candidate is tried. The first command
        to return readings is remembered. If none does, the command that returned the
        most data is remembered once all options are exhausted, so a device with no
        meter does not get an LED write or a useless extra request on each poll.
        """
        candidates = self._poll_candidates(state)
        merged: dict[str, Any] = {}
        best: tuple[int, str | None] = (0, None)
        failure: SonoffConnectionError | None = None
        rejected: SonoffKeyRejected | None = None
        answered = False

        for command, params in candidates:
            try:
                response = await self.request(command, params)
            except SonoffConnectionError as err:
                failure = err
                continue
            except SonoffKeyRejected as err:
                rejected = err
                continue
            except (SonoffDeviceError, SonoffProtocolError) as err:
                _LOGGER.debug("%s rejected by %s: %s", command, self.deviceid, err)
                answered = True
                continue
            answered = True
            if self._devicekey is not None and not response.encrypted:
                continue  # Unauthenticated data is ignored once a key is configured.
            merged.update(response.params)
            if METRIC_KEYS & response.params.keys():
                self._poll_command = command
                return merged
            if len(response.params) > best[0]:
                best = (len(response.params), command)

        if not answered:
            if rejected is not None:
                raise rejected
            if failure is not None:
                raise failure
            raise SonoffConnectionError("No poll command available")
        tried_led = any(command == COMMAND_SLED_ONLINE for command, _ in candidates)
        # An encrypted device's LED state may still arrive by TXT, so keep looking. A
        # plain device that does not report one will never be able to use the echo.
        no_led_expected = self._devicekey is None and COMMAND_SLED_ONLINE not in merged
        if best[1] is not None and (tried_led or no_led_expected):
            self._poll_command = best[1]
        return merged

    async def async_set_switch(
        self, turn_on: bool, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Switch the relay and return the resulting state.

        UIID 190 does not use ``/zeroconf/switch``: its command is ``switches`` with
        ``operSide`` (SonoffLAN ``XSwitchPOWR3``). DIY plugs use the documented
        ``switch`` command. The shape of the last known state decides, and with no
        state known yet the configured mode does.
        """
        value = "on" if turn_on else "off"
        if "switches" in state or (self.encrypted and "switch" not in state):
            expected: dict[str, Any] = {
                "switches": [{"outlet": 0, "switch": value}],
            }
            response = await self.request(COMMAND_SWITCHES, {**expected, "operSide": 1})
        else:
            expected = {"switch": value}
            response = await self.request(COMMAND_SWITCH, expected)

        if self._devicekey is not None and not response.encrypted:
            return expected
        return {**expected, **response.params}


def _is_busy_error(err: aiohttp.ClientError) -> bool:
    """Return True for the errors caused by the device's single-request server."""
    if isinstance(err, aiohttp.ServerDisconnectedError):
        return True
    return isinstance(err, aiohttp.ClientOSError) and err.errno == errno.ECONNRESET
