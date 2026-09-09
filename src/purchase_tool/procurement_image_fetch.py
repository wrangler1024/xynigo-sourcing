"""Bounded, credential-free downloads of source product images from SHEIN CDN."""
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import certifi


MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TIMEOUT_SECONDS = 10
MAX_IMAGE_REDIRECTS = 3


class ProcurementImageError(ValueError):
    pass


def trusted_procurement_image_url(value):
    if not isinstance(value, str):
        return ''
    source = value.strip()
    try:
        parsed = urlparse(source)
        host = (parsed.hostname or '').lower()
        valid = (parsed.scheme == 'https'
                 and (host == 'ltwebstatic.com' or host.endswith('.ltwebstatic.com'))
                 and parsed.port in (None, 443)
                 and parsed.username is None and parsed.password is None
                 and not any(ord(char) < 32 or ord(char) == 127 for char in source))
    except ValueError:
        return ''
    return source if valid else ''


def image_content_type(data):
    if not isinstance(data, bytes):
        raise ProcurementImageError('订单商品图片格式不受支持')
    if data.startswith(b'\xff\xd8'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    raise ProcurementImageError('订单商品图片格式不受支持')


class _NoAutomaticRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Check every destination before making the next request.
        return None


def fetch_procurement_image(url):
    current = trusted_procurement_image_url(url)
    if not current:
        raise ProcurementImageError('订单图片网址不是受支持的 SHEIN 图片地址')
    opener = build_opener(
        _NoAutomaticRedirect(),
        HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    for attempt in range(MAX_IMAGE_REDIRECTS + 1):
        request = Request(current, headers={
            'Accept': 'image/jpeg,image/png,image/gif,image/webp',
            'User-Agent': 'Xynigo-Sourcing-Image-Import',
        })
        try:
            with opener.open(request, timeout=IMAGE_TIMEOUT_SECONDS) as response:
                if response.getcode() != 200:
                    raise ProcurementImageError('订单图片网址返回异常状态')
                length = response.headers.get('Content-Length', '')
                if str(length).isdigit() and int(length) > MAX_IMAGE_BYTES:
                    raise ProcurementImageError('订单图片超过 5MB，已停止读取')
                data = response.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    raise ProcurementImageError('订单图片超过 5MB，已停止读取')
                image_content_type(data)
                return data
        except HTTPError as exc:
            status = exc.code
            location = exc.headers.get('Location', '')
            exc.close()
            if status in {301, 302, 303, 307, 308} and location:
                current = trusted_procurement_image_url(urljoin(current, location))
                if not current:
                    raise ProcurementImageError('订单图片重定向到非受支持地址，已停止读取') from None
                if attempt < MAX_IMAGE_REDIRECTS:
                    continue
                raise ProcurementImageError('订单图片重定向次数过多') from None
            raise ProcurementImageError('读取订单图片失败（HTTP %s）' % status) from None
        except (URLError, TimeoutError, OSError):
            raise ProcurementImageError('读取订单图片网址失败，请稍后续传') from None
    raise ProcurementImageError('读取订单图片失败')
