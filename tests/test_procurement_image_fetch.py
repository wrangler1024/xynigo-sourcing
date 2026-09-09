from io import BytesIO
from urllib.error import HTTPError, URLError
import pytest
from purchase_tool import procurement_image_fetch as images

URL = 'https://img.ltwebstatic.com/test/source.png'
PNG = b'\x89PNG\r\n\x1a\nsynthetic-image'


class Response(BytesIO):
    def __init__(self, body=PNG, headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def getcode(self):
        return 200


def install_opener(monkeypatch, responses):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            assert timeout == images.IMAGE_TIMEOUT_SECONDS
            assert not request.has_header('Authorization')
            assert not request.has_header('Cookie')
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr(images, 'build_opener', lambda *args: Opener())
    return calls


@pytest.mark.parametrize('url', [
    'http://img.ltwebstatic.com/a.png', 'https://127.0.0.1/a.png',
    'https://img.ltwebstatic.com.evil.test/a', 'file:///etc/passwd',
    'https://user:password@img.ltwebstatic.com/a',
    'https://img.ltwebstatic.com:444/a', 'https://img.ltwebstatic.com/a\nb',
])
def test_untrusted_source_is_rejected_before_network(monkeypatch, url):
    calls = install_opener(monkeypatch, [])
    with pytest.raises(images.ProcurementImageError):
        images.fetch_procurement_image(url)
    assert calls == []


def test_download_verifies_image_body_and_returns_bytes(monkeypatch):
    calls = install_opener(monkeypatch, [Response()])
    assert images.fetch_procurement_image(URL) == PNG
    assert [request.full_url for request in calls] == [URL]


def test_trusted_redirect_is_followed_after_validation(monkeypatch):
    calls = install_opener(monkeypatch, [
        HTTPError(URL, 302, 'redirect', {'Location': '/test/new.png'}, BytesIO()), Response()])
    assert images.fetch_procurement_image(URL) == PNG
    assert calls[-1].full_url == 'https://img.ltwebstatic.com/test/new.png'


def test_untrusted_redirect_is_never_requested(monkeypatch):
    calls = install_opener(monkeypatch, [
        HTTPError(URL, 302, 'redirect', {'Location': 'http://127.0.0.1/private'}, BytesIO())])
    with pytest.raises(images.ProcurementImageError, match='重定向到非受支持地址'):
        images.fetch_procurement_image(URL)
    assert len(calls) == 1


@pytest.mark.parametrize('case', ['html', 'empty', 'large-header', 'large-body', 'network', '404'])
def test_invalid_or_unavailable_images_raise_safe_row_errors(monkeypatch, case):
    response = {
        'html': lambda: Response(b'<html>not an image</html>'),
        'empty': lambda: Response(b''),
        'large-header': lambda: Response(PNG, {'Content-Length': str(images.MAX_IMAGE_BYTES + 1)}),
        'large-body': lambda: Response(b'x' * (images.MAX_IMAGE_BYTES + 1)),
        'network': lambda: URLError('synthetic network failure'),
        '404': lambda: HTTPError(URL, 404, 'missing', {}, BytesIO()),
    }[case]()
    install_opener(monkeypatch, [response])
    with pytest.raises(images.ProcurementImageError):
        images.fetch_procurement_image(URL)


def test_redirect_limit(monkeypatch):
    calls = install_opener(monkeypatch, [
        HTTPError(URL, 302, 'redirect', {'Location': URL}, BytesIO())
        for _ in range(images.MAX_IMAGE_REDIRECTS + 1)])
    with pytest.raises(images.ProcurementImageError, match='次数过多'):
        images.fetch_procurement_image(URL)
    assert len(calls) == images.MAX_IMAGE_REDIRECTS + 1
