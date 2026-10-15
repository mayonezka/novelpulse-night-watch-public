"""Resume-safe acquisition and publishing when fewer than three episode plans remain."""
import json
from pathlib import Path
import httpx
from app.pipeline import fetch_trending_royalroad
from app.pipeline import run_pipeline
from app.pipeline import Translator
from app.telegraph_seo import publish_all, publish_seo_cluster
from app.web_portal import generate_web_portal
from app.shorts import generate_shorts_queue, SHORTS_DIR
from app.deployment import deploy_github_pages, matches_public_text
from scripts.check_deploy_credentials import github_credential
from app.source_transport import SourceTransport
import re


def _has_cyrillic(value: str) -> bool:
    return bool(re.search(r'[А-Яа-яЁё]', value or ''))


async def deepen_existing_royalroad(db, settings, *, series_ids=(3, 4, 5, 6), chapters_each=2,
                                    client=None) -> dict:
    """Import the next source chapters and verify translated, priced SQLite rows."""
    from app.pipeline import royalroad_fiction_url
    import httpx

    if chapters_each < 1:
        raise ValueError('chapters_each must be positive')
    if client is None:
        async with httpx.AsyncClient(timeout=40, transport=SourceTransport()) as owned:
            return await deepen_existing_royalroad(db, settings, series_ids=series_ids,
                chapters_each=chapters_each, client=owned)
    translation_settings = settings.model_copy(update={'llm_api_key': None})
    results, errors = [], []
    for series_id in series_ids:
        series = await db.get_series(series_id)
        if not series or not series.get('source_url'):
            continue
        try:
            target_total = 10 if series_id in (3, 4) else 8
            before_list = await db.list_chapters(series_id)
            if len(before_list) >= target_total:
                before = [await db.get_chapter(row['id']) for row in before_list]
                valid_existing = [row for row in before if row['is_published'] and row['chapter_num'] > 2
                                  and row['price_stars'] == 10 and _has_cyrillic(row['title'] + '\n' + row['content_text'])]
                deepened = valid_existing[-chapters_each:]
                results.append({'series_id': series_id, 'id': series_id, 'title': series['title'],
                    'chapter_nums': sorted(row['chapter_num'] for row in deepened), 'imported': 0,
                    'max_chapter': max(row['chapter_num'] for row in before)})
                continue
            before = before_list
            source_url = royalroad_fiction_url(series['source_url'])
            old_nums = {row['chapter_num'] for row in before}
            run = await run_pipeline(source_url, translation_settings, db=db, client=client,
                series_id=series_id, max_chapters=chapters_each, free_chapters_limit=2)
            after = [await db.get_chapter(row['id']) for row in await db.list_chapters(series_id)]
            added = [row for row in after if row['chapter_num'] not in old_nums]
            valid = [row for row in added if row['is_published'] and row['chapter_num'] > 2
                     and row['price_stars'] == 10 and _has_cyrillic(row['title'] + '\n' + row['content_text'])]
            if len(valid) < chapters_each:
                raise ValueError(f'Серия {series_id}: получено {len(valid)} новых опубликованных глав с кириллицей, нужно {chapters_each}')
            results.append({'series_id': series_id, 'id': series_id, 'title': series['title'],
                'chapter_nums': sorted(row['chapter_num'] for row in valid), 'imported': run.imported,
                'max_chapter': max(row['chapter_num'] for row in after)})
        except Exception as error:
            from app.pipeline import failure_reason
            errors.append({'series_id': series_id, 'reason': failure_reason(error)})
    return {'updated': results, 'errors': errors}


async def replenish_content_queue(db, bot, settings, *, output_dir=SHORTS_DIR):
    rows = await db.list_shorts()
    pending = sum(row['status'] == 'pending' for row in rows)
    unfinished = json.loads(await db.get_state('replenishment_checkpoint') or '{}')
    if pending >= 3 and not unfinished:
        return {'status': 'idle', 'pending': pending}
    username = (await bot.get_me()).username
    if not unfinished:
        results = await fetch_trending_royalroad(1, 6, settings=settings, db=db)
        if not results:
            raise ValueError('Trending не вернул готовую новеллу')
        unfinished = {'series_ids': [r.series_id for r in results], 'step': 'acquired'}
        await db.set_state('replenishment_checkpoint', json.dumps(unfinished))
    await publish_all(db, username)
    await publish_seo_cluster(db, username, settings.channel_id)
    await generate_web_portal(db, username, output_dir='docs', base_url=settings.web_base_url, channel_id=settings.channel_id)
    token = await github_credential()
    if not token:
        raise ValueError('Нет неинтерактивных credentials GitHub для обновления портала')
    # Only the public whitelist is exported; credentials never enter docs or a Git remote URL.
    deployment = await deploy_github_pages(db, settings, username, settings.channel_id, token)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        checks = []
        for name in deployment['files']:
            response = await client.get(deployment['base_url'] + name)
            checks.append(response.status_code == 200 and matches_public_text(response.content, Path('docs') / name))
    if not all(checks):
        unfinished['step'] = 'pages_building'
        await db.set_state('replenishment_checkpoint', json.dumps(unfinished))
        return {'status': 'building', 'series_ids': unfinished['series_ids']}
    deployment['status'] = 'live'
    await db.set_state('public_web_deployment', json.dumps(deployment))
    planned = await generate_shorts_queue(db, username, settings.channel_id, output_dir=output_dir,
                                          portal_url=settings.web_base_url or '')
    await db.set_state('replenishment_checkpoint', '{}')
    return {'status': 'complete', 'series_ids': unfinished['series_ids'], 'queue_count': planned['queue_count']}
