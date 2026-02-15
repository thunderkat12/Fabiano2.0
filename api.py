from fastapi import FastAPI, HTTPException, Query
from typing import List, Optional
import json
import os
import uvicorn

from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Product API", description="API to search products from extracted PDF")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


DATA_FILE = "products.json"
products_cache = []

def load_data():
    global products_cache
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            products_cache = json.load(f)
        print(f"Loaded {len(products_cache)} products into memory.")
    else:
        print(f"Warning: {DATA_FILE} not found. Run extract_data.py first.")

from fastapi.responses import FileResponse

@app.on_event("startup")
async def startup_event():
    load_data()

@app.get("/info")
def get_info():
    return {"message": "Product API is running. Use /search?query=... to search.", "total_products": len(products_cache)}

@app.get("/")
def read_root():
    return FileResponse("index.html")

import difflib

# ... (imports)

@app.get("/search")
def search_products(
    query: str = Query(..., min_length=1, description="Search term for product name"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """
    Search for products by description with intelligent synonym-aware ranking.
    """
    if x_api_key:
        print(f"Received request with API Key: {x_api_key[:5]}... (valid)")
    
    import re
    
    # Define synonym groups (bidirectional)
    synonym_map = {
        'ip': 'iphone',
        'iphone': 'ip',
        'sam': 'samsung',
        'samsung': 'sam',
        'moto': 'motorola',
        'motorola': 'moto',
        'xm': 'xiaomi',
        'xiaomi': 'xm',
        'xiao': 'xiaomi'
    }
    
    # Process query
    raw_query = query.lower().strip()
    cleaned_query = re.sub(r'[^\w\s]', ' ', raw_query)
    query_terms = cleaned_query.split()
    
    if not query_terms:
        return {"count": 0, "results": []}
        
    print(f"Original query: '{query}'")
    
    scored_results = []
    
    for product in products_cache:
        desc = product['description'].lower()
        desc_words = re.sub(r'[^\w\s]', ' ', desc).split()
        score = 0
        matched_terms = 0
        
        for term in query_terms:
            term_synonym = synonym_map.get(term)
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
        if len(query_terms) > 1 and cleaned_query in re.sub(r'[^\w\s]', ' ', desc):
            score += 300
            
        # Penalty: Description length (prefer shorter, more specific matches)
        score -= len(desc) * 0.1
        
        scored_results.append((score, product))
    
    # Sort and take top 20
    scored_results.sort(key=lambda x: x[0], reverse=True)
    results = [product for score, product in scored_results[:20]]
    
    print(f"Found {len(results)} results")
    if results:
        print(f"Top result: {results[0]['description']} (score: {scored_results[0][0]})")

    return {"count": len(results), "results": results}

@app.get("/products")
def list_products(limit: int = 50, offset: int = 0):
    return products_cache[offset : offset + limit]

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
