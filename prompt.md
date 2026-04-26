# API de Busca de Produtos - Estado Atual

Documento de referencia do projeto `Fabiano_Acessorios`.
Status consolidado em **14/03/2026**.

Obs.: este resumo ja considera o estado atual do workspace local, incluindo alteracoes ainda nao commitadas em `api.py`, `index.html`, `gerenciador.html`, `.gitignore`, `products.json` e o novo `store_runtime.py`.

---

## 1) Visao geral

Aplicacao web para:
- manter catalogo de produtos por loja;
- buscar produtos via API FastAPI;
- fechar pedidos com protocolo e link de WhatsApp;
- operar um painel administrativo oculto;
- sincronizar catalogo/pedidos com banco externo quando a loja estiver em modo integrado.

O projeto saiu do modelo "catalogo unico em JSON" e hoje ja possui base para **multi-loja**, **persistencia operacional em SQLite** e **integracao opcional com fonte externa**.

---

## 2) Funcionalidades ativas

### Backend (FastAPI)

- Endpoints publicos:
  - `GET /`
  - `GET /info`
  - `GET /public-config`
  - `GET /order-config`
  - `GET /categories`
  - `GET /search`
  - `GET /products`
  - `POST /orders/submit`
  - `GET /media/{media_kind}/{filename}`
- Endpoints administrativos:
  - `POST /admin/login`
  - `GET /admin/me`
  - `GET /admin/config`
  - `PUT /admin/config`
  - `POST /admin/integration/test`
  - `POST /admin/integration/sync`
  - `GET /admin/orders`
  - `POST /admin/orders/{protocol}/retry`
  - `POST /admin/upload/logo`
  - `POST /admin/upload/product-image`
  - `GET /admin/products`
  - `POST /admin/products`
  - `PUT /admin/products/{product_id}`
  - `DELETE /admin/products/{product_id}`
  - `POST /upload-pdf`
  - `GET /upload-pdf/status/{job_id}`
- Painel admin:
  - `GET /gerenciador` continua bloqueado com `404`
  - rota real por chave: `/{MANAGER_ENTRY_KEY}` (fallback atual em codigo: `/Daniel@qwe`)
- Multi-loja:
  - endpoints publicos e admin aceitam `store` por query string;
  - configuracoes por loja em `data/stores/<store_id>/settings.json`;
  - catalogos por loja em `data/stores/<store_id>/products.json`;
  - loja padrao continua usando `app_settings.json` e `products.json`.
- Persistencia operacional:
  - `data/operations.sqlite3` guarda configuracoes de integracao, pedidos e jobs de sincronizacao;
  - `.gitignore` atualizado para ignorar esse banco local.
- Integracao externa:
  - modo `local_json` (padrao) ou `external_db`;
  - healthcheck, sync de catalogo e retry de pedidos;
  - fila simples de jobs com worker em background.
- Autenticacao admin:
  - token Bearer temporario;
  - rate limit por IP no login;
  - suporte a usuarios em `data/auth/users.json` com `role` e `store_id`.
- Uploads:
  - PDF assinado por token admin e processado em background;
  - upload de logo da loja;
  - upload de imagem de produto com deteccao de formato e limite de tamanho.
- Cache:
  - cache de produtos/indice por loja;
  - cache LRU de busca em memoria.

### Busca inteligente (`GET /search`)

- Normalizacao sem acentos.
- Stopwords removidas.
- Sinonimos e erros comuns de digitacao.
- Ranking por match exato, prefixo, substring, inicio da descricao e frase.
- Filtros:
  - `category`
  - `min_price`
  - `max_price`
  - `sort_by` (`relevance|price_asc|price_desc|name|code`)
  - `offset`
  - `limit` de 1 a 10
- Produtos com preco `<= 0` nao aparecem publicamente.

### Pedido (`POST /orders/submit`)

- Gera protocolo unico por pedido.
- Recalcula total no backend.
- Suporta taxa por regiao e fallback por endereco.
- Suporta pagamento em `pix` ou `dinheiro`.
- Para dinheiro, valida "troco para".
- Resolve WhatsApp por destino/regiao quando configurado.
- Persiste registro do pedido no SQLite operacional.
- Se a loja estiver em `external_db`, tenta sincronizar imediatamente e agenda retry quando necessario.

### Frontend da loja (`index.html`)

- Busca publica mobile-first.
- Paginacao fixa de **5** resultados por pagina.
- Branding dinamico:
  - nome;
  - subtitulo;
  - logo;
  - exibicao opcional de imagens;
  - paleta customizavel;
  - modo claro/escuro.
- Cards com imagem do produto e zoom ao clicar.
- Carrinho com:
  - selecao individual de itens;
  - alteracao de quantidade;
  - subtotal por item;
  - resumo com taxa e total final.
- Fluxo de pedido agora passa pelo backend antes do WhatsApp.
- Banner de release e tema visual mais refinado continuam ativos.

### Painel admin (`gerenciador.html`)

- Login/logout com sessao em `sessionStorage`.
- Dashboard dividido por secoes:
  - dashboard;
  - catalogo;
  - aparencia;
  - pedidos;
  - configuracoes.
- Gestao de catalogo:
  - busca semantica;
  - edicao inline;
  - criacao e exclusao;
  - upload de imagem por produto.
- Aparencia:
  - logo da loja;
  - ligar/desligar imagens;
  - paleta customizada;
  - presets de tema.
- Pedidos e integracao:
  - WhatsApp padrao;
  - destinos de WhatsApp por regiao;
  - regras de taxa por regiao;
  - healthcheck da integracao;
  - sync manual de catalogo externo;
  - fila de pedidos pendentes com retry manual.
- Catalogo entra em modo somente leitura quando a loja usa `external_db`.

---

## 3) Arquitetura simplificada

```text
index.html / gerenciador.html
            |
            v
          api.py
            |
            +--> app_settings.json / products.json
            +--> data/stores/<store_id>/{settings.json,products.json}
            +--> data/operations.sqlite3
            +--> data/media/{logos,products}
            |
            v
      store_runtime.py
```

Fluxo de catalogo por PDF:
1. Admin autentica.
2. Admin envia PDF.
3. `extract_data.py` extrai os itens.
4. Backend normaliza e substitui o catalogo da loja.
5. Cache e indice sao invalidados.

Fluxo de pedido:
1. Loja envia itens selecionados para `POST /orders/submit`.
2. Backend recalcula total, taxa e destino WhatsApp.
3. Pedido e salvo em `data/operations.sqlite3`.
4. Se houver integracao externa, a API tenta sincronizar e agenda retry quando falhar.

---

## 4) Variaveis de ambiente principais

- `ADMIN_USER` / `MASTER_USER`
- `ADMIN_PASSWORD` / `MASTER_PASSWORD`
- `ADMIN_TOKEN_TTL_SECONDS`
- `ADMIN_LOGIN_MAX_ATTEMPTS`
- `ADMIN_LOGIN_WINDOW_SECONDS`
- `ADMIN_LOGIN_BLOCK_SECONDS`
- `MANAGER_ENTRY_KEY`
- `SEARCH_CACHE_MAX_SIZE`
- `CREATOR_NAME`
- `CREATOR_WHATSAPP`
- `STORE_NAME`
- `STORE_TAGLINE`
- `STORE_LOGO_URL`
- `SHOW_PRODUCT_IMAGES`
- `THEME_BG`
- `THEME_BG_ALT`
- `THEME_SURFACE`
- `THEME_TEXT`
- `THEME_MUTED`
- `THEME_ACCENT`
- `THEME_ACCENT_STRONG`
- `THEME_ACCENT_DEEP`
- `API_BASE_URL`
- `ORDER_WHATSAPP_NUMBER`
- `ORDER_WHATSAPP_DESTINATIONS`
- `ORDER_COUPON_TITLE`
- `ORDER_COUPON_MESSAGE`
- `ORDER_COUPON_ADDRESS`
- `ORDER_COUPON_FOOTER`
- `ORDER_DELIVERY_FEE_AMOUNT`
- `ORDER_DELIVERY_FEE_REGIONS`
- `ORDER_DELIVERY_FEE_RULES`
- `PDF_UPLOAD_MAX_BYTES`
- `PDF_PROCESS_TIMEOUT_SECONDS`
- `IMAGE_UPLOAD_MAX_BYTES`

Nota importante:
- em producao, nao depender dos fallbacks inseguros hoje presentes em codigo para senha admin e rota do gerenciador.

---

## 5) Desenvolvimento local

### Requisitos
- Python 3.10+
- dependencias de `requirements.txt`

### Comandos

```bash
pip install -r requirements.txt
uvicorn api:app --reload
```

### Script auxiliar (Windows)

`start_app.bat`:
- sobe `python api.py` em segundo plano;
- abre `http://127.0.0.1:8000` no navegador.

### Observacoes

- `data/operations.sqlite3` e criado automaticamente no startup.
- a rota oculta do admin depende de `MANAGER_ENTRY_KEY`.
- para smoke tests rapidos, `python -m py_compile api.py store_runtime.py extract_data.py inspect_pdf.py` esta passando no estado atual.

---

## 6) Onde paramos (ultimos commits + estado local)

0. **Alteracoes locais em 14/03/2026 (ainda nao commitadas)**
   - Base multi-loja por query `store`.
   - Novo `store_runtime.py` para integracao, pedidos e fila de sync.
   - Novo endpoint `POST /orders/submit`.
   - Persistencia de pedidos e jobs em SQLite local.
   - Upload de logo e imagens de produto.
   - Painel admin com secao de integracao/pedidos pendentes.
   - `prompt.md` atualizado para refletir esse estado.
1. `614b811` (2026-03-14)
   `fix: make dark mode work with custom color palettes`
2. `d412bbf` (2026-03-14)
   `feat: add theme presets in manager and update footer brand text`
3. `340d538` (2026-03-14)
   `feat: improve product image flow and add click-to-zoom viewer`
4. `c3e0f58` (2026-03-14)
   Destaque "Gold Prime" no cabecalho
5. `dfc0817` (2026-03-14)
   Atualizacao do aviso/release para `v1.3.1`
6. `d6a4f04` (2026-03-01)
   Busca semantica melhorada e simplificacao da UI mobile
7. `4475e20` (2026-03-01)
   Prompt/documentacao atualizado na etapa anterior

---

## 7) Backlog imediato (proxima retomada)

### Correcao tecnica
- [ ] Remover fallbacks inseguros de `ADMIN_PASSWORD` e `MANAGER_ENTRY_KEY`
- [ ] Tornar o worker/job queue seguro para execucao com mais de um processo
- [ ] Isolar `localStorage` da loja publica por `store`
- [ ] Validar/normalizar `id` de produto tambem na ingestao de PDF/banco externo

### Produto / operacao
- [ ] Exportar carrinho em CSV
- [ ] Exportar comprovante em PDF
- [ ] Melhorar acessibilidade de teclado
- [ ] Dashboard de produtos mais buscados
- [ ] Logs de erros e performance

---

## 8) Achados da revisao tecnica em 14/03/2026

1. **Critico - credenciais e rota admin com fallback previsivel em codigo**
   `api.py` ainda possui senha admin padrao (`daniel142536`) e rota oculta padrao (`Daniel@qwe`). Isso so fica seguro se o deploy sempre sobrescrever por ambiente.
2. **Alto - fila de sync nao e segura para multi-processo**
   O startup sobe worker em background em todo processo, mas o claim do job nao e atomico. Em deploy com mais de uma instancia, o mesmo pedido pode ser sincronizado duas vezes.
3. **Alto - estado da loja publica vaza entre lojas diferentes**
   O frontend ja usa `?store=...`, porem as chaves de `localStorage` continuam globais. Carrinho, endereco e configuracoes podem atravessar entre lojas.
4. **Medio/alto - ingestao aceita IDs que o checkout depois rejeita**
   O catalogo vindo de PDF/banco externo aceita `id` livre, mas `POST /orders/submit` e as rotas admin exigem o padrao `^[A-Za-z0-9_.-]{1,60}$`.

---

## 9) Nota de manutencao

Este arquivo deve ser atualizado sempre que houver mudanca de:
- endpoints publicos ou administrativos;
- autenticacao/admin;
- fluxo de pedido WhatsApp;
- suporte multi-loja;
- integracao com banco externo;
- limites de upload;
- comandos de startup/deploy.
