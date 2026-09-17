# Loxone BAYROL Bridge

<!-- project-meta -->
> **Status:** Stable · **Current release:** `v2.0.0` · **License:** MIT · **Documentation:** English · **Issues/PRs:** English preferred

[Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Loxone integration](docs/loxone.md) · [Troubleshooting](docs/troubleshooting.md) · [Project collection](https://github.com/therealb4n4na/loxone-smart-home-projects)
<!-- /project-meta -->

A bridge between Loxone and a BAYROL pool controller. The project deliberately separates **periodic pool-data polling** from **direct control commands**.

## Features

- periodically reads pool values from the BAYROL web service
- stores the most recent valid state locally in `bayrol.json`
- exposes that cached state to Loxone on port `8092`
- reads live pH and chlorine automation status through MQTT/WebSocket when requested
- can switch pH and chlorine automation between `auto` and `off`
- exposes verified pH/chlorine dosing activity and cumulative pump runtime to Loxone
- provides conservative manual water-care recommendations with a persistent two-hour post-dose automation guard
- provides a persistent winter mode that suppresses controller writes/polling while the physical BAYROL unit is intentionally powered off
- tracks estimated chemical consumption and explicit canister-change confirmations with low write load
- verifies write commands by reading the resulting state back
- distinguishes cloud/device problems from the health of the local HTTP API

## Architecture

```text
BAYROL web service
      │
      │ HTTPS, periodic polling
      ▼
bayrol_bridge.py   <- systemd oneshot + timer
      │
      ▼
bayrol.json
      │
      ▼
bayrol_api.py :8092 ──────> Loxone / browser
      │
      └─ MQTT/WebSocket ──> pH status / pH auto / pH off
```

This separation matters: `/status` is fast and does not require a fresh cloud connection. Direct pH operations intentionally establish a live connection to the BAYROL system.

## Requirements

- Linux; developed and tested on DietPi / Debian
- Python 3
- Python packages `requests`, `beautifulsoup4`, and `paho-mqtt`
- valid BAYROL web access
- MQTT device information for pH control

Example virtual environment:

```bash
python3 -m venv venv
./venv/bin/pip install requests beautifulsoup4 paho-mqtt
```

## Configuration

Real credentials must **never** be committed to Git.

Templates:

- [`config.example.json`](config.example.json) for web access
- [`mqtt.example.json`](mqtt.example.json) for MQTT

Create local copies named:

```text
config.json
mqtt.json
```

Both are excluded by `.gitignore`.

`mqtt.json` also contains `write_client_ip`, which defines the only remote IP allowed to trigger pH write operations (`/auto` and `/off`), typically the Loxone Miniserver. Read-only status requests are unaffected.

## Services

The project uses two systemd components.

### `bayrolbridge.service` + `bayrolbridge.timer`

The poller service is a **oneshot** unit. Seeing it as `inactive` between runs is therefore normal. The important part is that the timer is active and recent service runs completed successfully.

```bash
systemctl status bayrolbridge.timer
systemctl status bayrolbridge.service
journalctl -u bayrolbridge.service -n 100 --no-pager
```

### `bayrolbridge-api.service`

This is the long-running HTTP service on port `8092`.

## HTTP API

### Cached pool status

```text
GET http://<HOST>:8092/status
```

This endpoint only reads `bayrol.json`. HTTP 200 therefore does not prove that the BAYROL cloud is reachable at that exact moment. Also evaluate fields such as `online`, `valid`, timestamps, and `error`.

### Local API health

```text
GET http://<HOST>:8092/health
```

This checks only whether the local API process is alive.

### Live pH status

```text
GET http://<HOST>:8092/api/v1/ph/status
```

The current pH dosing state is queried through MQTT/WebSocket.

### Enable automatic pH dosing

```text
GET http://<HOST>:8092/api/v1/ph/auto
```

### Disable pH dosing

```text
GET http://<HOST>:8092/api/v1/ph/off
```

### Chlorine automation

```text
GET http://<HOST>:8092/api/v1/chlorine/status
GET http://<HOST>:8092/api/v1/chlorine/auto
GET http://<HOST>:8092/api/v1/chlorine/off
```

The pH/chlorine write endpoints are restricted to the configured controller IP and localhost. Other clients receive HTTP `403`. During an active post-dose guard, `auto` requests are rejected with HTTP `409`; explicit `off` remains allowed.

### Winter mode

```text
GET http://<HOST>:8092/api/v1/winter
GET http://<HOST>:8092/api/v1/winter/on?confirm=1
GET http://<HOST>:8092/api/v1/winter/off?confirm=1
```

Winter mode is persistent runtime state. While it is active, the cloud poller exits successfully without querying BAYROL, controller write operations are rejected, manual-dose recommendations are suppressed, and an existing post-dose guard is cancelled so it cannot restore automation on a physically powered-off unit. The local API remains healthy and continues to expose `winter_mode=1` / `care_state_code=6` for Loxone and monitoring.

### Loxone / water-care status

```text
GET http://<HOST>:8092/api/v1/loxone
GET http://<HOST>:8092/api/v1/water-care
GET http://<HOST>:8092/api/v1/chemicals
```

`/api/v1/loxone` is the consolidated endpoint. Water-care recommendations are suppressed when cached measurements are stale/invalid or circulation is not confirmed. pH correction has priority over chlorine. A manual-dose acknowledgement starts a persistent two-hour automation guard: pH and chlorine automation are switched off, kept off during the guard, and only channels that were previously in `auto` are restored afterwards.

Canister changes are deliberately explicit write operations and never inferred from an empty-probe alarm:

```text
GET /api/v1/chemicals/ph/change?confirm=1
GET /api/v1/chemicals/chlorine/change?confirm=1
GET /api/v1/water-care/ack?confirm=1
```

These write endpoints use the same source-IP restriction as pH control. Chemical consumption is currently marked as an estimate because the configured 2.4 l/h pump delivery has not yet been calibrated on the installed pumps. The pH-Minus canister is verified as 20 l; the chlorine canister volume remains unset until the 25 kg product's actual volume/density is verified. Runtime state is stored below `runtime/` and excluded from Git. Raw MQTT JSONL capture is disabled during normal operation.

## Security model

- secrets exist only in local configuration files
- credentials, captures, runtime state, and backups are not versioned
- read-only diagnostics can remain available inside the trusted LAN
- HTTP write endpoints are additionally restricted to the configured controller source
- a write is considered successful only after the requested target state has been confirmed

## Loxone

For normal visualization, Loxone should read `/api/v1/loxone`. Use the dedicated write endpoints for pH/chlorine automation, canister confirmations, manual-dose acknowledgement and winter mode.

Details: [`docs/loxone.md`](docs/loxone.md).

## Reverse-engineering note

Parts of the integration are based on observed BAYROL web/MQTT behavior. Findings should therefore be classified as:

- **Verified** – repeatedly confirmed on real hardware
- **Experimental** – plausible and tested, but not yet sufficiently confirmed
- **Unknown** – observed behavior with unclear meaning

Raw captures should not be published when they may contain tokens, sessions, or device/account-specific information.

## Troubleshooting

See [`docs/troubleshooting.md`](docs/troubleshooting.md).

## License

MIT License – see [`LICENSE`](LICENSE).
