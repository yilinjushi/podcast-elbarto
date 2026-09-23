"""HTTPS adapter for the application's authenticated Worker R2 binding.

Environment: PODCAST_SERVICE_ORIGIN (HTTPS origin), PODCAST_STORE_TOKEN (secret).
This is a scoped application API, not a Cloudflare management-token fallback.
All errors are fixed codes: response bodies, URLs and credentials are suppressed.
"""
import hashlib
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DATA_KEYS = frozenset({'data/feed.xml', 'data/feed-manifest.json',
                       'data/publish-intent.json', 'data/private-state.zip'})


class HttpStoreError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even same-origin redirects are denied; Authorization must never be
        # forwarded to a URL supplied by an HTTP response.
        return None


def valid_key(key):
    return isinstance(key, str) and (key in DATA_KEYS or bool(
        re.fullmatch(r'(audio|staging)/[a-f0-9]{64}\.mp3', key)))


def valid_prefix(prefix):
    return isinstance(prefix, str) and prefix in ('', 'data/', 'audio/', 'staging/')


class HttpStore:
    def __init__(self, origin=None, token=None):
        origin = os.getenv('PODCAST_SERVICE_ORIGIN', '') if origin is None else origin
        token = os.getenv('PODCAST_STORE_TOKEN', '') if token is None else token
        try:
            if not isinstance(origin, str) or any(ord(c) <= 32 for c in origin):
                raise ValueError()
            parsed = urlsplit(origin)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.path not in ('', '/')
                    or parsed.query or parsed.fragment):
                raise ValueError()
            parsed.port  # reject invalid ports before building any request
            if not isinstance(token, str) or not token or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
                raise ValueError()
        except Exception:
            raise HttpStoreError('http_store_invalid_configuration') from None
        self._origin = origin.rstrip('/')
        self._token = token
        self._opener = build_opener(NoRedirect())

    def _request(self, method, path, data=None, content_type=None, missing_ok=False):
        try:
            headers = {'Authorization': 'Bearer ' + self._token,
                       'User-Agent': 'podcast-elbarto/1.0 (+https://github.com/yilinjushi/podcast-elbarto)'}
            if data is not None:
                headers['Content-Length'] = str(len(data))
                headers['Content-Type'] = content_type
                headers['X-Content-SHA256'] = hashlib.sha256(data).hexdigest()
            request = Request(self._origin + path, data=data, headers=headers, method=method)
            with self._opener.open(request, timeout=45) as response:
                status = response.status
                expected = {'GET': {200}, 'PUT': {200, 201, 204}, 'DELETE': {204}}[method]
                if status not in expected:
                    raise HttpStoreError('http_store_response_failed')
                return response.read() if method == 'GET' else b''
        except HTTPError as error:
            error.close()
            if missing_ok and error.code == 404:
                return None
            raise HttpStoreError('http_store_request_failed') from None
        except Exception:
            raise HttpStoreError('http_store_request_failed') from None

    def get(self, key):
        if not valid_key(key):
            raise HttpStoreError('http_store_invalid_key')
        return self._request('GET', '/_store/objects/' + key, missing_ok=True)

    def put(self, key, data, content_type='application/octet-stream'):
        if not valid_key(key):
            raise HttpStoreError('http_store_invalid_key')
        if not isinstance(data, bytes) or len(data) > 100_000_000 or not isinstance(content_type, str) or not re.fullmatch(
                r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:;[ A-Za-z0-9=_.+-]+)?', content_type):
            raise HttpStoreError('http_store_invalid_payload')
        self._request('PUT', '/_store/objects/' + key, data, content_type)

    def delete(self, key):
        if not valid_key(key):
            raise HttpStoreError('http_store_invalid_key')
        self._request('DELETE', '/_store/objects/' + key)

    def list(self, prefix=''):
        if not valid_prefix(prefix):
            raise HttpStoreError('http_store_invalid_prefix')
        result = {}
        seen = set()
        cursor = None
        for _ in range(10000):
            query = {'prefix': prefix}
            if cursor is not None:
                query['cursor'] = cursor
            raw = self._request('GET', '/_store/list?' + urlencode(query))
            try:
                page = json.loads(raw)
                if not isinstance(page, dict) or not isinstance(page.get('objects'), list) or 'cursor' not in page:
                    raise ValueError()
                for item in page['objects']:
                    key, size = item['key'], item['size']
                    if (not valid_key(key) or not key.startswith(prefix) or key in result
                            or type(size) is not int or size < 0):
                        raise ValueError()
                    result[key] = size
                cursor = page['cursor']
                if cursor is None:
                    return result
                if not isinstance(cursor, str) or not cursor or len(cursor) > 8192 or cursor in seen:
                    raise ValueError()
                seen.add(cursor)
            except Exception:
                raise HttpStoreError('http_store_invalid_listing') from None
        raise HttpStoreError('http_store_pagination_limit')
