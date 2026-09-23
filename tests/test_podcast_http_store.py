import io
import hashlib
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from podcast_http_store import HttpStore, HttpStoreError, NoRedirect


class Response(io.BytesIO):
    def __init__(self, data=b'', status=200):
        super().__init__(data)
        self.status = status


class Opener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class HttpStoreTests(unittest.TestCase):
    def store(self, replies):
        opener = Opener(replies)
        with patch('podcast_http_store.build_opener', return_value=opener) as build:
            store = HttpStore('https://worker.example/', 'private-test-token')
            self.assertIsInstance(build.call_args.args[0], NoRedirect)
        return store, opener

    def test_crud_headers_timeout_and_missing(self):
        store, opener = self.store([Response(b'data'), Response(status=204), Response(status=204),
                                   HTTPError('https://private', 404, 'secret', {}, None)])
        key = 'audio/' + 'a' * 64 + '.mp3'
        self.assertEqual(store.get(key), b'data')
        store.put(key, b'abcd', 'audio/mpeg')
        store.delete(key)
        self.assertIsNone(store.get(key))
        self.assertEqual([r.method for r, _ in opener.requests], ['GET', 'PUT', 'DELETE', 'GET'])
        self.assertTrue(all(t == 45 for _, t in opener.requests))
        request = opener.requests[1][0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer private-test-token')
        self.assertEqual(request.get_header('Content-length'), '4')
        self.assertEqual(request.get_header('Content-type'), 'audio/mpeg')
        self.assertEqual(request.get_header('X-content-sha256'), hashlib.sha256(b'abcd').hexdigest())
        self.assertEqual(request.full_url, 'https://worker.example/_store/objects/' + key)

    def test_pagination_cursor_is_encoded(self):
        first = 'audio/' + 'a' * 64 + '.mp3'
        second = 'audio/' + 'b' * 64 + '.mp3'
        cursor = 'next+/=&?token'
        store, opener = self.store([
            Response(json.dumps({'objects': [{'key': first, 'size': 10}], 'cursor': cursor}).encode()),
            Response(json.dumps({'objects': [{'key': second, 'size': 20}], 'cursor': None}).encode()),
        ])
        self.assertEqual(store.list('audio/'), {first: 10, second: 20})
        query = parse_qs(urlsplit(opener.requests[1][0].full_url).query)
        self.assertEqual(query, {'prefix': ['audio/'], 'cursor': [cursor]})

    def test_errors_never_include_server_text_or_token(self):
        for error in [HTTPError('https://secret-host', 403, 'private-test-token', {}, None),
                      URLError('https://secret-host/private-test-token'), TimeoutError('private-test-token')]:
            store, _ = self.store([error])
            with self.assertRaises(HttpStoreError) as caught:
                store.get('data/private-state.zip')
            self.assertEqual(str(caught.exception), 'http_store_request_failed')
            self.assertTrue(caught.exception.__suppress_context__)

    def test_redirects_are_refused(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://other'))
        for status in (301, 302, 303, 307, 308):
            store, opener = self.store([HTTPError('https://worker.example', status, 'redirect',
                                                  {'Location': 'https://attacker.example'}, None)])
            with self.assertRaisesRegex(HttpStoreError, '^http_store_request_failed$'):
                store.get('data/feed.xml')
            self.assertEqual(len(opener.requests), 1)

    def test_configuration_and_object_allowlist(self):
        for origin in ['http://worker.example', 'https://user:pass@worker.example',
                       'https://worker.example/path', 'https://worker.example?secret',
                       'https://worker.example#secret', 'https://worker.example\n']:
            with self.assertRaisesRegex(HttpStoreError, '^http_store_invalid_configuration$'):
                HttpStore(origin, 'token')
        store, opener = self.store([])
        for key in ['../secret', 'data/auth.json', 'audio/test.mp3', '/data/feed.xml',
                    'data/feed.xml?token', 'data\\feed.xml', 'audio/' + 'A' * 64 + '.mp3']:
            for operation in (store.get, store.delete, lambda k: store.put(k, b'x')):
                with self.assertRaisesRegex(HttpStoreError, '^http_store_invalid_key$'):
                    operation(key)
        self.assertEqual(opener.requests, [])

    def test_invalid_listing_rejected(self):
        for page in [{'objects': [{'key': 'data/secret', 'size': 1}], 'cursor': None},
                     {'objects': [{'key': 'data/feed.xml', 'size': -1}], 'cursor': None},
                     {'objects': [{'key': 'data/feed.xml', 'size': True}], 'cursor': None},
                     {'objects': [], 'cursor': 4}, {'objects': []}]:
            store, _ = self.store([Response(json.dumps(page).encode())])
            with self.assertRaisesRegex(HttpStoreError, '^http_store_invalid_listing$'):
                store.list()

    def test_repeated_cursor_stops(self):
        page = json.dumps({'objects': [], 'cursor': 'same'}).encode()
        store, opener = self.store([Response(page), Response(page)])
        with self.assertRaisesRegex(HttpStoreError, '^http_store_invalid_listing$'):
            store.list()
        self.assertEqual(len(opener.requests), 2)

    def test_header_injection_and_bad_prefix_stop_before_network(self):
        store, opener = self.store([])
        with self.assertRaisesRegex(HttpStoreError, '^http_store_invalid_payload$'):
            store.put('data/feed.xml', b'data', 'text/xml\r\nAuthorization: secret')
        with self.assertRaisesRegex(HttpStoreError, '^http_store_invalid_prefix$'):
            store.list('../')
        self.assertEqual(opener.requests, [])


if __name__ == '__main__':
    unittest.main()
