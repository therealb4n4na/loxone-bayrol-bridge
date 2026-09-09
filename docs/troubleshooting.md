# Troubleshooting

## API is running but data is old

This can happen because `bayrolbridge-api` and the periodic poller are separate components.

Check:

```bash
systemctl status bayrolbridge-api.service
systemctl status bayrolbridge.timer
systemctl status bayrolbridge.service
journalctl -u bayrolbridge.service -n 100 --no-pager
```

Then inspect `/status` for `online`, `valid`, timestamps, and `error`.

## `bayrolbridge.service` is inactive

That is normal for the intended oneshot/timer architecture. The service starts, performs one poll, and exits. `bayrolbridge.timer` is the component that should remain active.

## Live pH status does not work

```bash
curl -sS http://127.0.0.1:8092/api/v1/ph/status
journalctl -u bayrolbridge-api.service -n 100 --no-pager
```

Typical causes include missing or expired MQTT device information, network problems, or missing MQTT responses.

## HTTP 403 on pH auto/off

This is expected when the request does not originate from the configured controller IP. Read-only endpoints can still work.

## After changes

```bash
python3 -m py_compile bayrol_bridge.py bayrol_api.py
sudo systemctl restart bayrolbridge-api.service
sudo systemctl start bayrolbridge.service
```

Then verify `/status`, `/health`, and, if needed, `/api/v1/ph/status`.
