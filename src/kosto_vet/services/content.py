from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from uuid import UUID

import boto3
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kosto_vet.core.errors import DomainError, not_found
from kosto_vet.core.time import utc_now
from kosto_vet.models import (
    Article,
    ArticleMedia,
    AuditLog,
    IntegrationJob,
    MediaAsset,
    MediaVariant,
    Product,
    ProductMedia,
)


class ContentServiceMixin:
    @staticmethod
    def _validate_markdown(value: str) -> str:
        normalized = value.replace("\r\n", "\n").strip()
        if re.search(r"<\s*/?\s*[a-zA-Z!][^>]*>", normalized):
            raise DomainError("INVALID_REQUEST", "HTML в Markdown не допускается.", 422)
        return normalized

    async def _media_payload(
        self, session: AsyncSession, asset: MediaAsset, alt: str
    ) -> dict[str, Any]:
        variants = (
            await session.scalars(
                select(MediaVariant)
                .where(MediaVariant.asset_id == asset.id)
                .order_by(MediaVariant.kind, MediaVariant.format)
            )
        ).all()
        fallback = next((item for item in variants if item.kind == "detail"), None)
        return {
            "id": str(asset.id),
            "status": asset.status,
            "url": fallback.public_url if fallback else None,
            "alt": alt,
            "width": asset.width,
            "height": asset.height,
            "variants": [
                {
                    "kind": item.kind,
                    "format": item.format,
                    "url": item.public_url,
                    "width": item.width,
                    "height": item.height,
                }
                for item in variants
            ],
            "error_code": asset.error_code,
        }

    async def article_payload(self, session: AsyncSession, article: Article) -> dict[str, Any]:
        media_links = (
            await session.execute(
                select(ArticleMedia, MediaAsset)
                .join(MediaAsset, MediaAsset.id == ArticleMedia.asset_id)
                .where(ArticleMedia.article_id == article.id, MediaAsset.status == "ready")
                .order_by(ArticleMedia.role, ArticleMedia.sort_order, ArticleMedia.created_at)
            )
        ).all()
        cover_payload = None
        media = []
        for link, asset in media_links:
            payload = {"link_id": str(link.id), "role": link.role}
            payload.update(await self._media_payload(session, asset, link.alt))
            media.append(payload)
            if link.role == "article_cover" and cover_payload is None:
                cover_payload = payload
        return {
            "id": str(article.id),
            "slug": article.slug,
            "title": article.title,
            "excerpt": article.excerpt,
            "content_markdown": article.content_markdown,
            "status": article.status,
            "published_at": article.published_at.isoformat() if article.published_at else None,
            "seo": {"title": article.seo_title, "description": article.seo_description},
            "cover": cover_payload,
            "media": media,
            "version": article.version,
        }

    async def list_public_articles(
        self, session: AsyncSession, page: int, limit: int
    ) -> dict[str, Any]:
        where = [Article.status == "published", Article.published_at.is_not(None)]
        total = int(await session.scalar(select(func.count(Article.id)).where(*where)) or 0)
        rows = (
            await session.scalars(
                select(Article)
                .where(*where)
                .order_by(Article.published_at.desc(), Article.id.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        ).all()
        items = []
        for article in rows:
            payload = await self.article_payload(session, article)
            payload.pop("content_markdown")
            payload.pop("media")
            items.append(payload)
        return {
            "items": items,
            "page": page,
            "limit": limit,
            "total": total,
        }

    async def public_article(self, session: AsyncSession, slug: str) -> dict[str, Any]:
        article = await session.scalar(
            select(Article).where(
                Article.slug == slug,
                Article.status == "published",
                Article.published_at.is_not(None),
            )
        )
        if not article:
            raise not_found()
        return await self.article_payload(session, article)

    async def list_admin_articles(
        self, session: AsyncSession, page: int, limit: int
    ) -> dict[str, Any]:
        total = int(await session.scalar(select(func.count(Article.id))) or 0)
        rows = (
            await session.scalars(
                select(Article)
                .order_by(Article.updated_at.desc(), Article.id.desc())
                .offset((page - 1) * limit)
                .limit(limit)
            )
        ).all()
        return {
            "items": [await self.article_payload(session, article) for article in rows],
            "page": page,
            "limit": limit,
            "total": total,
        }

    async def create_article(
        self,
        session: AsyncSession,
        payload: dict[str, Any],
        actor_id: UUID,
        request_id: str,
        key: str,
    ) -> dict[str, Any]:
        replay = await self._idempotency_replay(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="adminCreateArticle",
            key=key,
            payload=payload,
        )
        if replay:
            return replay
        content = self._validate_markdown(payload.get("content_markdown", ""))
        seo = payload.get("seo") or {}
        article = Article(
            slug=payload["slug"],
            title=payload["title"],
            excerpt=payload.get("excerpt", ""),
            content_markdown=content,
            seo_title=seo.get("title"),
            seo_description=seo.get("description"),
            author_staff_id=actor_id,
        )
        session.add(article)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise DomainError(
                "ARTICLE_SLUG_EXISTS", "Такой URL статьи уже существует.", 409
            ) from exc
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="article.create",
                target_type="article",
                target_id=article.id,
                after={"slug": article.slug, "status": article.status},
                request_id=request_id,
            )
        )
        result = await self.article_payload(session, article)
        await self._save_idempotency(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="adminCreateArticle",
            key=key,
            payload=payload,
            response=result,
            resource_id=article.id,
        )
        await session.commit()
        return result

    async def admin_article(self, session: AsyncSession, article_id: UUID) -> dict[str, Any]:
        article = await session.get(Article, article_id)
        if not article:
            raise not_found()
        return await self.article_payload(session, article)

    async def update_article(
        self,
        session: AsyncSession,
        article_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        article = await session.get(Article, article_id, with_for_update=True)
        if not article:
            raise not_found()
        if article.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Статья была изменена.", 409)
        before = {"slug": article.slug, "status": article.status, "version": article.version}
        if "content_markdown" in payload:
            article.content_markdown = self._validate_markdown(payload["content_markdown"])
        for field in ("slug", "title", "excerpt"):
            if field in payload:
                setattr(article, field, payload[field])
        if "seo" in payload:
            seo = payload["seo"] or {}
            article.seo_title = seo.get("title")
            article.seo_description = seo.get("description")
        if "status" in payload:
            article.status = payload["status"]
            article.published_at = utc_now() if article.status == "published" else None
        article.version += 1
        try:
            await session.flush()
        except IntegrityError as exc:
            raise DomainError(
                "ARTICLE_SLUG_EXISTS", "Такой URL статьи уже существует.", 409
            ) from exc
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="article.update",
                target_type="article",
                target_id=article.id,
                before=before,
                after={"slug": article.slug, "status": article.status, "version": article.version},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.article_payload(session, article)

    def _s3_client(self) -> Any:
        if (
            not self.settings.s3_originals_bucket
            or not self.settings.s3_public_media_bucket
            or not self.settings.s3_access_key_id
            or not self.settings.s3_secret_access_key
        ):
            raise DomainError("FEATURE_DISABLED", "Хранилище изображений не настроено.", 503)
        return boto3.client(
            "s3",
            endpoint_url=self.settings.s3_endpoint_url,
            region_name=self.settings.s3_region,
            aws_access_key_id=self.settings.s3_access_key_id.get_secret_value(),
            aws_secret_access_key=self.settings.s3_secret_access_key.get_secret_value(),
        )

    async def create_media_upload_intent(
        self,
        session: AsyncSession,
        payload: dict[str, Any],
        actor_id: UUID,
        request_id: str,
        key: str,
    ) -> dict[str, Any]:
        replay = await self._idempotency_replay(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="createMediaUploadIntent",
            key=key,
            payload=payload,
        )
        if replay:
            return replay
        client = self._s3_client()
        asset = MediaAsset(
            original_object_key="pending",
            mime_type=payload["content_type"],
            size_bytes=payload["size_bytes"],
            upload_expires_at=utc_now() + timedelta(minutes=10),
            created_by_staff_id=actor_id,
        )
        session.add(asset)
        await session.flush()
        suffix = re.sub(r"[^a-zA-Z0-9._-]", "_", payload["filename"])[-120:]
        asset.original_object_key = f"originals/{asset.id}/{suffix or 'upload'}"
        upload = client.generate_presigned_post(
            self.settings.s3_originals_bucket,
            asset.original_object_key,
            Fields={"Content-Type": asset.mime_type},
            Conditions=[
                {"Content-Type": asset.mime_type},
                ["content-length-range", 1, 20 * 1024 * 1024],
            ],
            ExpiresIn=600,
        )
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="media.upload_intent.create",
                target_type="media_asset",
                target_id=asset.id,
                after={"content_type": asset.mime_type, "size_bytes": asset.size_bytes},
                request_id=request_id,
            )
        )
        result = {
            "asset_id": str(asset.id),
            "status": asset.status,
            "upload_url": upload["url"],
            "fields": upload["fields"],
            "expires_at": asset.upload_expires_at.isoformat() if asset.upload_expires_at else None,
        }
        await self._save_idempotency(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="createMediaUploadIntent",
            key=key,
            payload=payload,
            response=result,
            resource_id=asset.id,
        )
        await session.commit()
        return result

    async def complete_media_upload(
        self, session: AsyncSession, asset_id: UUID, actor_id: UUID, request_id: str, key: str
    ) -> dict[str, Any]:
        idempotency_payload = {"asset_id": str(asset_id)}
        replay = await self._idempotency_replay(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="completeMediaUpload",
            key=key,
            payload=idempotency_payload,
        )
        if replay:
            return replay
        asset = await session.get(MediaAsset, asset_id, with_for_update=True)
        if not asset:
            raise not_found()
        if asset.status == "ready":
            return await self._media_payload(session, asset, "")
        if (
            asset.status != "pending_upload"
            or not asset.upload_expires_at
            or asset.upload_expires_at < utc_now()
        ):
            raise DomainError("MEDIA_UPLOAD_INVALID", "Загрузка недоступна для завершения.", 409)
        client = self._s3_client()
        try:
            head = client.head_object(
                Bucket=self.settings.s3_originals_bucket, Key=asset.original_object_key
            )
        except Exception as exc:  # noqa: BLE001 - provider response is intentionally not exposed
            raise DomainError("MEDIA_UPLOAD_MISSING", "Файл не найден в хранилище.", 409) from exc
        size = int(head.get("ContentLength", 0))
        content_type = str(head.get("ContentType", "")).split(";", 1)[0].lower()
        if size < 1 or size > 20 * 1024 * 1024 or content_type != asset.mime_type:
            raise DomainError("MEDIA_UPLOAD_INVALID", "Файл не прошёл проверку загрузки.", 422)
        asset.size_bytes = size
        asset.status = "processing"
        session.add(
            IntegrationJob(
                provider="media",
                kind="process",
                status="queued",
                progress={"asset_id": str(asset.id)},
            )
        )
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="media.upload.complete",
                target_type="media_asset",
                target_id=asset.id,
                after={"status": "processing"},
                request_id=request_id,
            )
        )
        result = await self._media_payload(session, asset, "")
        await self._save_idempotency(
            session,
            actor_scope=f"staff:{actor_id}",
            endpoint="completeMediaUpload",
            key=key,
            payload=idempotency_payload,
            response=result,
            resource_id=asset.id,
        )
        await session.commit()
        return result

    async def admin_media(self, session: AsyncSession, asset_id: UUID) -> dict[str, Any]:
        asset = await session.get(MediaAsset, asset_id)
        if not asset:
            raise not_found()
        return await self._media_payload(session, asset, "")

    async def attach_article_media(
        self,
        session: AsyncSession,
        article_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        article = await session.get(Article, article_id, with_for_update=True)
        asset = await session.get(MediaAsset, UUID(str(payload["asset_id"])))
        if not article or not asset:
            raise not_found()
        if article.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Статья была изменена.", 409)
        if asset.status != "ready":
            raise DomainError("MEDIA_NOT_READY", "Изображение ещё не опубликовано.", 409)
        if payload["role"] == "article_cover":
            await session.execute(
                update(ArticleMedia)
                .where(ArticleMedia.article_id == article.id, ArticleMedia.role == "article_cover")
                .values(role="article_inline")
            )
        link = await session.scalar(
            select(ArticleMedia).where(
                ArticleMedia.article_id == article.id, ArticleMedia.asset_id == asset.id
            )
        )
        if not link:
            link = ArticleMedia(article_id=article.id, asset_id=asset.id)
            session.add(link)
        link.role = payload["role"]
        link.alt = payload.get("alt", "")
        link.sort_order = payload.get("sort_order", 0)
        article.version += 1
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="article.media.attach",
                target_type="article",
                target_id=article.id,
                after={"asset_id": str(asset.id), "role": link.role},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.article_payload(session, article)

    async def update_article_media(
        self,
        session: AsyncSession,
        article_id: UUID,
        link_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        article = await session.get(Article, article_id, with_for_update=True)
        link = await session.get(ArticleMedia, link_id)
        if not article or not link or link.article_id != article.id:
            raise not_found()
        if article.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Статья была изменена.", 409)
        if payload.get("role") == "article_cover":
            await session.execute(
                update(ArticleMedia)
                .where(ArticleMedia.article_id == article.id)
                .values(role="article_inline")
            )
        for field in ("alt", "sort_order", "role"):
            if field in payload and payload[field] is not None:
                setattr(link, field, payload[field])
        article.version += 1
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="article.media.update",
                target_type="article",
                target_id=article.id,
                after={"link_id": str(link.id)},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.article_payload(session, article)

    async def delete_article_media(
        self,
        session: AsyncSession,
        article_id: UUID,
        link_id: UUID,
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        article = await session.get(Article, article_id, with_for_update=True)
        link = await session.get(ArticleMedia, link_id)
        if not article or not link or link.article_id != article.id:
            raise not_found()
        if article.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Статья была изменена.", 409)
        asset_id = link.asset_id
        await session.delete(link)
        article.version += 1
        await session.flush()
        await self._retire_unused_asset(session, asset_id)
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="article.media.detach",
                target_type="article",
                target_id=article.id,
                after={"link_id": str(link_id)},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.article_payload(session, article)

    async def product_media_payload(
        self, session: AsyncSession, product: Product
    ) -> dict[str, Any]:
        rows = (
            await session.execute(
                select(ProductMedia, MediaAsset)
                .join(MediaAsset, MediaAsset.id == ProductMedia.asset_id)
                .where(ProductMedia.product_id == product.id)
                .order_by(ProductMedia.sort_order, ProductMedia.created_at)
            )
        ).all()
        return {
            "product_id": str(product.id),
            "version": product.version,
            "items": [
                {
                    "link_id": str(link.id),
                    "sort_order": link.sort_order,
                    "is_primary": link.is_primary,
                    **(await self._media_payload(session, asset, link.alt)),
                }
                for link, asset in rows
            ],
        }

    async def attach_product_media(
        self,
        session: AsyncSession,
        product_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        product = await session.get(Product, product_id, with_for_update=True)
        asset = await session.get(MediaAsset, UUID(str(payload["asset_id"])))
        if not product or not asset:
            raise not_found()
        if product.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Товар был изменён.", 409)
        if asset.status != "ready":
            raise DomainError("MEDIA_NOT_READY", "Изображение ещё не опубликовано.", 409)
        link = await session.scalar(
            select(ProductMedia).where(
                ProductMedia.product_id == product.id, ProductMedia.asset_id == asset.id
            )
        )
        if not link:
            count = int(
                await session.scalar(
                    select(func.count(ProductMedia.id)).where(ProductMedia.product_id == product.id)
                )
                or 0
            )
            if count >= 12:
                raise DomainError("MEDIA_LIMIT_EXCEEDED", "У товара может быть до 12 фото.", 409)
            link = ProductMedia(product_id=product.id, asset_id=asset.id)
            session.add(link)
        if payload.get("is_primary"):
            await session.execute(
                update(ProductMedia)
                .where(ProductMedia.product_id == product.id)
                .values(is_primary=False)
            )
        link.alt = payload.get("alt", "")
        link.sort_order = payload.get("sort_order", 0)
        link.is_primary = payload.get("is_primary", False)
        product.version += 1
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="product.media.attach",
                target_type="product",
                target_id=product.id,
                after={"asset_id": str(asset.id), "is_primary": link.is_primary},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.product_media_payload(session, product)

    async def _retire_unused_asset(self, session: AsyncSession, asset_id: UUID) -> None:
        product_count = int(
            await session.scalar(
                select(func.count(ProductMedia.id)).where(ProductMedia.asset_id == asset_id)
            )
            or 0
        )
        article_count = int(
            await session.scalar(
                select(func.count(ArticleMedia.id)).where(ArticleMedia.asset_id == asset_id)
            )
            or 0
        )
        if product_count or article_count:
            return
        asset = await session.get(MediaAsset, asset_id, with_for_update=True)
        if asset and asset.status == "ready":
            asset.status = "deleted"
            asset.deleted_at = utc_now()
            asset.purge_after = utc_now() + timedelta(days=30)

    async def update_product_media(
        self,
        session: AsyncSession,
        product_id: UUID,
        link_id: UUID,
        payload: dict[str, Any],
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        product = await session.get(Product, product_id, with_for_update=True)
        link = await session.get(ProductMedia, link_id)
        if not product or not link or link.product_id != product.id:
            raise not_found()
        if product.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Товар был изменён.", 409)
        if payload.get("is_primary"):
            await session.execute(
                update(ProductMedia)
                .where(ProductMedia.product_id == product.id)
                .values(is_primary=False)
            )
        for field in ("alt", "sort_order", "is_primary"):
            if field in payload and payload[field] is not None:
                setattr(link, field, payload[field])
        product.version += 1
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="product.media.update",
                target_type="product",
                target_id=product.id,
                after={"link_id": str(link.id)},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.product_media_payload(session, product)

    async def delete_product_media(
        self,
        session: AsyncSession,
        product_id: UUID,
        link_id: UUID,
        expected_version: int,
        actor_id: UUID,
        request_id: str,
    ) -> dict[str, Any]:
        product = await session.get(Product, product_id, with_for_update=True)
        link = await session.get(ProductMedia, link_id)
        if not product or not link or link.product_id != product.id:
            raise not_found()
        if product.version != expected_version:
            raise DomainError("VERSION_CONFLICT", "Товар был изменён.", 409)
        asset_id = link.asset_id
        await session.delete(link)
        product.version += 1
        await session.flush()
        await self._retire_unused_asset(session, asset_id)
        session.add(
            AuditLog(
                actor_type="staff",
                actor_id=actor_id,
                action="product.media.detach",
                target_type="product",
                target_id=product.id,
                after={"link_id": str(link_id)},
                request_id=request_id,
            )
        )
        await session.commit()
        return await self.product_media_payload(session, product)
