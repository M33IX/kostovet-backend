from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import boto3
from sqlalchemy import delete, select

from kosto_vet.bootstrap.settings import get_settings
from kosto_vet.core.types import utc_now
from kosto_vet.infrastructure.database import Database
from kosto_vet.infrastructure.models import (
    Category,
    LegalDocumentVersion,
    Product,
    ProductImage,
    ProductImageVariant,
    ProductSpec,
    RelatedProduct,
    StaffUser,
    StockItem,
    Warehouse,
)
from kosto_vet.infrastructure.security import hash_password, normalize_email

CATEGORIES = [
    ("plates", "Пластины", "Для фиксации переломов длинных и плоских костей"),
    ("screws", "Винты", "Для остеосинтеза и фиксации пластин"),
    ("tools", "Инструменты", "Специализированные инструменты для операций"),
    ("sutures", "Шовный материал", "Для мягкотканевых и кожных швов"),
]

PRODUCTS: list[dict[str, Any]] = [
    {
        "slug": "plate-t-58-6",
        "category": "plates",
        "name": "Пластина Т-образная",
        "subtitle": "Для фиксации переломов у собак мелких пород",
        "price": 1143,
        "stock": 12,
        "article": "KV-TP-058-06",
        "specs": {
            "Тип": "Т-образная",
            "Длина": "58 мм",
            "Ширина": "6 мм",
            "Количество отверстий": "6",
            "Материал": "Медицинская сталь",
        },
    },
    {
        "slug": "plate-straight-58-6",
        "category": "plates",
        "name": "Пластина прямая",
        "subtitle": "Для фиксации переломов длинных костей",
        "price": 1143,
        "stock": 12,
        "article": "KV-PL-058-06",
        "specs": {
            "Тип": "Прямая",
            "Длина": "58 мм",
            "Ширина": "6 мм",
            "Количество отверстий": "6",
            "Материал": "Медицинская сталь",
        },
    },
    {
        "slug": "plate-t-70-6",
        "category": "plates",
        "name": "Пластина Т-образная",
        "subtitle": "Для карликовых пород собак и мелких животных",
        "price": 1286,
        "stock": 8,
        "article": "KV-TP-070-06",
        "specs": {
            "Тип": "Т-образная",
            "Длина": "70 мм",
            "Ширина": "6 мм",
            "Количество отверстий": "6",
            "Материал": "Медицинская сталь",
        },
    },
    {
        "slug": "plate-l-60-6",
        "category": "plates",
        "name": "Пластина L-образная",
        "subtitle": "Для фиксации переломов под углом",
        "price": 1301,
        "stock": 5,
        "article": "KV-LP-060-06",
        "specs": {
            "Тип": "L-образная",
            "Длина": "60 мм",
            "Ширина": "6 мм",
            "Количество отверстий": "6",
            "Материал": "Медицинская сталь",
        },
    },
    {
        "slug": "plate-reconstructive-85",
        "category": "plates",
        "name": "Пластина реконструктивная",
        "subtitle": "Для сложных и оскольчатых переломов",
        "price": 1540,
        "stock": 3,
        "article": "KV-RP-085-06",
        "specs": {
            "Тип": "Реконструктивная",
            "Длина": "85 мм",
            "Ширина": "6 мм",
            "Количество отверстий": "6",
            "Материал": "Медицинская сталь",
        },
    },
    {
        "slug": "plate-straight-33-4",
        "category": "plates",
        "name": "Пластина прямая",
        "subtitle": "Узкая, для малых пород",
        "price": 972,
        "stock": 15,
        "article": "KV-PL-033-04",
        "specs": {
            "Тип": "Прямая",
            "Длина": "33 мм",
            "Ширина": "4,5 мм",
            "Количество отверстий": "4",
            "Материал": "Медицинская сталь",
        },
    },
    {
        "slug": "plate-t-47-6",
        "category": "plates",
        "name": "Пластина Т-образная",
        "subtitle": "Для фиксации плечевой кости",
        "price": 1128,
        "stock": 7,
        "article": "KV-TP-047-06",
        "specs": {
            "Тип": "Т-образная",
            "Длина": "47 мм",
            "Ширина": "6 мм",
            "Количество отверстий": "6",
            "Материал": "Медицинская сталь",
        },
    },
    {
        "slug": "plate-l-70-6",
        "category": "plates",
        "name": "Пластина L-образная",
        "subtitle": "Широкая, для крупных животных",
        "price": 1412,
        "stock": 4,
        "article": "KV-LP-070-06",
        "specs": {
            "Тип": "L-образная",
            "Длина": "70 мм",
            "Ширина": "6 мм",
            "Количество отверстий": "6",
            "Материал": "Медицинская сталь",
        },
    },
    {
        "slug": "screw-cortical-2-0",
        "category": "screws",
        "name": "Винт кортикальный 2,0 мм",
        "subtitle": "Самонарезающий, шестигранный шлиц",
        "price": 408,
        "stock": 48,
        "article": "KV-SC-020",
        "specs": {"Диаметр": "2,0 мм", "Длина": "6–22 мм", "Материал": "Медицинская сталь"},
    },
    {
        "slug": "driver-hex-2-0",
        "category": "tools",
        "name": "Отвёртка шестигранная 2,0 мм",
        "subtitle": "Хирургическая рукоятка с насадкой",
        "price": 850,
        "stock": 7,
        "article": "KV-DR-020",
        "specs": {"Размер": "2,0 мм", "Длина": "110 мм", "Стерилизация": "Автоклав"},
    },
    {
        "slug": "suture-monofilament-3-0",
        "category": "sutures",
        "name": "Монофиламент 3/0",
        "subtitle": "Атравматический шовный материал",
        "price": 850,
        "stock": 20,
        "article": "KV-SU-030",
        "specs": {"Размер": "3/0", "Игла": "26 мм", "Материал": "Полипропилен"},
    },
]


async def seed_demo() -> None:
    settings = get_settings()
    database = Database(settings)
    async with database.sessions() as session:
        categories: dict[str, Category] = {}
        for sort_order, (slug, title, description) in enumerate(CATEGORIES):
            row = await session.scalar(select(Category).where(Category.slug == slug))
            if not row:
                row = Category(
                    slug=slug,
                    path=slug,
                    depth=0,
                    title=title,
                    description=description,
                    sort_order=sort_order,
                    is_active=True,
                    is_published=True,
                )
                session.add(row)
                await session.flush()
            row.path = slug
            row.depth = 0
            row.title = title
            row.description = description
            row.sort_order = sort_order
            row.is_active = True
            row.is_published = True
            categories[slug] = row
        warehouse = await session.scalar(select(Warehouse).where(Warehouse.name == "Воронеж demo"))
        if not warehouse:
            warehouse = Warehouse(name="Воронеж demo", timezone="Europe/Moscow", is_active=True)
            session.add(warehouse)
            await session.flush()
        plate_ids = []
        for fixture in PRODUCTS:
            product = await session.scalar(select(Product).where(Product.slug == fixture["slug"]))
            if not product:
                product = Product(
                    category_id=categories[fixture["category"]].id,
                    article=fixture["article"],
                    slug=fixture["slug"],
                    name=fixture["name"],
                    subtitle=fixture["subtitle"],
                    description=fixture["subtitle"],
                    final_price_minor=fixture["price"] * 100,
                    currency="RUB",
                    unit="pcs",
                    is_active=True,
                    is_published=True,
                )
                session.add(product)
                await session.flush()
            product.category_id = categories[fixture["category"]].id
            product.article = fixture["article"]
            product.name = fixture["name"]
            product.subtitle = fixture["subtitle"]
            product.description = fixture["subtitle"]
            product.final_price_minor = fixture["price"] * 100
            product.currency = "RUB"
            product.unit = "pcs"
            product.is_active = True
            product.is_published = True
            await session.execute(delete(ProductSpec).where(ProductSpec.product_id == product.id))
            for index, (label, value) in enumerate(fixture["specs"].items()):
                session.add(
                    ProductSpec(
                        product_id=product.id,
                        key=f"spec_{index}",
                        label=label,
                        value=value,
                        sort_order=index,
                    )
                )
            stock = await session.scalar(
                select(StockItem).where(
                    StockItem.warehouse_id == warehouse.id, StockItem.product_id == product.id
                )
            )
            if stock:
                stock.stock_quantity = fixture["stock"]
                stock.available_quantity = fixture["stock"]
                stock.synced_at = utc_now()
                stock.source_updated_at = utc_now()
                stock.is_stale = False
            else:
                session.add(
                    StockItem(
                        warehouse_id=warehouse.id,
                        product_id=product.id,
                        stock_quantity=fixture["stock"],
                        available_quantity=fixture["stock"],
                        reserved_quantity=0,
                        in_transit_quantity=0,
                        synced_at=utc_now(),
                        source_updated_at=utc_now(),
                        is_stale=False,
                    )
                )
            if fixture["category"] == "plates":
                plate_ids.append(product.id)
        for product_id in plate_ids:
            for index, related_id in enumerate(pid for pid in plate_ids if pid != product_id):
                if not await session.get(RelatedProduct, (product_id, related_id)):
                    session.add(
                        RelatedProduct(
                            product_id=product_id,
                            related_product_id=related_id,
                            relation_type="similar",
                            sort_order=index,
                        )
                    )
        if not await session.scalar(
            select(LegalDocumentVersion).where(
                LegalDocumentVersion.document_type == "personal_data_consent",
                LegalDocumentVersion.version == "demo-v1",
            )
        ):
            session.add(
                LegalDocumentVersion(
                    document_type="personal_data_consent",
                    version="demo-v1",
                    effective_at=utc_now(),
                    content_hash="0" * 64,
                )
            )
        await session.commit()
    await database.close()


async def create_staff(email: str, password: str, name: str, role: str) -> None:
    if role not in {"admin", "manager", "content", "readonly"}:
        raise ValueError("invalid staff role")
    settings = get_settings()
    database = Database(settings)
    async with database.sessions() as session:
        normalized = normalize_email(email)
        staff = await session.scalar(
            select(StaffUser).where(StaffUser.normalized_email == normalized)
        )
        if staff:
            staff.password_hash = hash_password(password)
            staff.name = name
            staff.role = role
            staff.status = "active"
        else:
            session.add(
                StaffUser(
                    email=email,
                    normalized_email=normalized,
                    password_hash=hash_password(password),
                    name=name,
                    role=role,
                    status="active",
                )
            )
        await session.commit()
    await database.close()


async def import_media(manifest_path: Path) -> None:
    settings = get_settings()
    if (
        not settings.s3_originals_bucket
        or not settings.s3_public_media_bucket
        or not settings.s3_access_key_id
        or not settings.s3_secret_access_key
    ):
        raise RuntimeError("S3 settings are required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
    )
    database = Database(settings)
    async with database.sessions() as session:
        for entry in manifest["images"]:
            product = await session.scalar(
                select(Product).where(Product.slug == entry["product_slug"])
            )
            if not product:
                raise RuntimeError(f"unknown product {entry['product_slug']}")
            source = (manifest_path.parent / entry["original"]).resolve()
            original_key = f"originals/{product.id}/{source.name}"
            s3.upload_file(str(source), settings.s3_originals_bucket, original_key)
            image = ProductImage(
                product_id=product.id,
                original_object_key=original_key,
                public_url=None,
                alt=entry.get("alt", product.name),
                status="ready",
                sort_order=entry.get("sort_order", 0),
                is_primary=entry.get("is_primary", False),
            )
            session.add(image)
            await session.flush()
            for variant in entry.get("variants", []):
                file_path = (manifest_path.parent / variant["file"]).resolve()
                key = f"media/{product.id}/{image.id}/{file_path.name}"
                s3.upload_file(
                    str(file_path),
                    settings.s3_public_media_bucket,
                    key,
                    ExtraArgs={
                        "CacheControl": "public, max-age=31536000, immutable",
                        "ContentType": variant["content_type"],
                    },
                )
                url = (
                    f"{settings.s3_public_base_url.rstrip('/')}/{key}"
                    if settings.s3_public_base_url
                    else key
                )
                session.add(
                    ProductImageVariant(
                        image_id=image.id,
                        kind=variant["kind"],
                        format=variant["format"],
                        public_url=url,
                        public_object_key=key,
                        width=variant["width"],
                        height=variant["height"],
                        size_bytes=file_path.stat().st_size,
                        checksum=hashlib.sha256(file_path.read_bytes()).hexdigest(),
                    )
                )
                if image.public_url is None:
                    image.public_url = url
        await session.commit()
    await database.close()


def _catalog_allowlist(path: Path | None) -> set[str]:
    if path is None:
        return set()
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        values = json.loads(raw)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError("catalog allowlist JSON must be an array of external IDs")
        return {item.strip() for item in values if item.strip()}
    return {line.strip() for line in raw.splitlines() if line.strip() and not line.startswith("#")}


def _catalog_manifest(
    path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("catalog manifest must be a JSON object")
    warehouse = document.get("warehouse")
    categories = document.get("categories")
    products = document.get("products")
    if (
        not isinstance(warehouse, dict)
        or not isinstance(categories, list)
        or not isinstance(products, list)
    ):
        raise ValueError("catalog manifest requires warehouse, categories and products")
    if not all(isinstance(item, dict) for item in [warehouse, *categories, *products]):
        raise ValueError("catalog manifest entries must be objects")
    return warehouse, categories, products


def _required_string(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"catalog manifest requires non-empty {key}")
    return value.strip()


async def import_catalog(
    manifest_path: Path, allowlist_path: Path | None, *, apply: bool
) -> dict[str, int]:
    """Safely bootstrap a reviewed production catalog from an explicit manifest.

    The importer never enumerates a provider itself.  Every external ID must be in
    the manifest and, in production, in a separately reviewed allowlist.
    """
    settings = get_settings()
    warehouse_data, category_data, product_data = _catalog_manifest(manifest_path)
    allowlist = _catalog_allowlist(allowlist_path)
    if settings.app_env == "production" and not allowlist:
        raise ValueError("production catalog import requires --allowlist")
    warehouse_external_id = _required_string(warehouse_data, "external_id")
    warehouse_name = _required_string(warehouse_data, "name")
    database = Database(settings)
    report = {"created": 0, "updated": 0, "skipped": 0, "categories": 0}
    try:
        async with database.sessions() as session:
            categories: dict[str, Category] = {}
            for sort_order, entry in enumerate(category_data):
                slug = _required_string(entry, "slug")
                title = _required_string(entry, "title")
                category = await session.scalar(select(Category).where(Category.slug == slug))
                if category is None:
                    category = Category(
                        slug=slug,
                        path=slug,
                        depth=0,
                        title=title,
                        description=str(entry.get("description") or ""),
                        sort_order=sort_order,
                        is_active=bool(entry.get("active", True)),
                        is_published=bool(entry.get("published", False)),
                    )
                    session.add(category)
                    report["categories"] += 1
                else:
                    category.title = title
                    category.description = str(entry.get("description") or "")
                    category.sort_order = sort_order
                    category.is_active = bool(entry.get("active", True))
                    category.is_published = bool(entry.get("published", False))
                categories[slug] = category
            await session.flush()
            warehouse = await session.scalar(
                select(Warehouse).where(Warehouse.moysklad_id == warehouse_external_id)
            )
            if warehouse is None:
                warehouse = Warehouse(
                    moysklad_id=warehouse_external_id,
                    name=warehouse_name,
                    timezone="Europe/Moscow",
                    is_active=True,
                )
                session.add(warehouse)
                await session.flush()
            for entry in product_data:
                external_id = _required_string(entry, "external_id")
                if allowlist and external_id not in allowlist:
                    report["skipped"] += 1
                    continue
                category_slug = _required_string(entry, "category")
                category = categories.get(category_slug)
                if category is None:
                    raise ValueError(
                        f"product {external_id} references unknown category {category_slug}"
                    )
                article = _required_string(entry, "article")
                slug = _required_string(entry, "slug")
                name = _required_string(entry, "name")
                price_minor = entry.get("price_minor")
                stock_quantity = entry.get("stock_quantity")
                if not isinstance(price_minor, int) or price_minor < 0:
                    raise ValueError(
                        f"product {external_id} requires non-negative integer price_minor"
                    )
                if not isinstance(stock_quantity, int) or stock_quantity < 0:
                    raise ValueError(
                        f"product {external_id} requires non-negative integer stock_quantity"
                    )
                product = await session.scalar(
                    select(Product).where(Product.moysklad_id == external_id)
                )
                if product is None:
                    product = await session.scalar(
                        select(Product).where(Product.article == article)
                    )
                slug_owner = await session.scalar(select(Product).where(Product.slug == slug))
                if slug_owner is not None and slug_owner is not product:
                    raise ValueError(f"product slug {slug} is already assigned to another product")
                created = product is None
                if product is None:
                    product = Product(
                        category_id=category.id,
                        moysklad_id=external_id,
                        article=article,
                        slug=slug,
                        name=name,
                        subtitle=str(entry.get("subtitle") or "") or None,
                        description=str(entry.get("description") or "") or None,
                        final_price_minor=price_minor,
                        is_active=bool(entry.get("active", True)),
                        is_published=bool(entry.get("published", False)),
                    )
                    session.add(product)
                    await session.flush()
                else:
                    if product.moysklad_id not in (None, external_id):
                        raise ValueError(f"product {article} is mapped to another external ID")
                    product.moysklad_id = external_id
                    product.category_id = category.id
                    product.article = article
                    product.slug = slug
                    product.name = name
                    product.final_price_minor = price_minor
                    product.subtitle = str(entry.get("subtitle") or "") or None
                    product.description = str(entry.get("description") or "") or None
                    product.is_active = bool(entry.get("active", True))
                    product.is_published = bool(entry.get("published", False))
                    product.version += 1
                stock = await session.scalar(
                    select(StockItem).where(
                        StockItem.warehouse_id == warehouse.id, StockItem.product_id == product.id
                    )
                )
                if stock is None:
                    session.add(
                        StockItem(
                            warehouse_id=warehouse.id,
                            product_id=product.id,
                            stock_quantity=stock_quantity,
                            available_quantity=stock_quantity,
                            synced_at=utc_now(),
                            source_updated_at=utc_now(),
                            is_stale=False,
                        )
                    )
                else:
                    stock.stock_quantity = stock_quantity
                    stock.available_quantity = stock_quantity
                    stock.synced_at = utc_now()
                    stock.source_updated_at = utc_now()
                    stock.is_stale = False
                    stock.version += 1
                report["created" if created else "updated"] += 1
            if apply:
                await session.commit()
            else:
                await session.rollback()
    finally:
        await database.close()
    return report


def verify_s3() -> dict[str, object]:
    """Read-only readiness probe for the two-bucket production media topology."""
    settings = get_settings()
    access_key = settings.s3_access_key_id
    secret_key = settings.s3_secret_access_key
    originals_bucket = settings.s3_originals_bucket
    public_bucket = settings.s3_public_media_bucket
    public_base_url = settings.s3_public_base_url
    if (
        access_key is None
        or secret_key is None
        or originals_bucket is None
        or public_bucket is None
        or public_base_url is None
    ):
        raise RuntimeError("complete S3 settings are required")
    if not settings.admin_origins:
        raise RuntimeError("ADMIN_ORIGINS must contain the exact staff frontend origin")
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=access_key.get_secret_value(),
        aws_secret_access_key=secret_key.get_secret_value(),
    )
    s3.head_bucket(Bucket=originals_bucket)
    s3.head_bucket(Bucket=public_bucket)
    try:
        cors_rules = s3.get_bucket_cors(Bucket=originals_bucket)["CORSRules"]
    except Exception as exc:  # provider error is deliberately not exposed through HTTP
        raise RuntimeError("originals bucket CORS is not readable or not configured") from exc
    configured_origins = {
        origin for rule in cors_rules for origin in rule.get("AllowedOrigins", [])
    }
    missing_origins = set(settings.admin_origins) - configured_origins
    if missing_origins:
        raise RuntimeError("originals bucket CORS is missing an exact ADMIN_ORIGINS value")
    return {
        "endpoint": settings.s3_endpoint_url,
        "originals_bucket": originals_bucket,
        "public_media_bucket": public_bucket,
        "public_base_url": public_base_url,
        "admin_origins_verified": sorted(settings.admin_origins),
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="kosto-vet")
    sub = parser.add_subparsers(dest="command", required=True)
    api = sub.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    sub.add_parser("worker")
    sub.add_parser("scheduler")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--revision", default="head")
    sub.add_parser("seed-demo")
    staff = sub.add_parser("create-staff")
    staff.add_argument("--email", required=True)
    staff.add_argument("--password", required=True)
    staff.add_argument("--name", required=True)
    staff.add_argument("--role", choices=["admin", "manager", "content", "readonly"], required=True)
    media = sub.add_parser("import-media")
    media.add_argument("manifest", type=Path)
    catalog = sub.add_parser("import-catalog")
    catalog.add_argument("manifest", type=Path)
    catalog.add_argument("--allowlist", type=Path)
    catalog.add_argument(
        "--apply", action="store_true", help="write reviewed manifest; default is dry-run"
    )
    sub.add_parser("verify-s3", help="read-only validation of private/public S3 topology")
    args = parser.parse_args()
    if args.command == "api":
        import uvicorn

        uvicorn.run("kosto_vet.bootstrap.api:app", host=args.host, port=args.port)
    elif args.command == "worker":
        from kosto_vet.bootstrap.worker import main as worker_main

        asyncio.run(worker_main())
    elif args.command == "scheduler":
        from kosto_vet.bootstrap.scheduler import main as scheduler_main

        asyncio.run(scheduler_main())
    elif args.command == "migrate":
        from alembic.config import Config

        from alembic import command

        command.upgrade(Config("alembic.ini"), args.revision)
    elif args.command == "seed-demo":
        asyncio.run(seed_demo())
    elif args.command == "create-staff":
        asyncio.run(create_staff(args.email, args.password, args.name, args.role))
    elif args.command == "import-media":
        asyncio.run(import_media(args.manifest))
    elif args.command == "import-catalog":
        print(
            json.dumps(
                asyncio.run(import_catalog(args.manifest, args.allowlist, apply=args.apply)),
                ensure_ascii=False,
            )
        )
    elif args.command == "verify-s3":
        print(json.dumps(verify_s3(), ensure_ascii=False))


if __name__ == "__main__":
    main()
