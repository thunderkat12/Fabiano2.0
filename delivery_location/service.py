from __future__ import annotations

from typing import Any, Optional
import json
import os
import time
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PICKUP_TOKENS = {
    "retirar na loja",
    "retirada na loja",
    "retirar loja",
    "retirada",
    "pickup",
}

DELIVERY_LOCATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.delivery_order_locations (
    id BIGSERIAL PRIMARY KEY,
    store_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    delivery_region TEXT NOT NULL DEFAULT '',
    delivery_address TEXT NOT NULL DEFAULT '',
    query_text TEXT NOT NULL DEFAULT '',
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    geocode_status TEXT NOT NULL DEFAULT 'pending',
    geocode_source TEXT NOT NULL DEFAULT 'nominatim',
    geocode_error TEXT NOT NULL DEFAULT '',
    raw_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
    UNIQUE (store_id, protocol)
);

CREATE INDEX IF NOT EXISTS idx_delivery_order_locations_store_created
    ON public.delivery_order_locations (store_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_delivery_order_locations_status
    ON public.delivery_order_locations (geocode_status, updated_at DESC);
"""


def _normalize_text(value: Any) -> str:
    text = str(value or "")
    text = unicodedata.normalize("NFD", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    cleaned = "".join(char.lower() if char.isalnum() or char.isspace() else " " for char in text)
    return " ".join(cleaned.split())


def _import_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Dependencia psycopg nao instalada. Rode pip install -r requirements.txt.") from exc
    return psycopg, dict_row


def resolve_database_url(preferred_url: Any = "") -> tuple[str, str]:
    configured = str(preferred_url or "").strip()
    if configured:
        return configured, "panel"
    for key in ("SUPABASE_DB_URL", "SUPABASE_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL"):
        value = str(os.getenv(key, "")).strip()
        if value:
            return value, "environment"
    return "", "absent"


def _get_geocoder_user_agent() -> str:
    configured = str(os.getenv("GEOCODING_USER_AGENT", "")).strip()
    return configured or "fabiano-acessorios-delivery-location/1.0"


def _get_geocoder_email() -> str:
    return str(os.getenv("GEOCODING_EMAIL", "")).strip()


def _get_routing_base_url() -> str:
    configured = str(os.getenv("ROUTING_SERVICE_BASE_URL", "")).strip().rstrip("/")
    return configured or "https://router.project-osrm.org"


def _coerce_float(value: Any) -> Optional[float]:
    text = str(value if value is not None else "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _is_valid_coordinate_pair(latitude: Any, longitude: Any) -> bool:
    lat = _coerce_float(latitude)
    lng = _coerce_float(longitude)
    if lat is None or lng is None:
        return False
    return -90 <= lat <= 90 and -180 <= lng <= 180


def _extract_payload_coordinates(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("delivery_location") if isinstance(payload.get("delivery_location"), dict) else {}
    latitude = _coerce_float(nested.get("latitude", nested.get("lat", payload.get("delivery_latitude"))))
    longitude = _coerce_float(nested.get("longitude", nested.get("lng", payload.get("delivery_longitude"))))
    if not _is_valid_coordinate_pair(latitude, longitude):
        return {}
    accuracy = _coerce_float(nested.get("accuracy_meters", nested.get("accuracy")))
    return {
        "latitude": float(latitude),
        "longitude": float(longitude),
        "accuracy_meters": float(accuracy) if accuracy is not None and accuracy >= 0 else None,
        "source": str(nested.get("source", payload.get("delivery_location_source", "browser_geolocation")) or "browser_geolocation").strip(),
        "display_name": str(nested.get("display_name", nested.get("address", "")) or "").strip(),
        "confirmed_at": float(nested.get("confirmed_at", 0) or 0),
    }


def is_pickup_delivery(region_label: Any = "", region_value: Any = "", address_value: Any = "") -> bool:
    candidates = [
        _normalize_text(region_label),
        _normalize_text(region_value),
        _normalize_text(address_value),
    ]
    return any(candidate in PICKUP_TOKENS for candidate in candidates if candidate)


def _build_geocode_query(payload: dict[str, Any], settings: dict[str, Any]) -> str:
    parts = [
        str(payload.get("delivery_address", "") or "").strip(),
        str(payload.get("delivery_region_label", "") or "").strip(),
        str(settings.get("delivery_geo_city", "Brasilia") or "Brasilia").strip(),
        str(settings.get("delivery_geo_state", "DF") or "DF").strip(),
        str(settings.get("delivery_geo_country", "Brasil") or "Brasil").strip(),
    ]
    query_parts = []
    seen = set()
    for part in parts:
        normalized = _normalize_text(part)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        query_parts.append(part)
    return ", ".join(query_parts)


def _geocode_query(query_text: str) -> dict[str, Any]:
    if not query_text:
        return {"status": "missing_address", "message": "Endereco vazio para geocodificacao.", "payload": []}

    params = {
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 1,
        "q": query_text,
    }
    email = _get_geocoder_email()
    if email:
        params["email"] = email
    url = f"https://nominatim.openstreetmap.org/search?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "User-Agent": _get_geocoder_user_agent(),
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"status": "error", "message": f"Falha ao consultar geocodificacao: {exc}", "payload": []}

    if not isinstance(payload, list) or not payload:
        return {"status": "not_found", "message": "Endereco nao encontrado.", "payload": []}

    first = payload[0] if isinstance(payload[0], dict) else {}
    try:
        latitude = float(first.get("lat", 0) or 0)
        longitude = float(first.get("lon", 0) or 0)
    except (TypeError, ValueError):
        return {"status": "error", "message": "Resposta de geocodificacao invalida.", "payload": payload}

    return {
        "status": "resolved",
        "message": "Endereco geocodificado com sucesso.",
        "latitude": latitude,
        "longitude": longitude,
        "payload": payload,
    }


def reverse_geocode_coordinates(latitude: Any, longitude: Any) -> dict[str, Any]:
    lat = _coerce_float(latitude)
    lng = _coerce_float(longitude)
    if not _is_valid_coordinate_pair(lat, lng):
        return {"status": "invalid_coordinates", "message": "Coordenadas invalidas.", "payload": {}}

    params = {
        "format": "jsonv2",
        "lat": f"{float(lat):.7f}",
        "lon": f"{float(lng):.7f}",
        "addressdetails": 1,
    }
    email = _get_geocoder_email()
    if email:
        params["email"] = email
    url = f"https://nominatim.openstreetmap.org/reverse?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "User-Agent": _get_geocoder_user_agent(),
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"status": "error", "message": f"Falha ao consultar endereco reverso: {exc}", "payload": {}}

    if not isinstance(payload, dict) or not payload:
        return {"status": "not_found", "message": "Endereco nao encontrado para estas coordenadas.", "payload": {}}

    return {
        "status": "resolved",
        "message": "Endereco localizado com sucesso.",
        "latitude": float(lat),
        "longitude": float(lng),
        "display_name": str(payload.get("display_name", "") or "").strip(),
        "address": payload.get("address") if isinstance(payload.get("address"), dict) else {},
        "payload": payload,
    }


def calculate_route_snapshot(
    origin_latitude: Any,
    origin_longitude: Any,
    destination_latitude: Any,
    destination_longitude: Any,
) -> dict[str, Any]:
    origin_lat = _coerce_float(origin_latitude)
    origin_lng = _coerce_float(origin_longitude)
    destination_lat = _coerce_float(destination_latitude)
    destination_lng = _coerce_float(destination_longitude)
    if not (
        _is_valid_coordinate_pair(origin_lat, origin_lng)
        and _is_valid_coordinate_pair(destination_lat, destination_lng)
    ):
        return {"status": "missing_coordinates", "message": "Coordenadas insuficientes para calcular a rota."}

    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "false",
    }
    url = (
        f"{_get_routing_base_url()}/route/v1/driving/"
        f"{float(origin_lng):.7f},{float(origin_lat):.7f};"
        f"{float(destination_lng):.7f},{float(destination_lat):.7f}"
        f"?{urlencode(params)}"
    )
    request = Request(
        url,
        headers={
            "User-Agent": _get_geocoder_user_agent(),
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"status": "error", "message": f"Falha ao calcular rota: {exc}"}

    if not isinstance(payload, dict) or str(payload.get("code", "")).strip().lower() != "ok":
        return {"status": "not_found", "message": "Servico de rota nao retornou um trajeto valido."}

    routes = payload.get("routes") if isinstance(payload.get("routes"), list) else []
    first_route = routes[0] if routes and isinstance(routes[0], dict) else {}
    if not first_route:
        return {"status": "not_found", "message": "Nenhuma rota encontrada para este destino."}

    geometry = first_route.get("geometry") if isinstance(first_route.get("geometry"), dict) else {}
    legs = first_route.get("legs") if isinstance(first_route.get("legs"), list) else []
    leg = legs[0] if legs and isinstance(legs[0], dict) else {}
    return {
        "status": "ok",
        "message": "Rota calculada com sucesso.",
        "distance_meters": float(first_route.get("distance", 0) or 0),
        "duration_seconds": float(first_route.get("duration", 0) or 0),
        "summary": str(leg.get("summary", "") or first_route.get("weight_name", "") or "").strip(),
        "geometry": geometry if geometry.get("type") == "LineString" else {},
        "provider": "osrm",
    }


def _connect(database_url: Any = ""):
    resolved_url, _ = resolve_database_url(database_url)
    database_url = str(resolved_url or "").strip()
    if not database_url:
        raise RuntimeError(
            "Configure a URL do Supabase no painel ou defina SUPABASE_DB_URL / DATABASE_URL para ativar a persistencia."
        )
    psycopg, dict_row = _import_psycopg()
    return psycopg.connect(database_url, autocommit=True, row_factory=dict_row)


def ensure_delivery_locations_table(database_url: Any = "") -> dict[str, Any]:
    resolved_url, source = resolve_database_url(database_url)
    with _connect(resolved_url) as conn:
        with conn.cursor() as cur:
            cur.execute(DELIVERY_LOCATION_TABLE_SQL)
            cur.execute("SELECT COUNT(*) AS total FROM public.delivery_order_locations")
            row = cur.fetchone() or {}
    return {
        "configured": True,
        "source": source,
        "table": "public.delivery_order_locations",
        "rows": int(row.get("total", 0) or 0),
        "message": "Tabela de localizacao pronta no Supabase.",
    }


def _upsert_delivery_location(record: dict[str, Any], database_url: Any = "") -> dict[str, Any]:
    resolved_url, _ = resolve_database_url(database_url)
    with _connect(resolved_url) as conn:
        with conn.cursor() as cur:
            cur.execute(DELIVERY_LOCATION_TABLE_SQL)
            cur.execute(
                """
                INSERT INTO public.delivery_order_locations (
                    store_id,
                    protocol,
                    delivery_region,
                    delivery_address,
                    query_text,
                    latitude,
                    longitude,
                    geocode_status,
                    geocode_source,
                    geocode_error,
                    raw_payload_json,
                    updated_at
                ) VALUES (
                    %(store_id)s,
                    %(protocol)s,
                    %(delivery_region)s,
                    %(delivery_address)s,
                    %(query_text)s,
                    %(latitude)s,
                    %(longitude)s,
                    %(geocode_status)s,
                    %(geocode_source)s,
                    %(geocode_error)s,
                    %(raw_payload_json)s::jsonb,
                    timezone('utc', now())
                )
                ON CONFLICT (store_id, protocol) DO UPDATE SET
                    delivery_region = EXCLUDED.delivery_region,
                    delivery_address = EXCLUDED.delivery_address,
                    query_text = EXCLUDED.query_text,
                    latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    geocode_status = EXCLUDED.geocode_status,
                    geocode_source = EXCLUDED.geocode_source,
                    geocode_error = EXCLUDED.geocode_error,
                    raw_payload_json = EXCLUDED.raw_payload_json,
                    updated_at = timezone('utc', now())
                RETURNING
                    store_id,
                    protocol,
                    delivery_region,
                    delivery_address,
                    query_text,
                    latitude,
                    longitude,
                    geocode_status,
                    geocode_source,
                    geocode_error,
                    created_at,
                    updated_at
                """,
                record,
            )
            row = cur.fetchone() or {}
    return dict(row)


def sync_delivery_location_record(
    store_id: str,
    protocol: str,
    payload: dict[str, Any],
    settings: dict[str, Any],
    database_url: Any = "",
) -> dict[str, Any]:
    delivery_region = str(payload.get("delivery_region_label", "") or payload.get("delivery_region", "") or "").strip()
    coordinates = _extract_payload_coordinates(payload)
    delivery_address = str(payload.get("delivery_address", "") or coordinates.get("display_name", "") or "").strip()
    query_text = _build_geocode_query(payload, settings)

    geocode_status = "pending"
    geocode_source = "nominatim"
    geocode_error = ""
    latitude = None
    longitude = None
    raw_payload = {}

    raw_payload = {"client_location": coordinates} if coordinates else {}

    if is_pickup_delivery(delivery_region, payload.get("delivery_region", ""), delivery_address):
        geocode_status = "pickup"
        geocode_source = "manual"
    elif coordinates:
        geocode_status = "resolved"
        geocode_source = str(coordinates.get("source", "browser_geolocation") or "browser_geolocation")
        latitude = float(coordinates.get("latitude", 0) or 0)
        longitude = float(coordinates.get("longitude", 0) or 0)
        geocode_error = "Localizacao confirmada pelo cliente."
    elif not delivery_address:
        geocode_status = "missing_address"
        geocode_source = "manual"
        geocode_error = "Pedido sem endereco informado."
    else:
        result = _geocode_query(query_text)
        geocode_status = str(result.get("status", "error"))
        geocode_error = str(result.get("message", "") or "")
        if geocode_status == "resolved":
            latitude = float(result.get("latitude", 0) or 0)
            longitude = float(result.get("longitude", 0) or 0)
        raw_payload["geocoder_response"] = result.get("payload", [])

    record = {
        "store_id": str(store_id),
        "protocol": str(protocol),
        "delivery_region": delivery_region,
        "delivery_address": delivery_address,
        "query_text": query_text,
        "latitude": latitude,
        "longitude": longitude,
        "geocode_status": geocode_status,
        "geocode_source": geocode_source,
        "geocode_error": geocode_error,
        "raw_payload_json": json.dumps(raw_payload, ensure_ascii=False),
    }
    saved = _upsert_delivery_location(record, database_url=database_url)
    saved["message"] = geocode_error or "Localizacao sincronizada."
    return saved


def sync_delivery_locations_batch(
    store_id: str,
    orders: list[dict[str, Any]],
    settings: dict[str, Any],
    database_url: Any = "",
) -> dict[str, Any]:
    synced = 0
    failed = 0
    results = []
    for item in orders:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        protocol = str(item.get("protocol", "") or "").strip()
        if not protocol:
            continue
        try:
            results.append(
                sync_delivery_location_record(
                    store_id,
                    protocol,
                    payload,
                    settings,
                    database_url=database_url,
                )
            )
            synced += 1
        except Exception as exc:
            failed += 1
            results.append({"protocol": protocol, "geocode_status": "error", "message": str(exc)})
    return {"synced": synced, "failed": failed, "results": results}


def fetch_delivery_locations(
    store_id: str,
    *,
    protocols: Optional[list[str]] = None,
    created_from: Optional[float] = None,
    created_to: Optional[float] = None,
    limit: int = 200,
    database_url: Any = "",
) -> list[dict[str, Any]]:
    resolved_url, _ = resolve_database_url(database_url)
    with _connect(resolved_url) as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT
                    store_id,
                    protocol,
                    delivery_region,
                    delivery_address,
                    query_text,
                    latitude,
                    longitude,
                    geocode_status,
                    geocode_source,
                    geocode_error,
                    created_at,
                    updated_at
                FROM public.delivery_order_locations
                WHERE store_id = %(store_id)s
            """
            params: dict[str, Any] = {"store_id": str(store_id), "limit": max(1, int(limit))}
            if protocols:
                sql += " AND protocol = ANY(%(protocols)s)"
                params["protocols"] = [str(item) for item in protocols if str(item).strip()]
            if created_from is not None:
                sql += " AND created_at >= to_timestamp(%(created_from)s)"
                params["created_from"] = float(created_from)
            if created_to is not None:
                sql += " AND created_at < to_timestamp(%(created_to)s)"
                params["created_to"] = float(created_to)
            sql += " ORDER BY updated_at DESC LIMIT %(limit)s"
            cur.execute(sql, params)
            rows = cur.fetchall() or []
    return [dict(row) for row in rows]


def get_delivery_location_status(store_id: str, database_url: Any = "") -> dict[str, Any]:
    resolved_url, source = resolve_database_url(database_url)
    if not resolved_url:
        return {
            "configured": False,
            "provider": "supabase_postgres",
            "source": "absent",
            "table_ready": False,
            "total_records": 0,
            "message": "Supabase ausente. Configure a URL no painel ou defina SUPABASE_DB_URL / DATABASE_URL no ambiente.",
        }

    try:
        ready = ensure_delivery_locations_table(resolved_url)
        rows = fetch_delivery_locations(store_id, limit=20, database_url=resolved_url)
    except Exception as exc:
        return {
            "configured": True,
            "provider": "supabase_postgres",
            "source": source,
            "table_ready": False,
            "total_records": 0,
            "message": str(exc),
        }

    return {
        "configured": True,
        "provider": "supabase_postgres",
        "source": source,
        "table_ready": True,
        "total_records": int(ready.get("rows", 0) or 0),
        "recent_records": len(rows),
        "message": "Supabase pronto para persistir localizacoes de entrega.",
    }
