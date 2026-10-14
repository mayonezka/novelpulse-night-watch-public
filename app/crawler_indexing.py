"""Polite Save Page Now submissions; reports archive receipts, never promises SEO indexing."""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from app.config import PipelineSettings
from app.db import Database
from app.pipeline import failure_reason


async def live_seo_urls(db: Database) -> list[str]:
    urls = [s['telegraph_url'] for s in await db.list_series() if s['telegraph_url']]
    urls += [h['url'] for h in json.loads(await db.get_state('seo_hubs') or '[]')]
    for series in await db.list_series():
        urls += [p['url'] for p in json.loads(await db.get_state(f"seo_article_parts:{series['id']}") or '[]')]
    return list(dict.fromkeys(url for url in urls if urlparse(url).netloc == 'telegra.ph' and url.startswith('https://')))


async def submit_archive_urls(urls: list[str], *, client: httpx.AsyncClient | None = None,
                             delay_seconds: float = 2) -> list[dict]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=45, follow_redirects=False,
        headers={'User-Agent': 'NovelPulse-ArchiveSubmitter/1.0 (public article preservation)'})
    results = []
    try:
        for index, url in enumerate(dict.fromkeys(urls)):
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.hostname != 'telegra.ph' or parsed.username or parsed.password:
                results.append({'url': url, 'status': 'rejected', 'reason': 'Only public HTTPS Telegraph articles are accepted'})
                continue
            if index:
                await asyncio.sleep(max(0, delay_seconds))
            result = {'url': url, 'status': 'failed'}
            try:
                for attempt in range(2):
                    response = await client.get('https://web.archive.org/save/' + url)
                    if response.status_code in (429, 502, 503, 504) and attempt == 0:
                        retry = response.headers.get('Retry-After', '')
                        wait = min(30, max(2, int(retry))) if retry.isdigit() else 4
                        await asyncio.sleep(wait)
                        continue
                    break
                result['http_status'] = response.status_code
                location = response.headers.get('Content-Location') or response.headers.get('Location', '')
                receipt = urljoin('https://web.archive.org', location) if location else ''
                if (response.status_code in (200, 201, 302, 303) and
                        receipt.startswith('https://web.archive.org/web/') and url in receipt):
                    result.update(status='archived', archive_url=receipt)
                elif response.status_code in (200, 201, 202):
                    # A 200 HTML form/challenge is not proof that a capture was saved.
                    result.update(status='unconfirmed', reason='No archive receipt returned')
                else:
                    result['reason'] = f'Wayback HTTP {response.status_code}'
            except Exception as error:
                result['reason'] = failure_reason(error)
            results.append(result)
    finally:
        if own:
            await client.aclose()
    return results


async def _cli(args):
    settings = PipelineSettings()
    db = Database(settings.db_path)
    await db.open()
    try:
        urls = await live_seo_urls(db)
        results = await submit_archive_urls(urls)
        report = {'submitted_at': datetime.now(timezone.utc).isoformat(), 'results': results,
                  'indexnow': 'not_applicable: telegra.ph is not an owned host; no verified portal domain/key configured'}
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        await db.set_state('crawler_indexing_report', json.dumps(report, ensure_ascii=False))
        print(json.dumps(report, ensure_ascii=True, indent=2))
    finally:
        await db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Submit live Telegraph articles to Save Page Now with rate limiting')
    parser.add_argument('--report', default='data/stage8_indexing_report.json')
    asyncio.run(_cli(parser.parse_args()))
