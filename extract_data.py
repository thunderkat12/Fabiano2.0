import argparse
import json
import re
from typing import Any

import pdfplumber

DEFAULT_OUTPUT_PATH = "products.json"

# Regex pattern to match the line structure:
# Code | Description | Unit | Price1 | Price2 | Price3
LINE_PATTERN = re.compile(r"^(\d+)\s+(.+?)\s+([A-Z]{2})\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s*$")


def parse_product_line(line: str) -> dict[str, str] | None:
    match = LINE_PATTERN.match(line)
    if not match:
        return None

    code, desc, unit, p1, p2, p3 = match.groups()
    return {
        "id": code,
        "description": desc.strip(),
        "unit": unit,
        "price_sight": p1.replace(".", "").replace(",", "."),
        "price_term": p2.replace(".", "").replace(",", "."),
        "price_wholesale": p3.replace(".", "").replace(",", "."),
    }


def extract_products_from_pdf(pdf_path: str) -> tuple[list[dict[str, str]], int]:
    products: list[dict[str, str]] = []
    print(f"Opening {pdf_path}...")

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        print(f"Processing {total_pages} pages...")

        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()
            if not text:
                continue

            for line in text.split("\n"):
                product = parse_product_line(line)
                if product:
                    products.append(product)

            if i % 10 == 0:
                print(f"Processed {i}/{total_pages} pages...")

    print(f"Extraction complete. Found {len(products)} products.")
    return products, total_pages


def save_products(products: list[dict[str, Any]], output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"Saved to {output_path}")


def process_pdf(pdf_path: str, output_path: str) -> dict[str, Any]:
    products, total_pages = extract_products_from_pdf(pdf_path)
    save_products(products, output_path)
    return {
        "total_products": len(products),
        "total_pages": total_pages,
        "output_path": output_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extrai produtos de PDF para JSON.")
    parser.add_argument("--pdf", required=True, help="Caminho do arquivo PDF de origem.")
    parser.add_argument("--out", default=DEFAULT_OUTPUT_PATH, help="Caminho do arquivo JSON de saida.")
    args = parser.parse_args()

    try:
        process_pdf(args.pdf, args.out)
    except Exception as exc:
        print(f"Error: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
