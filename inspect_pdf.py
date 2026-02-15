import pdfplumber

pdf_path = "TABELA FABIANO ACESSORIOS E TELAS 09-02-2026.pdf"

with pdfplumber.open(pdf_path) as pdf:
    print(f"Total pages: {len(pdf.pages)}")
    
    # Inspect first page
    first_page = pdf.pages[0]
    print("\n--- First Page Text ---")
    print(first_page.extract_text())
    
    print("\n--- First Page Tables ---")
    tables = first_page.extract_tables()
    for i, table in enumerate(tables):
        print(f"Table {i+1}:")
        for row in table:
            print(row)
