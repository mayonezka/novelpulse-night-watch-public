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


async def verify_royalroad_book(db, series_id: int, chapter_count: int) -> dict:
    """Reject partial imports, untranslated bodies, gaps and incorrect paid prices."""
    series = await db.get_series(series_id)
    if not series or 'royalroad.com/fiction/' not in (series.get('source_url') or ''):
        raise ValueError(f'Series {series_id}: missing RoyalRoad source')
    rows = [await db.get_chapter(c['id']) for c in await db.list_chapters(series_id)]
    if {c['chapter_num'] for c in rows} != set(range(1, chapter_count + 1)):
        raise ValueError(f'Series {series_id}: expected exactly chapters 1–{chapter_count}')
    if series['free_chapters_limit'] != 2:
        raise ValueError(f'Series {series_id}: expected two free chapters')
    for chapter in rows:
        letters = [c for c in chapter['content_text'] if c.isalpha()]
        russian = sum(bool(re.match('[А-Яа-яЁё]', c)) for c in letters)
        if (not letters or russian / len(letters) < .5 or not chapter['is_published']
                or (chapter['chapter_num'] > 2 and chapter['price_stars'] != 10)):
            raise ValueError(f'Series {series_id}, chapter {chapter["chapter_num"]}: RU/publication/price validation failed')
    return series


async def expand_catalog_batch(db, settings, *, client, deepen_ids=(10, 11), new_ids=(12, 13)) -> dict:
    """Resume a bounded catalog release without invoking rendering or upload workers."""
    deepening = await deepen_existing_royalroad(db, settings, series_ids=deepen_ids,
        chapters_each=4, target_chapters=10, client=client)
    if deepening['errors']:
        raise ValueError(deepening['errors'])
    for sid in deepen_ids:
        await verify_royalroad_book(db, sid, 10)
    imported = []
    for sid in new_ids:
        if await db.get_series(sid):
            series = await verify_royalroad_book(db, sid, 6)
            entry = {'series_id': sid, 'title': series['title'], 'source_url': series['source_url'], 'resumed': True}
        else:
            entry = await import_trending_royalroad_series(db, settings, client=client, chapter_count=6)
            if entry['series_id'] != sid:
                raise ValueError(f'Expected Series {sid}, received {entry["series_id"]}')
            await verify_royalroad_book(db, sid, 6)
        imported.append(entry)
    return {'deepening': deepening, 'new_series': imported}


async def deepen_existing_royalroad(db, settings, *, series_ids=(3, 4, 5, 6), chapters_each=2,
                                    client=None, target_chapters: int | None = None) -> dict:
    """Import the next source chapters and verify translated, priced SQLite rows."""
    from app.pipeline import royalroad_fiction_url
    import httpx

    if chapters_each < 1:
        raise ValueError('chapters_each must be positive')
    if target_chapters is not None and target_chapters < 1:
        raise ValueError('target_chapters must be positive')
    if client is None:
        async with httpx.AsyncClient(timeout=40, transport=SourceTransport()) as owned:
            return await deepen_existing_royalroad(db, settings, series_ids=series_ids,
                chapters_each=chapters_each, client=owned, target_chapters=target_chapters)
    translation_settings = settings.model_copy(update={'llm_api_key': None})
    results, errors = [], []
    for series_id in series_ids:
        series = await db.get_series(series_id)
        if not series or not series.get('source_url'):
            continue
        try:
            # RoyalRoad catalog IDs 7 and 8 were initially seeded with six chapters;
            # Stage 33 extends them through chapter 10 just like the earlier hits.
            target_total = target_chapters or (10 if series_id in (3, 4, 5, 6, 7, 8, 9) else 8)
            before_list = await db.list_chapters(series_id)
            before_nums = {row['chapter_num'] for row in before_list}
            if set(range(1, target_total + 1)).issubset(before_nums):
                before = [await db.get_chapter(row['id']) for row in before_list]
                valid_existing = [row for row in before if row['is_published'] and row['chapter_num'] > 2
                                  and row['price_stars'] == 10 and _has_cyrillic(row['title'] + '\n' + row['content_text'])]
                deepened = valid_existing[-chapters_each:]
                results.append({'series_id': series_id, 'id': series_id, 'title': series['title'],
                    'chapter_nums': sorted(row['chapter_num'] for row in deepened), 'imported': 0,
                    'max_chapter': max(row['chapter_num'] for row in before), 'providers': {},
                    'already_complete': True})
                continue
            before = before_list
            source_url = royalroad_fiction_url(series['source_url'])
            old_nums = {row['chapter_num'] for row in before}
            translator = Translator(translation_settings, client)
            missing = set(range(1, target_total + 1)) - before_nums
            request_count = min(chapters_each, len(missing))
            run = await run_pipeline(source_url, translation_settings, db=db, client=client,
                series_id=series_id, max_chapters=request_count, max_total_chapters=target_total,
                free_chapters_limit=2,
                translator=translator)
            after = [await db.get_chapter(row['id']) for row in await db.list_chapters(series_id)]
            added = [row for row in after if row['chapter_num'] not in old_nums]
            valid = [row for row in added if row['is_published'] and row['chapter_num'] > 2
                     and row['price_stars'] == 10 and _has_cyrillic(row['title'] + '\n' + row['content_text'])]
            if len(valid) < request_count:
                raise ValueError(f'Серия {series_id}: получено {len(valid)} новых опубликованных глав с кириллицей, нужно {request_count}')
            results.append({'series_id': series_id, 'id': series_id, 'title': series['title'],
                'chapter_nums': sorted(row['chapter_num'] for row in valid), 'imported': run.imported,
                'max_chapter': max(row['chapter_num'] for row in after),
                'providers': dict(translator.provider_usage)})
        except Exception as error:
            from app.pipeline import failure_reason
            errors.append({'series_id': series_id, 'reason': failure_reason(error)})
    return {'updated': results, 'errors': errors}


def _genre_for_series(title: str, description: str) -> tuple[str, str]:
    """Map an imported title to one of the existing smart-catalog shelves."""
    from app.db import GENRES
    # Descriptions often mention ordinary monsters, pets, or magic in passing.
    # Use the title for shelf assignment; a broad description match mislabels
    # general progression LitRPG as taming or cultivation.
    text = title.casefold()
    rules = (
        ('cultivation', ('cultivat', 'spirit garden', 'immortal', 'martial', 'культивац', 'садовниц')),
        ('science_magic', ('railgun', 'lightning', 'science', 'engineer', 'technology', 'рельсотрон', 'молни', 'науч')),
        ('taming', ('taming', 'tamer', 'beast', 'pet', 'evolution', 'укрот', 'прируч', 'эволюц')),
        ('comedy_isekai', ('saint', 'comedy', 'humor', 'свят', 'комеди', 'юмор')),
        ('isekai', ('isekai', 'reincarnat', 'transmigrat', 'another world', 'попадан', 'исэкай', 'перерожд')),
    )
    for slug, needles in rules:
        if any(needle in text for needle in needles):
            return slug, GENRES[slug][0]
    return 'litrpg', GENRES['litrpg'][0]


async def import_trending_royalroad_series(db, settings, *, client=None, chapter_count: int = 6) -> dict:
    """Import a new RoyalRoad series only, using the zero-key batched clients5 translator."""
    if chapter_count < 3:
        raise ValueError('Для SEO-страницы нужны минимум три главы')
    import httpx
    from app.pipeline import fetch_trending_royalroad
    from app.source_transport import SourceTransport
    if client is None:
        async with httpx.AsyncClient(timeout=45, transport=SourceTransport()) as owned:
            return await import_trending_royalroad_series(db, settings, client=owned, chapter_count=chapter_count)
    translation_settings = settings.model_copy(update={'llm_api_key': None})
    from app.pipeline import Translator
    translator = Translator(translation_settings, client)
    before = {item['id'] for item in await db.list_all_series()}
    try:
        results = await fetch_trending_royalroad(1, chapter_count, settings=translation_settings,
            db=db, client=client, allow_fallback=False, translator=translator)
    except BaseException:
        await _discard_new_series(db, before)
        raise
    new = [result for result in results if result.series_id not in before]
    if len(new) != 1:
        await _discard_new_series(db, before)
        raise ValueError('Trending не добавил ровно одну новую RoyalRoad-новеллу')
    try:
        series = await db.get_series(new[0].series_id)
        chapters = [await db.get_chapter(item['id']) for item in await db.list_chapters(series['id'])]
        chapters = [row for row in chapters if row]
        expected = set(range(1, chapter_count + 1))
        nums = {row['chapter_num'] for row in chapters}
        invalid = [row['chapter_num'] for row in chapters if not row['is_published']
            or (row['chapter_num'] > 2 and row['price_stars'] != 10)
            or not _has_cyrillic(row['title'] + '\n' + row['content_text'])]
        if not expected.issubset(nums) or invalid:
            raise ValueError(f'Новая серия не прошла проверку глав/RU/цены: missing={sorted(expected-nums)}, invalid={invalid}')
        if not translator.provider_usage.get('clients5'):
            raise ValueError('clients5 не перевёл ни одного текстового чанка; новая серия не будет опубликована')
        slug, genre_title = _genre_for_series(series['title'], series['description'])
        async with db._lock:
            await db.connection.execute('UPDATE series SET genre_slug=?, genre_title=? WHERE id=?',
                                        (slug, genre_title, series['id']))
            await db.connection.commit()
        # If Trending returned a later successful title after a partial source, discard only the
        # other IDs created by this run so the catalog does not retain a broken stub.
        await _discard_new_series(db, before, keep={series['id']})
        return {'series_id': series['id'], 'title': series['title'], 'source_url': series['source_url'],
            'chapter_nums': sorted(expected), 'chapter_count': len(chapters),
            'free_chapters_limit': series['free_chapters_limit'], 'genre_slug': slug,
            'genre_title': genre_title, 'mode': new[0].mode,
            'providers': dict(translator.provider_usage)}
    except BaseException:
        await _discard_new_series(db, before)
        raise


async def _discard_new_series(db, previous_ids: set[int], *, keep: set[int] | None = None) -> None:
    keep = keep or set()
    created = [item['id'] for item in await db.list_all_series()
               if item['id'] not in previous_ids and item['id'] not in keep]
    if not created:
        return
    async with db._lock:
        for series_id in created:
            await db.connection.execute('DELETE FROM chapters WHERE series_id=?', (series_id,))
            await db.connection.execute('DELETE FROM series WHERE id=?', (series_id,))
        await db.connection.commit()


async def plan_series_short_metadata(db, series_id: int, username: str, channel_id,
                                     *, output_dir=SHORTS_DIR, portal_url='') -> list[dict]:
    """Queue three JIT upload plans without rendering a video or creating any media file."""
    from app.shorts import short_copy, viral_filename
    from app.telegraph_seo import channel_link
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    series = await db.get_series(series_id)
    if not series:
        raise ValueError('Новелла для очереди не найдена')
    channel_url = channel_link(channel_id) if isinstance(channel_id, str) else ''
    plans = []
    for episode in (1, 2, 3):
        filename = viral_filename(series, episode)
        path = directory / filename
        _, description = short_copy(series, episode, username, channel_url, portal_url)
        short_id = await db.save_short(series_id, episode, str(path), path.stem, description)
        plans.append({'id': short_id, 'episode_num': episode, 'title': path.stem,
                      'video_path': str(path), 'status': 'pending'})
    return plans


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
