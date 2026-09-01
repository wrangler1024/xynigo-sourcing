# -*- coding: utf-8 -*-
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from purchase_tool.workspace_rpc import WorkspaceRpcClient


RPC_TOKEN = 'rpc-token-' + ('x' * 40)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        if self.headers.get('X-Xynigo-Executor-RPC') != RPC_TOKEN:
            self.send_error(403)
            return
        if self.path == '/api/file':
            body = b'file-content'
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''result.bin")
        else:
            body = json.dumps({'path': self.path}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class WorkspaceRpcClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = WorkspaceRpcClient(
            'http://127.0.0.1:%d' % cls.server.server_port,
            RPC_TOKEN,
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_json_response(self):
        result = self.client.execute({
            'method': 'GET', 'path': '/api/progress?mode=live', 'body': None,
        })
        self.assertEqual(result['httpStatus'], 200)
        self.assertEqual(result['responseType'], 'json')
        self.assertEqual(result['body']['path'], '/api/progress?mode=live')

    def test_binary_response(self):
        result = self.client.execute({
            'method': 'GET', 'path': '/api/file', 'body': None,
        })
        self.assertEqual(result['responseType'], 'base64')
        self.assertEqual(result['bodyBase64'], 'ZmlsZS1jb250ZW50')
        self.assertIn('result.bin', result['contentDisposition'])

    def test_external_url_is_rejected(self):
        with self.assertRaises(ValueError):
            self.client.execute({
                'method': 'GET',
                'path': 'https://attacker.invalid/api/progress',
                'body': None,
            })

    def test_slow_mutations_receive_extended_internal_timeout(self):
        self.assertEqual(
            self.client._request_timeout('POST', '/api/envbatch/start'),
            120.0,
        )
        self.assertEqual(
            self.client._request_timeout('POST', '/api/query'),
            120.0,
        )
        self.assertEqual(
            self.client._request_timeout('GET', '/api/progress'),
            30.0,
        )


if __name__ == '__main__':
    unittest.main()
