#!/usr/bin/env python3
"""
BAYROL Cloud-Poller
===================

Zweck
-----
Dieser Prozess laeuft NICHT dauerhaft. systemd startet ihn ueber
bayrolbridge.timer etwa alle 120 Sekunden als oneshot. Im normalen Betrieb
meldet sich jeder Lauf bei BAYROL PoolAccess an, liest die aktuellen
Controllerwerte und schreibt sie atomar nach /opt/bayrolbridge/bayrol.json.
Im persistenten Winterbetrieb wird der Cloud-Poll absichtlich ohne Netzwerkzugriff
uebersprungen und der oneshot endet erfolgreich. Danach endet der Prozess wieder.

Datenfluss
----------
BAYROL Web-Cloud -> HTTPS Login -> Controllerauswahl -> Messwerte parsen ->
bayrol.json -> bayrol_api.py:/status -> Loxone

Code-Leseplan / Fehlersuche
---------------------------
load_config()        -> Zugangsdaten/CID aus config.json laden
login()              -> Web-Login bei BAYROL PoolAccess
get_controllers()    -> verfuegbare Controller und CIDs ermitteln
fetch_values()       -> Messwerte zuerst via getdata.php, dann Fallback-Uebersicht
parse_boxes()        -> pH/Redox/Temperatur und Alarmklassen aus HTML lesen
atomic_write_json()  -> erst Temp-Datei schreiben, dann atomar ersetzen
make_error_output()  -> alte Werte behalten, aber valid=0/online=0 + Fehler setzen
main()               -> kompletter Einzel-Poll; Exit 0 bei Erfolg, Exit 1 bei Fehler

Wichtig bei Fehlern
-------------------
- "bayrolbridge.service inactive (dead)" ist NORMAL, solange
  bayrolbridge.timer active/waiting ist. Der Service ist ein oneshot.
- Der wichtigste Zustandsnachweis ist bayrol.json:
  valid=1 + online=1 + frisches updated = letzter Poll erfolgreich.
- Bei einem Fehler werden die letzten Messwerte absichtlich NICHT geloescht.
  Stattdessen setzt make_error_output() valid=0, online=0 und schreibt den Grund
  nach error. Dadurch zeigt Loxone nicht ploetzlich Fantasiewerte/Nullwerte.
- Wenn BAYROL sein HTML aendert, ist parse_boxes()/parse_controllers() die erste
  Stelle, die geprueft werden muss.
- Zugangsdaten liegen in config.json und werden hier nicht fest codiert.

Dieser Poller ist vom dauerhaften HTTP/MQTT-Dienst bayrolbridge-api.service
getrennt. Fehler in einem Teil bedeuten deshalb nicht automatisch, dass der
andere Teil ausgefallen ist.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.bayrol-poolaccess.de/webview"
CONFIG_PATH = Path("/opt/bayrolbridge/config.json")
OUTPUT_PATH = Path("/opt/bayrolbridge/bayrol.json")
OPERATING_STATE_PATH = Path("/opt/bayrolbridge/runtime/operating_state.json")
TIMEOUT = (10, 30)

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
}

LABEL_MAP = {
    "ph": "ph",
    "redox": "redox",
    "mv": "redox",
    "temp.": "temperature",
    "temperatur": "temperature",
    "t": "temperature",
    "t1": "temperature",
    "cl": "chlorine",
    "chlor": "chlorine",
    "salz": "salt",
    "salt": "salt",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def winter_mode_enabled() -> bool:
    try:
        state = json.loads(OPERATING_STATE_PATH.read_text(encoding="utf-8"))
        return bool(state.get("winter_mode"))
    except Exception:
        return False


def load_config() -> Dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    username = str(config.get("username", "")).strip()
    password = str(config.get("password", ""))
    cid = str(config.get("cid", "")).strip()

    if not username or not password:
        raise RuntimeError("username oder password fehlt in config.json")

    return {"username": username, "password": password, "cid": cid}


def atomic_write_json(data: Dict[str, Any]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = OUTPUT_PATH.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(str(temp_path), str(OUTPUT_PATH))


def parse_number(text: str) -> Optional[float]:
    normalized = text.replace("\xa0", " ").strip().replace(",", ".")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", normalized)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_boxes(root: Any) -> Dict[str, Any]:
    values: Dict[str, Any] = {}

    for box in root.find_all("div", class_="tab_box"):
        label_node = box.find("span")
        value_node = box.find("h1")
        if not label_node or not value_node:
            continue

        label = label_node.get_text(" ", strip=True).replace("\xa0", " ")
        label = re.split(r"\[", label, maxsplit=1)[0].strip().lower()
        key = LABEL_MAP.get(label)
        if not key:
            continue

        number = parse_number(value_node.get_text(" ", strip=True))
        if number is None:
            continue

        classes = set(box.get("class", []))
        alarm = int("stat_warning" in classes or "stat_alarm" in classes)
        values[key] = number
        values["{}_alarm".format(key)] = alarm

    return values


def parse_controllers(html: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    controllers: List[Dict[str, str]] = []
    seen: Set[str] = set()

    for row in soup.find_all("div", class_="tab_row"):
        tab_1 = row.find("div", class_="tab_1")
        tab_2 = row.find("div", class_="tab_2")
        cid = ""

        if tab_2:
            match = re.search(r"tab_data(\d+)", str(tab_2.get("id", "")))
            if match:
                cid = match.group(1)

        if not cid and tab_1:
            onclick_node = tab_1.find(onclick=re.compile(r"c=\d+"))
            if onclick_node:
                match = re.search(r"c=(\d+)", str(onclick_node.get("onclick", "")))
                if match:
                    cid = match.group(1)

        if not cid or cid in seen:
            continue

        name = "BAYROL Controller"
        if tab_1:
            name_node = tab_1.find("p")
            if name_node and name_node.get_text(strip=True):
                name = name_node.get_text(" ", strip=True)

        seen.add(cid)
        controllers.append({"cid": cid, "name": name})

    return controllers


def login(session: requests.Session, username: str, password: str) -> None:
    login_page = session.get(
        "{}/m/login.php".format(BASE_URL),
        headers=dict(BASE_HEADERS, Accept="text/html,application/xhtml+xml"),
        timeout=TIMEOUT,
    )
    login_page.raise_for_status()

    soup = BeautifulSoup(login_page.text, "html.parser")
    form = soup.find("form", id="form_login")
    if not form:
        raise RuntimeError("BAYROL-Loginformular wurde nicht gefunden")

    payload: Dict[str, str] = {}
    for field in form.find_all("input"):
        name = field.get("name")
        if name:
            payload[str(name)] = str(field.get("value", ""))

    payload["username"] = username
    payload["password"] = password

    headers = dict(BASE_HEADERS)
    headers.update({
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.bayrol-poolaccess.de",
        "Referer": "{}/m/login.php".format(BASE_URL),
    })

    response = session.post(
        "{}/m/login.php?r=reg".format(BASE_URL),
        data=payload,
        headers=headers,
        allow_redirects=True,
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    error_box = BeautifulSoup(response.text, "html.parser").find("div", class_="error_text")
    if error_box:
        raise RuntimeError(
            "BAYROL-Login fehlgeschlagen: {}".format(
                error_box.get_text(" ", strip=True)
            )
        )


def get_controllers(session: requests.Session) -> List[Dict[str, str]]:
    response = session.get(
        "{}/m/plants.php".format(BASE_URL),
        headers=dict(BASE_HEADERS, Referer="{}/m/login.php".format(BASE_URL)),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return parse_controllers(response.text)


def fetch_values(session: requests.Session, cid: str) -> Tuple[Dict[str, Any], str]:
    headers = dict(BASE_HEADERS)
    headers.update({
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "{}/m/plants.php".format(BASE_URL),
    })
    response = session.get(
        "{}/getdata.php".format(BASE_URL),
        params={"cid": cid},
        headers=headers,
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    values = parse_boxes(soup)
    if values:
        return values, "getdata"

    overview = session.get(
        "{}/p/plants.php".format(BASE_URL),
        headers=dict(BASE_HEADERS, Referer="{}/m/plants.php".format(BASE_URL)),
        timeout=TIMEOUT,
    )
    overview.raise_for_status()
    overview_soup = BeautifulSoup(overview.text, "html.parser")
    controller_box = overview_soup.find("div", id="tab_data{}".format(cid))
    values = parse_boxes(controller_box or overview_soup)
    if values:
        return values, "overview"

    offline_text = "{}\n{}".format(response.text, overview.text).lower()
    if (
        "no connection to the controller" in offline_text
        or "keine verbindung" in offline_text
    ):
        raise RuntimeError("BAYROL-Controller ist laut Cloud offline")

    raise RuntimeError("Keine Messwerte in der BAYROL-Antwort gefunden")


def make_error_output(message: str) -> Dict[str, Any]:
    previous: Dict[str, Any] = {}
    try:
        if OUTPUT_PATH.exists():
            previous = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}

    previous.update({
        "valid": 0,
        "online": 0,
        "last_attempt": now_iso(),
        "last_attempt_unix": int(time.time()),
        "error": message[:300],
    })
    return previous


def main() -> int:
    try:
        if winter_mode_enabled():
            print(json.dumps({
                "ok": 1,
                "winter_mode": 1,
                "message": "BAYROL Cloud-Poll im Winterbetrieb absichtlich uebersprungen",
                "checked_at": now_iso(),
            }, ensure_ascii=False))
            return 0

        config = load_config()
        session = requests.Session()
        session.headers.update(BASE_HEADERS)

        login(session, config["username"], config["password"])
        controllers = get_controllers(session)
        if not controllers:
            raise RuntimeError("Login möglich, aber kein BAYROL-Controller gefunden")

        cid = config["cid"]
        selected: Optional[Dict[str, str]] = None

        if cid:
            selected = next(
                (item for item in controllers if item["cid"] == cid),
                None,
            )
            if selected is None:
                available = ", ".join(item["cid"] for item in controllers)
                raise RuntimeError(
                    "CID {} nicht gefunden. Vorhanden: {}".format(cid, available)
                )
        elif len(controllers) == 1:
            selected = controllers[0]
        else:
            available = ", ".join(
                "{}={}".format(item["name"], item["cid"])
                for item in controllers
            )
            raise RuntimeError(
                "Mehrere Controller gefunden. CID in config.json setzen: {}".format(
                    available
                )
            )

        values, source = fetch_values(session, selected["cid"])
        timestamp = int(time.time())
        output: Dict[str, Any] = dict(values)
        output.update({
            "valid": 1,
            "online": 1,
            "cid": selected["cid"],
            "controller_name": selected["name"],
            "source": source,
            "updated": now_iso(),
            "updated_unix": timestamp,
            "last_attempt": now_iso(),
            "last_attempt_unix": timestamp,
            "error": "",
        })
        atomic_write_json(output)
        print(json.dumps(output, ensure_ascii=False))
        return 0

    except Exception as exc:
        message = "{}: {}".format(type(exc).__name__, exc)
        atomic_write_json(make_error_output(message))
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
