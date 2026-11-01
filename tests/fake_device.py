"""A fake S60TPF for tests.

This models the device as described by Sonoff's documents and by SonoffLAN's source
and issue tracker. It is NOT a recording of real hardware. Passing tests show the code
matches this model, not that the model matches a physical plug. Each quirk below names
its evidence so it can be corrected once real captures exist.

Default state: redacted S60TPF diagnostics from SonoffLAN issue #1514.

Modelled quirks:
- Answers any LAN request with its current state (DEVICES.md comment for S60TPF).
- Rejects plain requests with error 400 and ``encrypt: true`` (issue #975), or 401 if
  ``plain_error`` says so (the documented code). Requests encrypted with the wrong key
  get the same reply. That part is a guess: no source shows what a device really does.
- Replies ``error: 0`` but ignores JSON containing spaces (PR #1884).
- Resets the connection of a request it is too busy for (SonoffLAN local.py).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.sonoff_local.protocol import (
    decrypt_bytes,
    encode_json,
    encrypt_params,
)

DEVICE_ID = "100226eeda"
# Random value generated for tests. It is not a real device's key.
DEVICE_KEY = "11111111-2222-4333-8444-555555555555"
SERVICE_NAME = f"eWeLink_{DEVICE_ID}._ewelink._tcp.local."
TXT_CHUNK = 249  # A TXT string is at most 255 bytes, including the "dataN=" prefix.


def s60_state() -> dict[str, Any]:
    """Return a typical S60TPF state."""
    return {
        "version": 8,
        "fwVersion": "1.2.0",
        "switches": [{"switch": "on", "outlet": 0}],
        "configure": [{"startup": "off", "outlet": 0}],
        "pulses": [{"pulse": "off", "switch": "off", "outlet": 0, "width": 500}],
        "sledOnline": "on",
        "ssid": "home-wifi",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "staMac": "11:22:33:44:55:66",
        "rssi": -42,
        "current": 5,
        "voltage": 23717,
        "power": 724,
        "uiActive": 60,
        "timeZone": 2,
        "dayKwh": 2,
        "monthKwh": 117,
        "operSide": 1,
    }


@dataclass
class RecordedRequest:
    """One request as the device received it."""

    command: str
    body: dict[str, Any]
    params: dict[str, Any] | None
    monotonic: float


class FakeS60TPF:
    """An HTTP server that behaves like the modelled device."""

    def __init__(
        self,
        deviceid: str = DEVICE_ID,
        devicekey: str | None = DEVICE_KEY,
        *,
        state: dict[str, Any] | None = None,
        plain_error: int = 400,
    ) -> None:
        """Create the device. ``devicekey=None`` selects unencrypted operation."""
        self.deviceid = deviceid
        self.devicekey = devicekey
        self.state = s60_state() if state is None else state
        self.plain_error = plain_error
        self.seq = 100
        self.requests: list[RecordedRequest] = []
        self.led_writes = 0
        self.reset_next = 0
        self.delay = 0.0
        # Commands that answer an error, and commands whose reply omits readings.
        self.unsupported: dict[str, int] = {}
        self.stateless_commands: set[str] = set()
        self.empty_replies = False
        self.garbage_replies = False
        # Commands refused as if unreadable, and whether every command is.
        self.key_rejected_commands: set[str] = set()
        self.rejects_everything = False
        # Commands answered with an empty HTTP 404, as T6 does for getState (#1907).
        self.http_404_commands: set[str] = set()
        # Encrypts replies with this key instead, to model a reply we cannot read.
        self.reply_key: str | None = None
        self._server: TestServer | None = None

    @property
    def port(self) -> int:
        """Return the bound port."""
        assert self._server is not None
        assert self._server.port is not None
        return self._server.port

    @property
    def host(self) -> str:
        """Return the bound host."""
        return "127.0.0.1"

    async def start(self) -> None:
        """Start serving."""
        app = web.Application()
        app.router.add_post("/zeroconf/{command}", self._handle)
        self._server = TestServer(app, host=self.host)
        await self._server.start_server()

    async def stop(self) -> None:
        """Stop serving."""
        if self._server is not None:
            await self._server.close()
            self._server = None

    def commands(self) -> list[str]:
        """Return the commands received, in order."""
        return [request.command for request in self.requests]

    def set_power(self, raw_power: int) -> None:
        """Change the power reading."""
        self.state["power"] = raw_power

    def is_on(self) -> bool:
        """Return the relay state."""
        return self.state["switches"][0]["switch"] == "on"

    def _reply(self, payload: dict[str, Any], *, encrypted: bool) -> web.Response:
        self.seq += 1
        reply: dict[str, Any] = {"seq": self.seq, "error": 0}
        if encrypted:
            reply["encrypt"] = True
            if (key := self.reply_key or self.devicekey) is not None:
                reply["data"], reply["iv"] = encrypt_params(payload, key)
        else:
            reply["data"] = payload
        return web.json_response(reply)

    def _apply(self, command: str, params: dict[str, Any]) -> None:
        if command == "switches":
            self.state["switches"] = params["switches"]
        elif command == "switch":
            self.state["switch"] = params["switch"]
        elif command == "sledOnline":
            self.led_writes += 1
            self.state["sledOnline"] = params["sledOnline"]

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        command = request.match_info["command"]
        raw = await request.read()
        body = json.loads(raw)
        params = None if self.devicekey else body.get("data")
        spaced = undecryptable = False
        if self.devicekey and body.get("encrypt"):
            try:
                plaintext = decrypt_bytes(body["data"], body["iv"], self.devicekey)
                params = json.loads(plaintext)
                spaced = encode_json(params) != plaintext
            except ValueError:
                undecryptable = True
        self.requests.append(RecordedRequest(command, body, params, time.monotonic()))

        if self.reset_next > 0:
            self.reset_next -= 1
            assert request.transport is not None
            request.transport.abort()
            return web.Response()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.garbage_replies:
            return web.Response(text="<html>nope</html>")
        if command in self.http_404_commands:
            return web.Response(status=404)

        refused = self.rejects_everything or command in self.key_rejected_commands
        if self.devicekey and (undecryptable or refused or not body.get("encrypt")):
            self.seq += 1
            return web.json_response(
                {
                    "sequence": body.get("sequence"),
                    "seq": self.seq,
                    "error": self.plain_error,
                    "encrypt": True,
                }
            )
        if body.get("deviceid") != self.deviceid:
            return web.json_response({"seq": self.seq, "error": 404})
        if command in self.unsupported:
            return web.json_response(
                {"seq": self.seq, "error": self.unsupported[command]}
            )

        if params is not None and not spaced:
            self._apply(command, params)

        encrypted = self.devicekey is not None
        if self.empty_replies:
            self.seq += 1
            return web.json_response({"seq": self.seq, "error": 0})
        if command in self.stateless_commands:
            return self._reply({}, encrypted=encrypted)
        return self._reply(self.state, encrypted=encrypted)

    def txt(self, params: dict[str, Any] | None = None) -> dict[str, str]:
        """Return TXT properties as the device would announce them."""
        payload = self.state if params is None else params
        self.seq += 1
        txt = {
            "txtvers": "1",
            "id": self.deviceid,
            "type": "plug",
            "apivers": "1",
            "seq": str(self.seq),
        }
        if self.devicekey is not None:
            blob, iv = encrypt_params(payload, self.devicekey)
            txt["encrypt"] = "true"
            txt["iv"] = iv
        else:
            blob = encode_json(payload).decode()
        chunks = [blob[i : i + TXT_CHUNK] for i in range(0, len(blob), TXT_CHUNK)]
        assert len(chunks) <= 4
        txt.update({f"data{i}": chunk for i, chunk in enumerate(chunks, start=1)})
        return txt
