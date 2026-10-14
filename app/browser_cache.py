"""Explicit, integrity-checked input adapter for operator-exported RoyalRoad pages."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx


class BrowserExportTransport(httpx.AsyncBaseTransport):
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        self.entries = {entry["url"]: entry for entry in manifest["entries"]}
        self.network = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        entry = self.entries.get(str(request.url))
        if entry and request.method == "GET":
            if request.url.host not in ("www.royalroad.com", "royalroad.com"):
                raise ValueError("Browser cache разрешён только для RoyalRoad")
            path = (self.directory / entry["file"]).resolve()
            if not path.is_relative_to(self.directory):
                raise ValueError("Некорректный путь в manifest")
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise ValueError("HTML не соответствует контрольной сумме браузерного экспорта")
            return httpx.Response(200, content=body, headers={"Content-Type": "text/html; charset=utf-8", "X-NovelPulse-Acquisition": "live-browser-export"})
        return await self.network.handle_async_request(request)

    async def aclose(self) -> None:
        await self.network.aclose()
