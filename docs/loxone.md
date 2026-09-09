# Loxone integration

## Reading values

For normal visualization:

```text
http://<DIETPI-IP>:8092/status
```

Depending on the controller, useful fields include pH, redox, temperature, `online`, `valid`, update timestamp, and `error`.

A polling interval of roughly 60 seconds is usually sufficient. The BAYROL poller itself runs at a slower interval, so faster Loxone polling does not create fresher cloud data.

## Writing values

Enable automatic pH dosing:

```text
http://<DIETPI-IP>:8092/api/v1/ph/auto
```

Disable pH dosing:

```text
http://<DIETPI-IP>:8092/api/v1/ph/off
```

Only the configured controller IP may call these endpoints. A browser on another trusted computer can still read `/status` without being allowed to switch dosing modes.

## Status logic

Evaluate `online=1` and `valid=1` together, and also check the age of the most recent update. A formally valid but old cached value should not be treated as current indefinitely.
