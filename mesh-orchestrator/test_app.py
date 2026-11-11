import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app


class ModelRouteParsingTest(unittest.TestCase):
    def test_defaults_do_not_require_stopped_ninfer(self):
        for alias in ("chat", "deep", "worker", "worker-big"):
            self.assertEqual(app.DEFAULT_MODEL_ROUTES[alias]["model"], "Qwen3-30B-A3B-Instruct-2507-int4-ov")
        self.assertNotIn("vision", app.DEFAULT_MODEL_ROUTES)
        self.assertNotIn("ninfer", app.DEFAULT_MODEL_ROUTES)

    def test_parse_model_routes_keeps_provider_url_and_real_model(self):
        routes = app.parse_model_routes(json.dumps({
            "worker": {
                "base_url": "http://ia-gpu-intel-openvino:8000/v1/",
                "model": "Qwen3-30B-A3B-Instruct-2507-int4-ov",
            },
            "chat": {
                "base_url": "http://host.docker.internal:8090/v1",
                "model": "ninfer",
            },
        }))

        self.assertEqual(
            routes["worker"].base_url,
            "http://ia-gpu-intel-openvino:8000/v1",
        )
        self.assertEqual(
            routes["worker"].model,
            "Qwen3-30B-A3B-Instruct-2507-int4-ov",
        )
        self.assertEqual(routes["chat"].model, "ninfer")

    def test_parse_model_routes_rejects_incomplete_route(self):
        with self.assertRaisesRegex(ValueError, "worker.*model"):
            app.parse_model_routes(json.dumps({
                "worker": {"base_url": "http://provider:8000/v1"},
            }))


class DirectProviderCallTest(unittest.IsolatedAsyncioTestCase):
    async def test_call_llm_uses_direct_provider_and_real_model_without_gateway_key(self):
        captured = {}

        async def handle_provider(reader, writer):
            header = await reader.readuntil(b"\r\n\r\n")
            header_text = header.decode("iso-8859-1")
            content_length = 0
            for line in header_text.split("\r\n"):
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1])
            body = await reader.readexactly(content_length)
            captured["path"] = header_text.split(" ", 2)[1]
            captured["headers"] = header_text.lower()
            captured["body"] = json.loads(body)
            response = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"total_tokens": 1},
            }).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(response)}\r\nConnection: close\r\n\r\n".encode()
                + response
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_provider, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        base_url = f"http://127.0.0.1:{port}/v1"
        old_routes = app.MODEL_ROUTES
        app.MODEL_ROUTES = app.parse_model_routes(json.dumps({
            "worker": {"base_url": base_url, "model": "real-provider-model"},
        }))
        try:
            response = await app.call_llm(
                "worker", [{"role": "user", "content": "prueba"}], max_tokens=7,
            )
        finally:
            app.MODEL_ROUTES = old_routes
            app.LANES.pop("worker", None)
            server.close()
            await server.wait_closed()

        self.assertEqual(response["choices"][0]["message"]["content"], "ok")
        self.assertEqual(captured["path"], "/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "real-provider-model")
        self.assertEqual(captured["body"]["max_tokens"], 7)
        self.assertNotIn("authorization:", captured["headers"])


if __name__ == "__main__":
    unittest.main()
