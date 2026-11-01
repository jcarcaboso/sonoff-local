"""Constants for Sonoff Local."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "sonoff_local"

CONF_DEVICE_ID: Final = "device_id"
CONF_DEVICE_KEY: Final = "device_key"
CONF_LOCAL_TYPE: Final = "local_type"
CONF_POLL_INTERVAL: Final = "poll_interval"
CONF_CAPTURE_PAYLOADS: Final = "capture_payloads"

# Upstream polls every 5 s. A poll may be an LED write if getState gives no readings,
# and whether the firmware persists that write is unknown, so the default is slower.
DEFAULT_POLL_INTERVAL: Final = 10
MIN_POLL_INTERVAL: Final = 5
MAX_POLL_INTERVAL: Final = 300
