# Loxone BAYROL Bridge

Lokale Bridge zwischen Loxone und einem BAYROL Pool-Controller. Das Projekt trennt bewusst das **zyklische Lesen der Poolwerte** von **direkten Steuerbefehlen**.

## Was das Projekt macht

- liest Pooldaten zyklisch aus dem BAYROL-Webzugang
- speichert den letzten gültigen Zustand lokal in `bayrol.json`
- stellt diesen Zustand auf Port `8092` für Loxone bereit
- liest den pH-Dosierstatus bei Bedarf live über MQTT/WebSocket
- kann die pH-Dosierung kontrolliert zwischen `auto` und `off` schalten
- bestätigt einen Schreibbefehl durch Rücklesen des Zustands
- trennt Cloud-/Gerätefehler vom Zustand des lokalen HTTP-Dienstes

## Architektur

```text
BAYROL Webzugang
      │
      │ HTTPS, zyklisch
      ▼
bayrol_bridge.py   <- systemd oneshot + Timer
      │
      ▼
bayrol.json
      │
      ▼
bayrol_api.py :8092 ──────> Loxone / Browser
      │
      └─ MQTT/WebSocket ──> pH-Status / pH auto / pH off
```

Die Trennung ist wichtig: `/status` ist schnell und benötigt keine neue Cloud-Verbindung. Ein direkter pH-Befehl baut dagegen bewusst eine Live-Verbindung zum BAYROL-System auf.

## Voraussetzungen

- Linux, getestet mit DietPi/Debian
- Python 3
- Python-Pakete `requests`, `beautifulsoup4`, `paho-mqtt`
- gültiger BAYROL-Webzugang
- für pH-Steuerung zusätzlich die zum Gerät gehörenden MQTT-Informationen

Beispiel:

```bash
python3 -m venv venv
./venv/bin/pip install requests beautifulsoup4 paho-mqtt
```

## Konfiguration

Echte Zugangsdaten gehören **nie** ins Git-Repository.

Vorlagen:

- [`config.example.json`](config.example.json) für den Webzugang
- [`mqtt.example.json`](mqtt.example.json) für MQTT

Lokal werden daraus erzeugt:

```text
config.json
mqtt.json
```

Diese Dateien stehen in `.gitignore`.

`mqtt.json` enthält zusätzlich `write_client_ip`. Dort wird die einzige entfernte IP eingetragen, die pH-Schreibbefehle (`/auto` und `/off`) auslösen darf – typischerweise der Loxone Miniserver. Statusabfragen bleiben davon unberührt.

## Dienste

Das Projekt besteht aus zwei systemd-Komponenten:

### `bayrolbridge.service` + `bayrolbridge.timer`

Der Service ist ein **oneshot**. Deshalb ist `inactive` zwischen zwei Durchläufen normal. Entscheidend ist, dass der Timer aktiv ist und die letzten Läufe erfolgreich waren.

Beispielprüfung:

```bash
systemctl status bayrolbridge.timer
systemctl status bayrolbridge.service
journalctl -u bayrolbridge.service -n 100 --no-pager
```

### `bayrolbridge-api.service`

Dieser Dienst läuft dauerhaft und stellt Port `8092` bereit.

## HTTP-API

### Letzter gecachter Poolstatus

```text
GET http://<HOST>:8092/status
```

Dieser Aufruf liest nur `bayrol.json`. HTTP 200 bedeutet daher nicht automatisch, dass die BAYROL-Cloud in diesem Moment erreichbar ist. Für die Datenqualität zusätzlich `online`, `valid`, Zeitstempel und `error` auswerten.

### API-Health

```text
GET http://<HOST>:8092/health
```

Prüft nur, ob der lokale API-Prozess lebt.

### pH-Status live

```text
GET http://<HOST>:8092/api/v1/ph/status
```

Hier wird der aktuelle Zustand über MQTT/WebSocket abgefragt.

### pH-Automatik einschalten

```text
GET http://<HOST>:8092/api/v1/ph/auto
```

### pH-Dosierung ausschalten

```text
GET http://<HOST>:8092/api/v1/ph/off
```

Die beiden Schreibendpunkte sind zusätzlich auf die konfigurierte Loxone-/Steuer-IP und localhost beschränkt. Andere Clients erhalten HTTP `403`.

## Sicherheitsmodell

- Secrets ausschließlich in lokalen JSON-Dateien
- Secrets, Captures, Runtime-Status und Backups werden nicht versioniert
- lesende Diagnose bleibt im erlaubten LAN verfügbar
- schreibende HTTP-Endpunkte sind zusätzlich auf die Steuerquelle begrenzt
- ein Schreibvorgang gilt erst nach bestätigtem Zielzustand als erfolgreich

## Loxone

Für normale Visualisierung sollte Loxone `/status` verwenden. Direkte pH-Schaltbefehle nur gezielt über die beiden dafür vorgesehenen Endpunkte senden.

Details: [`docs/loxone.md`](docs/loxone.md).

## Reverse-Engineering-Hinweis

Das Projekt basiert teilweise auf beobachtetem Verhalten der BAYROL-Web-/MQTT-Kommunikation. Erkenntnisse sollten deshalb klar getrennt werden in:

- **verifiziert** – mehrfach am realen Gerät bestätigt
- **experimentell** – plausibel, aber noch nicht ausreichend bestätigt
- **unbekannt** – beobachtet, Bedeutung offen

Captures selbst werden nicht veröffentlicht, wenn darin Tokens, Sessions oder andere personenbezogene/gerätebezogene Daten enthalten sein können.

## Fehlersuche

Siehe [`docs/troubleshooting.md`](docs/troubleshooting.md).

## Lizenz

MIT License – siehe [`LICENSE`](LICENSE).
