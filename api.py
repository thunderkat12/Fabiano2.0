from typing import Optional, Any
from collections import OrderedDict
import hashlib
import heapq
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Query, Header, HTTPException, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from delivery_location import (
    calculate_route_snapshot,
    ensure_delivery_locations_table,
    fetch_delivery_locations,
    get_delivery_location_status,
    is_pickup_delivery,
    reverse_geocode_coordinates,
    sync_delivery_location_record,
    sync_delivery_locations_batch,
)
from extract_data import save_products
from store_runtime import (
    DEFAULT_STORE_ID,
    build_retry_delay_seconds,
    build_store_connector,
    claim_due_sync_jobs,
    complete_sync_job,
    count_pending_sync_jobs,
    create_order_record,
    enqueue_sync_job,
    fail_sync_job,
    get_order_record,
    get_store_integration,
    init_ops_db,
    integration_admin_view,
    list_orders,
    list_orders_by_created_range,
    normalize_store_id,
    save_store_integration,
    store_products_path,
    store_settings_path,
    update_order_record,
)

app = FastAPI(title="Product API", description="API to search products from extracted PDF")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


DATA_DIR = Path("data")
STORES_DIR = DATA_DIR / "stores"
AUTH_DIR = DATA_DIR / "auth"
LEGACY_STORE_DIR = DATA_DIR / "stores" / "default"
PRODUCTS_FILE = Path("products.json")
SETTINGS_FILE = Path("app_settings.json")
LEGACY_PRODUCTS_FILE = LEGACY_STORE_DIR / "products.json"
LEGACY_SETTINGS_FILE = LEGACY_STORE_DIR / "settings.json"
USERS_FILE = AUTH_DIR / "users.json"
RUNTIME_SECURITY_FILE = AUTH_DIR / "runtime_secrets.json"
MEDIA_DIR = DATA_DIR / "media"
MEDIA_LOGOS_DIR = MEDIA_DIR / "logos"
MEDIA_PRODUCTS_DIR = MEDIA_DIR / "products"

CREATOR_NAME = os.getenv("CREATOR_NAME", "Desenvolvido por Daniel Victor | Sistemas & Automações").strip()
CREATOR_WHATSAPP = os.getenv("CREATOR_WHATSAPP", "(61) 9 9565-1684").strip()
ADMIN_TOKEN_TTL_SECONDS = int(os.getenv("ADMIN_TOKEN_TTL_SECONDS", "28800"))
ADMIN_LOGIN_MAX_ATTEMPTS = max(1, int(os.getenv("ADMIN_LOGIN_MAX_ATTEMPTS", "5")))
ADMIN_LOGIN_WINDOW_SECONDS = max(60, int(os.getenv("ADMIN_LOGIN_WINDOW_SECONDS", "300")))
ADMIN_LOGIN_BLOCK_SECONDS = max(60, int(os.getenv("ADMIN_LOGIN_BLOCK_SECONDS", "900")))
SYNC_JOB_RUNNING_TIMEOUT_SECONDS = max(60, int(os.getenv("SYNC_JOB_RUNNING_TIMEOUT_SECONDS", "900")))

USERNAME_PATTERN = re.compile(r"^[a-z0-9_.-]{3,40}$")
PRODUCT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_./-]{1,60}$")
MANAGER_ENTRY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{6,80}$")

products_cache: dict[str, list[dict[str, Any]]] = {}
products_index_cache: dict[str, list[dict[str, Any]]] = {}
products_version: dict[str, int] = {}
settings_cache: dict[str, dict[str, str]] = {}

products_lock = Lock()
settings_lock = Lock()
admin_tokens_lock = Lock()
search_cache_lock = Lock()
login_attempts_lock = Lock()
pdf_jobs_lock = Lock()
sync_worker_lock = Lock()
admin_tokens: dict[str, dict[str, Any]] = {}
search_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
login_attempts: dict[str, dict[str, float | int]] = {}
pdf_jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
sync_worker_started = False

SEARCH_CACHE_MAX_SIZE = max(0, int(os.getenv("SEARCH_CACHE_MAX_SIZE", "128")))
DEFAULT_SEARCH_LIMIT = 10
PDF_UPLOAD_MAX_BYTES = max(1024 * 1024, int(os.getenv("PDF_UPLOAD_MAX_BYTES", str(25 * 1024 * 1024))))
PDF_UPLOAD_CHUNK_SIZE = max(64 * 1024, int(os.getenv("PDF_UPLOAD_CHUNK_SIZE", str(1024 * 1024))))
PDF_JOB_RETENTION_SECONDS = max(300, int(os.getenv("PDF_JOB_RETENTION_SECONDS", "3600")))
PDF_JOB_MAX_ENTRIES = max(10, int(os.getenv("PDF_JOB_MAX_ENTRIES", "50")))
PDF_PROCESS_TIMEOUT_SECONDS = max(60, int(os.getenv("PDF_PROCESS_TIMEOUT_SECONDS", "900")))
IMAGE_UPLOAD_MAX_BYTES = max(256 * 1024, int(os.getenv("IMAGE_UPLOAD_MAX_BYTES", str(5 * 1024 * 1024))))
IMAGE_UPLOAD_CHUNK_SIZE = max(64 * 1024, int(os.getenv("IMAGE_UPLOAD_CHUNK_SIZE", str(512 * 1024))))

NON_WORD_PATTERN = re.compile(r"[^\w\s]")
DIGITS_PATTERN = re.compile(r"\D")
HEX_COLOR_PATTERN = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")
WHATSAPP_DESTINATION_LINE_PATTERN = re.compile(r"^(.*?)\s*(?:\||=|:)\s*(.+)$")
MAX_WHATSAPP_DESTINATIONS = 4


def sanitize_manager_entry_key(value: Any) -> str:
    text = str(value or "").strip()
    if "#" in text:
        text = text.split("#", 1)[0]
    text = text.strip().strip("/")
    if not text:
        return ""
    if not MANAGER_ENTRY_KEY_PATTERN.fullmatch(text):
        raise RuntimeError("MANAGER_ENTRY_KEY invalido. Use de 6 a 80 caracteres com letras, numeros, '.', '_', '-' ou '@'.")
    return text


def load_runtime_security_secrets(admin_user: str) -> dict[str, Any]:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    parsed: dict[str, Any] = {}
    if RUNTIME_SECURITY_FILE.exists():
        try:
            with open(RUNTIME_SECURITY_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                parsed = dict(loaded)
        except (OSError, json.JSONDecodeError):
            parsed = {}

    updated = False
    payload = dict(parsed)
    payload["admin_user"] = admin_user
    if not payload.get("created_at"):
        payload["created_at"] = int(time.time())
        updated = True

    admin_password = str(payload.get("admin_password", "")).strip()
    if not admin_password:
        admin_password = secrets.token_urlsafe(18)
        payload["admin_password"] = admin_password
        updated = True

    manager_entry_key = ""
    try:
        manager_entry_key = sanitize_manager_entry_key(payload.get("manager_entry_key", ""))
    except RuntimeError:
        manager_entry_key = ""
    if not manager_entry_key:
        manager_entry_key = sanitize_manager_entry_key(f"painel-{secrets.token_urlsafe(12)}")
        payload["manager_entry_key"] = manager_entry_key
        updated = True

    if updated:
        with open(RUNTIME_SECURITY_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    return payload


DEFAULT_ADMIN_PASSWORD = "daniel142536"
DEFAULT_MANAGER_ENTRY_KEY = "Daniel@qwe"
ADMIN_USER = os.getenv("ADMIN_USER", os.getenv("MASTER_USER", "admin")).strip().lower() or "admin"
ENV_ADMIN_PASSWORD = str(os.getenv("ADMIN_PASSWORD", os.getenv("MASTER_PASSWORD", ""))).strip()
ENV_MANAGER_ENTRY_KEY = sanitize_manager_entry_key(os.getenv("MANAGER_ENTRY_KEY", ""))
RUNTIME_SECURITY_SECRETS: dict[str, Any] = {}
if not ENV_ADMIN_PASSWORD or not ENV_MANAGER_ENTRY_KEY:
    RUNTIME_SECURITY_SECRETS = load_runtime_security_secrets(ADMIN_USER)

ADMIN_PASSWORD = ENV_ADMIN_PASSWORD or DEFAULT_ADMIN_PASSWORD
MANAGER_ENTRY_KEY = ENV_MANAGER_ENTRY_KEY or DEFAULT_MANAGER_ENTRY_KEY
MANAGER_ENTRY_ROUTE = f"/{MANAGER_ENTRY_KEY}"

for _media_dir in (MEDIA_DIR, MEDIA_LOGOS_DIR, MEDIA_PRODUCTS_DIR):
    _media_dir.mkdir(parents=True, exist_ok=True)

SYNONYM_MAP = {
    "ip": "iphone",
    "iphone": "ip",
    "sam": "samsung",
    "samsung": "sam",
    "moto": "motorola",
    "motorola": "moto",
    "xm": "xiaomi",
    "xiaomi": "xm",
    "xiao": "xiaomi",
}

STOPWORDS = {
    "a",
    "as",
    "o",
    "os",
    "um",
    "uma",
    "uns",
    "umas",
    "de",
    "do",
    "da",
    "dos",
    "das",
    "e",
    "em",
    "no",
    "na",
    "nos",
    "nas",
    "para",
    "com",
    "sem",
}

TERM_NORMALIZATION_MAP = {
    "diplsay": "display",
    "diplay": "display",
    "dislpay": "display",
    "display": "display",
    "iphne": "iphone",
    "ifone": "iphone",
    "ifon": "iphone",
    "fone": "fone",
    "pelicula": "pelicula",
    "peliculaa": "pelicula",
    "saiomi": "xiaomi",
    "xaiomi": "xiaomi",
    "shaomi": "xiaomi",
    "xiaomy": "xiaomi",
    "xiomi": "xiaomi",
    "xioami": "xiaomi",
    "xiami": "xiaomi",
    "xaomi": "xiaomi",
    "xiaomise": "xiaomi",
}

CATEGORY_RULES = [
    ("Bateria", ("bateria", "battery")),
    ("Tela", ("display", "tela", "lcd", "touch")),
    ("Carregador", ("carregador", "charger", "fonte")),
    ("Cabo e Adaptador", ("cabo", "adaptador", "conector", "otg", "hdmi", "lightning", "type c", "usb")),
    ("Fone e Audio", ("fone", "earphone", "headset", "audio", "caixa de som", "speaker")),
    ("Peliculas e Protecao", ("pelicula", "protetor", "vidro", "glass")),
    ("Capas e Cases", ("capa", "case", "capinha", "bumper")),
    ("Ferramentas", ("alicate", "chave", "estacao", "ferro de solda", "solda", "pinca")),
    ("Suportes", ("suporte", "tripe", "holder")),
]


def env_text(key: str, default: str = "") -> str:
    return os.getenv(key, default).replace("\\n", "\n").strip()


DELIVERY_RULE_LINE_RE = re.compile(r"^(?P<region>.+?)\s*(?:\||=|:)\s*(?P<fee>\d+(?:[.,]\d{1,2})?)$")
REGION_MAP_POINT_LINE_RE = re.compile(
    r"^(?P<region>.+?)\s*\|\s*(?P<lat>-?\d+(?:[.,]\d+)?)\s*\|\s*(?P<lng>-?\d+(?:[.,]\d+)?)$"
)


def parse_delivery_fee_rules(text: str) -> list[tuple[str, float]]:
    rules_by_region: dict[str, tuple[str, float]] = {}
    for raw_line in str(text or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        match = DELIVERY_RULE_LINE_RE.match(line)
        if not match:
            raise HTTPException(
                status_code=400,
                detail=f"delivery_fee_rules invalido na linha: '{line}'. Use formato 'Regiao=10.00'.",
            )

        region = match.group("region").strip()
        fee_raw = match.group("fee").replace(",", ".")
        try:
            fee_value = float(fee_raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"delivery_fee_rules invalido na linha: '{line}'.",
            ) from exc

        if fee_value < 0:
            raise HTTPException(status_code=400, detail="delivery_fee_rules nao pode conter taxa negativa")

        normalized_key = region.lower()
        rules_by_region[normalized_key] = (region, fee_value)

    return list(rules_by_region.values())


def format_delivery_fee_rules(rules: list[tuple[str, float]]) -> str:
    return "\n".join([f"{region}={fee:.2f}" for region, fee in rules if str(region).strip()])


def build_delivery_fee_rules_from_legacy(amount_text: Any, regions_text: Any) -> str:
    amount_raw = str(amount_text or "0").replace(",", ".").strip()
    try:
        amount = float(amount_raw) if amount_raw else 0.0
    except ValueError:
        amount = 0.0
    amount = max(0.0, amount)

    regions = [part.strip() for part in re.split(r"[\n,;]+", str(regions_text or "")) if part.strip()]
    if not regions:
        return ""
    return format_delivery_fee_rules([(region, amount) for region in regions])


def build_default_delivery_fee_rules() -> str:
    explicit = env_text("ORDER_DELIVERY_FEE_RULES", "")
    if explicit:
        return format_delivery_fee_rules(parse_delivery_fee_rules(explicit))

    fallback_amount_raw = env_text("ORDER_DELIVERY_FEE_AMOUNT", "10.00").replace(",", ".")
    try:
        fallback_amount = float(fallback_amount_raw)
    except ValueError:
        fallback_amount = 10.0
    fallback_amount = max(0.0, fallback_amount)

    regions_raw = env_text("ORDER_DELIVERY_FEE_REGIONS", "Ceilandia, Samambaia")
    regions = [part.strip() for part in re.split(r"[\n,;]+", regions_raw) if part.strip()]
    if not regions:
        regions = ["Ceilandia", "Samambaia"]
    return format_delivery_fee_rules([(region, fallback_amount) for region in regions])


def parse_region_map_points(text: str) -> list[dict[str, Any]]:
    points_by_region: dict[str, dict[str, Any]] = {}
    for raw_line in str(text or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        match = REGION_MAP_POINT_LINE_RE.match(line)
        if not match:
            raise HTTPException(
                status_code=400,
                detail=f"delivery_region_map_points invalido na linha: '{line}'. Use formato 'Regiao|latitude|longitude'.",
            )

        region = str(match.group("region") or "").strip()
        lat_raw = str(match.group("lat") or "").replace(",", ".")
        lng_raw = str(match.group("lng") or "").replace(",", ".")
        try:
            lat = float(lat_raw)
            lng = float(lng_raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"delivery_region_map_points invalido na linha: '{line}'.",
            ) from exc

        if not region:
            raise HTTPException(status_code=400, detail="delivery_region_map_points exige uma regiao por linha")
        if lat < -90 or lat > 90:
            raise HTTPException(status_code=400, detail=f"Latitude invalida para a regiao '{region}'")
        if lng < -180 or lng > 180:
            raise HTTPException(status_code=400, detail=f"Longitude invalida para a regiao '{region}'")

        normalized_key = normalize_text(region)
        if not normalized_key:
            raise HTTPException(status_code=400, detail=f"Regiao invalida em delivery_region_map_points: '{region}'")
        points_by_region[normalized_key] = {
            "region": region,
            "value": normalized_key,
            "lat": lat,
            "lng": lng,
        }

    return list(points_by_region.values())


def format_region_map_points(points: list[dict[str, Any]]) -> str:
    lines = []
    for point in points:
        region = str(point.get("region", "")).strip()
        if not region:
            continue
        lat = float(point.get("lat", 0) or 0)
        lng = float(point.get("lng", 0) or 0)
        lines.append(f"{region}|{lat:.6f}|{lng:.6f}")
    return "\n".join(lines)


def normalize_store_id_or_400(value: Any, *, default: str = DEFAULT_STORE_ID) -> str:
    try:
        return normalize_store_id(value, default=default)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="store invalido") from exc


def settings_file_for_store(store_id: str) -> Path:
    return store_settings_path(normalize_store_id_or_400(store_id))


def products_file_for_store(store_id: str) -> Path:
    return store_products_path(normalize_store_id_or_400(store_id))


def resolve_public_store_id(requested_store: Optional[str] = None) -> str:
    return normalize_store_id_or_400(requested_store, default=DEFAULT_STORE_ID)


DEFAULT_SETTINGS = {
    "store_name": os.getenv("STORE_NAME", "Busca Inteligente de Produtos"),
    "store_tagline": os.getenv("STORE_TAGLINE", "Busca com filtros, paginacao e carrinho persistente."),
    "store_logo_url": os.getenv("STORE_LOGO_URL", "").strip(),
    "show_product_images": os.getenv("SHOW_PRODUCT_IMAGES", "1").strip(),
    "theme_bg": os.getenv("THEME_BG", "").strip(),
    "theme_bg_alt": os.getenv("THEME_BG_ALT", "").strip(),
    "theme_surface": os.getenv("THEME_SURFACE", "").strip(),
    "theme_text": os.getenv("THEME_TEXT", "").strip(),
    "theme_muted": os.getenv("THEME_MUTED", "").strip(),
    "theme_accent": os.getenv("THEME_ACCENT", "").strip(),
    "theme_accent_strong": os.getenv("THEME_ACCENT_STRONG", "").strip(),
    "theme_accent_deep": os.getenv("THEME_ACCENT_DEEP", "").strip(),
    "api_base_url": os.getenv("API_BASE_URL", "").strip(),
    "whatsapp_number": env_text("ORDER_WHATSAPP_NUMBER", ""),
    "whatsapp_destinations": env_text("ORDER_WHATSAPP_DESTINATIONS", ""),
    "coupon_title": env_text("ORDER_COUPON_TITLE", "CUPOM DE PEDIDO"),
    "coupon_message": env_text("ORDER_COUPON_MESSAGE", "Segue pedido com itens selecionados."),
    "coupon_address": env_text("ORDER_COUPON_ADDRESS", ""),
    "coupon_footer": env_text("ORDER_COUPON_FOOTER", "Obrigado pela preferencia."),
    "delivery_fee_amount": env_text("ORDER_DELIVERY_FEE_AMOUNT", "10.00"),
    "delivery_fee_regions": env_text("ORDER_DELIVERY_FEE_REGIONS", "Ceilandia, Samambaia"),
    "delivery_fee_rules": build_default_delivery_fee_rules(),
    "delivery_region_map_points": env_text("ORDER_DELIVERY_REGION_MAP_POINTS", ""),
    "delivery_geo_city": env_text("ORDER_DELIVERY_GEO_CITY", "Brasilia"),
    "delivery_geo_state": env_text("ORDER_DELIVERY_GEO_STATE", "DF"),
    "delivery_geo_country": env_text("ORDER_DELIVERY_GEO_COUNTRY", "Brasil"),
    "delivery_store_label": env_text("ORDER_DELIVERY_STORE_LABEL", "Loja"),
    "delivery_store_address": env_text("ORDER_DELIVERY_STORE_ADDRESS", ""),
    "delivery_store_latitude": env_text("ORDER_DELIVERY_STORE_LATITUDE", ""),
    "delivery_store_longitude": env_text("ORDER_DELIVERY_STORE_LONGITUDE", ""),
}
ALLOWED_SETTING_KEYS = set(DEFAULT_SETTINGS.keys())
PRIVATE_INTEGRATION_FIELDS = {
    "integration_mode",
    "integration_connector_type",
    "integration_db_engine",
    "integration_db_host",
    "integration_db_port",
    "integration_db_name",
    "integration_db_user",
    "integration_db_password",
    "integration_db_path",
    "integration_connection_options",
    "integration_healthcheck_sql",
    "integration_catalog_query",
    "integration_order_insert_sql",
    "integration_stock_update_sql",
    "integration_order_finalize_sql",
    "location_supabase_db_url",
}
THEME_COLOR_KEYS = {
    "theme_bg",
    "theme_bg_alt",
    "theme_surface",
    "theme_text",
    "theme_muted",
    "theme_accent",
    "theme_accent_strong",
    "theme_accent_deep",
}
ORDER_ROUTE_STATUS_LABELS = {
    "submitted": "Aguardando saida",
    "route_started": "Rota iniciada",
    "delivered": "Entregue",
}
ORDER_ROUTE_STATUS_SORT = {
    "route_started": 0,
    "submitted": 1,
    "delivered": 2,
}


def normalize_bool_setting(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "sim", "on"}:
        return "1"
    if text in {"0", "false", "no", "nao", "off", ""}:
        return "0"
    raise HTTPException(status_code=400, detail="Valor booleano invalido")


def bool_setting_value(value: Any) -> bool:
    try:
        return normalize_bool_setting(value) == "1"
    except HTTPException:
        return False


def normalize_hex_color_setting(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not HEX_COLOR_PATTERN.fullmatch(text):
        raise HTTPException(status_code=400, detail=f"{field_name} invalido. Use formato HEX como #AABBCC.")
    return text.lower()


def normalize_optional_media_url(value: Any, field_name: str, *, strict: bool = True) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 500:
        if strict:
            raise HTTPException(status_code=400, detail=f"{field_name} excede o limite de 500 caracteres")
        return ""
    if re.match(r"^https?://", text, flags=re.IGNORECASE) or text.startswith("/"):
        return text
    if strict:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} invalido. Use URL http(s) ou caminho iniciado por '/'.",
        )
    return ""


def normalize_coordinate_setting(value: Any, field_name: str, *, minimum: float, maximum: float) -> str:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} invalido.") from exc
    if number < minimum or number > maximum:
        raise HTTPException(status_code=400, detail=f"{field_name} fora do intervalo permitido.")
    return f"{number:.7f}"


def normalize_setting_value(key: str, value: Any) -> str:
    text = str(value).replace("\r\n", "\n").strip()
    if key == "api_base_url":
        return text.rstrip("/")
    if key == "whatsapp_number":
        return normalize_whatsapp_number(value, key)
    if key == "whatsapp_destinations":
        if not text:
            return ""
        return format_whatsapp_destinations(parse_whatsapp_destinations(text))
    if key in THEME_COLOR_KEYS:
        return normalize_hex_color_setting(value, key)
    if key == "store_logo_url":
        return normalize_optional_media_url(value, "store_logo_url", strict=True)
    if key == "show_product_images":
        return normalize_bool_setting(value)
    if key == "delivery_fee_amount":
        if not text:
            return "0.00"
        normalized = text.replace(",", ".")
        try:
            amount = float(normalized)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="delivery_fee_amount invalido") from exc
        if amount < 0:
            raise HTTPException(status_code=400, detail="delivery_fee_amount nao pode ser negativo")
        return f"{amount:.2f}"
    if key == "delivery_fee_regions":
        return "\n".join([line.strip() for line in text.split("\n") if line.strip()])
    if key == "delivery_fee_rules":
        if not text:
            return ""
        return format_delivery_fee_rules(parse_delivery_fee_rules(text))
    if key == "delivery_region_map_points":
        if not text:
            return ""
        return format_region_map_points(parse_region_map_points(text))
    if key == "delivery_store_latitude":
        return normalize_coordinate_setting(value, key, minimum=-90, maximum=90)
    if key == "delivery_store_longitude":
        return normalize_coordinate_setting(value, key, minimum=-180, maximum=180)
    return text


def normalize_integration_field(key: str, value: Any, current: dict[str, Any]) -> Any:
    text = str(value or "").replace("\r\n", "\n").strip()
    if key == "integration_mode":
        normalized = text.lower() or "local_json"
        if normalized not in {"local_json", "external_db"}:
            raise HTTPException(status_code=400, detail="integration_mode invalido")
        return normalized
    if key == "integration_connector_type":
        return text.lower() or "builtin"
    if key == "integration_db_engine":
        normalized = text.lower()
        if normalized and normalized not in {"sqlite", "sqlserver", "postgresql", "mysql", "custom"}:
            raise HTTPException(status_code=400, detail="integration_db_engine invalido")
        return normalized
    if key == "integration_db_port":
        if not text:
            return ""
        if not text.isdigit():
            raise HTTPException(status_code=400, detail="integration_db_port invalido")
        return text
    if key == "integration_db_password":
        if text:
            return text
        return str(current.get("db_password", ""))
    if key == "location_supabase_db_url":
        if text:
            return text
        return str(current.get("location_supabase_db_url", ""))
    return text


def build_integration_updates(payload: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "integration_mode": "mode",
        "integration_connector_type": "connector_type",
        "integration_db_engine": "db_engine",
        "integration_db_host": "db_host",
        "integration_db_port": "db_port",
        "integration_db_name": "db_name",
        "integration_db_user": "db_user",
        "integration_db_password": "db_password",
        "integration_db_path": "db_path",
        "integration_connection_options": "connection_options",
        "integration_healthcheck_sql": "healthcheck_sql",
        "integration_catalog_query": "catalog_query",
        "integration_order_insert_sql": "order_insert_sql",
        "integration_stock_update_sql": "stock_update_sql",
        "integration_order_finalize_sql": "order_finalize_sql",
        "location_supabase_db_url": "location_supabase_db_url",
    }
    updates: dict[str, Any] = {}
    for public_key, private_key in mapping.items():
        if public_key in payload:
            updates[private_key] = normalize_integration_field(public_key, payload[public_key], current)
    return updates


def normalize_whatsapp_number(value: Any, field_name: str) -> str:
    digits = DIGITS_PATTERN.sub("", str(value or ""))
    if not digits:
        return ""
    if len(digits) < 8 or len(digits) > 20:
        raise HTTPException(status_code=400, detail=f"{field_name} invalido. Use apenas numeros com DDD e codigo do pais.")
    return digits


def parse_whatsapp_destinations(value: Any) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen = set()
    lines = str(value or "").replace("\r\n", "\n").split("\n")
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        match = WHATSAPP_DESTINATION_LINE_PATTERN.match(line)
        if not match:
            raise HTTPException(
                status_code=400,
                detail="whatsapp_destinations invalido. Use o formato Nome ou regiao=5561999999999.",
            )
        label = str(match.group(1) or "").strip()
        phone = normalize_whatsapp_number(match.group(2), "whatsapp_destinations")
        if not label:
            raise HTTPException(status_code=400, detail="whatsapp_destinations invalido. Informe um nome ou regiao.")
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append((label, phone))
    if len(rows) > MAX_WHATSAPP_DESTINATIONS:
        raise HTTPException(status_code=400, detail=f"whatsapp_destinations aceita no maximo {MAX_WHATSAPP_DESTINATIONS} destinos.")
    return rows


def format_whatsapp_destinations(rows: list[tuple[str, str]]) -> str:
    return "\n".join(f"{label}={phone}" for label, phone in rows)


def normalize_username(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not USERNAME_PATTERN.fullmatch(text):
        raise HTTPException(
            status_code=400,
            detail="username invalido. Use 3-40 caracteres: letras, numeros, '.', '_' ou '-'.",
        )
    return text


def load_settings_from_disk(store_id: str = DEFAULT_STORE_ID) -> dict[str, str]:
    settings_file = settings_file_for_store(store_id)
    loaded: dict[str, Any] = {}
    if settings_file.exists():
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            if isinstance(parsed, dict):
                loaded = parsed
        except (OSError, json.JSONDecodeError):
            loaded = {}

    merged = dict(DEFAULT_SETTINGS)
    has_non_empty_rules_in_loaded = False
    for key in ALLOWED_SETTING_KEYS:
        if key in loaded:
            normalized = normalize_setting_value(key, loaded[key])
            merged[key] = normalized
            if key == "delivery_fee_rules" and str(normalized).strip():
                has_non_empty_rules_in_loaded = True

    if not has_non_empty_rules_in_loaded:
        legacy_rules = build_delivery_fee_rules_from_legacy(
            loaded.get("delivery_fee_amount", merged.get("delivery_fee_amount", "0.00")),
            loaded.get("delivery_fee_regions", merged.get("delivery_fee_regions", "")),
        )
        if legacy_rules:
            merged["delivery_fee_rules"] = legacy_rules
    return merged


def write_settings_to_disk(settings: dict[str, str], store_id: str = DEFAULT_STORE_ID) -> None:
    settings_file = settings_file_for_store(store_id)
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def get_settings(store_id: str = DEFAULT_STORE_ID) -> dict[str, str]:
    normalized_store_id = normalize_store_id_or_400(store_id)
    with settings_lock:
        cached = settings_cache.get(normalized_store_id)
        if cached is None:
            cached = load_settings_from_disk(normalized_store_id)
            settings_cache[normalized_store_id] = cached
        return dict(cached)


def update_settings(updates: dict[str, str], store_id: str = DEFAULT_STORE_ID) -> dict[str, str]:
    normalized_store_id = normalize_store_id_or_400(store_id)
    with settings_lock:
        current = settings_cache.get(normalized_store_id)
        if current is None:
            current = load_settings_from_disk(normalized_store_id)
        merged = dict(current)
        merged.update(updates)
        settings_cache[normalized_store_id] = merged
        write_settings_to_disk(merged, normalized_store_id)
        return dict(merged)


def load_products_from_disk(store_id: str = DEFAULT_STORE_ID) -> list[dict[str, Any]]:
    products_file = products_file_for_store(store_id)
    if not products_file.exists():
        return []
    try:
        with open(products_file, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        if isinstance(parsed, list):
            return normalize_product_list(parsed)
    except (OSError, json.JSONDecodeError):
        pass
    return []


def build_products_index(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed: list[dict[str, Any]] = []
    for product in products:
        description_raw = str(product.get("description", ""))
        description_lower = description_raw.lower()
        clean_description = normalize_text(description_lower)
        words = clean_description.split() if clean_description else []
        price = parse_price(product.get("price_sight", "0"))
        category = infer_category(description_raw)
        indexed.append(
            {
                "product": product,
                "description": description_lower,
                "clean_description": clean_description,
                "words": tuple(words),
                "word_set": set(words),
                "price": price,
                "is_sellable": price > 0,
                "category": category,
                "category_lower": category.lower(),
            }
        )
    return indexed


def clear_search_cache(store_id: Optional[str] = None) -> None:
    with search_cache_lock:
        if not store_id:
            search_cache.clear()
            return
        normalized_store_id = normalize_store_id_or_400(store_id)
        target_keys = [key for key in search_cache.keys() if key and key[0] == normalized_store_id]
        for key in target_keys:
            search_cache.pop(key, None)


def get_cached_search(cache_key: tuple[Any, ...]) -> Optional[dict[str, Any]]:
    if SEARCH_CACHE_MAX_SIZE <= 0:
        return None

    with search_cache_lock:
        cached = search_cache.get(cache_key)
        if cached is None:
            return None
        search_cache.move_to_end(cache_key)
        return cached


def set_cached_search(cache_key: tuple[Any, ...], payload: dict[str, Any]) -> None:
    if SEARCH_CACHE_MAX_SIZE <= 0:
        return

    with search_cache_lock:
        search_cache[cache_key] = payload
        search_cache.move_to_end(cache_key)
        while len(search_cache) > SEARCH_CACHE_MAX_SIZE:
            search_cache.popitem(last=False)


def get_products(store_id: str = DEFAULT_STORE_ID) -> list[dict[str, Any]]:
    normalized_store_id = normalize_store_id_or_400(store_id)
    with products_lock:
        cached = products_cache.get(normalized_store_id)
        if cached is None:
            cached = load_products_from_disk(normalized_store_id)
            products_cache[normalized_store_id] = cached
        return [dict(item) for item in cached]


def get_products_index(store_id: str = DEFAULT_STORE_ID) -> tuple[list[dict[str, Any]], int]:
    normalized_store_id = normalize_store_id_or_400(store_id)
    with products_lock:
        cached_products = products_cache.get(normalized_store_id)
        if cached_products is None:
            cached_products = load_products_from_disk(normalized_store_id)
            products_cache[normalized_store_id] = cached_products
        cached_index = products_index_cache.get(normalized_store_id)
        if cached_index is None:
            cached_index = build_products_index(cached_products)
            products_index_cache[normalized_store_id] = cached_index
        return cached_index, int(products_version.get(normalized_store_id, 0))


def replace_products(products: list[dict[str, Any]], store_id: str = DEFAULT_STORE_ID) -> None:
    normalized_store_id = normalize_store_id_or_400(store_id)
    normalized_products = normalize_product_list(products)
    products_file = products_file_for_store(normalized_store_id)
    products_file.parent.mkdir(parents=True, exist_ok=True)
    with products_lock:
        save_products(normalized_products, str(products_file))
        products_cache[normalized_store_id] = normalized_products
        products_index_cache[normalized_store_id] = build_products_index(normalized_products)
        products_version[normalized_store_id] = int(products_version.get(normalized_store_id, 0)) + 1
    clear_search_cache(normalized_store_id)


def migrate_legacy_files_if_needed() -> None:
    if not PRODUCTS_FILE.exists() and LEGACY_PRODUCTS_FILE.exists():
        with open(LEGACY_PRODUCTS_FILE, "r", encoding="utf-8") as src:
            content = src.read()
        with open(PRODUCTS_FILE, "w", encoding="utf-8") as dst:
            dst.write(content)

    if not SETTINGS_FILE.exists() and LEGACY_SETTINGS_FILE.exists():
        with open(LEGACY_SETTINGS_FILE, "r", encoding="utf-8") as src:
            content = src.read()
        with open(SETTINGS_FILE, "w", encoding="utf-8") as dst:
            dst.write(content)


def purge_expired_tokens(now: float) -> None:
    expired = [token for token, payload in admin_tokens.items() if float(payload.get("expires_at", 0)) <= now]
    for token in expired:
        admin_tokens.pop(token, None)


def issue_admin_token(username: str, *, role: str = "master", store_id: str = DEFAULT_STORE_ID) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + ADMIN_TOKEN_TTL_SECONDS
    payload = {
        "username": username,
        "role": role,
        "store_id": normalize_store_id_or_400(store_id),
        "expires_at": expires_at,
    }
    with admin_tokens_lock:
        purge_expired_tokens(time.time())
        admin_tokens[token] = payload
    return token


def require_admin_session(authorization: Optional[str]) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Nao autorizado")

    token = authorization.split(" ", 1)[1].strip()
    now = time.time()
    with admin_tokens_lock:
        purge_expired_tokens(now)
        payload = admin_tokens.get(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Sessao expirada ou invalida")
    return dict(payload)


def resolve_store_for_admin_session(session: dict[str, Any], requested_store: Optional[str] = None) -> str:
    base_store_id = normalize_store_id_or_400(session.get("store_id", DEFAULT_STORE_ID))
    role = str(session.get("role", "")).strip().lower()
    if role == "master" and requested_store:
        return normalize_store_id_or_400(requested_store)
    return base_store_id


def load_auth_users() -> list[dict[str, Any]]:
    if not USERS_FILE.exists():
        return []
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            parsed = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def build_password_hash_candidates(password: str, salt: str, username: str) -> set[str]:
    password_text = str(password or "")
    salt_text = str(salt or "")
    username_text = str(username or "").strip().lower()
    candidates: set[str] = set()
    if password_text:
        candidates.add(hashlib.sha256(password_text.encode("utf-8")).hexdigest())
        candidates.add(hashlib.sha256(f"{salt_text}{password_text}".encode("utf-8")).hexdigest())
        candidates.add(hashlib.sha256(f"{password_text}{salt_text}".encode("utf-8")).hexdigest())
        candidates.add(hashlib.sha256(f"{salt_text}:{password_text}".encode("utf-8")).hexdigest())
        candidates.add(hashlib.sha256(f"{password_text}:{salt_text}".encode("utf-8")).hexdigest())
        candidates.add(hashlib.sha256(f"{username_text}:{password_text}:{salt_text}".encode("utf-8")).hexdigest())
        candidates.add(hashlib.sha256(f"{username_text}:{salt_text}:{password_text}".encode("utf-8")).hexdigest())
    try:
        salt_bytes = bytes.fromhex(salt_text)
    except ValueError:
        salt_bytes = salt_text.encode("utf-8")
    if password_text:
        candidates.add(hashlib.sha256(salt_bytes + password_text.encode("utf-8")).hexdigest())
        candidates.add(hashlib.sha256(password_text.encode("utf-8") + salt_bytes).hexdigest())
    return candidates


def build_authenticated_user(item: dict[str, Any], username: str) -> dict[str, Any]:
    return {
        "username": username,
        "role": str(item.get("role", "store_admin") or "store_admin"),
        "store_id": normalize_store_id_or_400(item.get("store_id", DEFAULT_STORE_ID)),
    }


def authenticate_file_user(username: str, password: str) -> Optional[dict[str, Any]]:
    for item in load_auth_users():
        if not isinstance(item, dict):
            continue
        active = bool(item.get("active", True))
        user_name = str(item.get("username", "")).strip().lower()
        if not active or user_name != username:
            continue

        plain_password = str(item.get("password_plain", ""))
        if plain_password and secrets.compare_digest(plain_password, password):
            return build_authenticated_user(item, user_name)

        stored_hash = str(item.get("password_hash", "")).strip().lower()
        if stored_hash:
            salt = str(item.get("salt", "")).strip()
            for candidate in build_password_hash_candidates(password, salt, user_name):
                if secrets.compare_digest(candidate, stored_hash):
                    return build_authenticated_user(item, user_name)

    return None


def get_request_ip(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for", "")).strip()
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return str(request.client.host).strip()
    return "unknown"


def enforce_login_rate_limit(ip_address: str, now: float) -> None:
    with login_attempts_lock:
        current = login_attempts.get(ip_address)
        if current is None:
            return

        blocked_until = float(current.get("blocked_until", 0))
        if blocked_until > now:
            retry_after = int(blocked_until - now)
            raise HTTPException(
                status_code=429,
                detail=f"Muitas tentativas de login. Tente novamente em {max(1, retry_after)} segundos.",
            )

        window_start = float(current.get("window_start", now))
        if now - window_start > ADMIN_LOGIN_WINDOW_SECONDS:
            login_attempts.pop(ip_address, None)


def register_login_failure(ip_address: str, now: float) -> None:
    with login_attempts_lock:
        current = login_attempts.get(ip_address)
        if current is None:
            login_attempts[ip_address] = {
                "window_start": now,
                "count": 1,
                "blocked_until": 0.0,
            }
            return

        window_start = float(current.get("window_start", now))
        if now - window_start > ADMIN_LOGIN_WINDOW_SECONDS:
            login_attempts[ip_address] = {
                "window_start": now,
                "count": 1,
                "blocked_until": 0.0,
            }
            return

        count = int(current.get("count", 0)) + 1
        blocked_until = now + ADMIN_LOGIN_BLOCK_SECONDS if count >= ADMIN_LOGIN_MAX_ATTEMPTS else 0.0
        login_attempts[ip_address] = {
            "window_start": window_start,
            "count": count,
            "blocked_until": blocked_until,
        }


def clear_login_failures(ip_address: str) -> None:
    with login_attempts_lock:
        login_attempts.pop(ip_address, None)


def parse_price(value: str) -> float:
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return 0.0


def normalize_price_text(value: Any, field_name: str) -> str:
    normalized = str(value or "0").replace(",", ".").strip()
    if not normalized:
        normalized = "0"
    try:
        amount = float(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} invalido") from exc
    if amount < 0:
        raise HTTPException(status_code=400, detail=f"{field_name} nao pode ser negativo")
    return f"{amount:.2f}"


def normalize_stock_value(value: Any) -> int:
    text = str(value or "0").strip()
    if not text:
        return 0
    try:
        amount = int(float(text.replace(",", ".")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="stock invalido") from exc
    if amount < 0:
        raise HTTPException(status_code=400, detail="stock nao pode ser negativo")
    return amount


def normalize_product_id(value: Any) -> str:
    text = str(value or "").strip()
    if not PRODUCT_ID_PATTERN.fullmatch(text):
        raise HTTPException(status_code=400, detail="id invalido. Use ate 60 caracteres: letras, numeros, '_', '-', '.' ou '/'.")
    return text


def normalize_product_record(
    raw: Any,
    fallback_stock: Optional[int] = None,
    fallback_image_url: Optional[str] = None,
) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    try:
        product_id = normalize_product_id(payload.get("id", ""))
    except HTTPException:
        return {}
    description = str(payload.get("description", "")).strip()
    if not product_id or not description:
        return {}

    unit = str(payload.get("unit", "UN") or "UN").strip().upper()[:12] or "UN"
    try:
        stock = normalize_stock_value(payload.get("stock", fallback_stock if fallback_stock is not None else 0))
    except HTTPException:
        stock = max(0, int(fallback_stock or 0))

    try:
        price_sight = normalize_price_text(payload.get("price_sight", "0"), "price_sight")
    except HTTPException:
        price_sight = "0.00"
    try:
        price_term = normalize_price_text(payload.get("price_term", "0"), "price_term")
    except HTTPException:
        price_term = "0.00"
    try:
        price_wholesale = normalize_price_text(payload.get("price_wholesale", "0"), "price_wholesale")
    except HTTPException:
        price_wholesale = "0.00"
    image_url = normalize_optional_media_url(
        payload.get("image_url", fallback_image_url if fallback_image_url is not None else ""),
        "image_url",
        strict=False,
    )

    return {
        "id": product_id,
        "description": description,
        "unit": unit,
        "price_sight": price_sight,
        "price_term": price_term,
        "price_wholesale": price_wholesale,
        "stock": stock,
        "image_url": image_url,
    }


def build_existing_stock_map(products: list[dict[str, Any]]) -> dict[str, int]:
    stock_by_id: dict[str, int] = {}
    for item in products:
        try:
            product_id = normalize_product_id(item.get("id", ""))
        except HTTPException:
            continue
        try:
            stock_by_id[product_id] = normalize_stock_value(item.get("stock", 0))
        except HTTPException:
            stock_by_id[product_id] = 0
    return stock_by_id


def build_existing_image_url_map(products: list[dict[str, Any]]) -> dict[str, str]:
    image_url_by_id: dict[str, str] = {}
    for item in products:
        try:
            product_id = normalize_product_id(item.get("id", ""))
        except HTTPException:
            continue
        image_url_by_id[product_id] = normalize_optional_media_url(item.get("image_url", ""), "image_url", strict=False)
    return image_url_by_id


def normalize_product_list(
    raw_products: list[Any],
    stock_by_id: Optional[dict[str, int]] = None,
    image_url_by_id: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    normalized_products: list[dict[str, Any]] = []
    deduplicated_by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_products:
        payload = raw if isinstance(raw, dict) else {}
        try:
            product_id = normalize_product_id(payload.get("id", ""))
        except HTTPException:
            product_id = ""
        fallback_stock = stock_by_id.get(product_id) if (stock_by_id is not None and product_id) else None
        fallback_image_url = image_url_by_id.get(product_id) if (image_url_by_id is not None and product_id) else None
        normalized = normalize_product_record(
            payload,
            fallback_stock=fallback_stock,
            fallback_image_url=fallback_image_url,
        )
        if not normalized:
            continue
        deduplicated_by_id[normalized["id"]] = normalized
    for key in sorted(deduplicated_by_id.keys(), key=code_sort_key):
        normalized_products.append(deduplicated_by_id[key])
    return normalized_products


def normalize_text(value: str) -> str:
    text = str(value or "")
    text = unicodedata.normalize("NFD", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    cleaned = NON_WORD_PATTERN.sub(" ", text.lower().strip())
    return " ".join(cleaned.split())


def parse_whatsapp_destination_entries(text: str) -> list[dict[str, Any]]:
    entries = []
    seen = set()
    for raw_line in str(text or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        match = WHATSAPP_DESTINATION_LINE_PATTERN.match(line)
        if not match:
            continue
        label = str(match.group(1) or "").strip()
        phone = DIGITS_PATTERN.sub("", str(match.group(2) or ""))
        if not label or not phone:
            continue
        key = normalize_text(label)
        if not key or key in seen:
            continue
        seen.add(key)
        match_keys = {key}
        for inner_match in re.findall(r"\(([^)]+)\)", label):
            normalized = normalize_text(inner_match)
            if normalized:
                match_keys.add(normalized)
        for part in re.split(r"[,;/|-]+", label):
            normalized = normalize_text(part)
            if normalized:
                match_keys.add(normalized)
        entries.append({"key": key, "label": label, "phone": phone, "match_keys": tuple(match_keys)})
    return entries


def resolve_order_whatsapp_number(settings: dict[str, Any], region_value: str, region_label: str, address_value: str) -> str:
    fallback_phone = DIGITS_PATTERN.sub("", str(settings.get("whatsapp_number", "")))
    destinations = parse_whatsapp_destination_entries(str(settings.get("whatsapp_destinations", "")))
    if not destinations:
        return fallback_phone

    candidates = [
        normalize_text(region_label),
        normalize_text(region_value),
        normalize_text(address_value),
    ]
    candidates = [item for item in candidates if item]

    for candidate in candidates:
        for destination in destinations:
            if candidate in destination["match_keys"]:
                return str(destination["phone"])
    for candidate in candidates:
        for destination in destinations:
            if any(len(key) >= 3 and key in candidate for key in destination["match_keys"]):
                return str(destination["phone"])
    return fallback_phone


def get_delivery_region_options(settings: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rules = str(settings.get("delivery_fee_rules", "")).strip()
    if raw_rules:
        options = []
        for region, fee in parse_delivery_fee_rules(raw_rules):
            options.append({"value": normalize_text(region), "label": region, "fee": float(fee)})
        return options

    raw_regions = str(settings.get("delivery_fee_regions", ""))
    amount = parse_price(str(settings.get("delivery_fee_amount", "0")))
    options = []
    seen = set()
    for label in re.split(r"[\n,;]+", raw_regions):
        text = str(label or "").strip()
        if not text:
            continue
        value = normalize_text(text)
        if not value or value in seen:
            continue
        seen.add(value)
        options.append({"value": value, "label": text, "fee": max(0.0, amount)})
    return options


def get_delivery_region_label(settings: dict[str, Any], region_value: str) -> str:
    normalized = normalize_text(region_value)
    if not normalized:
        return ""
    for option in get_delivery_region_options(settings):
        if option["value"] == normalized:
            return str(option["label"])
    return str(region_value or "").strip()


def get_delivery_fee_for_order(settings: dict[str, Any], region_value: str, address_value: str) -> float:
    options = get_delivery_region_options(settings)
    if not options:
        fallback_amount = parse_price(str(settings.get("delivery_fee_amount", "0")))
        raw_regions = str(settings.get("delivery_fee_regions", "")).strip()
        return fallback_amount if fallback_amount > 0 and not raw_regions else 0.0

    normalized_region = normalize_text(region_value)
    if normalized_region:
        for option in options:
            if option["value"] == normalized_region:
                return float(option["fee"])

    normalized_address = normalize_text(address_value)
    if not normalized_address:
        return 0.0
    for option in options:
        if option["value"] and option["value"] in normalized_address:
            return float(option["fee"])
    return 0.0


def get_delivery_region_map_points(settings: dict[str, Any]) -> list[dict[str, Any]]:
    raw_points = str(settings.get("delivery_region_map_points", "")).strip()
    if not raw_points:
        return []
    return parse_region_map_points(raw_points)


def get_location_supabase_db_url(store_id: str) -> str:
    integration = get_store_integration(store_id)
    return str(integration.get("location_supabase_db_url", "") or "").strip()


def parse_optional_coordinate(value: Any) -> Optional[float]:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def build_client_delivery_location(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("delivery_location") if isinstance(payload.get("delivery_location"), dict) else {}
    latitude = parse_optional_coordinate(raw.get("latitude", raw.get("lat", payload.get("delivery_latitude"))))
    longitude = parse_optional_coordinate(raw.get("longitude", raw.get("lng", payload.get("delivery_longitude"))))
    if latitude is None or longitude is None:
        return {}
    if latitude < -90 or latitude > 90 or longitude < -180 or longitude > 180:
        raise HTTPException(status_code=400, detail="Coordenadas da localizacao invalida.")

    accuracy = parse_optional_coordinate(raw.get("accuracy_meters", raw.get("accuracy")))
    confirmed_at = parse_optional_coordinate(raw.get("confirmed_at"))
    return {
        "latitude": latitude,
        "longitude": longitude,
        "accuracy_meters": round(float(accuracy), 2) if accuracy is not None and accuracy >= 0 else 0.0,
        "source": str(raw.get("source", "browser_geolocation") or "browser_geolocation").strip() or "browser_geolocation",
        "display_name": str(raw.get("display_name", raw.get("address", "")) or "").strip(),
        "confirmed_at": float(confirmed_at or time.time()),
    }


def get_store_origin(settings: dict[str, Any]) -> dict[str, Any]:
    latitude = parse_optional_coordinate(settings.get("delivery_store_latitude", ""))
    longitude = parse_optional_coordinate(settings.get("delivery_store_longitude", ""))
    label = str(settings.get("delivery_store_label", "Loja") or "Loja").strip() or "Loja"
    address = str(settings.get("delivery_store_address", "") or "").strip()
    configured = latitude is not None and longitude is not None and -90 <= latitude <= 90 and -180 <= longitude <= 180
    return {
        "label": label,
        "address": address,
        "latitude": float(latitude) if configured else None,
        "longitude": float(longitude) if configured else None,
        "configured": bool(configured),
    }


def build_external_route_links(origin_lat: float, origin_lng: float, destination_lat: float, destination_lng: float) -> dict[str, str]:
    return {
        "google_maps": (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={origin_lat:.7f},{origin_lng:.7f}"
            f"&destination={destination_lat:.7f},{destination_lng:.7f}"
            "&travelmode=driving"
        ),
        "openstreetmap": (
            "https://www.openstreetmap.org/directions"
            f"?engine=fossgis_osrm_car&route={origin_lat:.7f},{origin_lng:.7f};{destination_lat:.7f},{destination_lng:.7f}"
        ),
    }


def build_route_order_entry(
    order: dict[str, Any],
    location_row: dict[str, Any],
    store_origin: dict[str, Any],
) -> dict[str, Any]:
    payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
    status = str(order.get("order_status", "submitted") or "submitted").strip().lower() or "submitted"
    created_at = float(order.get("created_at", 0) or 0)
    route_started_at = float(payload.get("route_started_at", 0) or 0)
    route_completed_at = float(payload.get("route_completed_at", 0) or 0)
    location_status = str(location_row.get("geocode_status", "") or "").strip().lower() if location_row else "missing"
    latitude = None
    longitude = None
    if location_row:
        latitude = parse_optional_coordinate(location_row.get("latitude"))
        longitude = parse_optional_coordinate(location_row.get("longitude"))

    route_payload: dict[str, Any] = {}
    if (
        store_origin.get("configured")
        and location_status == "resolved"
        and latitude is not None
        and longitude is not None
    ):
        route_payload = calculate_route_snapshot(
            store_origin.get("latitude"),
            store_origin.get("longitude"),
            latitude,
            longitude,
        )
        if str(route_payload.get("status", "")).strip().lower() == "ok":
            route_payload["links"] = build_external_route_links(
                float(store_origin["latitude"]),
                float(store_origin["longitude"]),
                float(latitude),
                float(longitude),
            )

    return {
        "protocol": str(order.get("protocol", "") or ""),
        "created_at": created_at,
        "order_status": status,
        "order_status_label": ORDER_ROUTE_STATUS_LABELS.get(status, "Pedido"),
        "route_started_at": route_started_at,
        "route_completed_at": route_completed_at,
        "region": str(payload.get("delivery_region_label", "") or payload.get("delivery_region", "") or "").strip(),
        "address": str(payload.get("delivery_address", "") or "").strip(),
        "order_total": round(float(payload.get("order_total", 0) or 0), 2),
        "delivery_fee": round(float(payload.get("delivery_fee", 0) or 0), 2),
        "payment_method": str(payload.get("payment_method", "") or "").strip(),
        "location_status": location_status,
        "latitude": float(latitude) if latitude is not None else None,
        "longitude": float(longitude) if longitude is not None else None,
        "location_message": str(location_row.get("geocode_error", "") or "") if location_row else "Pedido sem localizacao sincronizada.",
        "route": route_payload,
    }


def sync_delivery_location_async(store_id: str, protocol: str, payload: dict[str, Any], settings: dict[str, Any]) -> None:
    def runner() -> None:
        try:
            sync_delivery_location_record(
                store_id,
                protocol,
                payload,
                settings,
                database_url=get_location_supabase_db_url(store_id),
            )
        except Exception as exc:
            print(f"[delivery_location] falha ao sincronizar pedido {protocol}: {exc}")

    Thread(target=runner, daemon=True).start()


def build_today_range() -> tuple[float, float]:
    now = time.time()
    local_now = time.localtime(now)
    start = time.mktime(
        (
            local_now.tm_year,
            local_now.tm_mon,
            local_now.tm_mday,
            0,
            0,
            0,
            local_now.tm_wday,
            local_now.tm_yday,
            local_now.tm_isdst,
        )
    )
    return float(start), float(start + 86400)


def is_partial_delivery_address(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = normalize_text(text)
    if len(normalized) < 10:
        return True
    return not any(char.isdigit() for char in text)


def build_dashboard_metrics_payload(store_id: str, period: str = "today") -> dict[str, Any]:
    normalized_period = str(period or "today").strip().lower() or "today"
    if normalized_period != "today":
        raise HTTPException(status_code=400, detail="periodo invalido. Use period=today.")

    start_at, end_at = build_today_range()
    settings = get_settings(store_id)
    store_origin = get_store_origin(settings)
    orders = list_orders_by_created_range(store_id, created_from=start_at, created_to=end_at)
    region_points = get_delivery_region_map_points(settings)
    region_points_by_key = {str(item["value"]): item for item in region_points}
    order_protocols = [str(item.get("protocol", "") or "").strip() for item in orders if str(item.get("protocol", "") or "").strip()]

    delivery_location_error = ""
    location_database_url = get_location_supabase_db_url(store_id)
    try:
        delivery_location_rows = fetch_delivery_locations(
            store_id,
            protocols=order_protocols,
            created_from=start_at,
            created_to=end_at,
            limit=max(50, len(order_protocols) * 3 or 50),
            database_url=location_database_url,
        )
    except Exception as exc:
        delivery_location_rows = []
        delivery_location_error = str(exc)
    delivery_locations_by_protocol = {
        str(item.get("protocol", "") or "").strip(): item
        for item in delivery_location_rows
        if str(item.get("protocol", "") or "").strip()
    }

    total_orders = len(orders)
    items_sold = 0
    total_revenue = 0.0
    total_delivery_fee = 0.0
    ignored_without_region = 0
    ignored_without_address = 0
    ignored_without_coordinates = 0

    products_agg: dict[str, dict[str, Any]] = {}
    regions_agg: dict[str, dict[str, Any]] = {}
    address_agg: dict[str, dict[str, Any]] = {}
    hourly_agg: dict[int, dict[str, Any]] = {hour: {"hour": hour, "orders": 0, "items": 0, "revenue": 0.0} for hour in range(24)}

    for order in orders:
        payload = order.get("payload") if isinstance(order.get("payload"), dict) else {}
        created_at = float(order.get("created_at", 0) or 0)
        created_local = time.localtime(created_at or time.time())
        hour_bucket = int(created_local.tm_hour)
        order_total = float(payload.get("order_total", 0) or 0)
        delivery_fee = float(payload.get("delivery_fee", 0) or 0)
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        region_label = str(payload.get("delivery_region_label", "") or "").strip()
        region_value = normalize_text(str(payload.get("delivery_region", "") or ""))
        if not region_label and region_value:
            region_label = get_delivery_region_label(settings, region_value)
        if not region_label and region_value:
            region_label = str(payload.get("delivery_region", "") or "").strip()
        delivery_address = str(payload.get("delivery_address", "") or "").strip()
        pickup_order = is_pickup_delivery(region_label, region_value, delivery_address)

        order_items_total = 0
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            description = str(raw_item.get("description", "") or "").strip()
            if not description:
                continue
            qty = max(0, int(raw_item.get("qty", 0) or 0))
            subtotal = float(raw_item.get("subtotal", 0) or 0)
            if qty <= 0:
                continue
            order_items_total += qty
            product_key = normalize_text(description)
            if not product_key:
                continue
            current = products_agg.get(product_key)
            if current is None:
                current = {
                    "description": description,
                    "qty": 0,
                    "orders": set(),
                    "revenue": 0.0,
                }
                products_agg[product_key] = current
            current["qty"] += qty
            current["revenue"] += subtotal
            current["orders"].add(str(order.get("protocol", "")))

        items_sold += order_items_total
        total_revenue += order_total
        total_delivery_fee += delivery_fee
        hourly_agg[hour_bucket]["orders"] += 1
        hourly_agg[hour_bucket]["items"] += order_items_total
        hourly_agg[hour_bucket]["revenue"] += order_total

        if region_value:
            region_current = regions_agg.get(region_value)
            if region_current is None:
                region_current = {
                    "region": region_label or str(payload.get("delivery_region", "") or "").strip() or "Sem regiao",
                    "value": region_value,
                    "orders": 0,
                    "items": 0,
                    "revenue": 0.0,
                    "delivery_fee": 0.0,
                    "is_pickup": pickup_order,
                }
                regions_agg[region_value] = region_current
            region_current["orders"] += 1
            region_current["items"] += order_items_total
            region_current["revenue"] += order_total
            region_current["delivery_fee"] += delivery_fee
            region_current["is_pickup"] = bool(region_current["is_pickup"] or pickup_order)
        else:
            ignored_without_region += 1

        if delivery_address:
            address_key = normalize_text(delivery_address)
            if address_key:
                address_bucket_key = f"{region_value}|{address_key}" if region_value else address_key
                current_address = address_agg.get(address_bucket_key)
                if current_address is None:
                    current_address = {
                        "address": delivery_address,
                        "normalized_address": address_key,
                        "region": region_label or "Sem regiao",
                        "orders": 0,
                        "items": 0,
                        "revenue": 0.0,
                        "is_partial": is_partial_delivery_address(delivery_address),
                    }
                    address_agg[address_bucket_key] = current_address
                current_address["orders"] += 1
                current_address["items"] += order_items_total
                current_address["revenue"] += order_total
                current_address["is_partial"] = bool(current_address["is_partial"] or is_partial_delivery_address(delivery_address))
        else:
            ignored_without_address += 1

    top_products = sorted(
        [
            {
                "description": item["description"],
                "qty": int(item["qty"]),
                "orders": len(item["orders"]),
                "revenue": round(float(item["revenue"]), 2),
            }
            for item in products_agg.values()
        ],
        key=lambda item: (-int(item["qty"]), -float(item["revenue"]), str(item["description"]).lower()),
    )[:8]

    top_regions = []
    for item in regions_agg.values():
        has_coordinates = item["value"] in region_points_by_key
        if not has_coordinates and not bool(item.get("is_pickup")):
            ignored_without_coordinates += int(item["orders"])
        top_regions.append(
            {
                "region": item["region"],
                "value": item["value"],
                "orders": int(item["orders"]),
                "items": int(item["items"]),
                "revenue": round(float(item["revenue"]), 2),
                "delivery_fee": round(float(item["delivery_fee"]), 2),
                "has_coordinates": has_coordinates,
                "is_pickup": bool(item.get("is_pickup")),
            }
        )
    top_regions.sort(key=lambda item: (-int(item["orders"]), -int(item["items"]), str(item["region"]).lower()))
    top_regions = top_regions[:8]

    address_clusters = sorted(
        [
            {
                "address": item["address"],
                "region": item["region"],
                "orders": int(item["orders"]),
                "items": int(item["items"]),
                "revenue": round(float(item["revenue"]), 2),
                "is_partial": bool(item["is_partial"]),
            }
            for item in address_agg.values()
        ],
        key=lambda item: (-int(item["orders"]), -int(item["items"]), str(item["address"]).lower()),
    )[:8]

    map_points = []
    for region_key, metrics in regions_agg.items():
        if bool(metrics.get("is_pickup")):
            continue
        point = region_points_by_key.get(region_key)
        if point is None:
            continue
        map_points.append(
            {
                "region": metrics["region"],
                "value": region_key,
                "lat": float(point["lat"]),
                "lng": float(point["lng"]),
                "orders": int(metrics["orders"]),
                "items": int(metrics["items"]),
                "revenue": round(float(metrics["revenue"]), 2),
            }
        )
    map_points.sort(key=lambda item: (-int(item["orders"]), str(item["region"]).lower()))

    delivery_points = []
    for item in delivery_location_rows:
        status = str(item.get("geocode_status", "") or "").strip().lower()
        try:
            lat = float(item.get("latitude", 0) or 0)
            lng = float(item.get("longitude", 0) or 0)
        except (TypeError, ValueError):
            continue
        if status != "resolved" or not lat or not lng:
            continue
        delivery_points.append(
            {
                "protocol": str(item.get("protocol", "") or ""),
                "region": str(item.get("delivery_region", "") or ""),
                "address": str(item.get("delivery_address", "") or ""),
                "lat": lat,
                "lng": lng,
                "status": status,
                "source": str(item.get("geocode_source", "") or ""),
                "message": str(item.get("geocode_error", "") or ""),
            }
        )

    hourly_orders = [
        {
            "hour": item["hour"],
            "label": f"{int(item['hour']):02d}:00",
            "orders": int(item["orders"]),
            "items": int(item["items"]),
            "revenue": round(float(item["revenue"]), 2),
        }
        for item in hourly_agg.values()
    ]

    route_orders = [
        build_route_order_entry(order, delivery_locations_by_protocol.get(str(order.get("protocol", "") or "")), store_origin)
        for order in orders
    ]
    route_orders.sort(
        key=lambda item: (
            ORDER_ROUTE_STATUS_SORT.get(str(item.get("order_status", "") or "submitted"), 9),
            0 if str(item.get("location_status", "") or "") == "resolved" else 1,
            -float(item.get("created_at", 0) or 0),
        )
    )

    return {
        "store_id": store_id,
        "summary": {
            "orders_today": total_orders,
            "items_sold": items_sold,
            "revenue": round(total_revenue, 2),
            "average_ticket": round(total_revenue / total_orders, 2) if total_orders else 0.0,
            "average_delivery_fee": round(total_delivery_fee / total_orders, 2) if total_orders else 0.0,
            "regions_served": len(regions_agg),
            "route_started": sum(1 for item in route_orders if str(item.get("order_status", "")) == "route_started"),
            "delivered": sum(1 for item in route_orders if str(item.get("order_status", "")) == "delivered"),
            "exact_locations": len(delivery_points),
        },
        "top_products": top_products,
        "top_regions": top_regions,
        "address_clusters": address_clusters,
        "delivery_points": delivery_points,
        "map_points": map_points,
        "hourly_orders": hourly_orders,
        "route_orders": route_orders,
        "store_origin": store_origin,
        "meta": {
            "period": normalized_period,
            "generated_at": time.time(),
            "window_start": start_at,
            "window_end": end_at,
            "ignored_orders": {
                "without_region": ignored_without_region,
                "without_address": ignored_without_address,
                "without_coordinates": ignored_without_coordinates,
                "without_exact_location": sum(
                    1
                    for item in route_orders
                    if str(item.get("location_status", "") or "") != "resolved"
                ),
            },
            "delivery_location_error": delivery_location_error,
            "store_origin_configured": bool(store_origin.get("configured")),
        },
    }


def generate_order_protocol() -> str:
    now = time.localtime()
    prefix = time.strftime("%y%m%d%H%M", now)
    return f"{prefix}{secrets.randbelow(9000) + 1000}"


def build_whatsapp_message(
    settings: dict[str, Any],
    items: list[dict[str, Any]],
    protocol: str,
    delivery_region_label: str,
    delivery_address: str,
    delivery_location: dict[str, Any],
    payment_method: str,
    cash_change_for_label: str,
    products_total: float,
    delivery_fee: float,
    order_total: float,
) -> str:
    separator = "------------------------------"
    store_name = str(settings.get("store_name", "") or "Fabiano Acessorios").strip()
    coupon_title = str(settings.get("coupon_title", "") or "Pedido").strip()
    is_pickup_order = is_pickup_delivery(delivery_region_label, "", delivery_address)
    payment_label = "Dinheiro" if payment_method == "dinheiro" else "PIX"
    order_datetime = time.strftime("%d/%m/%Y %H:%M", time.localtime())
    maps_url = ""
    if (
        isinstance(delivery_location, dict)
        and delivery_location.get("latitude") is not None
        and delivery_location.get("longitude") is not None
    ):
        latitude = float(delivery_location.get("latitude", 0) or 0)
        longitude = float(delivery_location.get("longitude", 0) or 0)
        maps_url = f"https://www.google.com/maps?q={latitude:.7f},{longitude:.7f}"

    lines = [
        f"*{store_name}*",
        f"*{coupon_title} #{protocol}*",
        f"Data: {order_datetime}",
        separator,
    ]
    if settings.get("coupon_message"):
        lines.extend([str(settings.get("coupon_message")).strip(), separator])

    lines.append("*Entrega*")
    lines.append(f"Modalidade: {'Retirar na loja' if is_pickup_order else 'Entrega'}")
    if delivery_region_label:
        lines.append(f"Regiao: {delivery_region_label}")
    if is_pickup_order:
        pickup_address = str(settings.get("delivery_store_address", "") or settings.get("coupon_address", "") or "").strip()
        if pickup_address:
            lines.append(f"Endereco da loja: {pickup_address}")
    elif delivery_address:
        lines.append(f"Endereco: {delivery_address}")
    if maps_url:
        lines.extend(
            [
                separator,
                "*Localizacao do cliente*",
                "Localizacao confirmada pelo cliente.",
                f"Mapa: {maps_url}",
            ]
        )
    lines.extend([separator, "*Pagamento*", f"Forma: {payment_label}"])
    if cash_change_for_label:
        lines.append(f"Troco para: {cash_change_for_label}")
    lines.extend([separator, "*Itens*"])

    for index, item in enumerate(items, start=1):
        subtotal = float(item["subtotal"])
        lines.extend(
            [
                f"{index}. {item['description']}",
                f"   Qtd: {item['qty']} | Unit: {format_currency(item['unit_price'])}",
                f"   Subtotal: {format_currency(subtotal)}",
            ]
        )

    lines.extend([separator, "*Resumo*", f"Produtos: {format_currency(products_total)}"])
    lines.append(f"Taxa de entrega: {format_currency(delivery_fee)}" if delivery_fee > 0 else "Taxa: gratis")
    lines.append(f"*TOTAL: {format_currency(order_total)}*")
    if settings.get("coupon_footer"):
        lines.extend([separator, str(settings.get("coupon_footer")).strip()])
    return "\n".join(lines)


def format_currency(value: float) -> str:
    amount = max(0.0, float(value or 0))
    integer_part, decimal_part = f"{amount:.2f}".split(".")
    groups = []
    while integer_part:
        groups.append(integer_part[-3:])
        integer_part = integer_part[:-3]
    formatted_integer = ".".join(reversed(groups)) if groups else "0"
    return f"R$ {formatted_integer},{decimal_part}"


def ensure_store_catalog_is_mutable(store_id: str) -> None:
    integration = get_store_integration(store_id)
    mode = str(integration.get("mode", "local_json")).strip().lower()
    if mode == "external_db":
        raise HTTPException(status_code=403, detail="Catalogo desta loja e somente leitura porque a fonte e o banco externo.")


def build_admin_config_payload(store_id: str) -> dict[str, Any]:
    settings = get_settings(store_id)
    integration = get_store_integration(store_id)
    payload = dict(settings)
    payload["store_id"] = store_id
    payload["catalog_read_only"] = str(integration.get("mode", "local_json")).strip().lower() == "external_db"
    payload.update(integration_admin_view(integration))
    pending_orders = list_orders(store_id, sync_status="pending_retry", limit=10)
    payload["integration_pending_jobs"] = count_pending_sync_jobs(store_id)
    payload["integration_pending_orders"] = [
        {
            "protocol": str(item.get("protocol", "")),
            "sync_status": str(item.get("sync_status", "")),
            "sync_message": str(item.get("sync_message", "")),
            "attempts": int(item.get("attempts", 0) or 0),
            "last_error": str(item.get("last_error", "")),
            "created_at": float(item.get("created_at", 0) or 0),
        }
        for item in pending_orders
    ]
    return payload


def tokenize(value: str) -> list[str]:
    normalized = normalize_text(value)
    if not normalized:
        return []
    return normalized.split()


def normalize_query_terms(value: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in tokenize(value):
        normalized = TERM_NORMALIZATION_MAP.get(raw, raw)
        if normalized in STOPWORDS:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
    return terms


def score_text_against_terms(text: str, terms: list[str]) -> tuple[int, int]:
    normalized_text = normalize_text(text)
    words = tokenize(normalized_text)
    if not words:
        return (0, 0)

    word_set = set(words)
    score = 0
    matched = 0
    for term in terms:
        synonym = SYNONYM_MAP.get(term)
        candidates = [term]
        if synonym:
            candidates.append(synonym)

        term_score = 0
        for candidate in candidates:
            if candidate in word_set:
                term_score = max(term_score, 220)
            elif any(word.startswith(candidate) for word in words):
                term_score = max(term_score, 140)
            elif candidate in normalized_text:
                term_score = max(term_score, 90)
        if term_score > 0:
            matched += 1
            score += term_score

    return (score, matched)


def infer_category(description: str) -> str:
    desc = str(description or "").lower()
    for category_name, keywords in CATEGORY_RULES:
        if any(keyword in desc for keyword in keywords):
            return category_name
    return "Outros"


def code_sort_key(value: Any) -> tuple[int, str]:
    text = str(value or "").strip()
    digits = DIGITS_PATTERN.sub("", text)
    if digits:
        return (0, digits.zfill(12))
    return (1, text.lower())


def build_product_from_admin_payload(payload: dict[str, Any]) -> dict[str, Any]:
    product_id = normalize_product_id(payload.get("id", ""))
    description = str(payload.get("description", "")).strip()
    if not description:
        raise HTTPException(status_code=400, detail="description obrigatorio")

    unit = str(payload.get("unit", "UN") or "UN").strip().upper()[:12] or "UN"
    return {
        "id": product_id,
        "description": description,
        "unit": unit,
        "price_sight": normalize_price_text(payload.get("price_sight", "0"), "price_sight"),
        "price_term": normalize_price_text(payload.get("price_term", "0"), "price_term"),
        "price_wholesale": normalize_price_text(payload.get("price_wholesale", "0"), "price_wholesale"),
        "stock": normalize_stock_value(payload.get("stock", 0)),
        "image_url": normalize_optional_media_url(payload.get("image_url", ""), "image_url", strict=True),
    }


def find_product_position(products: list[dict[str, Any]], product_id: str) -> int:
    for index, item in enumerate(products):
        if str(item.get("id", "")).strip() == product_id:
            return index
    return -1


def sync_catalog_from_external(store_id: str) -> dict[str, Any]:
    normalized_store_id = normalize_store_id_or_400(store_id)
    integration = get_store_integration(normalized_store_id)
    connector = build_store_connector(integration)
    if str(integration.get("mode", "local_json")).strip().lower() != "external_db":
        raise HTTPException(status_code=400, detail="Esta loja nao esta configurada para banco externo.")

    rows = connector.pull_catalog()
    current_products = get_products(normalized_store_id)
    stock_by_id = build_existing_stock_map(current_products)
    image_url_by_id = build_existing_image_url_map(current_products)
    normalized_products = normalize_product_list(rows, stock_by_id=stock_by_id, image_url_by_id=image_url_by_id)
    replace_products(normalized_products, normalized_store_id)
    updated = save_store_integration(
        normalized_store_id,
        {
            "status": "catalog_synced",
            "last_error": "",
            "last_catalog_sync_at": time.time(),
            "catalog_synced_products": len(normalized_products),
        },
    )
    return {
        "store_id": normalized_store_id,
        "products": len(normalized_products),
        "integration": updated,
    }


def attempt_order_sync(store_id: str, protocol: str) -> dict[str, Any]:
    normalized_store_id = normalize_store_id_or_400(store_id)
    order_record = get_order_record(normalized_store_id, protocol)
    if not order_record:
        raise HTTPException(status_code=404, detail="Pedido nao encontrado para sincronizacao.")

    if str(order_record.get("sync_status", "")).strip().lower() == "synced":
        return {
            "sync_status": "synced",
            "sync_message": str(order_record.get("sync_message", "Pedido ja sincronizado.")),
            "external_reference": str(order_record.get("external_reference", "")),
        }

    integration = get_store_integration(normalized_store_id)
    if str(integration.get("mode", "local_json")).strip().lower() != "external_db":
        updated = update_order_record(
            normalized_store_id,
            protocol,
            {
                "sync_status": "local_only",
                "sync_message": "Loja local sem sincronizacao externa.",
                "last_error": "",
            },
        )
        return {
            "sync_status": str(updated.get("sync_status", "local_only")),
            "sync_message": str(updated.get("sync_message", "")),
            "external_reference": str(updated.get("external_reference", "")),
        }

    connector = build_store_connector(integration)
    attempts = int(order_record.get("attempts", 0) or 0) + 1
    try:
        result = connector.retry_order(order_record)
        updated = update_order_record(
            normalized_store_id,
            protocol,
            {
                "attempts": attempts,
                "sync_status": "synced",
                "sync_message": str(result.get("message", "Pedido sincronizado.")),
                "external_reference": str(result.get("external_reference", protocol)),
                "last_error": "",
                "synced_at": time.time(),
            },
        )
        save_store_integration(
            normalized_store_id,
            {
                "status": "order_synced",
                "last_error": "",
                "last_order_sync_at": time.time(),
            },
        )
        return {
            "sync_status": str(updated.get("sync_status", "synced")),
            "sync_message": str(updated.get("sync_message", "")),
            "external_reference": str(updated.get("external_reference", "")),
        }
    except Exception as exc:
        error_text = str(exc).strip() or "Falha ao sincronizar pedido."
        updated = update_order_record(
            normalized_store_id,
            protocol,
            {
                "attempts": attempts,
                "sync_status": "pending_retry",
                "sync_message": "Pedido pendente para nova tentativa.",
                "last_error": error_text,
            },
        )
        save_store_integration(
            normalized_store_id,
            {
                "status": "order_sync_error",
                "last_error": error_text,
            },
        )
        return {
            "sync_status": str(updated.get("sync_status", "pending_retry")),
            "sync_message": str(updated.get("sync_message", "")),
            "external_reference": str(updated.get("external_reference", "")),
            "error": error_text,
        }


def process_sync_job(job: dict[str, Any]) -> None:
    job_id = int(job.get("id", 0) or 0)
    if job_id <= 0:
        return
    running = dict(job)
    try:
        store_id = normalize_store_id_or_400(running.get("store_id", DEFAULT_STORE_ID))
        job_type = str(running.get("job_type", "")).strip().lower()
        if job_type == "order_sync":
            protocol = str(running.get("protocol", "")).strip()
            result = attempt_order_sync(store_id, protocol)
            if str(result.get("sync_status", "")).strip().lower() == "synced":
                complete_sync_job(job_id)
                return
            next_due = time.time() + build_retry_delay_seconds(int(running.get("attempts", 0) or 0) + 1)
            fail_sync_job(job_id, str(result.get("error", result.get("sync_message", ""))), next_due)
            return
        if job_type == "catalog_sync":
            sync_catalog_from_external(store_id)
            complete_sync_job(job_id)
            return
        complete_sync_job(job_id)
    except Exception as exc:
        next_due = time.time() + build_retry_delay_seconds(int(running.get("attempts", 0) or 0) + 1)
        fail_sync_job(job_id, str(exc).strip() or "Falha no job de sincronizacao.", next_due)


def sync_worker_loop() -> None:
    while True:
        # Claiming in SQLite keeps multiple API processes from running the same sync job.
        jobs = claim_due_sync_jobs(limit=5, stale_running_after_seconds=SYNC_JOB_RUNNING_TIMEOUT_SECONDS)
        if not jobs:
            time.sleep(3)
            continue
        for job in jobs:
            process_sync_job(job)
        time.sleep(1)


def ensure_sync_worker_started() -> None:
    global sync_worker_started
    with sync_worker_lock:
        if sync_worker_started:
            return
        worker = Thread(target=sync_worker_loop, daemon=True)
        worker.start()
        sync_worker_started = True


def max_pdf_upload_size_label() -> str:
    size_mb = PDF_UPLOAD_MAX_BYTES / (1024 * 1024)
    if float(size_mb).is_integer():
        return f"{int(size_mb)}MB"
    return f"{size_mb:.1f}MB"


def max_image_upload_size_label() -> str:
    size_mb = IMAGE_UPLOAD_MAX_BYTES / (1024 * 1024)
    if float(size_mb).is_integer():
        return f"{int(size_mb)}MB"
    return f"{size_mb:.1f}MB"


def sanitize_media_prefix(value: str) -> str:
    text = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower())
    text = text.strip("-_")
    return text[:40] if text else "image"


def detect_image_extension(filename: str, content_type: str, header_probe: bytes) -> str:
    header = header_probe[:32]
    if header.startswith(b"\xFF\xD8\xFF"):
        return "jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
        return "gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"

    normalized_content_type = str(content_type or "").strip().lower()
    map_content_type = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }
    if normalized_content_type in map_content_type:
        return map_content_type[normalized_content_type]

    suffix = Path(str(filename or "")).suffix.lower().lstrip(".")
    if suffix in {"jpeg", "jpg"}:
        return "jpg"
    if suffix in {"png", "gif", "webp"}:
        return suffix

    raise HTTPException(status_code=400, detail="Imagem invalida. Use JPG, PNG, GIF ou WEBP.")


async def save_uploaded_image_file(file: UploadFile, target_dir: Path, prefix: str) -> str:
    filename = str(file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Nome de arquivo invalido")

    bytes_received = 0
    header_probe = b""
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".upload", dir=str(target_dir)) as temp_file:
            temp_path = temp_file.name
            while True:
                chunk = await file.read(IMAGE_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break

                bytes_received += len(chunk)
                if bytes_received > IMAGE_UPLOAD_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Imagem muito grande. Limite de {max_image_upload_size_label()}.",
                    )

                if len(header_probe) < 64:
                    missing = 64 - len(header_probe)
                    header_probe += chunk[:missing]

                temp_file.write(chunk)

        if bytes_received <= 0:
            raise HTTPException(status_code=400, detail="Arquivo de imagem vazio")

        extension = detect_image_extension(filename, str(file.content_type or ""), header_probe)
        safe_prefix = sanitize_media_prefix(prefix)
        final_name = f"{safe_prefix}-{int(time.time())}-{secrets.token_hex(6)}.{extension}"
        final_path = target_dir / final_name
        os.replace(temp_path, final_path)
        temp_path = ""

        if target_dir == MEDIA_LOGOS_DIR:
            return f"/media/logos/{final_name}"
        if target_dir == MEDIA_PRODUCTS_DIR:
            return f"/media/products/{final_name}"
        raise HTTPException(status_code=500, detail="Diretorio de destino invalido")
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def purge_pdf_jobs(now: float) -> None:
    stale_job_ids: list[str] = []
    for job_id, job_data in list(pdf_jobs.items()):
        status = str(job_data.get("status", ""))
        if status not in {"completed", "failed"}:
            continue
        updated_at = float(job_data.get("updated_at", job_data.get("created_at", now)))
        if now - updated_at >= PDF_JOB_RETENTION_SECONDS:
            stale_job_ids.append(job_id)

    for job_id in stale_job_ids:
        pdf_jobs.pop(job_id, None)


def set_pdf_job_state(job_id: str, **updates: Any) -> None:
    with pdf_jobs_lock:
        current = pdf_jobs.get(job_id)
        if current is None:
            return
        current.update(updates)
        current["updated_at"] = time.time()
        purge_pdf_jobs(time.time())


def create_pdf_job(filename: str, file_size: int, store_id: str = DEFAULT_STORE_ID) -> dict[str, Any]:
    job_id = secrets.token_urlsafe(12)
    now = time.time()
    payload: dict[str, Any] = {
        "job_id": job_id,
        "store_id": normalize_store_id_or_400(store_id),
        "status": "queued",
        "message": "Upload recebido. Processamento em fila.",
        "filename": filename,
        "file_size": int(file_size),
        "created_at": now,
        "updated_at": now,
        "total_products": 0,
        "total_pages": 0,
        "error": "",
    }
    with pdf_jobs_lock:
        purge_pdf_jobs(now)
        pdf_jobs[job_id] = payload
        while len(pdf_jobs) > PDF_JOB_MAX_ENTRIES:
            oldest_id, oldest_job = next(iter(pdf_jobs.items()))
            oldest_status = str(oldest_job.get("status", ""))
            if oldest_status in {"queued", "processing"}:
                break
            pdf_jobs.pop(oldest_id, None)
    return dict(payload)


def get_pdf_job(job_id: str) -> Optional[dict[str, Any]]:
    with pdf_jobs_lock:
        purge_pdf_jobs(time.time())
        payload = pdf_jobs.get(job_id)
        if payload is None:
            return None
        return dict(payload)


def extract_products_with_worker(pdf_path: str) -> tuple[list[dict[str, Any]], int]:
    temp_output_path = ""
    worker_script = Path(__file__).with_name("extract_data.py")
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as output_file:
            temp_output_path = output_file.name

        command = [
            sys.executable,
            str(worker_script),
            "--pdf",
            str(pdf_path),
            "--out",
            temp_output_path,
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PDF_PROCESS_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            if not details:
                details = "Falha ao extrair PDF no worker"
            raise RuntimeError(details.splitlines()[-1][:400])

        with open(temp_output_path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        if not isinstance(parsed, list):
            raise RuntimeError("Worker retornou formato invalido")

        total_pages = 0
        match = re.search(r"Processing\s+(\d+)\s+pages", result.stdout or "")
        if match:
            try:
                total_pages = int(match.group(1))
            except ValueError:
                total_pages = 0
        return parsed, total_pages
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Tempo limite excedido ao processar PDF") from exc
    finally:
        if temp_output_path and os.path.exists(temp_output_path):
            try:
                os.remove(temp_output_path)
            except OSError:
                pass


def process_pdf_job(job_id: str, temp_path: str, store_id: str = DEFAULT_STORE_ID) -> None:
    normalized_store_id = normalize_store_id_or_400(store_id)
    set_pdf_job_state(job_id, status="processing", message="Processando PDF no servidor...")
    try:
        products, total_pages = extract_products_with_worker(temp_path)
        if not products:
            raise ValueError("Nenhum produto valido foi encontrado no PDF")
        existing_products = get_products(normalized_store_id)
        existing_stock_by_id = build_existing_stock_map(existing_products)
        existing_image_url_by_id = build_existing_image_url_map(existing_products)
        merged_products = normalize_product_list(
            products,
            stock_by_id=existing_stock_by_id,
            image_url_by_id=existing_image_url_by_id,
        )
        replace_products(merged_products, normalized_store_id)
        set_pdf_job_state(
            job_id,
            status="completed",
            message="PDF processado com sucesso",
            total_products=len(merged_products),
            total_pages=total_pages,
            store_id=normalized_store_id,
            error="",
        )
    except Exception as exc:
        error_text = str(exc).strip() or "Falha interna ao processar PDF"
        set_pdf_job_state(
            job_id,
            status="failed",
            message="Falha ao processar PDF",
            error=error_text,
        )
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


@app.on_event("startup")
async def startup_event():
    migrate_legacy_files_if_needed()
    init_ops_db()
    _ = get_settings(DEFAULT_STORE_ID)
    _ = get_products_index(DEFAULT_STORE_ID)
    ensure_sync_worker_started()


@app.get("/media/{media_kind}/{filename}")
def get_media_file(media_kind: str, filename: str):
    kind = str(media_kind or "").strip().lower()
    safe_name = Path(str(filename or "")).name
    if safe_name != filename or not safe_name:
        raise HTTPException(status_code=404, detail="Arquivo nao encontrado")

    if kind == "logos":
        base_dir = MEDIA_LOGOS_DIR
    elif kind == "products":
        base_dir = MEDIA_PRODUCTS_DIR
    else:
        raise HTTPException(status_code=404, detail="Arquivo nao encontrado")

    file_path = base_dir / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo nao encontrado")
    return FileResponse(str(file_path), headers={"Cache-Control": "public, max-age=86400"})


@app.get("/info")
def get_info(store: Optional[str] = Query(None)):
    store_id = resolve_public_store_id(store)
    total_products = len(get_products(store_id))
    return {
        "message": "Product API is running. Use /search?query=... to search.",
        "store_id": store_id,
        "total_products": total_products,
    }


@app.get("/public-config")
def get_public_config(store: Optional[str] = Query(None)):
    store_id = resolve_public_store_id(store)
    settings = get_settings(store_id)
    return {
        "store_id": store_id,
        "store_name": settings.get("store_name", DEFAULT_SETTINGS["store_name"]),
        "store_tagline": settings.get("store_tagline", DEFAULT_SETTINGS["store_tagline"]),
        "store_logo_url": settings.get("store_logo_url", DEFAULT_SETTINGS["store_logo_url"]),
        "show_product_images": bool_setting_value(
            settings.get("show_product_images", DEFAULT_SETTINGS["show_product_images"])
        ),
        "theme_bg": settings.get("theme_bg", DEFAULT_SETTINGS["theme_bg"]),
        "theme_bg_alt": settings.get("theme_bg_alt", DEFAULT_SETTINGS["theme_bg_alt"]),
        "theme_surface": settings.get("theme_surface", DEFAULT_SETTINGS["theme_surface"]),
        "theme_text": settings.get("theme_text", DEFAULT_SETTINGS["theme_text"]),
        "theme_muted": settings.get("theme_muted", DEFAULT_SETTINGS["theme_muted"]),
        "theme_accent": settings.get("theme_accent", DEFAULT_SETTINGS["theme_accent"]),
        "theme_accent_strong": settings.get("theme_accent_strong", DEFAULT_SETTINGS["theme_accent_strong"]),
        "theme_accent_deep": settings.get("theme_accent_deep", DEFAULT_SETTINGS["theme_accent_deep"]),
        "creator_name": CREATOR_NAME,
        "creator_whatsapp": CREATOR_WHATSAPP,
    }


@app.get("/order-config")
def get_order_config(store: Optional[str] = Query(None)):
    store_id = resolve_public_store_id(store)
    settings = get_settings(store_id)
    delivery_fee_rules = settings.get("delivery_fee_rules", DEFAULT_SETTINGS["delivery_fee_rules"])
    if not str(delivery_fee_rules).strip():
        delivery_fee_rules = build_delivery_fee_rules_from_legacy(
            settings.get("delivery_fee_amount", DEFAULT_SETTINGS["delivery_fee_amount"]),
            settings.get("delivery_fee_regions", DEFAULT_SETTINGS["delivery_fee_regions"]),
        )
    return {
        "store_id": store_id,
        "api_base_url": settings.get("api_base_url", ""),
        "whatsapp_number": settings.get("whatsapp_number", ""),
        "whatsapp_destinations": settings.get("whatsapp_destinations", DEFAULT_SETTINGS["whatsapp_destinations"]),
        "coupon_title": settings.get("coupon_title", DEFAULT_SETTINGS["coupon_title"]),
        "coupon_message": settings.get("coupon_message", DEFAULT_SETTINGS["coupon_message"]),
        "coupon_address": settings.get("coupon_address", DEFAULT_SETTINGS["coupon_address"]),
        "coupon_footer": settings.get("coupon_footer", DEFAULT_SETTINGS["coupon_footer"]),
        "delivery_fee_amount": settings.get("delivery_fee_amount", DEFAULT_SETTINGS["delivery_fee_amount"]),
        "delivery_fee_regions": settings.get("delivery_fee_regions", DEFAULT_SETTINGS["delivery_fee_regions"]),
        "delivery_fee_rules": str(delivery_fee_rules),
    }


@app.get("/categories")
def get_categories(store: Optional[str] = Query(None)):
    store_id = resolve_public_store_id(store)
    indexed_products, _ = get_products_index(store_id)

    counts: dict[str, int] = {}
    for entry in indexed_products:
        if not bool(entry.get("is_sellable", False)):
            continue
        category = str(entry.get("category", "Outros"))
        counts[category] = counts.get(category, 0) + 1

    categories = [{"name": name, "count": counts[name]} for name in sorted(counts.keys(), key=lambda item: item.lower())]
    return {"store_id": store_id, "categories": categories}


@app.get("/location/reverse-geocode")
def reverse_geocode_location_endpoint(
    lat: float = Query(...),
    lng: float = Query(...),
):
    result = reverse_geocode_coordinates(lat, lng)
    status = str(result.get("status", "") or "").strip().lower()
    if status == "resolved":
        return result
    if status in {"invalid_coordinates", "not_found"}:
        raise HTTPException(status_code=400 if status == "invalid_coordinates" else 404, detail=str(result.get("message", "")))
    raise HTTPException(status_code=503, detail=str(result.get("message", "Falha ao localizar endereco.")))


@app.post("/orders/submit")
def submit_order(payload: dict[str, Any]):
    store_id = resolve_public_store_id(payload.get("store"))
    settings = get_settings(store_id)
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(status_code=400, detail="Selecione ao menos um item para finalizar o pedido.")

    products_by_id: dict[str, dict[str, Any]] = {}
    for item in get_products(store_id):
        try:
            products_by_id[normalize_product_id(item.get("id", ""))] = item
        except HTTPException:
            continue
    items: list[dict[str, Any]] = []
    seen = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        product_id = normalize_product_id(raw_item.get("id", ""))
        if product_id in seen:
            raise HTTPException(status_code=400, detail="Pedido contem produto duplicado.")
        seen.add(product_id)
        product = dict(products_by_id.get(product_id) or {})
        if not product:
            raise HTTPException(status_code=404, detail=f"Produto {product_id} nao encontrado.")
        qty = normalize_stock_value(raw_item.get("qty", 0))
        if qty <= 0:
            raise HTTPException(status_code=400, detail=f"Quantidade invalida para o produto {product_id}.")
        unit_price = parse_price(product.get("price_sight", "0"))
        if unit_price <= 0:
            raise HTTPException(status_code=400, detail=f"Produto {product_id} nao esta disponivel para venda.")
        items.append(
            {
                "id": product_id,
                "description": str(product.get("description", "")).strip(),
                "unit": str(product.get("unit", "UN") or "UN").strip().upper()[:12] or "UN",
                "qty": qty,
                "unit_price": unit_price,
                "subtotal": unit_price * qty,
            }
        )

    delivery_region = normalize_text(payload.get("delivery_region", ""))
    delivery_address = str(payload.get("delivery_address", "")).strip()
    client_delivery_location = build_client_delivery_location(payload)
    payment_method = str(payload.get("payment_method", "pix") or "pix").strip().lower()
    if payment_method not in {"pix", "dinheiro"}:
        raise HTTPException(status_code=400, detail="payment_method invalido")

    products_total = sum(float(item["subtotal"]) for item in items)
    delivery_fee = get_delivery_fee_for_order(settings, delivery_region, delivery_address)
    order_total = products_total + delivery_fee
    region_label = get_delivery_region_label(settings, delivery_region)

    client_total = payload.get("client_total")
    if client_total is not None and abs(parse_price(client_total) - order_total) > 0.01:
        raise HTTPException(status_code=409, detail="Total do pedido divergente do catalogo atual.")

    client_delivery_fee = payload.get("client_delivery_fee")
    if client_delivery_fee is not None and abs(parse_price(client_delivery_fee) - delivery_fee) > 0.01:
        raise HTTPException(status_code=409, detail="Taxa de entrega divergente da configuracao atual.")

    cash_change_for_value = 0.0
    cash_change_for_label = ""
    if payment_method == "dinheiro":
        raw_change = str(payload.get("cash_change_for", "")).strip()
        if not raw_change:
            raise HTTPException(status_code=400, detail="Informe o troco para quanto quando o pagamento for em dinheiro.")
        cash_change_for_value = parse_price(raw_change)
        if cash_change_for_value <= 0:
            raise HTTPException(status_code=400, detail="Troco invalido. Informe um valor maior que zero.")
        if cash_change_for_value < order_total:
            raise HTTPException(status_code=400, detail="O troco deve ser para um valor maior ou igual ao total do pedido.")
        cash_change_for_label = format_currency(cash_change_for_value)

    phone = resolve_order_whatsapp_number(settings, delivery_region, region_label, delivery_address)
    if not phone:
        raise HTTPException(status_code=400, detail="WhatsApp nao configurado para esta loja.")

    protocol = generate_order_protocol()
    whatsapp_message = build_whatsapp_message(
        settings,
        items,
        protocol,
        region_label,
        delivery_address,
        client_delivery_location,
        payment_method,
        cash_change_for_label,
        products_total,
        delivery_fee,
        order_total,
    )
    whatsapp_url = f"https://wa.me/{phone}?text={quote(whatsapp_message)}"

    order_payload = {
        "store_id": store_id,
        "items": items,
        "delivery_region": delivery_region,
        "delivery_region_label": region_label,
        "delivery_address": delivery_address,
        "delivery_location": client_delivery_location,
        "payment_method": payment_method,
        "cash_change_for": cash_change_for_value,
        "products_total": round(products_total, 2),
        "delivery_fee": round(delivery_fee, 2),
        "order_total": round(order_total, 2),
    }

    integration = get_store_integration(store_id)
    integration_mode = str(integration.get("mode", "local_json")).strip().lower()
    initial_sync_status = "pending_retry" if integration_mode == "external_db" else "local_only"
    initial_sync_message = (
        "Aguardando sincronizacao com o banco externo."
        if integration_mode == "external_db"
        else "Loja local sem sincronizacao externa."
    )
    create_order_record(
        store_id,
        protocol,
        order_payload,
        whatsapp_message,
        whatsapp_url,
        initial_sync_status,
        initial_sync_message,
    )
    sync_delivery_location_async(store_id, protocol, dict(order_payload), dict(settings))

    if integration_mode == "external_db":
        sync_result = attempt_order_sync(store_id, protocol)
        if str(sync_result.get("sync_status", "")).strip().lower() != "synced":
            enqueue_sync_job(store_id, protocol, "order_sync", {"protocol": protocol})
    else:
        sync_result = {
            "sync_status": "local_only",
            "sync_message": "Loja local sem sincronizacao externa.",
            "external_reference": "",
        }

    return {
        "store_id": store_id,
        "protocol": protocol,
        "whatsapp_url": whatsapp_url,
        "sync_status": str(sync_result.get("sync_status", "local_only")),
        "sync_message": str(sync_result.get("sync_message", "")),
        "order_total": round(order_total, 2),
        "products_total": round(products_total, 2),
        "delivery_fee": round(delivery_fee, 2),
    }


@app.post("/admin/login")
def admin_login(payload: dict[str, Any], request: Request):
    now = time.time()
    ip_address = get_request_ip(request)
    enforce_login_rate_limit(ip_address, now)

    username = normalize_username(payload.get("username", ""))
    password = str(payload.get("password", ""))
    if not password:
        register_login_failure(ip_address, now)
        raise HTTPException(status_code=401, detail="Credenciais invalidas")

    auth_user = authenticate_file_user(username, password)
    if auth_user is None:
        expected_username = normalize_username(ADMIN_USER)
        if not secrets.compare_digest(username, expected_username) or not secrets.compare_digest(password, ADMIN_PASSWORD):
            register_login_failure(ip_address, now)
            raise HTTPException(status_code=401, detail="Credenciais invalidas")
        auth_user = {
            "username": username,
            "role": "master",
            "store_id": DEFAULT_STORE_ID,
        }

    clear_login_failures(ip_address)
    token = issue_admin_token(
        username=str(auth_user["username"]),
        role=str(auth_user.get("role", "master")),
        store_id=str(auth_user.get("store_id", DEFAULT_STORE_ID)),
    )
    return {
        "token": token,
        "expires_in": ADMIN_TOKEN_TTL_SECONDS,
        "username": auth_user["username"],
        "role": auth_user.get("role", "master"),
        "store_id": auth_user.get("store_id", DEFAULT_STORE_ID),
    }


@app.get("/admin/me")
def admin_me(store: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    session = require_admin_session(authorization)
    selected_store_id = resolve_store_for_admin_session(session, store)
    return {
        "username": session.get("username", ""),
        "role": session.get("role", "master"),
        "store_id": session.get("store_id", DEFAULT_STORE_ID),
        "selected_store_id": selected_store_id,
        "expires_in": ADMIN_TOKEN_TTL_SECONDS,
    }


@app.get("/admin/dashboard/metrics")
def admin_dashboard_metrics(
    period: str = Query("today"),
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    return build_dashboard_metrics_payload(store_id, period)


@app.get("/admin/location/status")
def admin_location_status(store: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    settings = get_settings(store_id)
    payload = get_delivery_location_status(store_id, database_url=get_location_supabase_db_url(store_id))
    payload["store_id"] = store_id
    payload["delivery_geo_city"] = settings.get("delivery_geo_city", DEFAULT_SETTINGS["delivery_geo_city"])
    payload["delivery_geo_state"] = settings.get("delivery_geo_state", DEFAULT_SETTINGS["delivery_geo_state"])
    payload["delivery_geo_country"] = settings.get("delivery_geo_country", DEFAULT_SETTINGS["delivery_geo_country"])
    return payload


@app.post("/admin/location/test")
def admin_test_location_integration(store: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    try:
        ensure_delivery_locations_table(get_location_supabase_db_url(store_id))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    payload = get_delivery_location_status(store_id, database_url=get_location_supabase_db_url(store_id))
    payload["store_id"] = store_id
    payload["message"] = "Tabela de localizacao criada/verificada com sucesso no Supabase."
    return payload


@app.post("/admin/location/sync")
def admin_sync_location_orders(
    period: str = Query("today"),
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    if str(period or "today").strip().lower() != "today":
        raise HTTPException(status_code=400, detail="periodo invalido. Use period=today.")
    start_at, end_at = build_today_range()
    orders = list_orders_by_created_range(store_id, created_from=start_at, created_to=end_at)
    settings = get_settings(store_id)
    try:
        result = sync_delivery_locations_batch(
            store_id,
            orders,
            settings,
            database_url=get_location_supabase_db_url(store_id),
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    result["store_id"] = store_id
    result["message"] = f"Localizacao sincronizada para {int(result.get('synced', 0) or 0)} pedido(s)."
    return result


@app.get("/admin/config")
def admin_get_config(store: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    return build_admin_config_payload(store_id)


@app.put("/admin/config")
def admin_update_config(
    payload: dict[str, Any],
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)

    updates: dict[str, str] = {}
    for key in ALLOWED_SETTING_KEYS:
        if key in payload:
            updates[key] = normalize_setting_value(key, payload[key])

    current_integration = get_store_integration(store_id)
    integration_updates = build_integration_updates(payload, current_integration)

    if not updates and not integration_updates:
        return {"message": "Nenhuma alteracao enviada", "config": build_admin_config_payload(store_id)}

    if updates:
        update_settings(updates, store_id)
    if integration_updates:
        save_store_integration(store_id, integration_updates)
    return {"message": "Configuracoes atualizadas", "config": build_admin_config_payload(store_id)}


@app.post("/admin/integration/test")
def admin_test_integration(store: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    integration = get_store_integration(store_id)
    connector = build_store_connector(integration)
    try:
        result = connector.healthcheck()
        save_store_integration(
            store_id,
            {
                "status": "healthy",
                "last_error": "",
                "last_healthcheck_at": time.time(),
            },
        )
        return {
            "message": str(result.get("message", "Conexao validada.")),
            "result": result,
            "config": build_admin_config_payload(store_id),
        }
    except Exception as exc:
        error_text = str(exc).strip() or "Falha ao testar conexao."
        save_store_integration(
            store_id,
            {
                "status": "healthcheck_error",
                "last_error": error_text,
                "last_healthcheck_at": time.time(),
            },
        )
        raise HTTPException(status_code=400, detail=error_text) from exc


@app.post("/admin/integration/sync")
def admin_sync_integration_catalog(store: Optional[str] = Query(None), authorization: Optional[str] = Header(None)):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    try:
        result = sync_catalog_from_external(store_id)
        return {
            "message": f"Catalogo sincronizado com {result['products']} produtos.",
            "result": result,
            "config": build_admin_config_payload(store_id),
        }
    except HTTPException:
        raise
    except Exception as exc:
        error_text = str(exc).strip() or "Falha ao sincronizar catalogo."
        save_store_integration(
            store_id,
            {
                "status": "catalog_sync_error",
                "last_error": error_text,
            },
        )
        raise HTTPException(status_code=400, detail=error_text) from exc


@app.get("/admin/orders")
def admin_list_orders(
    sync_status: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    orders = list_orders(store_id, sync_status=sync_status, limit=limit)
    results = []
    for item in orders:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        results.append(
            {
                "protocol": str(item.get("protocol", "")),
                "sync_status": str(item.get("sync_status", "")),
                "sync_message": str(item.get("sync_message", "")),
                "attempts": int(item.get("attempts", 0) or 0),
                "last_error": str(item.get("last_error", "")),
                "created_at": float(item.get("created_at", 0) or 0),
                "order_total": float(payload.get("order_total", 0) or 0),
            }
        )
    return {"store_id": store_id, "results": results}


@app.post("/admin/orders/{protocol}/retry")
def admin_retry_order_sync(
    protocol: str,
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    normalize_store_id_or_400(store_id)
    result = attempt_order_sync(store_id, protocol)
    if str(result.get("sync_status", "")).strip().lower() != "synced":
        enqueue_sync_job(store_id, protocol, "order_sync", {"protocol": protocol})
    return {
        "message": str(result.get("sync_message", "Tentativa executada.")),
        "result": result,
        "config": build_admin_config_payload(store_id),
    }


@app.post("/admin/orders/{protocol}/route-status")
def admin_update_order_route_status(
    protocol: str,
    payload: dict[str, Any],
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    order = get_order_record(store_id, protocol)
    if not order:
        raise HTTPException(status_code=404, detail="Pedido nao encontrado")

    next_status = str(payload.get("status", "") or "").strip().lower()
    if next_status not in ORDER_ROUTE_STATUS_LABELS:
        raise HTTPException(status_code=400, detail="Status de rota invalido")

    order_payload = dict(order.get("payload") or {})
    now = time.time()
    if next_status == "route_started":
        order_payload["route_started_at"] = float(order_payload.get("route_started_at", 0) or now)
        order_payload["route_completed_at"] = 0.0
    elif next_status == "delivered":
        if not float(order_payload.get("route_started_at", 0) or 0):
            order_payload["route_started_at"] = now
        order_payload["route_completed_at"] = now
    else:
        order_payload["route_started_at"] = 0.0
        order_payload["route_completed_at"] = 0.0

    updated = update_order_record(
        store_id,
        protocol,
        {
            "order_status": next_status,
            "payload": order_payload,
        },
    )
    return {
        "message": f"Pedido {protocol} atualizado para {ORDER_ROUTE_STATUS_LABELS[next_status].lower()}.",
        "order": {
            "protocol": str(updated.get("protocol", "")),
            "order_status": str(updated.get("order_status", "")),
            "route_started_at": float(order_payload.get("route_started_at", 0) or 0),
            "route_completed_at": float(order_payload.get("route_completed_at", 0) or 0),
        },
    }


@app.post("/admin/upload/logo")
async def admin_upload_logo(
    file: UploadFile = File(...),
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    try:
        media_url = await save_uploaded_image_file(file, MEDIA_LOGOS_DIR, prefix=f"{store_id}-logo")
        return {"message": "Logo enviada com sucesso", "media_url": media_url}
    finally:
        await file.close()


@app.post("/admin/upload/product-image")
async def admin_upload_product_image(
    file: UploadFile = File(...),
    product_id: Optional[str] = Query(None, max_length=60),
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    ensure_store_catalog_is_mutable(store_id)
    safe_product_id = ""
    if product_id:
        safe_product_id = normalize_product_id(product_id)
    prefix = f"{store_id}-product-{safe_product_id}" if safe_product_id else f"{store_id}-product"
    try:
        media_url = await save_uploaded_image_file(file, MEDIA_PRODUCTS_DIR, prefix=prefix)
        return {"message": "Imagem enviada com sucesso", "media_url": media_url}
    finally:
        await file.close()


@app.get("/admin/products")
def admin_list_products(
    query: str = Query("", max_length=120),
    limit: int = Query(5, ge=1, le=5),
    offset: int = Query(0, ge=0),
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    query_terms = normalize_query_terms(query)

    indexed_products, _ = get_products_index(store_id)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for entry in indexed_products:
        item = dict(entry["product"])
        if query_terms:
            id_score, id_matches = score_text_against_terms(str(item.get("id", "")), query_terms)
            desc_score, desc_matches = score_text_against_terms(str(item.get("description", "")), query_terms)
            matched_terms = max(id_matches, desc_matches)
            if matched_terms <= 0:
                continue
            if matched_terms < len(query_terms):
                continue
            total_score = (desc_score * 2) + id_score
            ranked.append((total_score, item))
        else:
            ranked.append((0, item))

    ranked.sort(key=lambda pair: (-pair[0], code_sort_key(pair[1].get("id", ""))))
    total = len(ranked)
    paged = ranked[offset : offset + limit]
    results = [item for _score, item in paged]
    return {
        "store_id": store_id,
        "total": total,
        "count": len(results),
        "offset": offset,
        "limit": limit,
        "results": results,
    }


@app.post("/admin/products")
def admin_create_product(
    payload: dict[str, Any],
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    ensure_store_catalog_is_mutable(store_id)
    new_product = build_product_from_admin_payload(payload)

    products = [dict(item) for item in get_products(store_id)]
    existing_index = find_product_position(products, new_product["id"])
    if existing_index >= 0:
        raise HTTPException(status_code=409, detail="Ja existe produto com este id")

    products.append(new_product)
    replace_products(products, store_id)
    return {"message": "Produto criado com sucesso", "product": new_product}


@app.put("/admin/products/{product_id:path}")
def admin_update_product(
    product_id: str,
    payload: dict[str, Any],
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    ensure_store_catalog_is_mutable(store_id)
    normalized_id = normalize_product_id(product_id)

    products = [dict(item) for item in get_products(store_id)]
    target_index = find_product_position(products, normalized_id)
    if target_index < 0:
        raise HTTPException(status_code=404, detail="Produto nao encontrado")

    current = dict(products[target_index])
    if "description" in payload:
        description = str(payload.get("description", "")).strip()
        if not description:
            raise HTTPException(status_code=400, detail="description obrigatorio")
        current["description"] = description
    if "unit" in payload:
        current["unit"] = str(payload.get("unit", "UN") or "UN").strip().upper()[:12] or "UN"
    if "price_sight" in payload:
        current["price_sight"] = normalize_price_text(payload.get("price_sight"), "price_sight")
    if "price_term" in payload:
        current["price_term"] = normalize_price_text(payload.get("price_term"), "price_term")
    if "price_wholesale" in payload:
        current["price_wholesale"] = normalize_price_text(payload.get("price_wholesale"), "price_wholesale")
    if "stock" in payload:
        current["stock"] = normalize_stock_value(payload.get("stock"))
    if "image_url" in payload:
        current["image_url"] = normalize_optional_media_url(payload.get("image_url"), "image_url", strict=True)

    normalized = normalize_product_record(current)
    if not normalized:
        raise HTTPException(status_code=400, detail="Produto invalido")

    products[target_index] = normalized
    replace_products(products, store_id)
    return {"message": "Produto atualizado com sucesso", "product": normalized}


@app.delete("/admin/products/{product_id:path}")
def admin_delete_product(
    product_id: str,
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    ensure_store_catalog_is_mutable(store_id)
    normalized_id = normalize_product_id(product_id)

    products = [dict(item) for item in get_products(store_id)]
    target_index = find_product_position(products, normalized_id)
    if target_index < 0:
        raise HTTPException(status_code=404, detail="Produto nao encontrado")

    removed = products.pop(target_index)
    replace_products(products, store_id)
    return {"message": "Produto removido com sucesso", "product": removed}


@app.get("/")
def read_root():
    return FileResponse("index.html", headers={"Cache-Control": "no-store"})


@app.get("/gerenciador")
def read_manager():
    raise HTTPException(status_code=404, detail="Pagina nao encontrada")


@app.get("/painel")
def read_manager_panel():
    return FileResponse("gerenciador.html", headers={"Cache-Control": "no-store"})


@app.get("/Daniel@qwe")
def read_manager_fixed():
    return FileResponse("gerenciador.html", headers={"Cache-Control": "no-store"})


@app.get(MANAGER_ENTRY_ROUTE)
def read_manager_hidden():
    return FileResponse("gerenciador.html", headers={"Cache-Control": "no-store"})


@app.post("/upload-pdf", status_code=202)
async def upload_pdf(
    file: UploadFile = File(...),
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)
    ensure_store_catalog_is_mutable(store_id)

    filename = str(file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Nome de arquivo invalido")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Formato invalido: envie um arquivo PDF")

    temp_path = ""
    bytes_received = 0
    header_probe = b""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            temp_path = temp_file.name
            while True:
                chunk = await file.read(PDF_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break

                bytes_received += len(chunk)
                if bytes_received > PDF_UPLOAD_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Arquivo muito grande. Limite de {max_pdf_upload_size_label()}.",
                    )

                if len(header_probe) < 1024:
                    missing = 1024 - len(header_probe)
                    header_probe += chunk[:missing]

                temp_file.write(chunk)

        if bytes_received <= 0:
            raise HTTPException(status_code=400, detail="Arquivo vazio")
        if b"%PDF" not in header_probe:
            raise HTTPException(status_code=400, detail="Arquivo invalido: cabecalho PDF nao encontrado")

        job_payload = create_pdf_job(filename=filename, file_size=bytes_received, store_id=store_id)
        worker = Thread(target=process_pdf_job, args=(str(job_payload["job_id"]), temp_path, store_id), daemon=True)
        worker.start()
        temp_path = ""

        return {
            "message": "Upload concluido. Processamento iniciado.",
            "job_id": job_payload["job_id"],
            "status": "queued",
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Falha no upload: {exc}") from exc
    finally:
        await file.close()
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


@app.get("/upload-pdf/status/{job_id}")
def get_upload_pdf_status(
    job_id: str,
    store: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    session = require_admin_session(authorization)
    store_id = resolve_store_for_admin_session(session, store)

    current = get_pdf_job(job_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Processamento nao encontrado ou expirado")
    if normalize_store_id_or_400(current.get("store_id", DEFAULT_STORE_ID)) != store_id:
        raise HTTPException(status_code=404, detail="Processamento nao encontrado para esta loja")

    payload = {
        "job_id": str(current.get("job_id", job_id)),
        "store_id": store_id,
        "status": str(current.get("status", "queued")),
        "message": str(current.get("message", "")),
        "filename": str(current.get("filename", "")),
        "file_size": int(current.get("file_size", 0)),
        "total_products": int(current.get("total_products", 0)),
        "total_pages": int(current.get("total_pages", 0)),
    }
    error_text = str(current.get("error", "")).strip()
    if error_text:
        payload["error"] = error_text
    return payload


@app.get("/search")
def search_products(
    query: str = Query(..., min_length=1, description="Search term for product name"),
    category: Optional[str] = Query(None, min_length=1, max_length=80, description="Product category"),
    min_price: Optional[float] = Query(None, ge=0, description="Minimum product price (a vista)"),
    max_price: Optional[float] = Query(None, ge=0, description="Maximum product price (a vista)"),
    sort_by: str = Query("relevance", pattern="^(relevance|price_asc|price_desc|name|code)$"),
    limit: int = Query(DEFAULT_SEARCH_LIMIT, ge=1, le=10),
    offset: int = Query(0, ge=0),
    store: Optional[str] = Query(None),
    _x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Search for products by description with intelligent synonym-aware ranking.
    """
    query_terms = normalize_query_terms(query)
    if not query_terms:
        return {"count": 0, "results": []}

    if min_price is not None and max_price is not None and min_price > max_price:
        return {"total": 0, "count": 0, "results": [], "offset": offset, "limit": limit}

    store_id = resolve_public_store_id(store)
    indexed_products, current_version = get_products_index(store_id)
    category_filter = str(category or "").strip().lower() or None
    query_variants = [(term, SYNONYM_MAP.get(term)) for term in query_terms]
    cleaned_query = " ".join(query_terms)

    cache_key = (store_id, current_version, tuple(query_terms), category_filter, min_price, max_price, sort_by, limit, offset)
    cached = get_cached_search(cache_key)
    if cached is not None:
        return {
            "total": cached.get("total", 0),
            "count": cached.get("count", 0),
            "offset": offset,
            "limit": limit,
            "results": [dict(item) for item in cached.get("results", [])],
        }

    scored_results: list[tuple[float, dict[str, Any]]] = []
    for entry in indexed_products:
        desc = str(entry["description"])
        clean_desc = str(entry["clean_description"])
        desc_words = entry["words"]
        desc_word_set = entry["word_set"]
        price = float(entry["price"])

        if not bool(entry.get("is_sellable", False)):
            continue

        if category_filter and str(entry["category_lower"]) != category_filter:
            continue
        if min_price is not None and price < min_price:
            continue
        if max_price is not None and price > max_price:
            continue

        score = 0
        matched_terms = 0
        for term, term_synonym in query_variants:
            term_found = False

            if term in desc_word_set or (term_synonym and term_synonym in desc_word_set):
                score += 200
                term_found = True
            elif any(word.startswith(term) for word in desc_words) or (
                term_synonym and any(word.startswith(term_synonym) for word in desc_words)
            ):
                score += 100
                term_found = True
            elif term in desc or (term_synonym and term_synonym in desc):
                score += 50
                term_found = True

            if term_found:
                matched_terms += 1

        if matched_terms < len(query_terms):
            continue
        if desc.startswith(query_terms[0]):
            score += 150
        if len(query_terms) > 1 and cleaned_query in clean_desc:
            score += 300

        score -= len(desc) * 0.1
        scored_results.append((score, entry))

    total = len(scored_results)
    window_end = offset + limit
    if total == 0:
        return {
            "total": 0,
            "count": 0,
            "offset": offset,
            "limit": limit,
            "results": [],
        }

    if sort_by == "price_asc":
        sort_key = lambda x: (float(x[1]["price"]), -x[0])
        ranked = heapq.nsmallest(window_end, scored_results, key=sort_key) if window_end < total else sorted(
            scored_results, key=sort_key
        )
    elif sort_by == "price_desc":
        sort_key = lambda x: (-float(x[1]["price"]), -x[0])
        ranked = heapq.nsmallest(window_end, scored_results, key=sort_key) if window_end < total else sorted(
            scored_results, key=sort_key
        )
    elif sort_by == "name":
        sort_key = lambda x: str(x[1]["description"])
        ranked = heapq.nsmallest(window_end, scored_results, key=sort_key) if window_end < total else sorted(
            scored_results, key=sort_key
        )
    elif sort_by == "code":
        sort_key = lambda x: code_sort_key(x[1]["product"].get("id", ""))
        ranked = heapq.nsmallest(window_end, scored_results, key=sort_key) if window_end < total else sorted(
            scored_results, key=sort_key
        )
    else:
        ranked = heapq.nlargest(window_end, scored_results, key=lambda x: x[0]) if window_end < total else sorted(
            scored_results, key=lambda x: x[0], reverse=True
        )

    paginated = ranked[offset:window_end]
    results: list[dict[str, Any]] = []
    for _score, entry in paginated:
        item = dict(entry["product"])
        product_category = str(entry["category"])
        item["category"] = product_category
        results.append(item)

    payload = {
        "total": total,
        "count": len(results),
        "results": results,
    }
    set_cached_search(cache_key, payload)
    return {
        "total": payload["total"],
        "count": payload["count"],
        "offset": offset,
        "limit": limit,
        "results": [dict(item) for item in payload["results"]],
    }


@app.get("/products")
def list_products(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    store: Optional[str] = Query(None),
):
    store_id = resolve_public_store_id(store)
    indexed_products, _ = get_products_index(store_id)
    visible_products = [dict(entry["product"]) for entry in indexed_products if bool(entry.get("is_sellable", False))]
    return {"store_id": store_id, "results": visible_products[offset : offset + limit]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
