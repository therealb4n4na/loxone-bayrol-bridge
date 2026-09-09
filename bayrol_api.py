#!/usr/bin/env python3
"""
BAYROL HTTP/MQTT API fuer Loxone
================================

Zweck
-----
Dieser dauerhaft laufende Dienst stellt auf TCP 8092 eine kleine HTTP-API
bereit. Er hat zwei getrennte Aufgaben:

1. /status liefert NUR die zuletzt von bayrol_bridge.py erzeugte Datei
   /opt/bayrolbridge/bayrol.json. Dafuer ist keine neue Cloud-Verbindung noetig.
2. /api/v1/ph/* spricht den BAYROL-Controller direkt ueber MQTT/WebSocket an,
   um den pH-Dosiermodus zu lesen oder zwischen "auto" und "off" zu schalten.

Wichtige Endpunkte
------------------
/health              -> prueft nur, ob dieser HTTP-Dienst selbst antwortet
/status              -> letzter gecachter Poolstatus aus bayrol.json
/api/v1/ph/status    -> pH-Dosierstatus live per MQTT lesen
/api/v1/ph/auto      -> pH-Dosierung einschalten; Zielzustand wird bestaetigt
/api/v1/ph/off       -> pH-Dosierung ausschalten; Zielzustand wird bestaetigt

Code-Leseplan / Fehlersuche
---------------------------
load_config()        -> liest MQTT-Geraetetoken und Topic aus mqtt.json
mqtt_client()        -> baut den TLS-WebSocket-MQTT-Client auf
query_ph_status()    -> Live-Abfrage des pH-Dosierstatus
set_ph_mode()        -> schreibt einen neuen Modus und wartet auf Bestaetigung
Handler.do_GET()     -> Zuordnung der HTTP-URLs zu den Funktionen oben
main()               -> prueft die Konfiguration und startet HTTP auf Port 8092

Wichtig bei Fehlern
-------------------
- systemd "active" bedeutet nur: der HTTP-Prozess laeuft.
- /health=200 beweist NICHT, dass BAYROL-Cloud/MQTT erreichbar ist.
- /status kann korrekt antworten, obwohl die Cloud gerade offline ist, weil es
  nur die zuletzt gespeicherte bayrol.json ausliefert. Fuer Aktualitaet immer
  updated/last_attempt/valid/online in dieser Datei beachten.
- Fehler unter /api/v1/ph/* entstehen typischerweise beim MQTT-Verbindungsaufbau,
  beim fehlenden Status-Reply oder wenn der geschriebene Zielzustand nicht
  bestaetigt wird. Diese Fehler werden als HTTP 503 und JSON "error" geliefert.
- Schreibend sind nur /api/v1/ph/auto und /api/v1/ph/off. Sie akzeptieren
  ausschließlich die konfigurierte Steuer-IP (typisch Loxone) sowie localhost;
  andere Clients erhalten HTTP 403.
- Zugangsdaten stehen NICHT in diesem Script, sondern in mqtt.json.

Dieses Script bewusst getrennt von bayrol_bridge.py betrachten: Port 8092 ist
API/Steuerung; der zyklische Cloud-Poll ist ein eigener oneshot+timer Dienst.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import paho.mqtt.client as mqtt

CONFIG = Path("/opt/bayrolbridge/mqtt.json")
STATUS_PATH = Path("/opt/bayrolbridge/bayrol.json")
HOST = "www.bayrol-poolaccess.de"
PORT = 8083

PH_ITEM = "5.42"
PH_VALUES = {
    "auto": "19.17",
    "off": "19.18",
}
PH_LABELS = {
    "19.17": "auto",
    "19.18": "off",
}

API_HOST = "0.0.0.0"
API_PORT = 8092

# Schreibbefehle dürfen nur von der in mqtt.json konfigurierten Steuer-IP
# (typisch: Loxone Miniserver) oder lokal vom DietPi kommen. Dadurch enthält
# der öffentliche Code keine installationsspezifische LAN-Adresse.
def load_write_client_ip():
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        value = str(data.get("write_client_ip", "")).strip()
        if value:
            return value
    except Exception:
        pass
    return "192.168.1.50"


WRITE_CLIENT_IP = load_write_client_ip()


def load_config():
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    token = str(data.get("device_token", "")).strip()
    device = str(data.get("device_topic", "")).strip()
    if not token or not device:
        raise RuntimeError("device_token oder device_topic fehlt in mqtt.json")
    return token, device


def mqtt_client(token):
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="user_" + secrets.token_hex(4),
        protocol=mqtt.MQTTv311,
        transport="websockets",
    )
    client.username_pw_set(token, "*")
    client.ws_set_options(path="/")
    client.tls_set()
    return client


def query_ph_status(timeout=8.0):
    token, device = load_config()
    value_topic = f"d02/{device}/v/{PH_ITEM}"
    get_topic = f"d02/{device}/g/{PH_ITEM}"

    connected = threading.Event()
    received = threading.Event()
    result = {"value": None, "raw": None, "error": None}

    client = mqtt_client(token)

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code != 0:
            result["error"] = f"MQTT connect reason={reason_code}"
            connected.set()
            return
        c.subscribe(value_topic, qos=0)
        time.sleep(0.15)
        c.publish(get_topic, payload=b"", qos=0, retain=False)
        connected.set()

    def on_message(c, userdata, msg):
        try:
            raw = msg.payload.decode("utf-8", errors="replace")
            data = json.loads(raw)
            result["raw"] = data
            result["value"] = str(data.get("v", ""))
            received.set()
        except Exception as exc:
            result["error"] = str(exc)
            received.set()

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(HOST, PORT, keepalive=30)
        client.loop_start()

        if not connected.wait(timeout):
            raise RuntimeError("Timeout beim MQTT-Verbindungsaufbau")
        if result["error"]:
            raise RuntimeError(result["error"])
        if not received.wait(timeout):
            raise RuntimeError("Keine Statusantwort von BAYROL erhalten")

        value = result["value"]
        return {
            "ok": 1,
            "item": PH_ITEM,
            "value": value,
            "mode": PH_LABELS.get(value, "unknown"),
            "raw": result["raw"],
        }
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        client.loop_stop()


def set_ph_mode(mode, timeout=10.0):
    if mode not in PH_VALUES:
        raise ValueError("mode muss auto oder off sein")

    wanted = PH_VALUES[mode]
    token, device = load_config()

    value_topic = f"d02/{device}/v/{PH_ITEM}"
    get_topic = f"d02/{device}/g/{PH_ITEM}"
    set_topic = f"d02/{device}/s/{PH_ITEM}"

    connected = threading.Event()
    initial = threading.Event()
    confirmed = threading.Event()

    state = {
        "before": None,
        "after": None,
        "published": False,
    }

    client = mqtt_client(token)

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code != 0:
            connected.set()
            return
        c.subscribe(value_topic, qos=0)
        time.sleep(0.15)
        c.publish(get_topic, payload=b"", qos=0, retain=False)
        connected.set()

    def on_message(c, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8", errors="replace"))
            value = str(data.get("v", ""))
        except Exception:
            return

        if not state["published"]:
            state["before"] = value
            initial.set()
            return

        if value == wanted:
            state["after"] = value
            confirmed.set()

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(HOST, PORT, keepalive=30)
        client.loop_start()

        if not connected.wait(timeout):
            raise RuntimeError("Timeout beim MQTT-Verbindungsaufbau")
        if not initial.wait(timeout):
            raise RuntimeError("Aktueller Dosierstatus konnte nicht gelesen werden")

        if state["before"] == wanted:
            return {
                "ok": 1,
                "changed": 0,
                "confirmed": 1,
                "mode": mode,
                "value": wanted,
                "message": "Gewünschter Zustand war bereits aktiv",
            }

        payload = json.dumps({"t": PH_ITEM, "v": wanted}, separators=(",", ":"))
        state["published"] = True
        info = client.publish(set_topic, payload=payload, qos=0, retain=False)
        info.wait_for_publish(timeout=5)

        if not confirmed.wait(timeout):
            raise RuntimeError("Befehl gesendet, aber BAYROL hat den Zielzustand nicht bestätigt")

        return {
            "ok": 1,
            "changed": 1,
            "confirmed": 1,
            "mode": mode,
            "value": wanted,
            "previous_value": state["before"],
            "message": "Zustand von BAYROL bestätigt",
        }
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        client.loop_stop()


class Handler(BaseHTTPRequestHandler):
    server_version = "BayrolBridgeAPI/2.0"

    def _json(self, status, payload):
        body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _write_allowed(self):
        return self.client_address[0] in {
            WRITE_CLIENT_IP,
            "127.0.0.1",
            "::1",
        }

    def _forbidden(self):
        self._json(403, {
            "ok": 0,
            "error": "write_access_denied",
            "client_ip": self.client_address[0],
        })

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")

        try:
            if path in ("", "/"):
                self._json(200, {
                    "service": "bayrolbridge-api",
                    "version": 2,
                    "endpoints": [
                        "/status",
                        "/api/v1/ph/status",
                        "/api/v1/ph/auto",
                        "/api/v1/ph/off",
                        "/health"
                    ]
                })
                return

            if path == "/health":
                self._json(200, {"ok": 1, "service": "bayrolbridge-api"})
                return

            if path == "/status":
                if not STATUS_PATH.exists():
                    self._json(503, {"ok": 0, "error": "status file not available"})
                    return

                with STATUS_PATH.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)

                self._json(200, payload)
                return

            if path == "/api/v1/ph/status":
                self._json(200, query_ph_status())
                return

            if path == "/api/v1/ph/auto":
                if not self._write_allowed():
                    self._forbidden()
                    return
                self._json(200, set_ph_mode("auto"))
                return

            if path == "/api/v1/ph/off":
                if not self._write_allowed():
                    self._forbidden()
                    return
                self._json(200, set_ph_mode("off"))
                return

            self._json(404, {"ok": 0, "error": "not found"})

        except ValueError as exc:
            self._json(400, {"ok": 0, "error": str(exc)})
        except Exception as exc:
            self._json(503, {"ok": 0, "error": str(exc)})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main():
    # Fail early if config is missing/broken.
    load_config()
    server = ThreadingHTTPServer((API_HOST, API_PORT), Handler)
    print(f"BAYROL Bridge API v2 lauscht auf {API_HOST}:{API_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
