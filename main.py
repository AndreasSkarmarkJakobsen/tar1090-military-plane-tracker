import gzip
import json
import logging
import os
import re
import ssl
import threading
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from functools import lru_cache
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from xml.etree import ElementTree as ET

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


def build_ssl_context(disable_verify=False):
    if disable_verify:
        return ssl._create_unverified_context()
    if SSL_CERT_PATH:
        return ssl.create_default_context(cafile=SSL_CERT_PATH)
    return ssl.create_default_context()


def get_http_opener(ssl_context=None):
    return build_opener(ProxyHandler({}), HTTPSHandler(context=ssl_context or SSL_CONTEXT))


BASE_URL = os.getenv("BASE_URL", "https://track.example.com").rstrip("/")
TAR1090_URL = os.getenv("TAR1090_URL", f"{BASE_URL}/data/aircraft.json")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "15"))
TAR1090_DB_FOLDER = None

DEFAULT_RSS_FEED_PATH = Path(__file__).resolve().parent / "military-feed.xml"
RSS_FEED_PATH = Path(os.getenv("RSS_FEED_PATH", str(DEFAULT_RSS_FEED_PATH)))
RSS_FEED_TITLE = os.getenv("RSS_FEED_TITLE", "Military Aircraft Tracker")
RSS_FEED_LINK = os.getenv("RSS_FEED_LINK", BASE_URL)
RSS_FEED_DESCRIPTION = os.getenv(
    "RSS_FEED_DESCRIPTION", "Detected military aircraft from tar1090 feed"
)
RSS_MAX_ITEMS = int(os.getenv("RSS_MAX_ITEMS", "100"))
RSS_HOST = os.getenv("RSS_HOST", "0.0.0.0")
RSS_PORT = int(os.getenv("RSS_PORT", "8787"))
RSS_ROUTE = os.getenv("RSS_ROUTE", "/feed.xml")
ENABLE_RSS = os.getenv("ENABLE_RSS", "true").strip().lower() in {"1", "true", "yes", "y"}

CONTACT_INFO = os.getenv("CONTACT_INFO", "your-email@example.com")
USER_AGENT_STRING = f"MilitaryAircraftTracker/1.3 (+{CONTACT_INFO})"

TEST_HEX = os.getenv("TEST_HEX", "")
COOLDOWN_WINDOW = int(os.getenv("COOLDOWN_WINDOW", "600"))

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "").rstrip("/")
MATRIX_ACCESS_TOKEN = os.getenv("MATRIX_ACCESS_TOKEN", "")
MATRIX_ROOM_ID = os.getenv("MATRIX_ROOM_ID", "")
MATRIX_MESSAGE_TYPE = os.getenv("MATRIX_MESSAGE_TYPE", "html").strip().lower()
MATRIX_DISABLE_TLS_VERIFY = os.getenv("MATRIX_DISABLE_TLS_VERIFY", "false").strip().lower() in {
    "1", "true", "yes", "y"
}
ENABLE_MATRIX = os.getenv("ENABLE_MATRIX", "true").strip().lower() in {"1", "true", "yes", "y"}

tracked_aircraft_cache = {}


def http_get_json(url, headers=None, timeout=5, log_errors=True, ssl_context=None):
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    try:
        request = Request(url, headers=request_headers, method="GET")
        with get_http_opener(ssl_context=ssl_context).open(request, timeout=timeout) as response:
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


def http_get_text(url, headers=None, timeout=5, log_errors=True, ssl_context=None):
    request_headers = {"Accept": "*/*"}
    if headers:
        request_headers.update(headers)

    try:
        request = Request(url, headers=request_headers, method="GET")
        with get_http_opener(ssl_context=ssl_context).open(request, timeout=timeout) as response:
            raw_bytes = response.read()
            content_encoding = str(response.headers.get("Content-Encoding", "")).lower()
            if "gzip" in content_encoding or raw_bytes.startswith(b"\x1f\x8b"):
                try:
                    raw_bytes = gzip.decompress(raw_bytes)
                except OSError:
                    if log_errors:
                        log.warning(f"Failed to decompress gzip response for {url}")
            return raw_bytes.decode("utf-8")
    except HTTPError as exc:
        if log_errors:
            log.warning(f"HTTP error {exc.code} while fetching {url}")
    except URLError as exc:
        if log_errors:
            log.error(f"Error communicating with {url}: {exc}")
    return None


def http_get_bytes(url, headers=None, timeout=10, log_errors=True, ssl_context=None):
    request_headers = {"Accept": "*/*"}
    if headers:
        request_headers.update(headers)

    try:
        request = Request(url, headers=request_headers, method="GET")
        with get_http_opener(ssl_context=ssl_context).open(request, timeout=timeout) as response:
            raw_bytes = response.read()
            content_encoding = str(response.headers.get("Content-Encoding", "")).lower()
            if "gzip" in content_encoding or raw_bytes.startswith(b"\x1f\x8b"):
                try:
                    raw_bytes = gzip.decompress(raw_bytes)
                except OSError:
                    if log_errors:
                        log.warning(f"Failed to decompress gzip response for {url}")

            return {
                "content": raw_bytes,
                "content_type": response.headers.get("Content-Type", "application/octet-stream"),
                "content_length": len(raw_bytes),
            }
    except HTTPError as exc:
        if log_errors:
            log.warning(f"HTTP error {exc.code} while fetching bytes from {url}")
    except URLError as exc:
        if log_errors:
            log.error(f"Error communicating with {url}: {exc}")
    return None


def normalize_base_url(url):
    return str(url).strip().rstrip("/")


def extract_db_folder_from_html(html, base_url):
    if not html:
        return None

    candidates = set()

    patterns = [
        r'["\']([^"\']*db-[a-zA-Z0-9]+(?:/[^"\']*)?)["\']',
        r'["\']([^"\']*/db/[^"\']*)["\']',
        r'\b(db-[a-zA-Z0-9]+)\b',
    ]

    for pattern in patterns:
        for match in re.findall(pattern, html):
            candidate = str(match).strip()
            if not candidate:
                continue

            db_match = re.search(r'(.*/db-[a-zA-Z0-9]+|db-[a-zA-Z0-9]+)', candidate)
            if db_match:
                candidate = db_match.group(1).rstrip("/")

            if "/db-" in candidate or candidate.startswith("db-"):
                full_url = urljoin(base_url + "/", candidate.lstrip("/"))
                candidates.add(full_url.rstrip("/"))

    for candidate in sorted(candidates):
        test_url = f"{candidate}/ranges.js"
        payload = http_get_json(test_url, timeout=10, log_errors=False)
        if isinstance(payload, dict):
            return candidate

    return None


def discover_tar1090_db_folder(base_url):
    configured = os.getenv("TAR1090_DB_FOLDER", "").strip()
    if configured:
        configured = configured.rstrip("/")
        payload = http_get_json(f"{configured}/ranges.js", timeout=10, log_errors=False)
        if isinstance(payload, dict):
            log.info(f"Using TAR1090_DB_FOLDER from environment: {configured}")
            return configured
        log.warning(f"Configured TAR1090_DB_FOLDER did not validate: {configured}")

    html = http_get_text(f"{normalize_base_url(base_url)}/", timeout=10, log_errors=True)
    if not html:
        log.warning("Could not fetch tar1090 index HTML for DB folder discovery")
        return None

    discovered = extract_db_folder_from_html(html, normalize_base_url(base_url))
    if discovered:
        log.info(f"Discovered tar1090 DB folder: {discovered}")
        return discovered

    log.warning("Could not auto-discover tar1090 DB folder from base URL HTML")
    return None


def refresh_tar1090_db_folder():
    global TAR1090_DB_FOLDER
    TAR1090_DB_FOLDER = discover_tar1090_db_folder(BASE_URL)
    get_tar1090_military_ranges.cache_clear()
    get_tar1090_type_descriptions.cache_clear()
    get_tar1090_db_record.cache_clear()


def get_registration_from_public_api(hex_code):
    if not hex_code:
        return None

    cleaned_hex = str(hex_code).strip().lower()

    try:
        url = f"https://api.adsb.one/v2/hex/{cleaned_hex}"
        data = http_get_json(url)
        if data:
            ac_list = data.get("ac", [])
            if ac_list and isinstance(ac_list, list):
                reg = ac_list[0].get("r")
                if reg:
                    log.info(f"Resolved hex {hex_code} to registration '{reg}' via ADSB.one")
                    return str(reg).strip()
    except Exception as exc:
        log.debug(f"ADSB.one lookup failed for hex {hex_code}: {exc}")

    try:
        url = f"https://opendata.adsb.fi/api/v2/hex/{cleaned_hex}"
        data = http_get_json(url)
        if data:
            ac_list = data.get("ac", [])
            if ac_list and isinstance(ac_list, list):
                reg = ac_list[0].get("r")
                if reg:
                    log.info(f"Resolved hex {hex_code} to registration '{reg}' via ADSB.fi")
                    return str(reg).strip()
    except Exception as exc:
        log.debug(f"ADSB.fi lookup failed for hex {hex_code}: {exc}")

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
    if not TAR1090_DB_FOLDER:
        return []

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


@lru_cache(maxsize=1)
def get_tar1090_type_descriptions():
    if not TAR1090_DB_FOLDER:
        return {}

    data = http_get_json(f"{TAR1090_DB_FOLDER}/icao_aircraft_types2.js", timeout=10, log_errors=False)
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=1)
def get_flags_js_url():
    html = http_get_text(f"{BASE_URL}/", timeout=10, log_errors=False)
    if not html:
        return None
    match = re.search(r'flags_[a-f0-9]+\.js', html)
    if not match:
        return None
    return f"{BASE_URL}/{match.group(0)}"


_ICAO_RANGE_ENTRY_RE = re.compile(
    r'start:\s*0x([0-9A-Fa-f]+)\s*,\s*end:\s*0x([0-9A-Fa-f]+)\s*,\s*country:\s*"([^"]*)"'
)


@lru_cache(maxsize=1)
def get_tar1090_country_ranges():
    flags_url = get_flags_js_url()
    if not flags_url:
        log.debug("Could not locate flags_*.js on tar1090 root page")
        return []

    raw = http_get_text(flags_url, timeout=10, log_errors=False)
    if not raw:
        return []

    ranges = []
    for match in _ICAO_RANGE_ENTRY_RE.finditer(raw):
        start_hex, end_hex, country = match.groups()
        try:
            ranges.append((int(start_hex, 16), int(end_hex, 16), country))
        except ValueError:
            continue

    if not ranges:
        log.debug(f"Could not extract any ICAO_Ranges entries from {flags_url}")

    return ranges


@lru_cache(maxsize=256)
def get_tar1090_db_record(hex_code):
    if not TAR1090_DB_FOLDER:
        return None

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


def get_callsign(aircraft):
    flight = aircraft.get("flight")
    if flight:
        cleaned = str(flight).strip()
        if cleaned:
            return cleaned
    return None


def get_aircraft_model(aircraft):
    hex_code = str(aircraft.get("hex", "")).strip().upper()
    db_record = get_tar1090_db_record(hex_code)

    if isinstance(db_record, (list, tuple)) and len(db_record) >= 4 and db_record[3]:
        return str(db_record[3])

    type_code = aircraft.get("t")
    if not type_code and isinstance(db_record, (list, tuple)) and len(db_record) >= 2:
        type_code = db_record[1]

    if type_code:
        entry = get_tar1090_type_descriptions().get(str(type_code).strip().upper())
        if isinstance(entry, (list, tuple)) and entry:
            name = entry[0]
            if name:
                return str(name)

    desc = aircraft.get("desc")
    if desc:
        return str(desc)

    return str(type_code) if type_code else "Unknown Model"


def get_aircraft_country(hex_code):
    try:
        numeric_hex = int(str(hex_code).strip().upper(), 16)
    except ValueError:
        return None

    for start_val, end_val, country in get_tar1090_country_ranges():
        if start_val <= numeric_hex <= end_val:
            return country
    return None


def is_military(aircraft):
    aircraft_hex = str(aircraft.get("hex", "")).strip().lower()
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
    if data and data.get("photos"):
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


def load_or_create_rss_channel():
    if RSS_FEED_PATH.exists():
        try:
            tree = ET.parse(RSS_FEED_PATH)
            root = tree.getroot()
            channel = root.find("channel")
            if root.tag == "rss" and channel is not None:
                return tree, root, channel
            log.warning(f"RSS file at {RSS_FEED_PATH} is invalid. Recreating feed.")
        except ET.ParseError:
            log.warning(f"Could not parse existing RSS file at {RSS_FEED_PATH}. Recreating feed.")

    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = RSS_FEED_TITLE
    ET.SubElement(channel, "link").text = RSS_FEED_LINK
    ET.SubElement(channel, "description").text = RSS_FEED_DESCRIPTION
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc), usegmt=True)
    return ET.ElementTree(root), root, channel


def init_rss_feed_file():
    if RSS_FEED_PATH.exists():
        return

    tree, _, _ = load_or_create_rss_channel()
    RSS_FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tree.write(RSS_FEED_PATH, encoding="utf-8", xml_declaration=True)


def start_rss_http_server():
    if not ENABLE_RSS:
        return None

    normalized_route = RSS_ROUTE if RSS_ROUTE.startswith("/") else f"/{RSS_ROUTE}"

    class RSSHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in {normalized_route, "/"}:
                if not RSS_FEED_PATH.exists():
                    self.send_error(404, "RSS feed not found")
                    return

                try:
                    payload = RSS_FEED_PATH.read_bytes()
                except OSError as exc:
                    self.send_error(500, f"Could not read RSS feed: {exc}")
                    return

                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            self.send_error(404, "Not Found")

        def log_message(self, fmt, *args):
            log.debug(f"RSS server: {fmt % args}")

    server = ThreadingHTTPServer((RSS_HOST, RSS_PORT), RSSHandler)
    thread = threading.Thread(target=server.serve_forever, name="rss-http-server", daemon=True)
    thread.start()
    log.info(f"RSS feed available at http://{RSS_HOST}:{RSS_PORT}{normalized_route}")
    return server


def write_rss_item(aircraft, registration, photo_details):
    if not ENABLE_RSS:
        return

    hex_code = str(aircraft.get("hex", "")).strip().upper()
    flight_link = f"{BASE_URL}/?icao={hex_code}" if hex_code else BASE_URL

    callsign = get_callsign(aircraft)
    display_reg = callsign or registration or hex_code

    photo_url = photo_details.get("image_url") if photo_details else None
    attribution_text = ""
    if photo_details:
        p_name = photo_details.get("photographer")
        p_url = photo_details.get("photo_page")
        attribution_text = f"\nPhoto by {p_name} via {p_url}"

    is_test = hex_code.lower() == str(TEST_HEX).strip().lower()
    model = get_aircraft_model(aircraft)
    altitude = aircraft.get("alt_baro", aircraft.get("alt_geom", "Unknown Altitude"))
    country = get_aircraft_country(hex_code) or "Unknown"

    tree, _, channel = load_or_create_rss_channel()

    title_text = f"Military Plane Detected: {display_reg}"
    if is_test:
        title_text += " (TESTING)"

    description_text = (
        f"Model: {model}\n"
        f"Altitude: {altitude} ft\n"
        f"Country of Registration: {country}\n"
        f"Flight Link: {flight_link}"
    )
    if attribution_text:
        description_text += attribution_text

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = title_text
    ET.SubElement(item, "link").text = flight_link
    ET.SubElement(item, "guid").text = f"{hex_code.lower()}-{int(time.time())}"
    ET.SubElement(item, "pubDate").text = format_datetime(datetime.now(timezone.utc), usegmt=True)
    ET.SubElement(item, "description").text = description_text

    if photo_url:
        ET.SubElement(item, "enclosure", url=photo_url, type="image/jpeg")

    existing_items = channel.findall("item")
    if len(existing_items) > RSS_MAX_ITEMS:
        for stale_item in existing_items[:-RSS_MAX_ITEMS]:
            channel.remove(stale_item)

    last_build = channel.find("lastBuildDate")
    if last_build is None:
        last_build = ET.SubElement(channel, "lastBuildDate")
    last_build.text = format_datetime(datetime.now(timezone.utc), usegmt=True)

    RSS_FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tree.write(RSS_FEED_PATH, encoding="utf-8", xml_declaration=True)
    log.info(f"RSS item written for hex {hex_code} -> {RSS_FEED_PATH}")


def build_matrix_message(aircraft, registration, photo_details):
    hex_code = str(aircraft.get("hex", "")).strip().upper()
    flight_link = f"{BASE_URL}/?icao={hex_code}" if hex_code else BASE_URL

    callsign = get_callsign(aircraft)
    display_reg = callsign or registration or hex_code or "Unknown"
    model = get_aircraft_model(aircraft)
    altitude = aircraft.get("alt_baro", aircraft.get("alt_geom", "Unknown"))
    country = get_aircraft_country(hex_code) or "Unknown"
    is_test = hex_code.lower() == str(TEST_HEX).strip().lower()

    title = f"Military Plane Detected: {display_reg}"
    if is_test:
        title += " (TESTING)"

    photo_url = photo_details.get("image_url") if photo_details else None
    photo_page = photo_details.get("photo_page") if photo_details else None
    photographer = photo_details.get("photographer") if photo_details else None

    text_lines = [
        title,
        f"Hex: {hex_code}",
        f"Registration/Callsign: {display_reg}",
        f"Model: {model}",
        f"Altitude: {altitude} ft",
        f"Country of Registration: {country}",
        f"Track: {flight_link}",
    ]

    if photo_page:
        text_lines.append(f"Photo page: {photo_page}")
    if photographer:
        text_lines.append(f"Photographer: {photographer}")
    if photo_url:
        text_lines.append(f"Source image: {photo_url}")

    text_body = "\n".join(text_lines)

    html_parts = [
        f"✈️ <strong>{escape(title)}</strong><br>",
        f"<strong>Hex:</strong> <code>{escape(hex_code)}</code><br>",
        f"<strong>Registration/Callsign:</strong> {escape(str(display_reg))}<br>",
        f"<strong>Model:</strong> {escape(str(model))}<br>",
        f"<strong>Altitude:</strong> {escape(str(altitude))} ft<br>",
        f"<strong>Country of Registration:</strong> {escape(str(country))}<br>",
        f'<strong>Track:</strong> <a href="{escape(flight_link, quote=True)}">{escape(flight_link)}</a>',
    ]

    if photo_page:
        html_parts.append(
            f'<br><strong>Photo page:</strong> <a href="{escape(photo_page, quote=True)}">{escape(photo_page)}</a>'
        )
    if photographer:
        html_parts.append(f"<br><strong>Photographer:</strong> {escape(str(photographer))}")

    return text_body, "".join(html_parts)


def send_matrix_event(event_type, content):
    if not ENABLE_MATRIX:
        return False

    if not MATRIX_HOMESERVER or not MATRIX_ACCESS_TOKEN or not MATRIX_ROOM_ID:
        log.warning("Matrix not fully configured; skipping event send")
        return False

    txn_id = str(int(time.time() * 1000))
    room_id = quote(MATRIX_ROOM_ID, safe="")
    url = f"{MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{room_id}/send/{event_type}/{txn_id}"

    matrix_ssl_context = build_ssl_context(disable_verify=MATRIX_DISABLE_TLS_VERIFY)

    try:
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {MATRIX_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            data=json.dumps(content).encode("utf-8"),
            method="PUT",
        )

        with get_http_opener(ssl_context=matrix_ssl_context).open(request, timeout=15) as response:
            response.read()
            return 200 <= getattr(response, "status", 200) < 300

    except HTTPError as exc:
        try:
            details = exc.read().decode("utf-8", errors="replace")
        except Exception:
            details = "<no body>"
        log.error(f"Matrix event send HTTP error {exc.code}: {details}")
    except URLError as exc:
        log.error(f"Matrix event send connection error: {exc}")
    except Exception as exc:
        log.error(f"Unexpected Matrix event send error: {exc}")

    return False


def send_matrix_message(text_body, formatted_body=None):
    payload = {
        "msgtype": "m.text",
        "body": text_body,
    }

    if formatted_body and MATRIX_MESSAGE_TYPE == "html":
        payload["format"] = "org.matrix.custom.html"
        payload["formatted_body"] = formatted_body

    ok = send_matrix_event("m.room.message", payload)
    if ok:
        log.info(f"Matrix text message sent successfully to room {MATRIX_ROOM_ID}")
    return ok


def upload_matrix_media(file_bytes, content_type, filename):
    if not ENABLE_MATRIX:
        return None

    if not MATRIX_HOMESERVER or not MATRIX_ACCESS_TOKEN:
        log.warning("Matrix not fully configured; skipping media upload")
        return None

    matrix_ssl_context = build_ssl_context(disable_verify=MATRIX_DISABLE_TLS_VERIFY)
    upload_url = f"{MATRIX_HOMESERVER}/_matrix/media/v3/upload?filename={quote(filename)}"

    try:
        request = Request(
            upload_url,
            headers={
                "Authorization": f"Bearer {MATRIX_ACCESS_TOKEN}",
                "Content-Type": content_type,
            },
            data=file_bytes,
            method="POST",
        )

        with get_http_opener(ssl_context=matrix_ssl_context).open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            content_uri = payload.get("content_uri")
            if content_uri:
                log.info(f"Uploaded media to Matrix: {content_uri}")
                return content_uri
            log.warning("Matrix media upload succeeded but no content_uri was returned")
    except HTTPError as exc:
        try:
            details = exc.read().decode("utf-8", errors="replace")
        except Exception:
            details = "<no body>"
        log.error(f"Matrix media upload HTTP error {exc.code}: {details}")
    except URLError as exc:
        log.error(f"Matrix media upload connection error: {exc}")
    except Exception as exc:
        log.error(f"Unexpected Matrix media upload error: {exc}")

    return None


def send_matrix_image(image_url, caption=None):
    image_data = http_get_bytes(
        image_url,
        headers={"User-Agent": USER_AGENT_STRING},
        timeout=15,
        log_errors=True,
    )
    if not image_data:
        return False

    content_type = image_data.get("content_type", "image/jpeg").split(";")[0].strip()
    file_bytes = image_data["content"]

    extension = ".jpg"
    if content_type == "image/png":
        extension = ".png"
    elif content_type == "image/webp":
        extension = ".webp"
    elif content_type == "image/gif":
        extension = ".gif"

    filename = f"aircraft{extension}"
    mxc_uri = upload_matrix_media(file_bytes, content_type, filename)
    if not mxc_uri:
        return False

    payload = {
        "msgtype": "m.image",
        "body": caption or filename,
        "url": mxc_uri,
        "info": {
            "mimetype": content_type,
            "size": image_data.get("content_length", len(file_bytes)),
        },
    }

    ok = send_matrix_event("m.room.message", payload)
    if ok:
        log.info(f"Matrix image sent successfully to room {MATRIX_ROOM_ID}")
    return ok


def emit_detection(aircraft, registration, photo_details):
    text_body, formatted_body = build_matrix_message(aircraft, registration, photo_details)

    photo_url = photo_details.get("image_url") if photo_details else None
    display_name = get_callsign(aircraft) or registration or str(aircraft.get("hex", "")).strip().upper() or "Unknown"

    if photo_url:
        send_matrix_image(photo_url, caption=f"Aircraft photo: {display_name}")

    send_matrix_message(text_body, formatted_body)
    write_rss_item(aircraft, registration, photo_details)


def main():
    refresh_tar1090_db_folder()

    if ENABLE_RSS:
        init_rss_feed_file()
        start_rss_http_server()

    log.info("Starting military aircraft tracker...")

    while True:
        current_time = time.time()

        expired_keys = [k for k, exp_time in tracked_aircraft_cache.items() if current_time > exp_time]
        for key in expired_keys:
            del tracked_aircraft_cache[key]
            log.info(f"Hex {key} has been out of range for over {COOLDOWN_WINDOW} seconds. Cache cleared.")

        try:
            data = http_get_json(TAR1090_URL, timeout=10)
            if not data:
                time.sleep(CHECK_INTERVAL)
                continue

            aircraft_list = data.get("aircraft", [])

            for aircraft in aircraft_list:
                hex_code = aircraft.get("hex")
                if not hex_code:
                    continue

                normalized_hex = str(hex_code).strip().upper()

                if not is_adsb_tracked(aircraft):
                    log.debug(f"Skipping non-ADS-B aircraft: {normalized_hex}")
                    continue

                if is_military(aircraft):
                    if normalized_hex not in tracked_aircraft_cache:
                        initial_reg = aircraft.get("r") or get_registration_from_tar1090_db(normalized_hex) or "Unknown"
                        photo_details, resolved_reg = get_aircraft_photo(normalized_hex, initial_reg)
                        emit_detection(aircraft, resolved_reg, photo_details)

                    tracked_aircraft_cache[normalized_hex] = current_time + COOLDOWN_WINDOW

        except Exception as exc:
            log.error(f"Error fetching data from tar1090: {exc}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()