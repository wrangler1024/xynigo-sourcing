"""Export tests never download real buyer product images."""
import pytest


@pytest.fixture(autouse=True)
def no_live_after_sale_images(monkeypatch):
    from xynigo_auth import after_sale_export_images
    def unavailable(_url, **_kwargs):
        raise OSError('synthetic image unavailable')
    monkeypatch.setattr(after_sale_export_images, 'fetch_procurement_image', unavailable)
