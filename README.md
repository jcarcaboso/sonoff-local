# Sonoff Local

A Home Assistant custom integration that controls Sonoff Wi-Fi plugs and reads their
power metrics **directly over your LAN**. It needs no cloud account at runtime and
does not depend on any other integration.

> [!WARNING]
> **Not yet tested on real hardware.** Everything here was built from Sonoff's
> documents, from reading [SonoffLAN](https://github.com/AlexxIT/SonoffLAN), and
> against a fake device written for the tests. It has run on a real Home Assistant
> server with real mDNS, but only with that fake plug, which shows the code matches
> the model and not that the model matches a physical plug. Follow the
> [S60TPF test checklist](docs/hardware-test-s60tpf.md) before relying on it.

**Primary target:** SONOFF **S60TPF** (UIID 190), in its normal encrypted LAN mode.

## Features

- Relay on/off, and power, current, voltage, and energy today / this month sensors
- Live updates from mDNS, plus HTTP polling for readings
- Automatic detection of encrypted or plain devices from the mDNS TXT record
- Discovery, plus manual entry for devices Home Assistant cannot discover
- Entities go unavailable when a device disappears
- Redacted payload capture for reporting parsing problems

## Install

Requires Home Assistant 2026.10 or newer. CI tests 2026.10, and the test suite was also
run by hand on the 2026.11 beta.

This is an **integration**. It is not an app, which is what Home Assistant calls
add-ons since 2026.2, so it does not go in the Apps section.

### Home Assistant 2026.11 and newer: Marketplace

HACS is built into Home Assistant from 2026.11 as the Marketplace, so there is nothing
extra to install. HACS itself can no longer be installed on these versions.

1. Go to **Settings → Marketplace**, and accept the community content warning the
   first time.
2. Select **Add from link** and enter `jcarcaboso/sonoff-local`. If asked for the type,
   choose **Integration**. Adding your own repository needs a GitHub connection, which
   Home Assistant asks for. It requests no private repository or write access.
3. Open the repository's page and install it. The setup page opens. If a repair asks
   you to restart Home Assistant, do so.

This repository has no releases yet, so the Marketplace follows the `main` branch and
every new commit appears as an update.

### Home Assistant 2026.10: HACS

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/jcarcaboso/sonoff-local` with the type **Integration**.
3. Install **Sonoff Local** and restart Home Assistant.

### By hand

Copy `custom_components/sonoff_local` into the `custom_components` folder of your Home
Assistant configuration directory and restart. Then add it under **Settings → Devices &
services**. A copy made this way gets no update notifications.

## Setup

For an S60TPF you need its **device ID** and its **devicekey**. See
[Getting the devicekey](#getting-the-devicekey-for-an-s60tpf).

1. A discovered device appears under **Settings → Devices & services**. Select
   **Add**, and enter the devicekey if it asks for one.
2. If nothing is discovered, choose **Add integration → Sonoff Local** and enter the
   IP address, device ID and devicekey by hand.

Discovery can legitimately miss a plug. Some S60TPF units never send an mDNS address
record ([SonoffLAN #1871](https://github.com/AlexxIT/SonoffLAN/issues/1871)), and
Home Assistant's discovery ignores services without an address. Those units still
work once added by hand, and their announcements are still used for live state. Give
them a DHCP reservation, because their address cannot be detected. If it does change,
use **Reconfigure**.

### Options

| Option | Default | Notes |
|---|---|---|
| Poll interval | 10 s | 5 to 300 s. See [Why it polls](#why-it-polls). |
| Capture payloads | Off | See [Reporting problems](#reporting-problems). |

## Getting the devicekey for an S60TPF

The devicekey is a per-device secret that encrypts LAN traffic. Treat it like a
password. The **device ID** is the 10-character value in the eWeLink app's device
settings.

None of these methods has been tried on an S60TPF for this project.

### Option A: from the plug, in pairing mode

This is the method [SonoffLAN documents](https://github.com/AlexxIT/SonoffLAN#getting-devicekey-manually).
It needs no account but **it resets the plug's Wi-Fi pairing**, so you must add the
plug to your network again afterwards.

1. Hold the plug's button for about 5 seconds until the Wi-Fi LED shows pairing mode,
   then hold it for another 5 seconds until the LED flashes quickly. This is
   Compatible Pairing Mode. The sequence comes from sibling models' manuals, as no
   S60TPF manual was found, so check yours.
2. Join the Wi-Fi network `ITEAD-XXXXXXXX` with the password `12345678`.
3. Open `http://10.10.7.1/device`.
4. Copy `deviceid` and `apikey`. That `apikey` is the devicekey.
5. Pair the plug through the eWeLink app as usual.

Whether current S60TPF firmware serves this page is unconfirmed. If it does not, use
option B.

### Option B: from your eWeLink account, once

This keeps the plug paired and does not touch it. It uses the eWeLink cloud once, on
your computer. The integration itself never talks to the cloud.

Create an application at the [eWeLink developer center](https://dev.ewelink.cc/) to
get your own AppID and AppSecret. Applications there are limited to brands such as
SONOFF, according to the [ewelink-api-next](https://github.com/nocmt/ewelink-api-next)
FAQ, which would cover this plug. Then run:

```python
"""Print the devicekey of each device on an eWeLink account."""

import base64
import getpass
import hashlib
import hmac
import json
import os
import urllib.request

BASE = os.environ.get("EWELINK_API", "https://eu-apia.coolkit.cc")  # eu, us, as, cn
APP_ID = os.environ["EWELINK_APP_ID"]
APP_SECRET = os.environ["EWELINK_APP_SECRET"]


def call(path, headers, data=None):
    request = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        reply = json.load(response)
    if reply["error"] != 0:
        raise SystemExit(f"API error {reply['error']}: {reply.get('msg')}")
    return reply["data"]


body = json.dumps(
    {
        "email": input("eWeLink email: "),
        "password": getpass.getpass("eWeLink password: "),
        "countryCode": input("Country code, for example +34: "),
    }
).encode()
digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).digest()
login = call(
    "/v2/user/login",
    {
        "Authorization": "Sign " + base64.b64encode(digest).decode(),
        "Content-Type": "application/json",
        "X-CK-Appid": APP_ID,
    },
    body,
)
things = call(
    "/v2/device/thing?num=0",
    {"Authorization": "Bearer " + login["at"], "X-CK-Appid": APP_ID},
)
for item in things["thingList"]:
    device = item["itemData"]
    if "deviceid" in device:
        print(
            device["deviceid"],
            device.get("productModel"),
            device.get("devicekey"),
            device.get("name"),
        )
```

Save it outside this repository and set `EWELINK_APP_ID`, `EWELINK_APP_SECRET` and,
if your account is not in the EU region, `EWELINK_API`. Its output contains secrets,
so do not paste it anywhere public.

Whether pairing a plug again changes its key is unconfirmed. If the key stops working,
Home Assistant asks for a new one.

## How it talks to an S60TPF

These details come from SonoffLAN's source at commit `08135f0`, and from diagnostics
its users posted. They are not from Sonoff, which does not document this protocol.

| Purpose | Request | Evidence |
|---|---|---|
| Relay | `POST /zeroconf/switches` with `{"switches":[{"outlet":0,"switch":"on"}],"operSide":1}` | `XSwitchPOWR3` in `switch.py` |
| Read state | `POST /zeroconf/getState`, else `POST /zeroconf/sledOnline` re-sending the current value | [DEVICES.md](https://github.com/AlexxIT/SonoffLAN/blob/master/DEVICES.md), `update_local` |

The documented DIY `/zeroconf/switch` command is not what UIID 190 uses.

Requests carry `sequence`, `deviceid` and `selfApikey: "123"`. Encrypted requests add
`encrypt: true` and `iv`, and `data` is base64 AES-128-CBC with PKCS7 padding, keyed
with `md5(devicekey)`. JSON must be compact, because some firmware accepts JSON with
spaces and then does nothing
([#1884](https://github.com/AlexxIT/SonoffLAN/pull/1884)).

### Metric mapping

| LAN key | Sensor | Unit | Raw example | Scale | Result |
|---|---|---|---|---|---|
| `power` | Power | W | `724` | × 0.01 | 7.24 W |
| `current` | Current | A | `5` | × 0.01 | 0.05 A |
| `voltage` | Voltage | V | `23717` | × 0.01 | 237.17 V |
| `dayKwh` | Energy today | kWh | `2` | × 0.01 | 0.02 kWh |
| `monthKwh` | Energy this month | kWh | `117` | × 0.01 | 1.17 kWh |
| `rssi` | Signal strength (disabled by default) | dBm | `-42` | none | -42 dBm |

The examples are from real S60TPF diagnostics in
[#1514](https://github.com/AlexxIT/SonoffLAN/issues/1514). That report's limits
support the scale: `voltage` max 27700 is 277 V, `current` max 1700 is 17 A, and
`actPow` max 400000 is 4000 W.

There is **no week counter** for UIID 190, and no lifetime counter. `weekKwh` and
`yearKwh` are handled for other models, and only appear if a device sends them. For a
lifetime total, add a Riemann sum helper on the power sensor, or use the month sensor
in the Energy dashboard.

Whether every firmware sends the energy keys over the LAN is unconfirmed: at least one
SonoffLAN user saw energy as unknown in local mode. Sensors are created when a key
first appears, so a missing key means a missing sensor.

### Why it polls

An S60TPF does not stream its readings to the LAN on its own, as far as is known:

- SonoffLAN's README says the plug sends power data **to the cloud** when the day
  energy counter rises. That is not a LAN push, and it stops at 0 W.
- eWeLink blocked cloud power, current and voltage updates for third parties in 2026
  ([#1707](https://github.com/AlexxIT/SonoffLAN/issues/1707)), which leaves local
  polling as the only route.
- Announcements may carry relay state only, so one arriving never delays a poll.

There is no real "get state" command in this protocol. SonoffLAN polls by re-sending
the current LED setting, because the device answers with its state. That is a **write
every few seconds**, and whether the firmware saves it to flash is unknown. So this
integration tries the read-only `getState` first, and only falls back to the LED echo
if `getState` returns no readings. The echo always re-sends the reported value, so the
LED never changes. The default interval is 10 s rather than SonoffLAN's 5 s for the
same reason.

## Supported models

**No model has been verified on real hardware**, so the Verified column is empty.

| Model | UIID | LAN type | Mode | Verified | Basis |
|---|---|---|---|---|---|
| **S60TPF** | 190 | `plug` | Encrypted | | Primary target, from SonoffLAN source and user diagnostics |
| S60TPF | 190 | unknown | DIY, unencrypted | | Best effort. No source confirms this model has DIY mode |
| POWR320D, POWCT | 190 | `plug` | Encrypted | | Same UIID as the S60TPF. POWCT supply sensors are not exposed |
| MINI, BASICR3, RFR3 | 1 | `diy_plug` | DIY | | Sonoff's DIY protocol document |
| POWR2, S31 | 32 | `enhanced_plug` | Either | | Decimal-string readings are handled, otherwise unexercised |

**Not supported:**

- **S60ZBTPF** (7032): Zigbee, cloud only over eWeLink
- **S61STPF** (276): reported to time out on every local command
- Multi-channel devices: only outlet 0 is exposed

## DIY mode

DIY mode is Sonoff's official unencrypted LAN API. The S60TPF does not need it, and it
is **not confirmed to exist on the S60TPF**. Use the devicekey path instead.

> [!CAUTION]
> Entering DIY mode **clears the device's settings and removes it from eWeLink**.

For the models in [Sonoff's guide](https://help.sonoff.tech/docs/DIY-MODE-API-PROTOCOL)
(BASICR3, RFR3, MINI, firmware 3.5.0 or newer):

1. Power on and hold the pairing button for 5 seconds. If it was paired with eWeLink,
   hold it for another 5 seconds. The LED blinks continuously in Compatible Pairing
   Mode.
2. Join `ITEAD-XXXXXXXX` with the password `12345678`.
3. Open `http://10.10.7.1/` and enter your 2.4 GHz Wi-Fi details.

The plug then appears in Home Assistant with no devicekey.

## Known S60TPF behaviour

These are reports from SonoffLAN users. This integration has not reproduced them.

- **Missing address records:** see [Setup](#setup). Reported per unit, even at the same
  firmware.
- **0 V readings:** some plugs report 0 V at times while the relay is off, with mains
  present ([#1518](https://github.com/AlexxIT/SonoffLAN/issues/1518)). They are shown
  as sent, so avoid alarming on a single 0 V reading.
- **Energy dips:** drops of up to 0.1 kWh are ignored, so a jitter is not mistaken for
  a reset ([#1759](https://github.com/AlexxIT/SonoffLAN/issues/1759)). A day that ends
  below 0.1 kWh may therefore take a little while to show its reset.
- **Polling:** one user reported some S60TPF plugs misbehaving soon after 5 s polling
  shipped in SonoffLAN, with no confirmed cause. Raise the poll interval if you see
  this, and please report it.

## Network requirements

- Home Assistant must reach TCP port 8081 on each plug, and should receive mDNS from
  it. Use host networking if you run Home Assistant in Docker.
- Wi-Fi "multicast to unicast" options can block mDNS. Disabling that on UniFi fixed
  one SonoffLAN user's power meter.
- Polling needs no mDNS once a device is added, so a plug on another VLAN should work
  if port 8081 is routable, though without live pushes. This is untested.

## Security

- **DIY mode has no authentication.** Anyone on your LAN can switch a DIY device and
  read its state. Isolate such devices on a trusted or IoT network.
- **Encrypted mode is not strong security.** The key derivation is MD5, and there is
  no message authentication, so captured commands could be replayed. Keep the plugs
  on a network you trust.
- **The devicekey is stored unencrypted** in Home Assistant's `.storage`, as with
  other integrations' credentials. Protect your backups.
- With a key configured, plaintext announcements and replies are ignored, and only
  frames that decrypt can change a device's address. So nobody on the LAN can downgrade
  or redirect it by spoofing mDNS. Spoofed traffic to a DIY device is not detectable.

## Reporting problems

1. Open the device's **Configure** dialog and enable **Capture payloads**.
2. Use the plug for a few minutes: toggle it and change the load.
3. Select **Download diagnostics** on the integration entry.

The file holds the last 200 TXT and HTTP frames, the raw readings and which poll
method worked. Keys, Wi-Fi details, MAC addresses and IP addresses are removed when a
frame is captured, and device IDs are reduced to their last 4 characters. Unknown
fields are kept, because they are the point. Please read it before posting anyway,
since a firmware field could hold something unexpected.

For a live log, add this to `configuration.yaml`. The log gets the same redaction:

```yaml
logger:
  logs:
    custom_components.sonoff_local: debug
```

## Development

```bash
uv sync --group dev
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

`protocol.py` has no Home Assistant imports, and a test enforces that.
`tests/fake_device.py` is a model of the device, and each quirk in it cites its source.
Replace those with real captures as they become available.

## Credits

The protocol knowledge comes largely from
[AlexxIT/SonoffLAN](https://github.com/AlexxIT/SonoffLAN) (MIT) and its contributors.
This integration shares no runtime code with that project. One test uses a ciphertext
vector from its test suite.
