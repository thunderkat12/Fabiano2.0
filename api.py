from typing import Optional, Any
from collections import OrderedDict
import heapq
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from threading import Lock

import uvicorn
from fastapi import FastAPI, Query, Header, HTTPException, UploadFile, File, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from extract_data import extract_products_from_pdf, save_products

app = FastAPI(title="Product API", description="API to search products from extracted PDF")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


DATA_DIR = Path("data")
LEGACY_STORE_DIR = DATA_DIR / "stores" / "default"
PRODUCTS_FILE = Path("products.json")
SETTINGS_FILE = Path("app_settings.json")
LEGACY_PRODUCTS_FILE = LEGACY_STORE_DIR / "products.json"
LEGACY_SETTINGS_FILE = LEGACY_STORE_DIR / "settings.json"

CREATOR_NAME = "thunderkat12"
CREATOR_WHATSAPP = "61995651684"
ADMIN_USER = os.getenv("ADMIN_USER", os.getenv("MASTER_USER", "admin")).strip().lower() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", os.getenv("MASTER_PASSWORD", "daniel142536"))
ADMIN_TOKEN_TTL_SECONDS = int(os.getenv("ADMIN_TOKEN_TTL_SECONDS", "28800"))
ADMIN_LOGIN_MAX_ATTEMPTS = max(1, int(os.getenv("ADMIN_LOGIN_MAX_ATTEMPTS", "5")))
ADMIN_LOGIN_WINDOW_SECONDS = max(60, int(os.getenv("ADMIN_LOGIN_WINDOW_SECONDS", "300")))
ADMIN_LOGIN_BLOCK_SECONDS = max(60, int(os.getenv("ADMIN_LOGIN_BLOCK_SECONDS", "900")))

DEFAULT_MANAGER_ENTRY_KEY = "Daniel@qwe"
MANAGER_ENTRY_KEY = os.getenv("MANAGER_ENTRY_KEY", DEFAULT_MANAGER_ENTRY_KEY).strip()
if "#" in MANAGER_ENTRY_KEY:
    MANAGER_ENTRY_KEY = MANAGER_ENTRY_KEY.split("#", 1)[0]
MANAGER_ENTRY_KEY = MANAGER_ENTRY_KEY.strip().strip("/")
if not MANAGER_ENTRY_KEY:
    MANAGER_ENTRY_KEY = DEFAULT_MANAGER_ENTRY_KEY
MANAGER_ENTRY_ROUTE = f"/{MANAGER_ENTRY_KEY}"

USERNAME_PATTERN = re.compile(r"^[a-z0-9_.-]{3,40}$")

products_cache: Optional[list[dict[str, Any]]] = None
products_index_cache: Optional[list[dict[str, Any]]] = None
products_version = 0
settings_cache: Optional[dict[str, str]] = None

products_lock = Lock()
settings_lock = Lock()
admin_tokens_lock = Lock()
search_cache_lock = Lock()
login_attempts_lock = Lock()
admin_tokens: dict[str, dict[str, Any]] = {}
search_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
login_attempts: dict[str, dict[str, float | int]] = {}

SEARCH_CACHE_MAX_SIZE = max(0, int(os.getenv("SEARCH_CACHE_MAX_SIZE", "128")))
DEFAULT_SEARCH_LIMIT = 10

NON_WORD_PATTERN = re.compile(r"[^\w\s]")
DIGITS_PATTERN = re.compile(r"\D")

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


DEFAULT_SETTINGS = {
    "store_name": os.getenv("STORE_NAME", "Busca Inteligente de Produtos"),
    "store_tagline": os.getenv("STORE_TAGLINE", "Busca com filtros, paginacao e carrinho persistente."),
    "api_base_url": os.getenv("API_BASE_URL", "").strip(),
    "whatsapp_number": env_text("ORDER_WHATSAPP_NUMBER", ""),
    "coupon_title": env_text("ORDER_COUPON_TITLE", "CUPOM DE PEDIDO"),
    "coupon_message": env_text("ORDER_COUPON_MESSAGE", "Segue pedido com itens selecionados."),
    "coupon_address": env_text("ORDER_COUPON_ADDRESS", ""),
    "coupon_footer": env_text("ORDER_COUPON_FOOTER", "Obrigado pela preferencia."),
}
ALLOWED_SETTING_KEYS = set(DEFAULT_SETTINGS.keys())


def normalize_setting_value(key: str, value: Any) -> str:
    text = str(value).replace("\r\n", "\n").strip()
    if key == "api_base_url":
        return text.rstrip("/")
    return text


def normalize_username(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not USERNAME_PATTERN.fullmatch(text):
        raise HTTPException(
            status_code=400,
            detail="username invalido. Use 3-40 caracteres: letras, numeros, '.', '_' ou '-'.",
        )
    return text


def load_settings_from_disk() -> dict[str, str]:
    loaded: dict[str, Any] = {}
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            if isinstance(parsed, dict):
                loaded = parsed
        except (OSError, json.JSONDecodeError):
            loaded = {}

    merged = dict(DEFAULT_SETTINGS)
    for key in ALLOWED_SETTING_KEYS:
        if key in loaded:
            merged[key] = normalize_setting_value(key, loaded[key])
    return merged


def write_settings_to_disk(settings: dict[str, str]) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def get_settings() -> dict[str, str]:
    global settings_cache
    with settings_lock:
        if settings_cache is None:
            settings_cache = load_settings_from_disk()
        return dict(settings_cache)


def update_settings(updates: dict[str, str]) -> dict[str, str]:
    global settings_cache
    with settings_lock:
        current = settings_cache if settings_cache is not None else load_settings_from_disk()
        merged = dict(current)
        merged.update(updates)
        settings_cache = merged
        write_settings_to_disk(merged)
        return dict(merged)


def load_products_from_disk() -> list[dict[str, Any]]:
    if not PRODUCTS_FILE.exists():
        return []
    try:
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        if isinstance(parsed, list):
            return parsed
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


def clear_search_cache() -> None:
    with search_cache_lock:
        search_cache.clear()


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


def get_products() -> list[dict[str, Any]]:
    global products_cache
    with products_lock:
        if products_cache is None:
            products_cache = load_products_from_disk()
        return products_cache


def get_products_index() -> tuple[list[dict[str, Any]], int]:
    global products_cache, products_index_cache, products_version
    with products_lock:
        if products_cache is None:
            products_cache = load_products_from_disk()
        if products_index_cache is None:
            products_index_cache = build_products_index(products_cache)
        return products_index_cache, products_version


def replace_products(products: list[dict[str, Any]]) -> None:
    global products_cache, products_index_cache, products_version
    with products_lock:
        save_products(products, str(PRODUCTS_FILE))
        products_cache = products
        products_index_cache = build_products_index(products)
        products_version += 1
    clear_search_cache()


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


def issue_admin_token(username: str) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + ADMIN_TOKEN_TTL_SECONDS
    payload = {
        "username": username,
        "role": "admin",
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


def normalize_text(value: str) -> str:
    cleaned = NON_WORD_PATTERN.sub(" ", str(value).lower().strip())
    return " ".join(cleaned.split())


def tokenize(value: str) -> list[str]:
    normalized = normalize_text(value)
    if not normalized:
        return []
    return normalized.split()


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


@app.on_event("startup")
async def startup_event():
    migrate_legacy_files_if_needed()
    _ = get_settings()
    _ = get_products_index()


@app.get("/info")
def get_info():
    total_products = len(get_products())
    return {
        "message": "Product API is running. Use /search?query=... to search.",
        "total_products": total_products,
    }


@app.get("/public-config")
def get_public_config():
    settings = get_settings()
    return {
        "store_name": settings.get("store_name", DEFAULT_SETTINGS["store_name"]),
        "store_tagline": settings.get("store_tagline", DEFAULT_SETTINGS["store_tagline"]),
        "creator_name": CREATOR_NAME,
        "creator_whatsapp": CREATOR_WHATSAPP,
    }


@app.get("/order-config")
def get_order_config():
    settings = get_settings()
    return {
        "api_base_url": settings.get("api_base_url", ""),
        "whatsapp_number": settings.get("whatsapp_number", ""),
        "coupon_title": settings.get("coupon_title", DEFAULT_SETTINGS["coupon_title"]),
        "coupon_message": settings.get("coupon_message", DEFAULT_SETTINGS["coupon_message"]),
        "coupon_address": settings.get("coupon_address", DEFAULT_SETTINGS["coupon_address"]),
        "coupon_footer": settings.get("coupon_footer", DEFAULT_SETTINGS["coupon_footer"]),
    }


@app.get("/categories")
def get_categories():
    indexed_products, _ = get_products_index()

    counts: dict[str, int] = {}
    for entry in indexed_products:
        if not bool(entry.get("is_sellable", False)):
            continue
        category = str(entry.get("category", "Outros"))
        counts[category] = counts.get(category, 0) + 1

    categories = [{"name": name, "count": counts[name]} for name in sorted(counts.keys(), key=lambda item: item.lower())]
    return {"categories": categories}


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

    expected_username = normalize_username(ADMIN_USER)
    if not secrets.compare_digest(username, expected_username) or not secrets.compare_digest(password, ADMIN_PASSWORD):
        register_login_failure(ip_address, now)
        raise HTTPException(status_code=401, detail="Credenciais invalidas")

    clear_login_failures(ip_address)
    token = issue_admin_token(username=username)
    return {
        "token": token,
        "expires_in": ADMIN_TOKEN_TTL_SECONDS,
        "username": username,
        "role": "admin",
    }


@app.get("/admin/me")
def admin_me(authorization: Optional[str] = Header(None)):
    session = require_admin_session(authorization)
    return {
        "username": session.get("username", ""),
        "role": session.get("role", "admin"),
        "expires_in": ADMIN_TOKEN_TTL_SECONDS,
    }


@app.get("/admin/config")
def admin_get_config(authorization: Optional[str] = Header(None)):
    _ = require_admin_session(authorization)
    return get_settings()


@app.put("/admin/config")
def admin_update_config(payload: dict[str, Any], authorization: Optional[str] = Header(None)):
    _ = require_admin_session(authorization)

    updates: dict[str, str] = {}
    for key in ALLOWED_SETTING_KEYS:
        if key in payload:
            updates[key] = normalize_setting_value(key, payload[key])

    if not updates:
        return {"message": "Nenhuma alteracao enviada", "config": get_settings()}

    updated = update_settings(updates)
    return {"message": "Configuracoes atualizadas", "config": updated}


@app.get("/")
def read_root():
    return FileResponse("index.html")


@app.get("/gerenciador")
def read_manager():
    raise HTTPException(status_code=404, detail="Pagina nao encontrada")


@app.get(MANAGER_ENTRY_ROUTE)
def read_manager_hidden():
    return FileResponse("gerenciador.html", headers={"Cache-Control": "no-store"})


@app.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...), authorization: Optional[str] = Header(None)):
    _ = require_admin_session(authorization)

    filename = str(file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Nome de arquivo invalido")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Formato invalido: envie um arquivo PDF")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Arquivo vazio")
    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Arquivo invalido: cabecalho PDF nao encontrado")

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name

        products, total_pages = await run_in_threadpool(extract_products_from_pdf, temp_path)
        if not products:
            raise HTTPException(status_code=422, detail="Nenhum produto valido foi encontrado no PDF")

        await run_in_threadpool(replace_products, products)
        return {
            "message": "PDF processado com sucesso",
            "total_products": len(products),
            "total_pages": total_pages,
            "data_file": str(PRODUCTS_FILE),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Falha ao processar PDF: {exc}") from exc
    finally:
        await file.close()
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


@app.get("/search")
def search_products(
    query: str = Query(..., min_length=1, description="Search term for product name"),
    category: Optional[str] = Query(None, min_length=1, max_length=80, description="Product category"),
    min_price: Optional[float] = Query(None, ge=0, description="Minimum product price (a vista)"),
    max_price: Optional[float] = Query(None, ge=0, description="Maximum product price (a vista)"),
    sort_by: str = Query("relevance", pattern="^(relevance|price_asc|price_desc|name|code)$"),
    limit: int = Query(DEFAULT_SEARCH_LIMIT, ge=1, le=10),
    offset: int = Query(0, ge=0),
    _x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Search for products by description with intelligent synonym-aware ranking.
    """
    query_terms = tokenize(query)
    if not query_terms:
        return {"count": 0, "results": []}

    if min_price is not None and max_price is not None and min_price > max_price:
        return {"total": 0, "count": 0, "results": [], "offset": offset, "limit": limit}

    indexed_products, current_version = get_products_index()
    category_filter = str(category or "").strip().lower() or None
    query_variants = [(term, SYNONYM_MAP.get(term)) for term in query_terms]
    cleaned_query = " ".join(query_terms)

    cache_key = (current_version, tuple(query_terms), category_filter, min_price, max_price, sort_by, limit, offset)
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
):
    indexed_products, _ = get_products_index()
    visible_products = [dict(entry["product"]) for entry in indexed_products if bool(entry.get("is_sellable", False))]
    return {"results": visible_products[offset : offset + limit]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
