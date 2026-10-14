"""Browser TLS fingerprints first, ordinary HTTP second, verified local cache last."""

import hashlib
import json
import logging
from pathlib import Path

import httpx
from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)


class SourceTransport(httpx.AsyncBaseTransport):
    def __init__(self, cache_dir: str | Path = "data/royalroad_browser_cache"):
        self.cache_dir = Path(cache_dir).resolve()
        self.network = httpx.AsyncHTTPTransport()
        self.browser = None

    def cached_response(self, request: httpx.Request) -> httpx.Response | None:
        manifest = self.cache_dir / "manifest.json"
        if request.url.host not in ("www.royalroad.com", "royalroad.com") or not manifest.is_file():
            return None
        entries = json.loads(manifest.read_text(encoding="utf-8"))["entries"]
        entry = next((e for e in entries if e["url"] == str(request.url)), None)
        if not entry:
            return None
        path = (self.cache_dir / entry["file"]).resolve()
        if not path.is_relative_to(self.cache_dir):
            raise ValueError("Путь кэшированной страницы выходит за пределы каталога")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError("Повреждён браузерный кэш: SHA256 не совпадает")
        logger.warning("Источник %s прочитан из локального браузерного кэша", request.url.path)
        return httpx.Response(200, content=content, headers={"Content-Type": "text/html; charset=utf-8",
                              "X-NovelPulse-Acquisition": "browser-cache"}, request=request)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        is_source = request.method == "GET" and request.url.host in (
            "www.royalroad.com", "royalroad.com", "www.reddit.com", "reddit.com")
        if not is_source:
            return await self.network.handle_async_request(request)
        try:
            if self.browser is None:
                self.browser = AsyncSession(impersonate="chrome")
            headers = {"Accept-Language": "en-US,en;q=0.9"}
            if "reddit.com" in request.url.host:
                headers["User-Agent"] = request.headers.get("User-Agent", "NovelPulse/1.0 (HFY reader)")
            result = await self.browser.get(str(request.url), headers=headers, timeout=30)
            if 200 <= result.status_code < 300:
                return httpx.Response(result.status_code, content=result.content,
                                      headers={"Content-Type": result.headers.get("Content-Type", "text/html"),
                                               "X-NovelPulse-Acquisition": "curl-chrome"}, request=request)
            logger.warning("curl Chrome %s: HTTP %s; пробую httpx", request.url.path, result.status_code)
        except Exception as error:
            logger.warning("curl Chrome %s: %s; пробую httpx", request.url.path, type(error).__name__)
        try:
            response = await self.network.handle_async_request(request)
            if response.status_code < 400:
                return response
            cached = self.cached_response(request)
            if cached:
                await response.aclose()
                return cached
            return response
        except httpx.TransportError:
            cached = self.cached_response(request)
            if cached:
                return cached
            raise

    async def aclose(self):
        if self.browser is not None:
            await self.browser.close()
        await self.network.aclose()
