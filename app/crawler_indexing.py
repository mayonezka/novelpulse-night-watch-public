"""Polite Save Page Now submissions; reports archive receipts, never promises SEO indexing."""

import argparse
import asyncio
import json
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import httpx

from app.config import PipelineSettings
from app.db import Database
from app.pipeline import failure_reason
from app.seo_hubs import SEO_FILENAMES
from app.portal_sections import READER_PAGES

INDEXNOW_ENDPOINTS = ('https://api.indexnow.org/indexnow', 'https://yandex.com/indexnow')
INDEXNOW_STATE = 'portal_indexnow_report'


def portal_base(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or '%' in parsed.path or any(p in ('.', '..') for p in parsed.path.split('/'))):
        raise ValueError('IndexNow requires a canonical public HTTPS portal base')
    return url.rstrip('/') + '/'


def indexnow_key(base_url: str) -> str:
    """Public ownership verification key, derived only from the public project identity."""
    return hashlib.sha256(('NovelPulse:IndexNow:v1:' + portal_base(base_url)).encode()).hexdigest()[:32]


def write_indexnow_key(directory: str | Path, base_url: str) -> Path:
    key = indexnow_key(base_url)
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / (key + '.txt')
    if target.is_symlink():
        raise ValueError('IndexNow key must be a regular public file')
    target.write_text(key, encoding='utf-8')
    return target


def sitemap_html_urls(directory: str | Path, base_url: str) -> list[str]:
    base_url = portal_base(base_url)
    data = (Path(directory) / 'sitemap.xml').read_bytes()
    if len(data) > 2_000_000 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Invalid portal sitemap')
    urls = []
    for node in ET.fromstring(data).iter():
        if node.tag.rsplit('}', 1)[-1] != 'loc':
            continue
        url = (node.text or '').strip()
        parsed = urlparse(url)
        name = url.removeprefix(base_url)
        if (not url.startswith(base_url) or parsed.query or parsed.fragment or '%' in url
                or unquote(name) != name or not (name in (*SEO_FILENAMES,*READER_PAGES) or re.fullmatch(r'(?:index|series_[1-9][0-9]*(?:_ch_[1-9][0-9]*)?)\.html', name))):
            raise ValueError('Sitemap URL is outside the verified portal HTML scope')
        if url not in urls:
            urls.append(url)
    if not 1 <= len(urls) <= 10_000:
        raise ValueError('IndexNow batch must contain 1..10000 HTML URLs')
    return urls


async def submit_portal_indexnow(db: Database, directory: str | Path, base_url: str, *,
                                client: httpx.AsyncClient | None = None, wait_seconds: float = 180,
                                force: bool = False) -> dict:
    """Verify the deployed build, then send one batch per engine. Receipt is not indexing."""
    base_url = portal_base(base_url)
    root = Path(directory)
    urls = sitemap_html_urls(root, base_url)
    key = indexnow_key(base_url)
    key_location = base_url + key + '.txt'
    if (root / (key + '.txt')).read_bytes() != key.encode():
        raise ValueError('Local IndexNow key file does not match the project key')
    digest = hashlib.sha256()
    for url in urls:
        digest.update(url.encode())
        digest.update((root / url.removeprefix(base_url)).read_bytes().replace(b'\r\n', b'\n'))
    fingerprint = digest.hexdigest()
    previous = json.loads(await db.get_state(INDEXNOW_STATE) or '{}')
    previous_results = {r['endpoint']: r for r in previous.get('results', [])}
    old = previous_results if previous.get('digest') == fingerprint and not force else {}
    now = datetime.now(timezone.utc)
    report = {'submitted_at': now.isoformat(), 'base_url': base_url, 'key': key, 'keyLocation': key_location,
              'url_count': len(urls), 'digest': fingerprint, 'results': [], 'status': 'waiting_for_key'}
    owned = client is None
    # A fresh public client must never inherit GitHub Authorization headers.
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=False,
        headers={'User-Agent': 'NovelPulse-IndexNow/1.0'})
    if 'authorization' in client.headers or 'cookie' in client.headers or client.cookies:
        if owned:
            await client.aclose()
        raise ValueError('IndexNow requires an unauthenticated public HTTP client')
    try:
        deadline = time.monotonic() + max(0, wait_seconds)
        while True:
            try:
                verification = await client.get(key_location, headers={'Cache-Control': 'no-cache'})
                report['key_http_status'] = verification.status_code
                if verification.status_code == 200 and verification.content == key.encode():
                    sitemap = await client.get(base_url + 'sitemap.xml', headers={'Cache-Control': 'no-cache'})
                    report['sitemap_http_status'] = sitemap.status_code
                    if (sitemap.status_code == 200 and sitemap.content.replace(b'\r\n', b'\n') ==
                            (root/'sitemap.xml').read_bytes().replace(b'\r\n', b'\n')):
                        report['key_verified'] = True
                        break
            except httpx.HTTPError as error:
                report['verification_error'] = type(error).__name__
            if time.monotonic() >= deadline:
                # A temporary Pages outage must not discard already accepted receipts.
                report['results'] = list(old.values())
                await db.set_state(INDEXNOW_STATE, json.dumps(report))
                return report
            await asyncio.sleep(min(5, max(0, deadline-time.monotonic())))
        payload = {'host': urlparse(base_url).hostname, 'key': key, 'keyLocation': key_location, 'urlList': urls}
        for endpoint in INDEXNOW_ENDPOINTS:
            prior = old.get(endpoint, {})
            if prior.get('http_status') in (200, 202):
                report['results'].append({**prior, 'deduplicated': True})
                continue
            prior_retry = previous_results.get(endpoint, {})
            if prior_retry.get('retry_at') and datetime.fromisoformat(prior_retry['retry_at']) > now:
                report['results'].append({**prior_retry, 'deferred': True})
                continue
            result = {'endpoint': endpoint}
            try:
                response = await client.post(endpoint, json=payload)
                result.update(http_status=response.status_code, accepted=response.status_code in (200, 202))
                if response.status_code == 429 or response.status_code >= 500:
                    from datetime import timedelta
                    retry = response.headers.get('Retry-After', '')
                    seconds = min(86400, max(60, int(retry))) if retry.isdigit() else 3600
                    result['retry_at'] = (now + timedelta(seconds=seconds)).isoformat()
            except httpx.HTTPError as error:
                result.update(accepted=False, error=type(error).__name__)
            report['results'].append(result)
            report['status'] = 'received' if all(r.get('accepted') for r in report['results']) else 'partial_or_failed'
            # Persist each engine receipt before contacting the next endpoint.
            await db.set_state(INDEXNOW_STATE, json.dumps(report))
        report['status'] = 'received' if all(r.get('accepted') for r in report['results']) else 'partial_or_failed'
        await db.set_state(INDEXNOW_STATE, json.dumps(report))
        return report
    finally:
        if owned:
            await client.aclose()


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
        if args.indexnow:
            report = await submit_portal_indexnow(db, args.directory, args.base_url or settings.web_base_url,
                                                  wait_seconds=args.wait_seconds)
        else:
            urls = await live_seo_urls(db)
            results = await submit_archive_urls(urls)
            report = {'submitted_at': datetime.now(timezone.utc).isoformat(), 'results': results,
                      'indexnow': 'Use --indexnow for the verified owned portal; Telegraph ownership cannot be verified here'}
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
    parser.add_argument('--indexnow', action='store_true', help='Submit the deployed owned portal instead of Telegraph archival')
    parser.add_argument('--base-url', default='')
    parser.add_argument('--directory', default='docs')
    parser.add_argument('--wait-seconds', type=float, default=180)
    asyncio.run(_cli(parser.parse_args()))
