"""State coordinator for one device."""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_CAPTURE_PAYLOADS,
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .mdns import MdnsEvent
from .protocol import (
    DEFAULT_PORT,
    METRIC_KEYS,
    SonoffClient,
    SonoffConnectionError,
    SonoffDecryptError,
    SonoffEncryptionRequired,
    SonoffError,
    SonoffKeyRejected,
    SonoffProtocolError,
    decode_txt,
    redact,
)

_LOGGER = logging.getLogger(__name__)

type SonoffConfigEntry = ConfigEntry[SonoffCoordinator]

# Frames kept for a diagnostics download while capture is on.
CAPTURE_SIZE = 200
# Missed polls before a silent device is reported unavailable.
STALE_POLLS = 3
# Bad TXT frames tolerated before asking for the devicekey again.
AUTH_FAILURE_LIMIT = 3
# A much lower TXT seq means the device rebooted, not that the frame is stale.
SEQ_RESET_WINDOW = 64


class SonoffCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Merges mDNS pushes and HTTP polls into one state dictionary.

    Polling is not a mere fallback here. Cloud P/I/V updates ended in 2026 and a TXT
    push may carry relay state only (SonoffLAN #1871), so a push must not delay the
    next metrics poll. Polling is therefore skipped only if readings are fresh, and
    ``async_set_updated_data`` is avoided because it would reschedule the poll.
    """

    config_entry: SonoffConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SonoffConfigEntry) -> None:
        """Create the coordinator."""
        self._poll_seconds: int = entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_DEVICE_ID]}",
            update_interval=timedelta(seconds=self._poll_seconds),
        )
        self.deviceid: str = entry.data[CONF_DEVICE_ID]
        self._devicekey: str | None = entry.data.get(CONF_DEVICE_KEY) or None
        self._capture_enabled: bool = entry.options.get(CONF_CAPTURE_PAYLOADS, False)
        self.frames: deque[dict[str, Any]] = deque(maxlen=CAPTURE_SIZE)
        self.client = SonoffClient(
            async_get_clientsession(hass),
            entry.data[CONF_HOST],
            self.deviceid,
            self._devicekey,
            entry.data.get(CONF_PORT, DEFAULT_PORT),
            observer=self.record,
        )

        self._last_contact: float | None = None
        self._last_metrics: float | None = None
        self._last_txt_seq: int | None = None
        self._last_txt_data: str | None = None
        self._auth_failures = 0
        self._key_proven = False
        self._warned_plaintext = False
        self.stats: dict[str, int] = {
            "txt_frames": 0,
            "txt_rejected": 0,
            "polls": 0,
            "polls_skipped": 0,
            "poll_failures": 0,
        }

    @property
    def stale_after(self) -> float:
        """Return the seconds of silence after which the device is unavailable."""
        return float(self._poll_seconds * STALE_POLLS)

    def record(self, kind: str, payload: dict[str, Any]) -> None:
        """Store a redacted frame. Nothing secret reaches the log or the buffer."""
        logging_enabled = _LOGGER.isEnabledFor(logging.DEBUG)
        if not self._capture_enabled and not logging_enabled:
            return
        frame = {
            "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "kind": kind,
            **redact(payload),
        }
        if self._capture_enabled:
            self.frames.append(frame)
        if logging_enabled:
            _LOGGER.debug("Frame %s: %s", self.deviceid[-4:], frame)

    def _mark_contact(self, params: dict[str, Any]) -> None:
        now = time.monotonic()
        self._last_contact = now
        if METRIC_KEYS & params.keys():
            self._last_metrics = now

    def _merge(self, params: dict[str, Any]) -> dict[str, Any]:
        self._mark_contact(params)
        return {**(self.data or {}), **params}

    def _readings_are_fresh(self) -> bool:
        reference = self._last_metrics if self._last_metrics else self._last_contact
        if reference is None or self.data is None:
            return False
        # The margin stops a push just before a tick from skipping that whole tick.
        return time.monotonic() - reference < self._poll_seconds * 0.8

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll, unless a push already brought fresh readings."""
        if self._readings_are_fresh():
            self.stats["polls_skipped"] += 1
            assert self.data is not None
            return self.data

        self.stats["polls"] += 1
        try:
            params = await self.client.async_read_state(self.data or {})
        except SonoffDecryptError as err:
            if isinstance(err, SonoffKeyRejected) and self._key_proven:
                # TXT frames decrypt with this key, so the key is fine and the device
                # simply refused these commands.
                self._mark_contact({})
                return self.data or {}
            # One unreadable reply can be a firmware glitch (SonoffLAN #1160), so only
            # repeated failures, or a failure with no state at all, doubt the key.
            self._auth_failures += 1
            if self.data is None or self._auth_failures >= AUTH_FAILURE_LIMIT:
                raise ConfigEntryAuthFailed("The devicekey no longer works") from err
            return self.data
        except SonoffEncryptionRequired as err:
            raise ConfigEntryAuthFailed("The device now requires a devicekey") from err
        except SonoffConnectionError as err:
            self.stats["poll_failures"] += 1
            if self.data is not None and self._last_contact is not None:
                silent_for = time.monotonic() - self._last_contact
                if silent_for < self.stale_after:
                    return self.data
            raise UpdateFailed(f"Device did not respond: {err}") from err

        if params:
            self._auth_failures = 0
        return self._merge(params)

    async def async_set_switch(self, turn_on: bool) -> None:
        """Switch the relay."""
        try:
            params = await self.client.async_set_switch(turn_on, self.data or {})
        except SonoffError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.data = self._merge(params)
        self.last_update_success = True
        self.async_update_listeners()

    @callback
    def async_handle_mdns(self, event: MdnsEvent) -> None:
        """Apply an mDNS event."""
        if event.removed:
            # Not proof it is gone, since mDNS records expire. Verify on the next poll.
            self.record("txt_removed", {})
            self._last_contact = None
            self._last_metrics = None
            self.config_entry.async_create_background_task(
                self.hass, self.async_request_refresh(), f"{DOMAIN} verify removal"
            )
            return
        if (frame := event.frame) is None:
            return
        self.record("txt", {"txt": frame.raw})

        if frame.deviceid is not None and frame.deviceid != self.deviceid:
            self.stats["txt_rejected"] += 1
            return
        if frame.encrypted is False and self._devicekey is not None:
            # A plain frame proves nothing about its sender, and a downgrade by anyone
            # on the LAN must not be possible. Reconfigure to change the mode.
            self.stats["txt_rejected"] += 1
            if not self._warned_plaintext:
                self._warned_plaintext = True
                _LOGGER.warning(
                    "Device %s announces plaintext but has a devicekey configured; "
                    "ignoring those announcements. If it is now in DIY mode, "
                    "reconfigure it with an empty devicekey",
                    self.deviceid,
                )
            return

        try:
            params = decode_txt(frame, self._devicekey)
        except SonoffDecryptError, SonoffEncryptionRequired:
            self.stats["txt_rejected"] += 1
            self._auth_failures += 1
            self.record("txt_undecodable", {})
            if self._auth_failures == AUTH_FAILURE_LIMIT:
                _LOGGER.warning("Cannot decode announcements from %s", self.deviceid)
                self.config_entry.async_start_reauth(self.hass)
            return
        except SonoffProtocolError:
            self.stats["txt_rejected"] += 1
            return

        self._auth_failures = 0
        self._key_proven = frame.encrypted
        self.record("txt_decoded", {"params": params})
        self._update_address(event)

        if self._is_stale_txt(frame.seq, frame.data):
            return
        self._last_txt_seq = frame.seq
        self._last_txt_data = frame.data
        self.stats["txt_frames"] += 1
        self.data = self._merge(params)
        self.last_update_success = True
        self.async_update_listeners()

    def _is_stale_txt(self, seq: int | None, data: str) -> bool:
        """Return True for a repeat or an out-of-order frame."""
        if data == self._last_txt_data:
            self._last_contact = time.monotonic()
            return True
        if seq is None or self._last_txt_seq is None:
            return False
        return 0 < self._last_txt_seq - seq < SEQ_RESET_WINDOW

    def _update_address(self, event: MdnsEvent) -> None:
        """Follow a new address. Only decoded frames get here."""
        host = event.host
        port = event.port or self.client.port
        if host is None or (host, port) == (self.client.host, self.client.port):
            return
        _LOGGER.debug("Device %s moved address", self.deviceid)
        self.client.set_address(host, port)
        entry = self.config_entry
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST: host, CONF_PORT: port}
        )

    def diagnostics(self) -> dict[str, Any]:
        """Return redacted runtime information."""
        state = self.data or {}
        return {
            "encrypted": self.client.encrypted,
            "poll_command": self.client.poll_command,
            "poll_interval": self._poll_seconds,
            "capture_enabled": self._capture_enabled,
            "stats": dict(self.stats),
            # A frozen reading is the classic failure here: check this first.
            "seconds_since_metrics": (
                round(time.monotonic() - self._last_metrics, 1)
                if self._last_metrics is not None
                else None
            ),
            "state": redact(state),
            "raw_metrics": {key: state[key] for key in METRIC_KEYS if key in state},
            "frames": list(self.frames),
        }
