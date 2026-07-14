import gzip
import json
import os
import re
import ssl
import time
import logging
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger("mil-watch")


def load_env_file():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()


def resolve_ssl_cert_path():
    configured_path = os.getenv("SSL_CERT_FILE")
    if configured_path:
        path = Path(configured_path)
        if path.exists():
            return str(path)

    for candidate in (
        "/etc/ssl/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
        "/usr/local/etc/openssl/cert.pem",
    ):
        path = Path(candidate)
        if path.exists():
            return str(path)

    return None


SSL_CERT_PATH = resolve_ssl_cert_path()
if SSL_CERT_PATH:
    os.environ.setdefault("SSL_CERT_FILE", SSL_CERT_PATH)
    SSL_CONTEXT = ssl.create_default_context(cafile=SSL_CERT_PATH)
else:
    SSL_CONTEXT = ssl.create_default_context()


def clear_proxy_environment():
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(key, None)


def get_http_opener():
    clear_proxy_environment()
    return build_opener(ProxyHandler({}), HTTPSHandler(context=SSL_CONTEXT))


BASE_URL = os.getenv("BASE_URL", "https://track.example.com").rstrip("/")
TAR1090_URL = os.getenv("TAR1090_URL", f"{BASE_URL}/data/aircraft.json")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://example.com/webhook")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))
TAR1090_DB_FOLDER = f"{BASE_URL}/db-22f6339"

CONTACT_INFO = os.getenv("CONTACT_INFO", "your-email@example.com")
USER_AGENT_STRING = f"MilitaryAircraftTracker/1.1 (+{CONTACT_INFO})"

TEST_HEX = os.getenv("TEST_HEX", "")

tracked_aircraft_cache = {}
COOLDOWN_WINDOW = int(os.getenv("COOLDOWN_WINDOW", "600"))


def http_get_json(url, headers=None, timeout=5, log_errors=True):
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    try:
        request = Request(url, headers=request_headers, method="GET")
        with get_http_opener().open(request, timeout=timeout) as response:
            raw_bytes = response.read()
            content_encoding = str(response.headers.get("Content-Encoding", "")).lower()
            if "gzip" in content_encoding or raw_bytes.startswith(b"\x1f\x8b"):
                try:
                    raw_bytes = gzip.decompress(raw_bytes)
                except OSError:
                    if log_errors:
                        log.warning(f"Failed to decompress gzip response for {url}")
            payload = raw_bytes.decode("utf-8")
            return json.loads(payload) if payload else None
    except HTTPError as exc:
        if log_errors:
            log.warning(f"HTTP error {exc.code} while fetching {url}")
    except URLError as exc:
        if log_errors:
            log.error(f"Error communicating with {url}: {exc}")
    except json.JSONDecodeError as exc:
        if log_errors:
            log.error(f"Failed to decode JSON response from {url}: {exc}")
    return None


def http_post_json(url, payload, headers=None, timeout=5):
    request_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    data = json.dumps(payload).encode("utf-8")
    try:
        request = Request(url, data=data, headers=request_headers, method="POST")
        with get_http_opener().open(request, timeout=timeout) as response:
            response.read()
            return True
    except HTTPError as exc:
        log.warning(f"Webhook returned HTTP status {exc.code} for {url}")
    except URLError as exc:
        log.error(f"Failed to send webhook to {url}: {exc}")
    return False


def get_registration_from_public_api(hex_code):
    if not hex_code:
        return None

    cleaned_hex = str(hex_code).strip().lower()

    try:
        url = f"https://api.adsb.one/v2/hex/{cleaned_hex}"
        data = http_get_json(url)
        if data:
            ac_list = data.get("ac", [])
            if ac_list and isinstance(ac_list, list) and len(ac_list) > 0:
                reg = ac_list[0].get("r")
                if reg:
                    log.info(f"Resolved hex {hex_code} to registration '{reg}' via ADSB.one")
                    return str(reg).strip()
    except Exception as e:
        log.debug(f"ADSB.one lookup failed for hex {hex_code}: {e}")

    try:
        url = f"https://opendata.adsb.fi/api/v2/hex/{cleaned_hex}"
        data = http_get_json(url)
        if data:
            ac_list = data.get("ac", [])
            if ac_list and isinstance(ac_list, list) and len(ac_list) > 0:
                reg = ac_list[0].get("r")
                if reg:
                    log.info(f"Resolved hex {hex_code} to registration '{reg}' via ADSB.fi")
                    return str(reg).strip()
    except Exception as e:
        log.debug(f"ADSB.fi lookup failed for hex {hex_code}: {e}")

    log.warning(f"Could not resolve a registration tail string for hex {hex_code} via public APIs")
    return None


def parse_tar1090_db_flags(db_flags):
    if db_flags is None:
        return False

    if isinstance(db_flags, bool):
        return db_flags

    if isinstance(db_flags, (int, float)):
        return int(db_flags) & 1 != 0

    if isinstance(db_flags, str):
        normalized = db_flags.strip().lower()
        if any(marker in normalized for marker in ("military", "mil")):
            return True
        if any(marker in normalized for marker in ("1", "true", "yes")):
            return True
        parts = [part for part in re.split(r"[,|;\s]+", normalized) if part]
        for part in parts:
            try:
                if int(part, 0) & 1:
                    return True
            except ValueError:
                continue

    if isinstance(db_flags, (list, tuple, set)):
        for entry in db_flags:
            if isinstance(entry, str) and any(marker in entry.lower() for marker in ("military", "mil")):
                return True
            try:
                if int(entry) & 1:
                    return True
            except (TypeError, ValueError):
                continue

    return False


@lru_cache(maxsize=1)
def get_tar1090_military_ranges():
    ranges_payload = http_get_json(f"{TAR1090_DB_FOLDER}/ranges.js", timeout=10, log_errors=False)
    if not isinstance(ranges_payload, dict):
        return []

    ranges = []
    for item in ranges_payload.get("military", []):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        start_hex, end_hex = item
        try:
            ranges.append((int(str(start_hex), 16), int(str(end_hex), 16)))
        except ValueError:
            continue
    return ranges


@lru_cache(maxsize=256)
def get_tar1090_db_record(hex_code):
    query_hex = str(hex_code).strip().upper()
    if not query_hex or query_hex.startswith("~"):
        return None

    level = 1
    while level <= len(query_hex):
        prefix = query_hex[:level]
        remainder = query_hex[level:]
        db_url = f"{TAR1090_DB_FOLDER}/{prefix}.js"
        data = http_get_json(db_url, timeout=10, log_errors=False)
        if not isinstance(data, dict):
            return None

        if remainder in data:
            record = data.get(remainder)
            return record if isinstance(record, (list, tuple)) else None

        children = data.get("children")
        if isinstance(children, list) and children:
            next_key = prefix + remainder[:1]
            if next_key in children:
                level += 1
                continue
        return None

    return None


def aircraft_in_military_range(hex_code):
    if not hex_code:
        return False

    try:
        numeric_hex = int(str(hex_code).strip().upper(), 16)
    except ValueError:
        return False

    for start_hex, end_hex in get_tar1090_military_ranges():
        if start_hex <= numeric_hex <= end_hex:
            return True
    return False


def get_registration_from_tar1090_db(hex_code):
    db_record = get_tar1090_db_record(hex_code)
    if not isinstance(db_record, (list, tuple)) or len(db_record) < 1:
        return None

    registration = str(db_record[0]).strip()
    return registration or None


def is_military(aircraft):
    raw_hex = aircraft.get("hex", "")
    aircraft_hex = str(raw_hex).strip().lower() if raw_hex else ""
    target_hex = str(TEST_HEX).strip().lower()

    if target_hex and aircraft_hex == target_hex:
        log.info(f"Test hex match found: {aircraft_hex}")
        return True

    for key in ("military", "mil"):
        value = aircraft.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            if value:
                return True
            continue
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "military", "mil"}:
                return True
            if normalized.startswith("mil"):
                return True
            if normalized in {"0", "false", "no", "n", "none", "null", ""}:
                continue
        if value:
            return True

    db_flags = aircraft.get("dbFlags")
    if parse_tar1090_db_flags(db_flags):
        return True

    if aircraft_hex and aircraft_in_military_range(aircraft_hex.upper()):
        log.info(f"ICAO range match for military aircraft: {aircraft_hex}")
        return True

    db_record = get_tar1090_db_record(aircraft_hex.upper())
    if isinstance(db_record, (list, tuple)) and len(db_record) >= 3:
        if parse_tar1090_db_flags(db_record[2]):
            log.info(f"tar1090 DB record indicates military for hex: {aircraft_hex}")
            return True

    return False


def is_adsb_tracked(aircraft):
    if not isinstance(aircraft, dict):
        return False

    if aircraft.get("mlat") or aircraft.get("tisb"):
        return False

    if "adsb" in aircraft:
        adsb_value = aircraft.get("adsb")
        if isinstance(adsb_value, bool):
            return adsb_value
        if isinstance(adsb_value, str):
            normalized = adsb_value.strip().lower()
            return normalized in {"true", "1", "yes", "y"}

    return any(
        aircraft.get(field) is not None
        for field in ("lat", "lon", "alt_baro", "alt_geom")
    )


def query_planespotters_endpoint(url):
    headers = {
        "User-Agent": USER_AGENT_STRING,
        "Accept": "application/json",
    }
    data = http_get_json(url, headers=headers, timeout=5)
    if data:
        if data.get("photos") and len(data["photos"]) > 0:
            photo_data = data["photos"][0]
            img_src = photo_data.get("thumbnail_large", {}).get("src")
            photo_link = photo_data.get("link", "https://www.planespotters.net")
            photographer = photo_data.get("photographer", "Unknown Photographer")
            return {
                "image_url": img_src,
                "photo_page": photo_link,
                "photographer": photographer,
            }
        log.warning(f"Planespotters endpoint returned 0 photos for query: {url}")
    return None


def get_aircraft_photo(hex_code, registration):
    if not registration or registration == "Unknown":
        log.info(f"Hex {hex_code} lacks structural registration. Attempting tar1090 DB extraction...")
        resolved_reg = get_registration_from_tar1090_db(hex_code)
        if not resolved_reg:
            log.info(f"Hex {hex_code} lacks tar1090 DB registration. Attempting public API extraction...")
            resolved_reg = get_registration_from_public_api(hex_code)
        if resolved_reg:
            registration = resolved_reg

    if registration and registration != "Unknown":
        log.info(f"Querying Planespotters database for registration asset: {registration}")
        url = f"https://api.planespotters.net/pub/photos/reg/{registration}"
        photo_details = query_planespotters_endpoint(url)
        if photo_details:
            return photo_details, registration

    if hex_code:
        log.info(f"Querying Planespotters database fallback via hardware hex code: {hex_code}")
        url = f"https://api.planespotters.net/pub/photos/hex/{hex_code}"
        photo_details = query_planespotters_endpoint(url)
        if photo_details:
            return photo_details, registration

    log.warning(f"Unable to safely pull photo references for target: {hex_code}")
    return None, registration


def send_webhook(aircraft, registration, photo_details):
    hex_code = aircraft.get("hex", "")
    flight_link = f"{BASE_URL}/?icao={hex_code}" if hex_code else BASE_URL
    display_reg = registration if registration else str(hex_code).upper()

    photo_url = photo_details.get("image_url") if photo_details else None

    attribution_text = ""
    if photo_details:
        p_name = photo_details.get("photographer")
        p_url = photo_details.get("photo_page")
        attribution_text = f"\n\n*Photo by [{p_name}]({p_url}) via Planespotters.net*"

    if str(hex_code).strip().lower() == str(TEST_HEX).strip().lower():
        description_text = (
            "**TESTING:** TESTING\n"
            "**TESTING:** TESTING\n"
            "**TESTING:** TESTING\n\n"
            f"[take me to the flight]({flight_link})"
            f"{attribution_text}"
        )
        payload = {
            "content": "TESTING",
            "embeds": [
                {
                    "title": "TESTING",
                    "description": description_text,
                    "image": {"url": photo_url} if photo_url else {},
                }
            ],
        }
    else:
        model = aircraft.get("desc", aircraft.get("t", "Unknown Model"))
        altitude = aircraft.get("alt_baro", aircraft.get("alt_geom", "Unknown Altitude"))
        country = aircraft.get("country", "Unknown")

        description_text = (
            f"**Model:** {model}\n"
            f"**Altitude:** {altitude} ft\n"
            f"**Country of Registration:** {country}\n\n"
            f"[take me to the flight]({flight_link})"
            f"{attribution_text}"
        )
        payload = {
            "content": "Military Aircraft Detected",
            "embeds": [
                {
                    "title": f"Military Plane Detected: {display_reg}",
                    "description": description_text,
                    "image": {"url": photo_url} if photo_url else {},
                }
            ],
        }

    if http_post_json(WEBHOOK_URL, payload, timeout=5):
        log.info(f"Webhook successfully sent for hex: {hex_code}")
    else:
        log.error(f"Failed to send webhook for hex {hex_code}")


def main():
    log.info("Starting military aircraft tracker...")
    while True:
        current_time = time.time()

        expired_keys = [k for k, exp_time in tracked_aircraft_cache.items() if current_time > exp_time]
        for k in expired_keys:
            del tracked_aircraft_cache[k]
            log.info(f"Hex {k} has been out of range for over 10 minutes. Cache cleared.")

        try:
            data = http_get_json(TAR1090_URL, timeout=10)
            if not data:
                continue

            aircraft_list = data.get("aircraft", [])

            for aircraft in aircraft_list:
                hex_code = aircraft.get("hex")

                if not hex_code:
                    continue

                if not is_adsb_tracked(aircraft):
                    log.debug(f"Skipping non-ADS-B aircraft: {hex_code}")
                    continue

                if is_military(aircraft):
                    if hex_code not in tracked_aircraft_cache:
                        initial_reg = aircraft.get("r") or get_registration_from_tar1090_db(hex_code) or "Unknown"
                        photo_details, resolved_reg = get_aircraft_photo(hex_code, initial_reg)
                        send_webhook(aircraft, resolved_reg, photo_details)

                    tracked_aircraft_cache[hex_code] = current_time + COOLDOWN_WINDOW

        except Exception as e:
            log.error(f"Error fetching data from tar1090: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()