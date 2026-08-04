from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LeadCreate(StrictModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=7, max_length=30)
    email: EmailStr | None = None
    company: str | None = Field(default=None, max_length=160)
    message: str | None = Field(default=None, max_length=1500)
    product_slug: str | None = None
    source: Literal["contact", "product", "weekend", "delivery"] = "contact"
    consent: Literal[True]
    website: str = Field(default="", max_length=0)


class StockSubscriptionCreate(StrictModel):
    product_slug: str
    name: str = Field(min_length=2, max_length=80)
    contact: str = Field(min_length=7, max_length=120)
    consent: Literal[True]
    website: str = Field(default="", max_length=0)


class OrderItemInput(StrictModel):
    product_id: UUID
    quantity: int = Field(ge=1, le=999)


class CustomerContactInput(StrictModel):
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=7, max_length=30)
    email: EmailStr | None = None


class LegalEntityInput(StrictModel):
    company_name: str = Field(min_length=2, max_length=180)
    inn: str = Field(min_length=10, max_length=12)
    kpp: str | None = Field(default=None, max_length=9)
    documents_email: EmailStr


class DeliveryInput(StrictModel):
    destination: Literal["voronezh", "intercity"]
    city: str = Field(min_length=2, max_length=120)
    address_line: str = Field(min_length=5, max_length=300)
    postal_code: str | None = Field(default=None, max_length=20)
    comment: str | None = Field(default=None, max_length=500)


class QuoteCreate(StrictModel):
    customer: CustomerContactInput
    legal_entity: LegalEntityInput
    delivery: DeliveryInput | None = None
    items: list[OrderItemInput] = Field(min_length=1)
    comment: str | None = Field(default=None, max_length=1500)
    consent: Literal[True]
    website: str = Field(default="", max_length=0)


class CheckoutCreate(StrictModel):
    customer: CustomerContactInput
    delivery: DeliveryInput
    items: list[OrderItemInput] = Field(min_length=1)
    payment_method: Literal["sbp", "card"] = "sbp"
    comment: str | None = Field(default=None, max_length=1500)
    consent: Literal[True]
    website: str = Field(default="", max_length=0)


class PaymentAttemptCreate(StrictModel):
    payment_method: Literal["sbp", "card"]


class CustomerRegister(StrictModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    phone: str | None = Field(default=None, min_length=7, max_length=30)
    customer_type: Literal["individual", "legal_entity"] = "individual"
    company_name: str | None = Field(default=None, max_length=180)
    inn: str | None = Field(default=None, min_length=10, max_length=12)
    documents_email: EmailStr | None = None
    consent: Literal[True]
    website: str = Field(default="", max_length=0)


class Login(StrictModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class CustomerUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    phone: str | None = Field(default=None, min_length=7, max_length=30)
    customer_type: Literal["individual", "legal_entity"] | None = None
    company_name: str | None = Field(default=None, max_length=180)
    inn: str | None = Field(default=None, min_length=10, max_length=12)
    documents_email: EmailStr | None = None
    version: int | None = Field(default=None, ge=0)


class CartItemUpsert(StrictModel):
    product_id: UUID
    quantity: int = Field(ge=1, le=999)


class CartItemUpdate(StrictModel):
    quantity: int = Field(ge=1, le=999)


class CartMerge(StrictModel):
    items: list[OrderItemInput]


class FavoriteCreate(StrictModel):
    product_id: UUID


class AdminOrderUpdate(StrictModel):
    status: Literal["assembling", "ready_for_dispatch", "shipped", "completed", "canceled"]
    reason: str | None = Field(default=None, max_length=120)
    comment: str | None = Field(default=None, max_length=500)
    version: int = Field(ge=0)


class AdminLeadUpdate(StrictModel):
    status: Literal["new", "in_progress", "resolved", "spam"]
    version: int = Field(ge=0)


class SeoInput(StrictModel):
    title: str | None = None
    description: str | None = None


class ProductUpdate(StrictModel):
    name: str | None = None
    subtitle: str | None = None
    description: str | None = None
    is_active: bool | None = None
    seo: SeoInput | None = None


class ArticleCreate(StrictModel):
    slug: str = Field(min_length=2, max_length=180, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=2, max_length=240)
    excerpt: str = Field(default="", max_length=600)
    content_markdown: str = Field(default="", max_length=100_000)
    seo: SeoInput | None = None


class ArticleUpdate(StrictModel):
    slug: str | None = Field(
        default=None, min_length=2, max_length=180, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
    title: str | None = Field(default=None, min_length=2, max_length=240)
    excerpt: str | None = Field(default=None, max_length=600)
    content_markdown: str | None = Field(default=None, max_length=100_000)
    status: Literal["draft", "published", "archived"] | None = None
    seo: SeoInput | None = None


class MediaUploadIntentCreate(StrictModel):
    filename: str = Field(min_length=1, max_length=180)
    content_type: Literal["image/jpeg", "image/png", "image/webp", "image/avif"]
    size_bytes: int = Field(gt=0, le=20 * 1024 * 1024)


class MediaAttach(StrictModel):
    asset_id: UUID
    alt: str = Field(default="", max_length=300)
    sort_order: int = Field(default=0, ge=0, le=1000)
    is_primary: bool = False


class ArticleMediaAttach(StrictModel):
    asset_id: UUID
    role: Literal["article_cover", "article_inline"] = "article_inline"
    alt: str = Field(default="", max_length=300)
    sort_order: int = Field(default=0, ge=0, le=1000)


class MediaLinkUpdate(StrictModel):
    alt: str | None = Field(default=None, max_length=300)
    sort_order: int | None = Field(default=None, ge=0, le=1000)
    is_primary: bool | None = None
    role: Literal["article_cover", "article_inline"] | None = None


class PasswordResetRequest(StrictModel):
    email: EmailStr


class PasswordResetConfirm(StrictModel):
    token: str
    password: str = Field(min_length=12, max_length=128)
