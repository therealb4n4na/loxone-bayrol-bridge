# Fehlersuche

## API läuft, Daten sind aber alt

Das ist möglich, weil `bayrolbridge-api` und der zyklische Poller getrennte Komponenten sind.

Prüfen:

```bash
systemctl status bayrolbridge-api.service
systemctl status bayrolbridge.timer
systemctl status bayrolbridge.service
journalctl -u bayrolbridge.service -n 100 --no-pager
```

Danach `/status` auf `online`, `valid`, Zeitstempel und `error` prüfen.

## `bayrolbridge.service` ist inactive

Das ist bei der vorgesehenen oneshot-/Timer-Architektur normal. Der Service startet, führt einen Poll durch und beendet sich wieder. Entscheidend ist `bayrolbridge.timer`.

## pH-Status funktioniert nicht

```bash
curl -sS http://127.0.0.1:8092/api/v1/ph/status
journalctl -u bayrolbridge-api.service -n 100 --no-pager
```

Typische Ursachen sind fehlende/abgelaufene MQTT-Geräteinformationen, Netzwerkprobleme oder ausbleibende MQTT-Antworten.

## HTTP 403 bei pH auto/off

Das ist beabsichtigt, wenn der Aufruf nicht von der freigegebenen Steuer-IP kommt. Lesende Endpunkte können trotzdem funktionieren.

## Nach Änderungen

```bash
python3 -m py_compile bayrol_bridge.py bayrol_api.py
sudo systemctl restart bayrolbridge-api.service
sudo systemctl start bayrolbridge.service
```

Anschließend `/status`, `/health` und gegebenenfalls `/api/v1/ph/status` prüfen.
