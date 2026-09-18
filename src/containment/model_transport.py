"""Bounded loopback HTTP only. No DNS, proxies, redirects, retries, or credentials."""

import asyncio
import json
from contextlib import suppress


async def _body(reader, headers):
    if "content-encoding" in headers:
        raise ValueError("Compressed model responses are unsupported")
    if "transfer-encoding" in headers:
        if headers["transfer-encoding"].lower() != "chunked" or "content-length" in headers:
            raise ValueError("Ambiguous model response framing")
        chunks = []
        total = 0
        # Bound both decoded size and framing overhead. No chunk extensions or trailers.
        for _ in range(4096):
            line = await reader.readuntil(b"\r\n")
            size_text = line[:-2]
            if not 1 <= len(size_text) <= 8 or any(
                c not in b"0123456789abcdefABCDEF" for c in size_text
            ):
                raise ValueError("Invalid chunk size")
            size = int(size_text, 16)
            if size == 0:
                if await reader.readexactly(2) != b"\r\n":
                    raise ValueError("Model response trailers are unsupported")
                return b"".join(chunks)
            total += size
            if total > 131072:
                raise ValueError("Model response exceeds limit")
            chunks.append(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise ValueError("Invalid chunk terminator")
        raise ValueError("Too many model response chunks")
    length = headers.get("content-length", "")
    if not length.isascii() or not length.isdecimal() or not 0 < int(length) <= 131072:
        raise ValueError("Invalid model response length")
    return await reader.readexactly(int(length))


async def _post(port: int, path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(body) > 262144:
        raise ValueError("Model request exceeds transport limit")
    reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=8192)
    try:
        method = "GET" if path == "/api/tags" else "POST"
        writer.write(
            f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode("ascii")
            + body
        )
        await writer.drain()
        header = await reader.readuntil(b"\r\n\r\n")
        if len(header) > 8192:
            raise ValueError("Model response headers exceed limit")
        lines = header.decode("ascii").split("\r\n")
        status = lines[0].split(" ")
        if len(status) < 2 or status[0] not in {"HTTP/1.0", "HTTP/1.1"} or status[1] != "200":
            raise ValueError("Model HTTP request failed; no retry")
        headers = {}
        for line in lines[1:-2]:
            key, value = line.split(":", 1)
            key = key.lower()
            if key in headers:
                raise ValueError("Duplicate model response header")
            headers[key] = value.strip()
        result = json.loads(await _body(reader, headers))
        if not isinstance(result, dict):
            raise ValueError("Model response must be an object")
        return result
    finally:
        # Abort closes the local socket promptly, including on timeout/cancellation.
        # It does not prove termination of generation at the server.
        writer.transport.abort()


async def _supervised_post(port, path, payload, timeout_seconds, checkpoint):
    checkpoint()
    task = asyncio.create_task(_post(port, path, payload))
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.1)
                checkpoint()
                if done:
                    return task.result()
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


def post(port, path, payload, timeout_seconds, checkpoint):
    if path not in {"/api/tags", "/api/generate"}:
        raise ValueError("Model endpoint is not allowed")
    return asyncio.run(_supervised_post(port, path, payload, timeout_seconds, checkpoint))
