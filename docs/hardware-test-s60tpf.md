# S60TPF hardware test checklist

Nothing in this integration has run on a real plug. Work through this in order: each
step tests one assumption, so the first failure points at the cause.

Before you start, note each plug's **firmware** from the eWeLink app, and turn on
**Capture payloads** in the integration's options straight after adding it.

Use a plug you can afford to lose control of for a while, with a simple load such as
a lamp. Avoid anything that must stay on while testing.

## 1. What the plug announces

```bash
avahi-browse -rt _ewelink._tcp
```

- [ ] The plug is listed as `eWeLink_<deviceid>`
- [ ] TXT has `type=plug`, `encrypt=true`, `iv` and `data1`
- [ ] An IP address is shown. If not, this unit has the missing-address fault
      (SonoffLAN #1871), and steps 2 and 10 will differ

## 2. Adding it

- [ ] It appears as discovered. If step 1 showed no address, expect it not to, and
      add it by hand instead
- [ ] A **wrong** devicekey is refused with "does not decrypt"
- [ ] The right devicekey is accepted
- [ ] By hand, leaving the key empty gives "a devicekey is required"

## 3. Readings and scaling

- [ ] Switch, power, current, voltage, energy today and energy this month all appear
- [ ] Voltage is near your mains value. A reading near 23000 or 2.3 means the scale
      is wrong
- [ ] Power with a known load (for example a 10 W lamp) is about right
- [ ] Current is roughly power ÷ voltage, allowing for power factor
- [ ] Values agree with the eWeLink app
- [ ] Are week or year sensors present? Expected: no. Tell me if they are

## 4. Relay

- [ ] Turning on and off in Home Assistant clicks the relay and the load follows
- [ ] The toggle stays put and does not flip back briefly
- [ ] Pressing the physical button updates Home Assistant, and note how quickly
- [ ] Switching from the eWeLink app updates Home Assistant
- [ ] Ten fast toggles in a row all work, with no errors in the log

## 5. Poll method

Download diagnostics and look at the `device` section.

- [ ] Record `poll_command`. `getState` is the good result, as polling makes no writes.
      `sledOnline` means every poll is an LED write. `null` means no method returned
      readings
- [ ] `seconds_since_metrics` stays near or below the poll interval
- [ ] The plug's LED setting has not changed

## 6. Metrics stay live

This is the failure SonoffLAN users hit most, so check it properly.

- [ ] Power follows when the load switches on and off, within one poll interval
- [ ] Power falls to about 0 and **stays live** with no load. Change the load again
      after 10 minutes at 0 W, and confirm it updates. A frozen value is a failure
- [ ] Voltage keeps moving slightly over time

## 7. No cloud dependency

Block the plug's internet access at your router, leaving the LAN working. This is the
main promise of the project and it has never been tested.

- [ ] After 10 minutes, the relay still switches from Home Assistant
- [ ] Power, voltage and current still update
- [ ] After power-cycling the plug with internet still blocked, it comes back and works

## 8. Energy

- [ ] Energy today rises with a sustained load
- [ ] It resets at midnight **your** time. SonoffLAN users reported a day counter
      stalling until after noon (#1759), so check the time it actually resets
- [ ] Energy dashboard shows no large phantom spike around the reset
- [ ] Energy this month rises and is consistent with energy today

## 9. Availability

- [ ] Unplug it: entities go unavailable within about 30 s at the default interval
- [ ] Plug it back in: entities recover by themselves
- [ ] The relay state after recovery is correct

## 10. Address changes

- [ ] Change the plug's IP (for example, a new DHCP reservation and a power cycle).
      A plug that sends address records should be followed automatically
- [ ] For a plug with no address records, confirm **Reconfigure** recovers it

## 11. Soak test

Leave it running for at least 24 hours at the default 10 s poll.

- [ ] The relay stays controllable, with no plug going unresponsive. One user reported
      S60TPF trouble soon after 5 s polling appeared in SonoffLAN
- [ ] No 0 V readings while the relay is on. Note any while it is off (#1518)
- [ ] With several plugs, all stay available at once
- [ ] Nothing repeats in the log

## 12. Pairing again

- [ ] Re-pair a plug in eWeLink. If the key changes, Home Assistant asks for a new one
      within a few minutes. Record whether the key changed

## 13. Optional and destructive: DIY mode

Entering DIY mode clears the plug's settings and removes it from eWeLink, and it may
not exist on this model. Only try this on a spare plug.

- [ ] Does the S60TPF offer DIY mode at all?
- [ ] If so, what does its TXT `type` say, and is `encrypt` false?
- [ ] Do the relay and metrics work without a key?

## Send back

Attach the diagnostics file from step 6 or later, plus your firmware version and which
steps failed. Keys and addresses are removed from it, but read it before posting.

The most useful answers: `poll_command`, the keys that appear in `raw_metrics`, whether
`seconds_since_metrics` stays low, and whether step 7 passes.
