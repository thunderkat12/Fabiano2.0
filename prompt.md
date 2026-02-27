# API de Busca de Produtos - Estado Atual

Documento de referencia do projeto `Fabiano_Acessorios`.
Status consolidado em **27/02/2026**.

---

## 1) Visao geral

Aplicacao web para:
- extrair catalogo de produtos a partir de PDF;
- expor busca via API FastAPI;
- oferecer interface de loja (`index.html`);
- oferecer painel administrativo oculto (`gerenciador.html`) com autenticacao.

---

## 2) Funcionalidades ativas

### Backend (FastAPI)

- API com endpoints publicos:
  - `GET /` -> serve `index.html`
  - `GET /info` -> status e total de produtos
  - `GET /public-config` -> dados publicos da loja
  - `GET /order-config` -> configuracoes de pedido/cupom
  - `GET /categories` -> categorias com contagem
  - `GET /search` -> busca com ranking e filtros
  - `GET /products` -> listagem paginada de produtos vendaveis
- API com endpoints administrativos:
  - `POST /admin/login`
  - `GET /admin/me`
  - `GET /admin/config`
  - `PUT /admin/config`
  - `POST /upload-pdf` (requer token Bearer)
  - `GET /upload-pdf/status/{job_id}` (polling do processamento de PDF)
  - `GET /admin/products` (listagem/pesquisa paginada para gestao)
  - `POST /admin/products` (criacao manual de produto)
  - `PUT /admin/products/{product_id}` (edicao de produto)
  - `DELETE /admin/products/{product_id}` (exclusao de produto)
- Painel admin:
  - `GET /gerenciador` retorna 404 (rota publica bloqueada)
  - rota real por chave: `/{MANAGER_ENTRY_KEY}` (padrao: `/Daniel@qwe`)
- CORS habilitado para todas as origens.
- Cache em memoria:
  - cache de produtos/index;
  - cache LRU de busca (`SEARCH_CACHE_MAX_SIZE`, padrao 128).
- Migracao automatica de legado:
  - de `data/stores/default/products.json` para `products.json`;
  - de `data/stores/default/settings.json` para `app_settings.json`.

### Busca inteligente (`GET /search`)

- Tokenizacao e normalizacao de texto.
- Remove termos pouco relevantes na consulta (ex.: `de`, `do`, `da`, etc.).
- Mapa de sinonimos (ex.: `ip` <-> `iphone`, `sam` <-> `samsung`).
- Normalizacao de erros comuns de digitacao (ex.: `diplsay` -> `display`, `ifone` -> `iphone`).
- Ranking por relevancia com pesos por:
  - match exato de palavra;
  - prefixo de palavra;
  - substring;
  - inicio da descricao;
  - frase completa.
- Filtros:
  - `category`
  - `min_price`
  - `max_price`
  - `sort_by` (`relevance|price_asc|price_desc|name|code`)
  - `offset`
  - `limit` de 1 a 10 (maximo atual).
- Produtos sem preco (preco <= 0) nao aparecem em busca/listagem publica.

### Seguranca administrativa

- Login com usuario/senha definidos por ambiente.
- Sessao por token temporario (Bearer), com expiracao.
- Protecao anti-forca-bruta por IP no login:
  - limite de tentativas em janela configuravel;
  - bloqueio temporario apos excesso.

### Frontend da loja (`index.html`)

- Busca com filtros e ordenacao.
- Paginacao com opcoes 5 ou 10 por pagina.
- Carrinho com persistencia local.
- Envio de pedido por WhatsApp:
  - gera protocolo de pedido;
  - limpa estado local apos envio;
  - fallback para mobile (`window.location`) se popup falhar.
- Tema visual atualizado (claro/escuro) com novo design.

### Painel admin (`gerenciador.html`)

- Login/logout com sessao em `sessionStorage`.
- Validacao de sessao via `/admin/me`.
- Edicao de configuracoes da loja/cupom:
  - nome/subtitulo;
  - URL base da API;
  - WhatsApp;
  - titulo/mensagem/endereco/rodape do cupom.
- Upload de PDF com:
  - drag-and-drop;
  - validacao de extensao/tipo;
  - barra de progresso;
  - feedback de erro/sucesso.
- Gestao completa de produtos:
  - busca semantica por codigo/descricao;
  - edicao inline de descricao/unidade/precos/estoque;
  - criacao e exclusao de itens;
  - paginacao em blocos de **5 itens por pagina** no admin.
- Layout da gestao de produtos reorganizado para leitura rapida.

---

## 3) Arquitetura simplificada

```text
PDF -> extract_data.py -> products.json -> api.py -> index.html / gerenciador.html
```

Fluxo de atualizacao de catalogo:
1. Admin autentica.
2. Admin envia PDF em `/upload-pdf`.
3. Backend extrai produtos, normaliza registros e preserva estoque por `id` quando aplicavel.
4. Cache de busca e indice sao invalidados/reconstruidos.

---

## 4) Variaveis de ambiente principais

- `ADMIN_USER` (fallback: `MASTER_USER`, padrao: `admin`)
- `ADMIN_PASSWORD` (fallback: `MASTER_PASSWORD`, padrao atual em codigo)
- `ADMIN_TOKEN_TTL_SECONDS` (padrao: `28800`)
- `ADMIN_LOGIN_MAX_ATTEMPTS` (padrao: `5`)
- `ADMIN_LOGIN_WINDOW_SECONDS` (padrao: `300`)
- `ADMIN_LOGIN_BLOCK_SECONDS` (padrao: `900`)
- `MANAGER_ENTRY_KEY` (padrao: `Daniel@qwe`)
- `SEARCH_CACHE_MAX_SIZE` (padrao: `128`)
- `STORE_NAME`, `STORE_TAGLINE`, `API_BASE_URL`
- `ORDER_WHATSAPP_NUMBER`
- `ORDER_COUPON_TITLE`, `ORDER_COUPON_MESSAGE`, `ORDER_COUPON_ADDRESS`, `ORDER_COUPON_FOOTER`

Nota: em producao, definir `ADMIN_PASSWORD` por ambiente e nao depender do valor padrao em codigo.

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

---

## 6) Onde paramos (ultimos commits)

1. `c61b260` (2026-02-27)  
   UI da loja: faixa de novidade da busca inteligente, destaque visual de termos buscados e micro-animacao de entrada dos cards.
2. `ae25143` (2026-02-27)  
   Admin: busca semantica melhorada, normalizacao de termos e redesign da gestao de produtos (com foco em edicao rapida).
3. `625e317` (2026-02-23)  
   Melhoria de estabilidade no upload/processamento de PDF e limpeza de artefatos antigos.
4. `bdf4b71` (2026-02-23)  
   Upload de PDF assincrono com polling de status.
5. `12ae74b` (2026-02-22)  
   Suporte a taxa de entrega por regiao no admin/carrinho.

---

## 7) Backlog atual (proxima sprint)

### Carrinho / Pedido
- [ ] Exportar carrinho em CSV (com subtotal por item)
- [ ] Exportar comprovante em PDF
- [ ] Salvar e recuperar rascunhos de pedido
- [ ] Suportar multiplos templates de cupom

### UX/UI
- [ ] Filtro adicional por marca
- [ ] Botao "limpar busca" no campo principal
- [ ] Melhorar acessibilidade de teclado
- [ ] Skeleton loading para resultados e carrinho

### Dados e operacao
- [ ] Dashboard de produtos mais buscados
- [ ] Estatisticas de uso da API
- [ ] Logs de erros e performance

---

## 8) Nota de manutencao

Este arquivo deve ser atualizado sempre que houver mudanca de:
- endpoints;
- autenticacao/admin;
- fluxo de pedido WhatsApp;
- limites de busca/paginacao;
- comandos de startup/deploy.
