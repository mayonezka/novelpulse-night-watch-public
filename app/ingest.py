"""Import an HTTP(S) article as a new chapter; drafts are the default."""

import argparse
import asyncio
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import Settings
from app.db import Database


async def fetch_article(url: str, *, selector: str | None = None,
                        client: httpx.AsyncClient | None = None) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Provide an HTTP or HTTPS article URL")
    if client is None:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                      headers={"User-Agent": "farm-unit-1/1.0"}) as owned:
            return await fetch_article(url, selector=selector, client=owned)
    response = await client.get(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for element in soup.select("script, style, nav, footer, header, aside, noscript"):
        element.decompose()
    root = soup.select_one(selector) if selector else (soup.find("article") or soup.find("main") or soup.body)
    if root is None:
        raise ValueError("No article content matched; use --selector for the article container")
    heading = root.find("h1") or soup.find("h1") or soup.title
    title = heading.get_text(" ", strip=True) if heading else parsed.hostname
    content = "\n\n".join(line.strip() for line in root.get_text("\n", strip=True).splitlines() if line.strip())
    if not content:
        raise ValueError("The article contains no readable text")
    return title, content


async def import_article(args: argparse.Namespace) -> int:
    settings = Settings()
    title, content = await fetch_article(args.url, selector=args.selector)
    db = Database(settings.db_path)
    await db.open()
    try:
        return await db.save_chapter(args.series_id, args.chapter_num, args.title or title,
                                     content, args.price_stars, args.publish)
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--series-id", type=int, required=True)
    parser.add_argument("--chapter-num", type=int, required=True)
    parser.add_argument("--title")
    parser.add_argument("--selector", help="CSS selector for the article container")
    parser.add_argument("--price-stars", type=int, default=1)
    parser.add_argument("--publish", action="store_true", help="Publish immediately instead of saving a draft")
    args = parser.parse_args()
    if args.series_id < 1 or args.chapter_num < 1 or args.price_stars < 1:
        parser.error("Series ID, chapter number, and price must be positive")
    chapter_id = asyncio.run(import_article(args))
    print(f"Imported chapter {chapter_id} ({'published' if args.publish else 'draft'})")


if __name__ == "__main__":
    main()
