# sonoff-local

A Home Assistant custom integration to control Sonoff devices and read their metrics **directly over the local network** — no cloud, no third-party integration.

Status: work in progress.

## Goals
- Switch control (on/off) for Sonoff switches/plugs
- Local metrics (power, current, voltage, energy where supported)
- Local discovery via mDNS (`_ewelink._tcp`)
- Config flow UI, HACS-installable

## Protocol notes
Sonoff DIY mode exposes a local HTTP API on port 8081 (POST, JSON). See the official
[DIY MODE API protocol](https://help.sonoff.tech/docs/DIY-MODE-API-PROTOCOL).
