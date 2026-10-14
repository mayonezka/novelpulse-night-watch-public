"""Resume-safe acquisition and publishing when fewer than three episode plans remain."""
import json
from pathlib import Path
import httpx
from app.pipeline import fetch_trending_royalroad
from app.telegraph_seo import publish_all, publish_seo_cluster
from app.web_portal import generate_web_portal
from app.shorts import generate_shorts_queue, SHORTS_DIR
from app.deployment import deploy_github_pages, matches_public_text
from scripts.check_deploy_credentials import github_credential


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
