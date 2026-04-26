# Documentação de Estudos: Projeto Extração de PDF e API

Este documento detalha o processo de transformação de um script de extração de dados em uma aplicação web completa, cobrindo Backend (API), Frontend (Interface) e Automação (Script).

---

## 1. Backend: Configurando a API (Python/FastAPI)

O objetivo era permitir que nossa API fosse acessada por um navegador web (frontend).

### O Desafio
Originalmente, a API (`api.py`) funcionava, mas por padrões de segurança, navegadores bloqueiam requisições feitas de uma origem (ex: arquivo local ou outro site) para outra (nossa API local). Isso é chamado de política de **CORS** (Cross-Origin Resource Sharing).

### A Solução
Adicionamos um "middleware" (uma camada intermediária) no `api.py` para dizer ao navegador: "Tudo bem, aceito requisições de qualquer lugar".

**Código Adicionado:**
```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Permite qualquer origem (Frontend local, servidor, etc)
    allow_credentials=True,
    allow_methods=["*"], # Permite todos os métodos (GET, POST, etc)
    allow_headers=["*"], # Permite todos os cabeçalhos
)
```

---

## 2. Frontend: Criando a Interface (HTML/JS)

Precisávamos de uma tela amigável para o usuário interagir com a API sem precisar usar terminal ou ferramentas como Postman.

### Tecnologias Usadas
*   **HTML5**: Estrutura da página.
*   **Tailwind CSS**: Estilização rápida e moderna (via CDN, sem instalação).
*   **JavaScript (Vanilla)**: Lógica para conectar com a API.

### Funcionalidades Chave do `index.html`

#### A. Configuração Dinâmica da API
Criamos um campo de input para a URL da API. Isso é crucial porque em diferentes ambientes (local, produção, rede interna), o endereço da API pode mudar.

```javascript
// Salva a URL no navegador do usuário para não precisar digitar toda vez
localStorage.setItem('apiBaseUrl', baseUrl);
```

#### B. Requisição Assíncrona (Fetch API)
Usamos `fetch` para chamar o endpoint `/search` da nossa API Python.

```javascript
// A função é 'async' para podermos 'esperar' (await) a resposta da API
const response = await fetch(`${baseUrl}/search?query=${query}`);
const data = await response.json();
```

#### C. Renderização do DOM
O JavaScript pega os dados JSON recebidos e cria elementos HTML dinamicamente para mostrar na tela.

---

## 3. Automação: Script de Inicialização (.bat)

Para facilitar o uso, criamos um script que faz tudo com um clique.

### O Arquivo `start_app.bat`
Comandos de lote do Windows (Batch) para orquestrar a inicialização.

1.  **`start /min cmd /c "python api.py"`**: Abre um terminal minimizado rodando o servidor Python. O `/c` executa e fecha se der erro, mas aqui mantemos o servidor rodando.
2.  **`timeout /t 3`**: Espera 3 segundos. Isso é **crucial** para dar tempo da API subir antes do navegador tentar acessar.
3.  **`start index.html`**: Abre o arquivo HTML no navegador padrão do sistema.

---

## Resumo do Fluxo de Dados

1.  **Usuário** clica em `start_app.bat`.
2.  **Script** sobe o servidor Python (`uvicorn`) e abre o Browser.
3.  **Usuário** digita "Cabo" na busca e aperta Enter.
4.  **Frontend (JS)** envia requisição GET para `http://localhost:8000/search?query=Cabo`.
5.  **Backend (Python)** recebe, processa a busca (fuzzy matching) na lista `products.json`.
6.  **Backend** retorna JSON com os resultados.
7.  **Frontend** recebe o JSON e desenha os cards dos produtos na tela.

---

## Supabase MCP no Codex

Para este projeto, mantenha o servidor MCP `supabase` atual e adicione um segundo alias dedicado ao projeto Fabiano:

```bash
codex mcp add supabase_fabiano --url https://mcp.supabase.com/mcp?project_ref=gevkemqetvblxhhsfbcg
codex mcp login supabase_fabiano
```

Depois, valide no Codex com `/mcp` ou no terminal com:

```bash
codex mcp list
```

Observacao:
- nao sobrescrever o alias `supabase` ja existente;
- o alias novo deve coexistir com o projeto atual.

---

## Deploy no Render

Use estes comandos no painel do Render:

```bash
pip install -r requirements.txt
```

```bash
uvicorn api:app --host 0.0.0.0 --port $PORT
```

Importante: no campo **Start Command** do Render, nao coloque `web:` antes do comando.
O prefixo `web:` pertence apenas ao arquivo `Procfile`.

