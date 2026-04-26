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
