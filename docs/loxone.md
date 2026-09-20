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

## Winter mode

Read or switch the persistent operating mode with:

```text
http://<DIETPI-IP>:8092/api/v1/winter
http://<DIETPI-IP>:8092/api/v1/winter/on?confirm=1
http://<DIETPI-IP>:8092/api/v1/winter/off?confirm=1
```

While winter mode is active, the physical controller may be completely powered off without creating a BAYROL health warning. The cloud poller is skipped, controller writes and manual-dose/canister confirmations are blocked, and the consolidated Loxone endpoint reports `winter_mode=1`, `operating_state_code=1`, `care_state_code=6` and `care_winter=1`. The compact dosing, chemical, manual-dose, pH-auto and chlorine-auto status codes also use `6` for the intentional winter state so the visualization does not show stale controller data as a fault. Leaving winter mode does not reset or recalibrate pump-flow constants; it only returns monitoring/control to normal operation.

## Status logic

Evaluate `online=1` and `valid=1` together only in normal operation. `care_state_code=6` means intentional winter mode and takes precedence over stale/offline controller data. `care_state_code=7` means BAYROL currently reports the confirmed Redox-high warning (`8.28`); the flat fields then expose `care_ok=0` and `care_redox_high=1`. The MQTT subscriber requests topic `10` (the current active alarm list) after every reconnect so an already active warning is restored immediately after a service restart. A post-dose guard is also an intentional operating state, not a controller fault. `dosing_guard_state_code=1` means the timed guard is active; `2` means the timer has expired but automatic restoration is still pending or failed and will be retried.
