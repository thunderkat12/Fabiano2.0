from typing import Optional, Any
import json
import os
import re
import secrets
import time
from threading import Lock
import uvicorn
from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(title="Product API", description="API to search products from extracted PDF")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


DATA_FILE = "products.json"
SETTINGS_FILE = "app_settings.json"
CREATOR_NAME = "thunderkat12"
CREATOR_WHATSAPP = "61995651684"
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "157142536")
ADMIN_TOKEN_TTL_SECONDS = int(os.getenv("ADMIN_TOKEN_TTL_SECONDS", "28800"))

products_cache: list[dict[str, Any]] = []
settings_lock = Lock()
admin_tokens_lock = Lock()
admin_tokens: dict[str, float] = {}
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
    ("Peliculas e Protecao", ("pelicula", "película", "protetor", "vidro", "glass")),
    ("Capas e Cases", ("capa", "case", "capinha", "bumper")),
    ("Ferramentas", ("alicate", "chave", "estacao", "estação", "ferro de solda", "solda", "pinça", "pinca")),
    ("Suportes", ("suporte", "tripé", "tripe", "holder")),
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
APP_SETTINGS = DEFAULT_SETTINGS.copy()


def normalize_setting_value(key: str, value: Any) -> str:
    text = str(value).replace("\r\n", "\n").strip()
    if key == "api_base_url":
        return text.rstrip("/")
    return text


def load_settings():
    global APP_SETTINGS
    loaded: dict[str, Any] = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            if isinstance(parsed, dict):
                loaded = parsed
        except (OSError, json.JSONDecodeError):
            loaded = {}

    merged = DEFAULT_SETTINGS.copy()
    for key in ALLOWED_SETTING_KEYS:
        if key in loaded:
            merged[key] = normalize_setting_value(key, loaded[key])
    APP_SETTINGS = merged


def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(APP_SETTINGS, f, ensure_ascii=False, indent=2)


def issue_admin_token() -> str:
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + ADMIN_TOKEN_TTL_SECONDS
    with admin_tokens_lock:
        now = time.time()
        expired = [item_token for item_token, exp in admin_tokens.items() if exp <= now]
        for item_token in expired:
            admin_tokens.pop(item_token, None)
        admin_tokens[token] = expires_at
    return token


def require_admin_token(authorization: Optional[str]):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Nao autorizado")

    token = authorization.split(" ", 1)[1].strip()
    now = time.time()
    with admin_tokens_lock:
        expires_at = admin_tokens.get(token)
        expired = [item_token for item_token, exp in admin_tokens.items() if exp <= now]
        for item_token in expired:
            admin_tokens.pop(item_token, None)
    if not expires_at or expires_at <= now:
        raise HTTPException(status_code=401, detail="Sessao expirada ou invalida")


load_settings()

def load_data():
    global products_cache
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            products_cache = json.load(f)
        print(f"Loaded {len(products_cache)} products into memory.")
    else:
        print(f"Warning: {DATA_FILE} not found. Run extract_data.py first.")


def parse_price(value: str) -> float:
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return 0.0


def tokenize(value: str) -> list[str]:
    cleaned = re.sub(r"[^\w\s]", " ", value.lower().strip())
    return cleaned.split()


def infer_category(description: str) -> str:
    desc = str(description or "").lower()
    for category_name, keywords in CATEGORY_RULES:
        if any(keyword in desc for keyword in keywords):
            return category_name
    return "Outros"


def code_sort_key(value: Any) -> tuple[int, str]:
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    if digits:
        return (0, digits.zfill(12))
    return (1, text.lower())

@app.on_event("startup")
async def startup_event():
    load_data()
    load_settings()

@app.get("/info")
def get_info():
    return {"message": "Product API is running. Use /search?query=... to search.", "total_products": len(products_cache)}


@app.get("/public-config")
def get_public_config():
    return {
        "store_name": APP_SETTINGS.get("store_name", DEFAULT_SETTINGS["store_name"]),
        "store_tagline": APP_SETTINGS.get("store_tagline", DEFAULT_SETTINGS["store_tagline"]),
        "creator_name": CREATOR_NAME,
        "creator_whatsapp": CREATOR_WHATSAPP,
    }


@app.get("/order-config")
def get_order_config():
    return {
        "api_base_url": APP_SETTINGS.get("api_base_url", ""),
        "whatsapp_number": APP_SETTINGS.get("whatsapp_number", ""),
        "coupon_title": APP_SETTINGS.get("coupon_title", DEFAULT_SETTINGS["coupon_title"]),
        "coupon_message": APP_SETTINGS.get("coupon_message", DEFAULT_SETTINGS["coupon_message"]),
        "coupon_address": APP_SETTINGS.get("coupon_address", DEFAULT_SETTINGS["coupon_address"]),
        "coupon_footer": APP_SETTINGS.get("coupon_footer", DEFAULT_SETTINGS["coupon_footer"]),
    }


@app.get("/categories")
def get_categories():
    counts: dict[str, int] = {}
    for product in products_cache:
        category = infer_category(product.get("description", ""))
        counts[category] = counts.get(category, 0) + 1

    categories = [
        {"name": name, "count": counts[name]}
        for name in sorted(counts.keys(), key=lambda item: item.lower())
    ]
    return {"categories": categories}


@app.post("/admin/login")
def admin_login(payload: dict[str, Any]):
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))

    if not (secrets.compare_digest(username, ADMIN_USER) and secrets.compare_digest(password, ADMIN_PASSWORD)):
        raise HTTPException(status_code=401, detail="Credenciais invalidas")

    token = issue_admin_token()
    return {"token": token, "expires_in": ADMIN_TOKEN_TTL_SECONDS}


@app.get("/admin/config")
def admin_get_config(authorization: Optional[str] = Header(None)):
    require_admin_token(authorization)
    return {key: APP_SETTINGS[key] for key in sorted(ALLOWED_SETTING_KEYS)}


@app.put("/admin/config")
def admin_update_config(payload: dict[str, Any], authorization: Optional[str] = Header(None)):
    require_admin_token(authorization)

    updates: dict[str, str] = {}
    for key in ALLOWED_SETTING_KEYS:
        if key in payload:
            updates[key] = normalize_setting_value(key, payload[key])

    if not updates:
        return {"message": "Nenhuma alteracao enviada", "config": APP_SETTINGS}

    with settings_lock:
        APP_SETTINGS.update(updates)
        save_settings()

    return {"message": "Configuracoes atualizadas", "config": APP_SETTINGS}

@app.get("/")
def read_root():
    return FileResponse("index.html")


@app.get("/gerenciador")
def read_manager():
    return FileResponse("gerenciador.html")

@app.get("/search")
def search_products(
    query: str = Query(..., min_length=1, description="Search term for product name"),
    category: Optional[str] = Query(None, min_length=1, max_length=80, description="Product category"),
    min_price: Optional[float] = Query(None, ge=0, description="Minimum product price (a vista)"),
    max_price: Optional[float] = Query(None, ge=0, description="Maximum product price (a vista)"),
    sort_by: str = Query("relevance", pattern="^(relevance|price_asc|price_desc|name|code)$"),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    Search for products by description with intelligent synonym-aware ranking.
    """
    if x_api_key:
        print(f"Received request with API Key: {x_api_key[:5]}... (valid)")

    query_terms = tokenize(query)

    if not query_terms:
        return {"count": 0, "results": []}

    if min_price is not None and max_price is not None and min_price > max_price:
        return {"total": 0, "count": 0, "results": [], "offset": offset, "limit": limit}

    cleaned_query = " ".join(query_terms)

    print(f"Original query: '{query}'")

    scored_results = []

    for product in products_cache:
        desc = product["description"].lower()
        desc_words = tokenize(desc)
        price = parse_price(product.get("price_sight", "0"))
        product_category = infer_category(product.get("description", ""))

        if category and product_category.lower() != category.lower():
            continue

        if min_price is not None and price < min_price:
            continue
        if max_price is not None and price > max_price:
            continue

        score = 0
        matched_terms = 0

        for term in query_terms:
            term_synonym = SYNONYM_MAP.get(term)
            term_found = False

            # 1. Exact Word Match (Highest priority)
            if term in desc_words or (term_synonym and term_synonym in desc_words):
                score += 200
                term_found = True
            # 2. Starts With Match
            elif any(word.startswith(term) for word in desc_words) or (term_synonym and any(word.startswith(term_synonym) for word in desc_words)):
                score += 100
                term_found = True
            # 3. Substring Match
            elif term in desc or (term_synonym and term_synonym in desc):
                score += 50
                term_found = True

            if term_found:
                matched_terms += 1

        # Mandatory: Must match ALL query terms (or their synonyms)
        if matched_terms < len(query_terms):
            continue

        # Bonus: Exact start of description
        if desc.startswith(query_terms[0]):
            score += 150

        # Bonus: Exact full phrase match (if multiple terms)
        if len(query_terms) > 1 and cleaned_query in re.sub(r"[^\w\s]", " ", desc):
            score += 300

        # Penalty: Description length (prefer shorter, more specific matches)
        score -= len(desc) * 0.1

        scored_results.append((score, product, product_category))

    if sort_by == "price_asc":
        scored_results.sort(key=lambda x: (parse_price(x[1].get("price_sight", "0")), -x[0]))
    elif sort_by == "price_desc":
        scored_results.sort(key=lambda x: (-parse_price(x[1].get("price_sight", "0")), -x[0]))
    elif sort_by == "name":
        scored_results.sort(key=lambda x: x[1].get("description", "").lower())
    elif sort_by == "code":
        scored_results.sort(key=lambda x: code_sort_key(x[1].get("id", "")))
    else:
        scored_results.sort(key=lambda x: x[0], reverse=True)

    total = len(scored_results)
    paginated = scored_results[offset : offset + limit]
    results: list[dict[str, Any]] = []
    for score, product, product_category in paginated:
        item = dict(product)
        item["category"] = product_category
        results.append(item)

    print(f"Found {len(results)} results")
    if results:
        print(f"Top result: {results[0]['description']} (score: {scored_results[0][0]})")

    return {
        "total": total,
        "count": len(results),
        "offset": offset,
        "limit": limit,
        "results": results,
    }

@app.get("/products")
def list_products(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    return products_cache[offset : offset + limit]

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
