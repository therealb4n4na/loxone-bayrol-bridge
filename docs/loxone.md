# Loxone-Einbindung

## Lesen

Für die normale Visualisierung:

```text
http://<DIETPI-IP>:8092/status
```

Die wichtigsten Felder sind je nach Controller unter anderem pH, Redox, Temperatur, `online`, `valid`, `updated`/Zeitstempel und `error`.

Empfohlenes Polling: etwa 60 Sekunden. Der eigentliche BAYROL-Poller läuft in einem deutlich langsameren Takt; häufigeres Loxone-Polling erzeugt daher keine frischeren Cloud-Daten.

## Schreiben

pH-Automatik:

```text
http://<DIETPI-IP>:8092/api/v1/ph/auto
```

pH-Dosierung aus:

```text
http://<DIETPI-IP>:8092/api/v1/ph/off
```

Nur die freigegebene Steuer-IP darf diese Endpunkte verwenden. Ein Browser auf einem anderen Rechner darf weiterhin `/status` lesen, aber nicht schalten.

## Statuslogik

`online=1` und `valid=1` sollten gemeinsam betrachtet werden. Zusätzlich ist das Alter des letzten Updates relevant. Ein alter, aber formal gültiger Wert sollte nicht unbegrenzt als aktueller Ist-Wert weiterverwendet werden.
