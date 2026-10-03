from __future__ import annotations

import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from caseflow.provider import Config, Provider, ProviderError


def response_payload():
    return {
        'choices': [{'message': {
            'role': 'assistant', 'content': None,
            'reasoning_content': 'private reasoning must not be persisted',
            'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {
                'name': 'ask_customer', 'arguments': '{"question":"订单号是什么？"}'}}],
        }}],
        'usage': {'total_tokens': 17},
    }


class ProviderHTTPTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        self.payload = response_payload()
        self.raw_body = None
        self.headers = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                owner.requests.append({
                    'path': self.path, 'authorization': self.headers.get('Authorization'),
                    'body': json.loads(body),
                })
                raw = owner.raw_body
                if raw is None:
                    raw = json.dumps(owner.payload, ensure_ascii=False).encode('utf-8')
                self.send_response(owner.status)
                for name, value in owner.headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                owner.requests.append({'path': self.path, 'method': 'GET'})
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.base_url = f'http://127.0.0.1:{self.server.server_port}'
        self.provider = Provider(Config('unit-test-secret', self.base_url, 'unit-test-model', 2))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def complete(self):
        return self.provider.complete(
            [{'role': 'user', 'content': '请处理我的售后请求'}],
            [{'type': 'function', 'function': {'name': 'ask_customer',
               'parameters': {'type': 'object', 'properties': {
                   'question': {'type': 'string'}}, 'required': ['question']}}}],
        )

    def test_sends_tool_protocol_and_strips_private_reasoning(self):
        result = self.complete()
        self.assertEqual(result['tokens'], 17)
        self.assertNotIn('reasoning_content', result['message'])
        self.assertNotIn('private reasoning', json.dumps(result))
        self.assertEqual(result['message']['tool_calls'][0]['function']['name'], 'ask_customer')
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request['path'], '/chat/completions')
        self.assertEqual(request['authorization'], 'Bearer unit-test-secret')
        self.assertEqual(request['body']['model'], 'unit-test-model')
        self.assertEqual(request['body']['tool_choice'], 'required')
        self.assertFalse(request['body']['parallel_tool_calls'])
        self.assertEqual(request['body']['tools'][0]['function']['name'], 'ask_customer')

    def test_missing_usage_remains_unknown(self):
        del self.payload['usage']
        self.assertIsNone(self.complete()['tokens'])

    def test_invalid_usage_and_duplicate_call_ids_are_rejected(self):
        malformed = []
        for usage in ({'total_tokens': -1}, {'total_tokens': True},
                      {'total_tokens': '17'}, ['unexpected-usage-shape']):
            value = response_payload()
            value['usage'] = usage
            malformed.append(value)
        value = response_payload()
        calls = value['choices'][0]['message']['tool_calls']
        calls.append(copy.deepcopy(calls[0]))
        malformed.append(value)
        for payload in malformed:
            with self.subTest(payload=payload):
                self.payload = payload
                with self.assertRaisesRegex(ProviderError, '^provider_invalid_response$'):
                    self.complete()
        self.assertEqual(len(self.requests), len(malformed))

    def test_http_failure_does_not_leak_response_body_or_retry(self):
        self.status = 401
        self.raw_body = b'{"error":"unit-test-secret and customer-private-data"}'
        with self.assertRaises(ProviderError) as caught:
            self.complete()
        self.assertEqual(str(caught.exception), 'provider_http_401')
        self.assertEqual(len(self.requests), 1)

    def test_redirect_is_not_followed_with_credentials(self):
        for status in (302, 307):
            with self.subTest(status=status):
                self.status = status
                self.headers = {'Location': self.base_url + '/credential-sink'}
                with self.assertRaisesRegex(ProviderError, f'^provider_http_{status}$'):
                    self.complete()
        self.assertEqual([r['path'] for r in self.requests],
                         ['/chat/completions', '/chat/completions'])

    def test_invalid_and_oversized_json_use_safe_errors(self):
        self.raw_body = b'not-json unit-test-secret'
        with self.assertRaisesRegex(ProviderError, '^provider_invalid_json$'):
            self.complete()
        self.raw_body = b' ' * 1_048_577
        with self.assertRaisesRegex(ProviderError, '^model_response_too_large$'):
            self.complete()

    def test_credentials_and_insecure_remote_endpoints_rejected_before_request(self):
        for url in ('http://example.com', 'https://user:password@example.com',
                    'https://example.com?api_key=secret', 'file:///tmp/key',
                    'http://127.0.0.1:invalid'):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ProviderError, '^invalid_base_url$'):
                    Provider(Config('unit-test-secret', url))
        with self.assertRaisesRegex(ProviderError, '^missing_api_key$'):
            Provider(Config(''))
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
