import pdfplumber
import re
import json
import sys

pdf_path = "TABELA FABIANO ACESSORIOS E TELAS 09-02-2026.pdf"
output_path = "products.json"

# Regex pattern to match the line structure
# Code | Description | Unit | Price1 | Price2 | Price3
# Example: 3166 ADAPTADOR AUXILIAR DE AUDIO HMASTON UN 0,00 0,00 0,00
# We anchor to the end of the line to catch the prices and unit reliably
line_pattern = re.compile(r"^(\d+)\s+(.+?)\s+([A-Z]{2})\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s*$")

products = []

print(f"Opening {pdf_path}...")
try:
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        print(f"Processing {total_pages} pages...")
        
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if not text:
                continue
                
            for line in text.split('\n'):
                match = line_pattern.match(line)
                if match:
                    code, desc, unit, p1, p2, p3 = match.groups()
                    products.append({
                        "id": code,
                        "description": desc.strip(),
                        "unit": unit,
                        "price_sight": p1.replace('.', '').replace(',', '.'), # Convert to float-friendly format
                        "price_term": p2.replace('.', '').replace(',', '.'),
                        "price_wholesale": p3.replace('.', '').replace(',', '.')
                    })
            
            # Progress indicator
            if (i + 1) % 10 == 0:
                print(f"Processed {i + 1}/{total_pages} pages...")

    print(f"\nExtraction complete. Found {len(products)} products.")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    
    print(f"Saved to {output_path}")

except Exception as e:
    print(f"Error: {e}")
