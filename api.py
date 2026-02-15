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
    query: str = Query(..., min_length=2, description="Search term for product name"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """
    Search for products by description (case-insensitive) with fuzzy matching.
    """
    if x_api_key:
        print(f"Received request with API Key: {x_api_key[:5]}... (valid)")
    
    # Clean query: Remove special chars and stopwords
    import re
    # Remove punctuation
    cleaned_query = re.sub(r'[^\w\s]', '', query.lower())
    
    # Common stopwords in Portuguese context
    stopwords = {'tem', 'temm', 'vc', 'voce', 'gostaria', 'quero', 'preciso', 'de', 'do', 'da', 'o', 'a', 'e', 'um', 'uma', 'por', 'favor', 'preço', 'valor', 'quanto', 'custa'}
    
    query_terms = [term for term in cleaned_query.split() if term not in stopwords]
    
    if not query_terms:
        # If everything was a stopword, fall back to original query split
        query_terms = query.lower().split()
        
    print(f"Searching for terms: {query_terms}")

    results = []
    
    # 1. Exact/Partial Match (High Priority)
    for product in products_cache:
        desc = product['description'].lower()
        if all(term in desc for term in query_terms):
            results.append(product)
            
    # 2. Fuzzy Match (if few results) - handles typos like "iphonme"
    if len(results) < 5:
        all_descriptions = [p['description'] for p in products_cache]
        # Find close matches for the full query string
        # cutoff=0.6 means 60% similarity required
        matches = difflib.get_close_matches(query.upper(), all_descriptions, n=5, cutoff=0.5)
        
        for match in matches:
            # Add matched products if not already in results
            for product in products_cache:
                if product['description'] == match and product not in results:
                    results.append(product)

    return {"count": len(results), "results": results}

@app.get("/products")
def list_products(limit: int = 50, offset: int = 0):
    return products_cache[offset : offset + limit]

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
