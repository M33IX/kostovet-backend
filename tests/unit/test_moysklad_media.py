from __future__ import annotations

from io import BytesIO

import pytest
import respx
from PIL import Image
from pydantic import SecretStr

from kosto_vet.bootstrap.settings import Settings
from kosto_vet.infrastructure.integrations import MoySkladAdapter
from kosto_vet.services.moysklad_media import _image_id, _variants


def test_moysklad_image_id_accepts_only_safe_provider_ids() -> None:
    assert _image_id({"meta": {"href": "https://api.moysklad.ru/images/image-1"}}) == "image-1"
    with pytest.raises(ValueError, match="safe ID"):
        _image_id({"id": "../outside"})


def test_moysklad_image_generates_webp_variants() -> None:
    original = BytesIO()
    Image.new("RGB", (800, 600), "blue").save(original, format="PNG")
    variants = _variants(original.getvalue())
    assert set(variants) == {"thumb", "card", "detail"}
    with Image.open(BytesIO(variants["thumb"])) as thumb:
        assert thumb.format == "WEBP"
        assert thumb.size == (320, 240)
    with Image.open(BytesIO(variants["detail"])) as detail:
        assert detail.size == (800, 600)


@respx.mock
async def test_moysklad_image_download_keeps_token_off_cdn() -> None:
    url = "https://storage.example.test/image.jpg"
    request = respx.get(url).respond(200, content=b"image-bytes")
    settings = Settings(
        _env_file=None,
        moysklad_mode="sandbox",
        moysklad_access_token=SecretStr("test-token"),
        moysklad_warehouse_id="warehouse",
        moysklad_price_type_id="price",
    )
    result = await MoySkladAdapter(settings).download_image(url)
    assert result == b"image-bytes"
    assert request.calls.last.request.headers.get("Authorization") is None


@respx.mock
async def test_moysklad_image_download_follows_provider_redirect_without_leaking_token() -> None:
    provider_url = "https://api.moysklad.ru/api/remap/1.2/download/image-1"
    cdn_url = "https://storage.example.test/image-1"
    provider = respx.get(provider_url).respond(302, headers={"Location": cdn_url})
    cdn = respx.get(cdn_url).respond(200, content=b"image-bytes")
    settings = Settings(
        _env_file=None,
        moysklad_mode="sandbox",
        moysklad_access_token=SecretStr("test-token"),
        moysklad_warehouse_id="warehouse",
        moysklad_price_type_id="price",
    )
    result = await MoySkladAdapter(settings).download_image(provider_url)
    assert result == b"image-bytes"
    assert provider.calls.last.request.headers["Authorization"] == "Bearer test-token"
    assert cdn.calls.last.request.headers.get("Authorization") is None


async def test_moysklad_image_rejects_insecure_download_url() -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(ValueError, match="HTTPS"):
        await MoySkladAdapter(settings).download_image("http://storage.example.test/image.jpg")
