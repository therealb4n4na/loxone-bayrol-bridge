# Loxone integration

## Reading values

Use the consolidated endpoint for the current BAYROL/Loxone integration:

```text
http://<DIETPI-IP>:8092/api/v1/loxone
```

It exposes pH, redox, temperature, connection/validity, dosing state, pH/chlorine automation state, chemical tracking, manual-dose recommendations and the post-dose automation guard. The cached `/status` endpoint remains available for the slower BAYROL web-service poller.

A polling interval of roughly 60 seconds is usually sufficient.

## Writing automation states

pH automation:

```text
http://<DIETPI-IP>:8092/api/v1/ph/auto
http://<DIETPI-IP>:8092/api/v1/ph/off
```

Chlorine automation:

```text
http://<DIETPI-IP>:8092/api/v1/chlorine/auto
http://<DIETPI-IP>:8092/api/v1/chlorine/off
```

Only the configured controller IP may call these endpoints. The bridge confirms the resulting BAYROL state before reporting success.

## Manual-dose guard

The existing manual-dose confirmation remains:

```text
http://<DIETPI-IP>:8092/api/v1/water-care/ack?confirm=1
```

After confirmation the bridge reads the current pH/chlorine automation states, switches both channels off and starts a persistent two-hour guard. During the guard, attempts to enable automation through the bridge are rejected and the background guard re-enforces `off` if necessary. When the two hours expire, only channels that were in `auto` before the confirmation are restored. The guard survives a bridge restart because its state is stored below `runtime/`.

Read guard state at:

```text
http://<DIETPI-IP>:8092/api/v1/dosing-guard
```

Useful flat fields in `/api/v1/loxone` are `ph_auto_state_code`, `chlorine_auto_state_code`, `dosing_guard_state_code`, `dosing_guard_active` and `dosing_guard_remaining_min`.

## Status logic

Evaluate `online=1` and `valid=1` together. A post-dose guard is an intentional operating state, not a controller fault. `dosing_guard_state_code=1` means the timed guard is active; `2` means the timer has expired but automatic restoration is still pending or failed and will be retried.
