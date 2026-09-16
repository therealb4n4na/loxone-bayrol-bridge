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
from urllib.parse import parse_qs, urlparse

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

# Temporärer Full-Topic-Capture für die Analyse der BAYROL-MQTT-Werte.
# Er läuft im Hintergrund desselben systemd-Dienstes und sendet selbst
# keinerlei Steuerbefehle.
CAPTURE_ENABLED = True
CAPTURE_LOG_RAW = False
CAPTURE_LOG_DIR = Path("/opt/bayrolbridge/logs")
CAPTURE_STATE = {
    "enabled": CAPTURE_ENABLED,
    "connected": False,
    "started_at": None,
    "last_message_at": None,
    "messages": 0,
    "file": None,
    "error": "",
}
CAPTURE_LOCK = threading.Lock()

# Nur durch gezielte Tests bzw. reproduzierbare Anlagenzustände bestätigte
# MQTT-Zuordnungen. Chlor-Dosierleistung, -Status und -Pumpenlaufzeit wurden
# am 2026-09-16 während eines realen Dosiervorgangs verifiziert.
LOXONE_ITEMS = ("4.89", "5.79", "4.340", "4.90", "5.168", "4.335", "5.42", "11.30", "11.31", "11.32", "11.33", "15")
LIVE_VALUES = {}
LIVE_VALUE_TS = {}
ACTIVE_ALARMS = set()
POOL_VOLUME_M3 = 45.0
PH_TARGET = 7.3
PH_HIGH_LIMIT = 7.4
REDOX_LOW_LIMIT_MV = 650.0
PH_MINUS_G_PER_10M3_PER_02 = 150.0
CHLOR_FIRST_DOSE_G_PER_10M3 = 20.0
DOSING_PUMP_MAX_LPH = 2.4
# Pumpenfoerdermenge ist noch nicht an der konkreten Anlage kalibriert.
# Deshalb daraus bis zur Verifikation keine exakten Liter ableiten.
PUMP_FLOW_CALIBRATED = False
CHEM_STATE_PATH = Path("/opt/bayrolbridge/runtime/chemical_state.json")
PH_CANISTER_L = 20.0
CHLORINE_CANISTER_L = None  # Volumen des 25-kg-Gebindes noch nicht verifiziert.
MIXING_LOCKOUT_S = 45 * 60
STATUS_MAX_AGE_S = 5 * 60
CHEM_STATE_LOCK = threading.Lock()


def _alarm_update(events):
    if not isinstance(events, list):
        return
    for event in events:
        alarm_id = str(event.get("id", ""))
        alarm_class = str(event.get("class", ""))
        if alarm_class == "20.5":
            ACTIVE_ALARMS.add(alarm_id)
        elif alarm_class == "20.3":
            ACTIVE_ALARMS.discard(alarm_id)

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


def start_mqtt_capture():
    """Startet einen rein lesenden MQTT-Logger in einem Daemon-Thread."""
    if not CAPTURE_ENABLED:
        return

    def worker():
        log_handle = None
        try:
            token, device = load_config()
            log_path = None
            if CAPTURE_LOG_RAW:
                CAPTURE_LOG_DIR.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d_%H%M%S")
                log_path = CAPTURE_LOG_DIR / f"mqtt_capture_{stamp}.jsonl"
                log_handle = log_path.open("a", encoding="utf-8", buffering=1)

            with CAPTURE_LOCK:
                CAPTURE_STATE["started_at"] = time.time()
                CAPTURE_STATE["file"] = str(log_path) if log_path else None
                CAPTURE_STATE["error"] = ""

            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id="logger_" + secrets.token_hex(4),
                protocol=mqtt.MQTTv311,
                transport="websockets",
            )
            client.username_pw_set(token, "*")
            client.ws_set_options(path="/")
            client.tls_set()

            def write_record(record):
                if log_handle is None:
                    return
                record["ts"] = time.time()
                log_handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

            def on_connect(c, userdata, flags, reason_code, properties):
                ok = reason_code == 0
                with CAPTURE_LOCK:
                    CAPTURE_STATE["connected"] = ok
                    if not ok:
                        CAPTURE_STATE["error"] = f"MQTT connect reason={reason_code}"
                write_record({"event": "connected", "reason_code": str(reason_code)})
                if ok:
                    topic_filter = f"d02/{device}/#"
                    result, mid = c.subscribe(topic_filter, qos=0)
                    write_record({
                        "event": "subscribed",
                        "topic_filter": topic_filter,
                        "result": result,
                        "mid": mid,
                    })
                    print(f"BAYROL MQTT Capture subscribed: {topic_filter}", flush=True)
                    # Nach Neustarts die bestätigten Werte aktiv anfordern.
                    for item in LOXONE_ITEMS:
                        c.publish(f"d02/{device}/g/{item}", payload=b"", qos=0, retain=False)

            def on_disconnect(c, userdata, disconnect_flags, reason_code, properties):
                with CAPTURE_LOCK:
                    CAPTURE_STATE["connected"] = False
                write_record({"event": "disconnected", "reason_code": str(reason_code)})

            def on_message(c, userdata, msg):
                raw = msg.payload.decode("utf-8", errors="replace")
                record = {
                    "event": "message",
                    "topic": msg.topic,
                    "qos": msg.qos,
                    "retain": bool(msg.retain),
                    "payload_raw": raw,
                }
                try:
                    record["payload"] = json.loads(raw)
                except Exception:
                    pass
                write_record(record)
                try:
                    item = msg.topic.rsplit("/", 1)[-1]
                    payload = record.get("payload")
                    with CAPTURE_LOCK:
                        if item in LOXONE_ITEMS:
                            LIVE_VALUES[item] = payload.get("v") if isinstance(payload, dict) else payload
                            LIVE_VALUE_TS[item] = time.time()
                        if item == "15":
                            _alarm_update(payload)
                except Exception:
                    pass
                with CAPTURE_LOCK:
                    CAPTURE_STATE["messages"] += 1
                    CAPTURE_STATE["last_message_at"] = time.time()

            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            client.on_message = on_message
            client.connect(HOST, PORT, keepalive=30)
            client.loop_forever(retry_first_connection=True)

        except Exception as exc:
            with CAPTURE_LOCK:
                CAPTURE_STATE["connected"] = False
                CAPTURE_STATE["error"] = str(exc)
            print(f"BAYROL MQTT Capture Fehler: {exc}", flush=True)
        finally:
            if log_handle is not None:
                log_handle.close()

    threading.Thread(target=worker, name="bayrol-mqtt-capture", daemon=True).start()


def capture_status():
    with CAPTURE_LOCK:
        state = dict(CAPTURE_STATE)
    now = time.time()
    started = state.get("started_at")
    last = state.get("last_message_at")
    state["uptime_s"] = round(now - started, 1) if started else None
    state["last_message_age_s"] = round(now - last, 1) if last else None
    return state


def _load_pool_status():
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _default_chem_state(ph_runtime=None, chlorine_runtime=None):
    return {
        "version": 1,
        "baseline": {
            "ph_runtime_s": ph_runtime,
            "chlorine_runtime_s": chlorine_runtime,
            "ts": time.time(),
        },
        "ph": {"last_change_runtime_s": None, "last_change_ts": None, "confirmed_changes": 0},
        "chlorine": {"last_change_runtime_s": None, "last_change_ts": None, "confirmed_changes": 0},
        "year": {"value": time.localtime().tm_year, "ph_runtime_s": ph_runtime, "chlorine_runtime_s": chlorine_runtime},
        "changes_by_year": {},
        "manual_dose_ack_ts": None,
    }


def _load_chem_state(ph_runtime=None, chlorine_runtime=None):
    with CHEM_STATE_LOCK:
        try:
            return json.loads(CHEM_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = _default_chem_state(ph_runtime, chlorine_runtime)
            CHEM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CHEM_STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(CHEM_STATE_PATH)
            return state


def _save_chem_state(state):
    with CHEM_STATE_LOCK:
        CHEM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CHEM_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(CHEM_STATE_PATH)


def _runtime_liters(delta_s):
    if delta_s is None:
        return None
    # Schaetzwert bis die reale Foerdermenge an dieser Anlage kalibriert wurde.
    return max(0.0, float(delta_s)) * DOSING_PUMP_MAX_LPH / 3600.0


def chemical_status(values=None):
    if values is None:
        with CAPTURE_LOCK:
            values = dict(LIVE_VALUES)
    ph_runtime = values.get("4.340")
    chlorine_runtime = values.get("4.335")
    state = _load_chem_state(ph_runtime, chlorine_runtime)
    year = time.localtime().tm_year
    year_state = state.get("year")
    if not isinstance(year_state, dict) or year_state.get("value") != year:
        state["year"] = {"value": year, "ph_runtime_s": ph_runtime, "chlorine_runtime_s": chlorine_runtime}
        state.setdefault("changes_by_year", {})
        _save_chem_state(state)
    year_state = state["year"]
    result = {"ok": 1, "year": year, "flow_lph_assumed": DOSING_PUMP_MAX_LPH, "flow_calibrated": 1 if PUMP_FLOW_CALIBRATED else 0}
    for name, runtime, capacity in (("ph", ph_runtime, PH_CANISTER_L), ("chlorine", chlorine_runtime, CHLORINE_CANISTER_L)):
        item = state.get(name, {})
        baseline_runtime = state.get("baseline", {}).get(f"{name}_runtime_s")
        since_baseline_s = None if runtime is None or baseline_runtime is None else max(0, float(runtime) - float(baseline_runtime))
        change_runtime = item.get("last_change_runtime_s")
        since_change_s = None if runtime is None or change_runtime is None else max(0, float(runtime) - float(change_runtime))
        consumed_total = _runtime_liters(since_baseline_s)
        consumed_since_change = _runtime_liters(since_change_s)
        year_runtime = year_state.get(f"{name}_runtime_s")
        year_delta_s = None if runtime is None or year_runtime is None else max(0, float(runtime) - float(year_runtime))
        consumed_year = _runtime_liters(year_delta_s)
        remaining = None if capacity is None or consumed_since_change is None else max(0.0, capacity - consumed_since_change)
        result[name] = {
            "pump_runtime_s": runtime,
            "consumed_l_est_since_tracking": None if consumed_total is None else round(consumed_total, 3),
            "consumed_l_est_since_change": None if consumed_since_change is None else round(consumed_since_change, 3),
            "consumed_l_est_year": None if consumed_year is None else round(consumed_year, 3),
            "equivalent_canisters_est_year": None if consumed_year is None or not capacity else round(consumed_year / capacity, 3),
            "confirmed_changes_year": int(state.get("changes_by_year", {}).get(str(year), {}).get(name, 0)),
            "canister_capacity_l": capacity,
            "remaining_l_est": None if remaining is None else round(remaining, 3),
            "remaining_pct_est": None if remaining is None or not capacity else round(100.0 * remaining / capacity, 1),
            "confirmed_changes": int(item.get("confirmed_changes", 0)),
            "last_change_ts": item.get("last_change_ts"),
        }
    return result


def confirm_canister_change(name, values=None):
    if name not in ("ph", "chlorine"):
        raise ValueError("unknown chemical")
    if values is None:
        with CAPTURE_LOCK:
            values = dict(LIVE_VALUES)
    runtime_key = "4.340" if name == "ph" else "4.335"
    runtime = values.get(runtime_key)
    if runtime is None:
        raise RuntimeError("pump runtime unavailable")
    state = _load_chem_state(values.get("4.340"), values.get("4.335"))
    state[name]["last_change_runtime_s"] = runtime
    state[name]["last_change_ts"] = time.time()
    state[name]["confirmed_changes"] = int(state[name].get("confirmed_changes", 0)) + 1
    year_key = str(time.localtime().tm_year)
    yearly = state.setdefault("changes_by_year", {}).setdefault(year_key, {"ph": 0, "chlorine": 0})
    yearly[name] = int(yearly.get(name, 0)) + 1
    _save_chem_state(state)
    return chemical_status(values)


def acknowledge_manual_dose(values=None):
    if values is None:
        with CAPTURE_LOCK:
            values = dict(LIVE_VALUES)
    state = _load_chem_state(values.get("4.340"), values.get("4.335"))
    state["manual_dose_ack_ts"] = time.time()
    _save_chem_state(state)
    return water_care_status(values)


def water_care_status(values=None):
    if values is None:
        with CAPTURE_LOCK:
            values = dict(LIVE_VALUES)
    pool = _load_pool_status()
    state = _load_chem_state(values.get("4.340"), values.get("4.335"))
    now = time.time()
    updated = pool.get("updated_unix")
    age = None if updated is None else max(0.0, now - float(updated))
    valid = pool.get("valid") == 1 and pool.get("online") == 1 and age is not None and age <= STATUS_MAX_AGE_S
    pump_bits = [values.get(x) for x in ("11.30", "11.31", "11.32", "11.33")]
    circulation = pump_bits == [0, 1, 0, 1]
    ack_ts = state.get("manual_dose_ack_ts")
    lockout_left = max(0, int(MIXING_LOCKOUT_S - (now - ack_ts))) if ack_ts else 0
    ph = pool.get("ph")
    redox = pool.get("redox")
    ph_g = 0
    chlorine_g = 0
    reliable = bool(valid and circulation and ph is not None and redox is not None)
    with CAPTURE_LOCK:
        alarms = set(ACTIVE_ALARMS)
    chemical_empty = "8.17" in alarms or "8.32" in alarms

    # Empfehlungen nur aus plausiblen, frischen Messwerten ableiten. pH hat
    # Vorrang: bei gleichzeitig hohem pH und niedrigem Redox wird zuerst pH
    # korrigiert und erst nach dem Misch-/Nachmessfenster Chlor empfohlen.
    if reliable and lockout_left == 0:
        if float(ph) > PH_HIGH_LIMIT:
            delta = max(0.0, float(ph) - PH_TARGET)
            ph_g = int(round((PH_MINUS_G_PER_10M3_PER_02 * (POOL_VOLUME_M3 / 10.0) * (delta / 0.2)) / 50.0) * 50)
        elif "8.29" in alarms or float(redox) < REDOX_LOW_LIMIT_MV:
            chlorine_g = int(round(CHLOR_FIRST_DOSE_G_PER_10M3 * (POOL_VOLUME_M3 / 10.0) / 10.0) * 10)

    if lockout_left > 0:
        care_state = "Nach Dosierung - Umwaelzen"
        care_state_code = 5
    elif chemical_empty:
        care_state = "Chemie leer"
        care_state_code = 3
    elif not reliable:
        care_state = "Messung nicht zuverlaessig"
        care_state_code = 4
    elif ph_g > 0:
        care_state = "pH korrigieren"
        care_state_code = 1
    elif chlorine_g > 0:
        care_state = "Desinfektion zu niedrig"
        care_state_code = 2
    else:
        care_state = "Wasserwerte OK"
        care_state_code = 0
    return {
        "ok": 1,
        "care_state": care_state,
        "care_state_code": care_state_code,
        "measurement_reliable": 1 if reliable else 0,
        "status_age_s": None if age is None else round(age, 1),
        "circulation_running": 1 if circulation else 0,
        "ph": ph,
        "redox_mv": redox,
        "manual_ph_minus_g": ph_g,
        "manual_chlorine_g": chlorine_g,
        "mixing_lockout_s": lockout_left,
        "ph_target": PH_TARGET,
        "ph_high_limit": PH_HIGH_LIMIT,
        "redox_low_limit_mv": REDOX_LOW_LIMIT_MV,
    }


def loxone_status():
    """Kompakter Status ausschließlich aus bestätigten MQTT-Zuordnungen."""
    with CAPTURE_LOCK:
        values = dict(LIVE_VALUES)
        timestamps = dict(LIVE_VALUE_TS)
        alarms = set(ACTIVE_ALARMS)

    ph_state = str(values.get("5.79", ""))
    chlorine_state = str(values.get("5.168", ""))
    ph_mode = str(values.get("5.42", ""))
    pump_bits = [values.get(x) for x in ("11.30", "11.31", "11.32", "11.33")]
    filter_running = None
    if pump_bits == [0, 1, 0, 1]:
        filter_running = 1
    elif pump_bits == [1, 0, 1, 0]:
        filter_running = 0

    return {
        "ok": 1,
        "ph_dosing_pct": values.get("4.89"),
        "ph_dosing_active": 1 if ph_state == "19.54" else (0 if ph_state == "19.134" else None),
        "ph_mode_auto": 1 if ph_mode == "19.17" else (0 if ph_mode == "19.18" else None),
        "ph_pump_runtime_s": values.get("4.340"),
        "filter_running": filter_running,
        "ph_minus_empty": 1 if "8.17" in alarms else 0,
        "chlorine_empty": 1 if "8.32" in alarms else 0,
        "redox_high": 1 if "8.28" in alarms else 0,
        "redox_low": 1 if "8.29" in alarms else 0,
        "chlorine_dosing_pct": values.get("4.90"),
        "chlorine_dosing_active": 1 if chlorine_state == "19.54" else (0 if chlorine_state == "19.134" else None),
        "chlorine_pump_runtime_s": values.get("4.335"),
        "raw_confirmed": {
            "4.89": values.get("4.89"), "5.79": values.get("5.79"),
            "4.340": values.get("4.340"), "4.90": values.get("4.90"),
            "5.168": values.get("5.168"), "4.335": values.get("4.335"), "5.42": values.get("5.42"), "11.30": values.get("11.30"),
            "11.31": values.get("11.31"), "11.32": values.get("11.32"),
            "11.33": values.get("11.33")
        },
        "last_mqtt_ts": max(timestamps.values()) if timestamps else None,
    }


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
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)

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
                        "/api/v1/capture/status",
                        "/api/v1/loxone",
                        "/api/v1/water-care",
                        "/api/v1/chemicals",
                        "/api/v1/chemicals/ph/change?confirm=1",
                        "/api/v1/chemicals/chlorine/change?confirm=1",
                        "/api/v1/water-care/ack?confirm=1",
                        "/health"
                    ]
                })
                return

            if path == "/health":
                self._json(200, {"ok": 1, "service": "bayrolbridge-api"})
                return

            if path == "/api/v1/capture/status":
                self._json(200, capture_status())
                return

            if path == "/api/v1/loxone":
                payload = loxone_status()
                pool = _load_pool_status()
                water = water_care_status()
                chemicals = chemical_status()
                ph_chem = chemicals.get("ph", {})
                chlorine_chem = chemicals.get("chlorine", {})
                # Flache Felder sind absichtlich zusätzlich enthalten: Loxone
                # Virtual HTTP Inputs können sie ohne verschachtelte JSON-Pfade
                # robust mit einfachen Check-Ausdrücken auslesen.
                payload.update({
                    "ph": pool.get("ph"),
                    "redox": pool.get("redox"),
                    "temperature": pool.get("temperature"),
                    "online": pool.get("online"),
                    "valid": pool.get("valid"),
                    "care_state_code": water.get("care_state_code"),
                    "care_ok": 1 if water.get("care_state_code") == 0 else 0,
                    "care_ph": 1 if water.get("care_state_code") == 1 else 0,
                    "care_chlor": 1 if water.get("care_state_code") == 2 else 0,
                    "care_chem_empty": 1 if water.get("care_state_code") == 3 else 0,
                    "care_unreliable": 1 if water.get("care_state_code") == 4 else 0,
                    "care_lockout": 1 if water.get("care_state_code") == 5 else 0,
                    "measurement_reliable": water.get("measurement_reliable"),
                    "manual_ph_minus_g": water.get("manual_ph_minus_g"),
                    "manual_chlorine_g": water.get("manual_chlorine_g"),
                    "mixing_lockout_s": water.get("mixing_lockout_s"),
                    "ph_consumed_l_year": ph_chem.get("consumed_l_est_year"),
                    "ph_remaining_l": ph_chem.get("remaining_l_est"),
                    "ph_remaining_pct": ph_chem.get("remaining_pct_est"),
                    "ph_canister_changes_year": ph_chem.get("confirmed_changes_year"),
                    "chlorine_consumed_l_year": chlorine_chem.get("consumed_l_est_year"),
                    "chlorine_remaining_l": chlorine_chem.get("remaining_l_est"),
                    "chlorine_remaining_pct": chlorine_chem.get("remaining_pct_est"),
                    "chlorine_canister_changes_year": chlorine_chem.get("confirmed_changes_year"),
                    "chemical_flow_calibrated": chemicals.get("flow_calibrated"),
                })
                payload["water_care"] = water
                payload["chemicals"] = chemicals
                self._json(200, payload)
                return

            if path == "/api/v1/water-care":
                self._json(200, water_care_status())
                return

            if path == "/api/v1/chemicals":
                self._json(200, chemical_status())
                return

            if path in ("/api/v1/chemicals/ph/change", "/api/v1/chemicals/chlorine/change"):
                if not self._write_allowed():
                    self._forbidden()
                    return
                if query.get("confirm") != ["1"]:
                    self._json(400, {"ok": 0, "error": "confirm=1 required"})
                    return
                name = "ph" if "/ph/" in path else "chlorine"
                self._json(200, confirm_canister_change(name))
                return

            if path == "/api/v1/water-care/ack":
                if not self._write_allowed():
                    self._forbidden()
                    return
                if query.get("confirm") != ["1"]:
                    self._json(400, {"ok": 0, "error": "confirm=1 required"})
                    return
                self._json(200, acknowledge_manual_dose())
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
    start_mqtt_capture()
    server = ThreadingHTTPServer((API_HOST, API_PORT), Handler)
    print(f"BAYROL Bridge API v2 lauscht auf {API_HOST}:{API_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
