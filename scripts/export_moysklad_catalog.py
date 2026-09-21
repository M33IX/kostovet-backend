from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

from kosto_vet.bootstrap.settings import Settings
from kosto_vet.infrastructure.integrations import MoySkladAdapter
from kosto_vet.services.moysklad import (
    CATEGORY_ORDER,
    _integer_quantity,
    _price_minor,
    category_slug,
    product_slug,
)


def external_id(row: dict[str, Any]) -> str:
    return str(
        row.get("assortmentId")
        or row.get("meta", {}).get("href", "").rstrip("/").rsplit("/", 1)[-1]
    )


async def export(
    manifest_path: Path,
    allowlist_path: Path,
    *,
    published: bool = False,
) -> dict[str, int]:
    settings = Settings()
    adapter = MoySkladAdapter(settings)
    folders, products, stock_rows = await asyncio.gather(
        adapter.fetch_pages("entity/productfolder"),
        adapter.fetch_pages("entity/product"),
        adapter.fetch_pages(
            "report/stock/all",
            params={"store.id": str(settings.moysklad_warehouse_id)},
        ),
    )
    folder_by_id = {str(row["id"]): row for row in folders}
    folder_to_category = {
        folder_id: category_slug(str(row.get("name") or ""), folder_id)
        for folder_id, row in folder_by_id.items()
    }
    stock_by_product = {external_id(row): row for row in stock_rows if external_id(row)}

    categories = [
        {
            "slug": slug,
            "title": str(folder_by_id[folder_id].get("name") or slug),
            "description": str(folder_by_id[folder_id].get("description") or ""),
            "active": True,
            "published": published,
        }
        for folder_id, slug in folder_to_category.items()
    ]
    categories.sort(key=lambda row: (CATEGORY_ORDER.get(row["slug"], 99), row["title"]))

    draft_products: list[dict[str, Any]] = []
    slugs: Counter[str] = Counter()
    for row in products:
        product_id = str(row.get("id") or "")
        article = str(row.get("article") or row.get("code") or "").strip()
        folder_id = (
            str(row.get("productFolder", {}).get("meta", {}).get("href", ""))
            .rstrip("/")
            .rsplit("/", 1)[-1]
        )
        category = folder_to_category.get(folder_id)
        price = _price_minor(row, str(settings.moysklad_price_type_id))
        if not product_id or not article or category is None or price is None:
            continue
        slug = product_slug(category, article, product_id)
        slugs[slug] += 1
        if slugs[slug] > 1:
            slug = f"{slug}-{product_id.split('-', 1)[0]}"
        stock = stock_by_product.get(product_id, {})
        draft_products.append(
            {
                "external_id": product_id,
                "category": category,
                "article": article,
                "slug": slug,
                "name": str(row.get("name") or article),
                "subtitle": "",
                "description": str(row.get("description") or ""),
                "price_minor": price,
                "stock_quantity": max(0, _integer_quantity(stock.get("stock", 0))),
                "active": not bool(row.get("archived")),
                "published": published,
            }
        )
    draft_products.sort(key=lambda row: (CATEGORY_ORDER.get(row["category"], 99), row["article"]))

    manifest = {
        "warehouse": {
            "external_id": str(settings.moysklad_warehouse_id),
            "name": "Основной склад",
        },
        "categories": categories,
        "products": draft_products,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    allowlist_path.write_text(
        "# Generated draft: review before production import.\n"
        + "\n".join(row["external_id"] for row in draft_products)
        + "\n",
        encoding="utf-8",
    )
    return {
        "provider_products": len(products),
        "exported_products": len(draft_products),
        "categories": len(categories),
        "stock_rows": len(stock_rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a non-publishing catalog import draft from MoySklad."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("allowlist", type=Path)
    parser.add_argument(
        "--published",
        action="store_true",
        help="mark all exported categories and products as published",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                export(
                    args.manifest,
                    args.allowlist,
                    published=args.published,
                )
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
