import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_RULES_PATH = Path(__file__).resolve().parents[1] / "config" / "rules.json"
_VISIBLE_ATTRIBUTES = {
    "friendly_name",
    "unit_of_measurement",
    "temperature",
    "current_temperature",
    "hvac_mode",
    "battery_level",
    "brightness",
}
_ALLOWED_ACTIONS = {
    "light": {"turn_on", "turn_off"},
    "switch": {"turn_on", "turn_off"},
    "fan": {"turn_on", "turn_off"},
    "climate": {"set_temperature"},
}


class HomeAssistantError(Exception):
    pass


def _rules():
    try:
        with _RULES_PATH.open(encoding="utf-8") as rules_file:
            return json.load(rules_file)
    except (OSError, json.JSONDecodeError):
        return {}


def _allowed_entities():
    entities = _rules().get("home_assistant", {}).get("allowed_entities", [])
    if not isinstance(entities, list):
        return set()
    return {entity for entity in entities if isinstance(entity, str) and "." in entity}


def _allowed_services(domain):
    configured = _rules().get("home_assistant", {}).get("allowed_services", {}).get(domain, [])
    if not isinstance(configured, list):
        return set()
    return _ALLOWED_ACTIONS.get(domain, set()).intersection(
        service for service in configured if isinstance(service, str)
    )


def _base_url():
    base_url = os.environ.get("JARVIS_HA_URL", "").rstrip("/")
    token = os.environ.get("JARVIS_HA_TOKEN", "")
    if not base_url or not token:
        raise HomeAssistantError("Set JARVIS_HA_URL and JARVIS_HA_TOKEN after Home Assistant is installed.")

    parsed_url = urlsplit(base_url)
    if not parsed_url.hostname or parsed_url.username or parsed_url.password:
        raise HomeAssistantError("JARVIS_HA_URL must be a valid base URL without embedded credentials.")
    if parsed_url.scheme == "https":
        return base_url
    if parsed_url.scheme != "http":
        raise HomeAssistantError("Home Assistant must use HTTPS, or HTTP on a private/local network.")

    hostname = parsed_url.hostname.lower()
    local_hostname = hostname in {"localhost", "homeassistant"} or hostname.endswith(".local")
    try:
        address = ipaddress.ip_address(hostname)
        local_address = address.is_private or address.is_loopback or address.is_link_local
    except ValueError:
        local_address = False
    if not (local_hostname or local_address):
        raise HomeAssistantError("HTTP is allowed only for a private/local Home Assistant address; use HTTPS remotely.")
    return base_url


def status():
    configured = bool(os.environ.get("JARVIS_HA_URL") and os.environ.get("JARVIS_HA_TOKEN"))
    allowed_entities = sorted(_allowed_entities())
    return {
        "configured": configured,
        "ready": configured and bool(allowed_entities),
        "allowed_entities": len(allowed_entities),
        "detail": "ready" if configured and allowed_entities else "not connected; configure Home Assistant and explicitly allow entities",
    }


def _request(method, path, payload=None, timeout=10):
    base_url = _base_url()
    headers = {"Authorization": f"Bearer {os.environ['JARVIS_HA_TOKEN']}", "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    http_request = Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(http_request, timeout=timeout) as response:
            response_body = response.read()
    except HTTPError as error:
        raise HomeAssistantError(f"Home Assistant returned HTTP {error.code}.") from error
    except (URLError, TimeoutError, OSError) as error:
        raise HomeAssistantError("Home Assistant could not be reached or timed out.") from error
    if not response_body:
        return None
    try:
        return json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HomeAssistantError("Home Assistant returned an invalid response.") from error


def list_allowed_entities():
    allowed_entities = _allowed_entities()
    if not allowed_entities:
        return []
    states = _request("GET", "/api/states")
    if not isinstance(states, list):
        raise HomeAssistantError("Home Assistant returned an invalid entity list.")
    result = []
    for item in states:
        if not isinstance(item, dict) or item.get("entity_id") not in allowed_entities:
            continue
        attributes = item.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}
        result.append({
            "entity_id": item["entity_id"],
            "state": str(item.get("state", "unknown"))[:100],
            "attributes": {key: value for key, value in attributes.items() if key in _VISIBLE_ATTRIBUTES},
        })
    return result


def _normalize_entity_name(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def resolve_control_request(message):
    match = re.fullmatch(
        r"\s*(?:please\s+)?(?:turn|switch)\s+(on|off)\s+(?:the\s+)?(.+?)\s*[.!?]*\s*",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    if not status()["ready"]:
        raise HomeAssistantError("Home Assistant is not connected or no entities are allowlisted yet.")

    requested_name = re.sub(r"\s+please$", "", match.group(2).strip(), flags=re.IGNORECASE)
    requested_name = _normalize_entity_name(requested_name)
    if not requested_name:
        raise HomeAssistantError("Name an explicitly allowlisted Home Assistant entity.")

    service = "turn_on" if match.group(1).lower() == "on" else "turn_off"
    matches = []
    for entity in list_allowed_entities():
        entity_id = entity["entity_id"]
        domain = entity_id.split(".", 1)[0]
        if service not in _allowed_services(domain):
            continue
        aliases = {
            _normalize_entity_name(entity_id.split(".", 1)[-1].replace("_", " ")),
            _normalize_entity_name(str(entity["attributes"].get("friendly_name", ""))),
        }
        if requested_name in aliases:
            matches.append(entity)

    if not matches:
        raise HomeAssistantError("No allowlisted light, switch, or fan exactly matches that name.")
    if len(matches) > 1:
        raise HomeAssistantError("That name matches multiple allowed devices. Use a unique entity name.")
    entity = matches[0]
    return {
        "entity_id": entity["entity_id"],
        "friendly_name": entity["attributes"].get("friendly_name", entity["entity_id"]),
        "service": service,
    }


def call_allowed_service(entity_id, service, data=None):
    if entity_id not in _allowed_entities():
        raise HomeAssistantError("That entity is not enabled in config/rules.json.")
    domain = entity_id.split(".", 1)[0]
    if service not in _allowed_services(domain):
        raise HomeAssistantError("That service is not allowed for this entity type.")
    service_data = {"entity_id": entity_id}
    if service == "set_temperature":
        if not isinstance(data, dict):
            raise HomeAssistantError("A temperature value is required.")
        temperature = data.get("temperature")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 5 <= temperature <= 35:
            raise HomeAssistantError("Temperature must be between 5 and 35 degrees Celsius.")
        service_data["temperature"] = temperature
    result = _request("POST", f"/api/services/{domain}/{service}", service_data)
    return {"success": True, "entity_id": entity_id, "service": service, "result_count": len(result) if isinstance(result, list) else None}