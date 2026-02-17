# 🚀 API de Busca de Produtos - Extração de PDF

[![Deploy Status](https://img.shields.io/badge/deploy-live-success)](https://busca-produto.onrender.com)
[![Python](https://img.shields.io/badge/python-3.10-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green)](https://fastapi.tiangolo.com/)

Aplicação web completa para extração de dados de PDF e busca inteligente de produtos com interface moderna.

🌐 **[Ver aplicação ao vivo](https://busca-produto.onrender.com)**

---

## 📋 Índice

- [Visão Geral](#visão-geral)
- [Funcionalidades](#funcionalidades)
- [Arquitetura](#arquitetura)
- [Deploy em Produção](#deploy-em-produção)
- [Desenvolvimento Local](#desenvolvimento-local)
- [Próximas Melhorias](#próximas-melhorias)

---

## 🎯 Visão Geral

Este projeto transforma dados de PDFs em uma API REST com interface web, permitindo:
- Extração automática de produtos de PDFs tabulares
- Busca inteligente com fuzzy matching (tolerância a erros de digitação)
- Interface web responsiva e moderna
- Deploy gratuito em produção

---

## ✨ Funcionalidades

### Backend (FastAPI)
- ✅ **API REST** com endpoints `/search`, `/products`, `/info`
- ✅ **Busca inteligente** com remoção de stopwords e fuzzy matching
- ✅ **CORS habilitado** para acesso de qualquer origem
- ✅ **Documentação automática** em `/docs` (Swagger UI)
- ✅ **Cache em memória** para performance (5868 produtos)

### Frontend (HTML/CSS/JS)
- ✅ **Interface moderna** com Tailwind CSS
- ✅ **Busca em tempo real** com feedback visual
- ✅ **Auto-detecção de URL** (funciona local e em produção)
- ✅ **Animações suaves** e design responsivo
- ✅ **Formatação de preços** em Real (R$)

### Extração de Dados
- ✅ **Parser de PDF** usando `pdfplumber`
- ✅ **Limpeza de dados** automática
- ✅ **Exportação para JSON** estruturado

---

## 🏗️ Arquitetura

```
┌─────────────────┐
│   PDF Source    │
│  (Tabela de     │
│   Produtos)     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ extract_data.py │  ← Extração com pdfplumber
│                 │
│ products.json   │  ← 5868 produtos estruturados
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    api.py       │  ← FastAPI + Uvicorn
│  (Backend)      │
│                 │
│  Endpoints:     │
│  GET /          │  → index.html
│  GET /search    │  → Busca de produtos
│  GET /info      │  → Status da API
│  GET /products  │  → Lista paginada
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  index.html     │  ← Frontend (Tailwind + Vanilla JS)
│  (Frontend)     │
└─────────────────┘
```

---

## 🌍 Deploy em Produção

### Plataforma: Render.com (Free Tier)

#### Arquivos de Configuração

**`Procfile`**
```
web: uvicorn api:app --host 0.0.0.0 --port $PORT
```

**`runtime.txt`**
```
python-3.10.12
```

**`requirements.txt`**
```
fastapi
uvicorn
pdfplumber
```

#### Passos do Deploy

1. **Push para GitHub**
   ```bash
   git add .
   git commit -m "Deploy to production"
   git push origin main
   ```

2. **Configurar no Render**
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn api:app --host 0.0.0.0 --port $PORT`
   - Plano: Free

3. **Acessar**
   - URL: `https://busca-produto.onrender.com`

#### [MODIFY] [api.py](file:///c:/Users/daniel/Desktop/extrair%20pdf/api.py)
Melhoria radical no algoritmo de busca para suportar sinônimos e ranking inteligente.

**Principais mudanças:**
- **Dicionário de Sinônimos**: Mapeamento bi-direcional (ip ↔ iphone, sam ↔ samsung, etc.).
- **Ranking por Relevância**:
  - Match exato de palavra: +200 pontos.
  - Início de palavra: +100 pontos.
  - Substring: +50 pontos.
  - Início da descrição: +150 pontos extra.
  - Frase exata: +300 pontos extra.
- **Filtro Estrito por Termos**: Agora garante que *todos* os termos da busca (ou seus sinônimos) estejam presentes no produto.
- **Bônus de Especificidade**: Penalidade leve por comprimento da descrição para priorizar nomes mais curtos e diretos.

#### [MODIFY] [index.html](file:///c:/Users/daniel/Desktop/extrair%20pdf/index.html)
- ✅ Campo de URL da API agora vazio por padrão (auto-detecta o domínio atual)
- ✅ Teste de conexão usa `/info` em vez de `/`
- ✅ Funciona tanto localmente quanto no Render sem configuração manual
- ✅ Bug fix: Busca agora usa URLs relativas corrigindo erro no Render.

---

## 💻 Desenvolvimento Local

### Pré-requisitos
- Python 3.10+
- pip

### Instalação

```bash
# Clone o repositório
git clone https://github.com/thunderkat12/Busca_produto.git
cd Busca_produto

# Instale as dependências
pip install -r requirements.txt

# (Opcional) Extraia dados de um novo PDF
python extract_data.py

# Inicie o servidor
uvicorn api:app --reload
```

### Acessar Localmente
- Frontend: `http://localhost:8000`
- API Docs: `http://localhost:8000/docs`

---

## 🔮 Próximas Melhorias

### 📌 Checklist de Funcionalidades

#### 🔄 Upload de PDF pelo Frontend
- [ ] Criar endpoint `POST /upload-pdf` para receber arquivo
- [ ] Processar PDF no backend usando `extract_data.py`
- [ ] Atualizar `products.json` dinamicamente
- [ ] Interface de upload com drag-and-drop
- [ ] Feedback visual de progresso
- [ ] Validação de formato de arquivo

#### 🛒 Carrinho de Compras
- [x] Adicionar botão "Adicionar ao Carrinho" em cada produto
- [x] Implementar seleção múltipla de produtos
- [x] Criar componente de carrinho lateral
- [x] Calcular total automaticamente
- [x] Persistir carrinho no `localStorage`
- [ ] Exportar lista de produtos selecionados (PDF/CSV)
- [x] Ícone do WhatsApp com envio de pedido por itens selecionados e cupom editável (título/mensagem/endereço/rodapé)

**Novas metas (Carrinho - próxima sprint)**
- [ ] Exportar carrinho selecionado em CSV com subtotal por item
- [ ] Exportar comprovante em PDF com layout de cupom
- [ ] Permitir salvar e recuperar rascunhos de pedido
- [ ] Suportar múltiplos templates de cupom (ex.: por cliente com CNPJ)

#### 🎨 Melhorias de UX/UI
- [x] Filtros avançados (faixa de preço, categoria)
- [x] Ordenação (preço, nome, código)
- [x] Paginação de resultados
- [x] Modo escuro (dark mode)
- [x] Histórico de buscas recentes

**Novas metas (UX/UI - próxima sprint)**
- [ ] Filtro adicional por marca (extraída da descrição)
- [ ] Botão "limpar busca" no campo principal
- [ ] Melhorar acessibilidade de teclado (atalhos, foco visível e navegação por Tab)
- [ ] Exibir skeleton loading para resultados e carrinho

#### 🔐 Autenticação (Opcional)
- [ ] Sistema de login/cadastro
- [ ] Salvar carrinhos por usuário
- [ ] Histórico de pedidos

#### 📊 Analytics
- [ ] Dashboard de produtos mais buscados
- [ ] Estatísticas de uso da API
- [ ] Logs de erros e performance

---

## 📚 Tecnologias Utilizadas

| Categoria | Tecnologia |
|-----------|-----------|
| **Backend** | Python, FastAPI, Uvicorn |
| **Frontend** | HTML5, Tailwind CSS, Vanilla JavaScript |
| **Extração** | pdfplumber |
| **Deploy** | Render.com, Git |
| **Dados** | JSON |

---

## 🤝 Contribuindo

Contribuições são bem-vindas! Para mudanças importantes:
1. Fork o projeto
2. Crie uma branch (`git checkout -b feature/NovaFuncionalidade`)
3. Commit suas mudanças (`git commit -m 'Adiciona nova funcionalidade'`)
4. Push para a branch (`git push origin feature/NovaFuncionalidade`)
5. Abra um Pull Request

---

## 📄 Licença

Este projeto é de código aberto para fins educacionais.

---

## 👤 Autor

**Daniel** - [GitHub](https://github.com/thunderkat12)

---

## 🎉 Status do Projeto

✅ **Em Produção** - Totalmente funcional e acessível publicamente

**Última atualização**: Fevereiro 2026
