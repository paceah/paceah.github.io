import asyncio
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

spec = importlib.util.spec_from_file_location('packserver', Path(__file__).with_name('main.py'))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class PackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.DATA = Path(self.tmp.name)
        server.OBJECTS = server.DATA / 'objects'
        server.SNAPSHOT = server.DATA / 'snapshot.json'
        self.content = b'example-file-for-range-download'
        self.digest = hashlib.sha1(self.content).hexdigest()
        self.head = json.dumps({'journalHead': 1, 'contentToken': 'abc', 'policy': {'categories': {'General': {'main': {'files': {'a': {'sha1': self.digest, 'size': len(self.content)}}}}}}}).encode()
        self.journal = b'{"seq":1,"contentToken":"abc"}\n'
        server.get_bytes = lambda url: self.head if url.endswith('/head') else self.journal
        server.request = lambda url: __import__('io').BytesIO(self.content)
        server.current = None
        self.assertTrue(server.sync_pack())
        self.client = TestClient(TestServer(server.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_range_and_access(self):
        response = await self.client.get('/objects/' + self.digest, headers={'Range': 'bytes=8-15'})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), self.content[8:16])
        response = await self.client.head('/objects/' + self.digest)
        self.assertEqual(response.status, 200)
        self.assertEqual(int(response.headers['Content-Length']), len(self.content))
        self.assertEqual((await self.client.get('/objects/' + '0' * 40)).status, 404)
        self.assertEqual((await self.client.get('/head')).status, 200)

    async def test_failed_update_keeps_previous_version(self):
        old = server.current
        server.get_bytes = lambda url: self.head if url.endswith('/head') else b'{"seq":2}\n'
        self.assertFalse(server.sync_pack())
        self.assertIs(server.current, old)

if __name__ == '__main__':
    unittest.main()
