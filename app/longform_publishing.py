"""JIT long-form publications, durable receipts and explicit feature-deferred work."""
import asyncio
import json
from datetime import datetime,timezone
from pathlib import Path

from app.scheduling import youtube_blocked_until
from app.storage_cleanup import media_slot,delete_video
from app import storage_cleanup
from app.privacy import anonymous_series_url, sanitize_public_text, assert_public_safe
from app.longform_video import with_longform_cta


def publication_key(series_id,*,edition='full'):
    if edition not in ('full','standard'):raise ValueError('Unknown audiobook edition')
    return f'youtube_longform:{series_id}:'+('1-2' if edition=='full' else 'standard')


def pilot_metadata(series,*,edition='full',full_chapter=True):
    sid=series['id']
    title=series['title'].split(' (LitRPG')[0].strip()
    heading=f'«{title}» · Главы 1 и 2 (Полная Аудиокнига) | ЛитРПГ'
    if len(heading)>100:raise ValueError('Audiobook title exceeds YouTube limit')
    link=f'https://t.me/NovelPulseBot?start=yt_long_s_{sid}'
    description=(f'🎧 Полная аудиокнига «{title}» (Главы 1 и 2).\n\n'
        '📖 Продолжить чтение и слушать следующие главы без рекламы в Telegram:\n'
        f'👉 {link}\n\n#литрпг #аудиокнига #ранобэ #система')
    comment=f'📖 Продолжение истории: главы 3 и далее — читать и слушать в Telegram: {link}'
    if edition=='standard':
        label='Глава 1 целиком' if full_chapter else 'Начало истории · Аудиофрагмент'
        heading=f'Рельсотрон против магии | {label} | ЛитРПГ аудиокнига' if sid==5 else f'{title[:45]} | {label}'
        description=(f'🎧 «{title}» — '+('первая глава полностью.' if full_chapter else 'ознакомительный фрагмент первой главы.')+
            '\nЕстественная русская озвучка · горизонтальный формат 16:9.\n\n'
            f'📖 Читать и слушать продолжение в Telegram:\n{link}\n'
            '🔎 Поиск в Telegram: @NovelPulseBot\n'
            '📢 Канал: https://t.me/novelpulse_litrpg\n'
            f'🌐 Библиотека: {anonymous_series_url(series)}\n\n'
            '#литрпг #аудиокнига #магия #исэкай')
        comment=f'📖 Первые главы бесплатно, продолжение и аудиоглавы: {link}'
    description = with_longform_cta(description)
    assert_public_safe((heading, description, comment))
    return {'series_id':sid,'title':heading,'description':description,'comment_text':comment,
            'bot_link':link,'chapters':[1,2] if edition=='full' else [1],'speech_rate':'+8%','format':'1280x720'}


async def plan_longform(db,series_id,*,defer_reason='',edition='full'):
    series=await db.get_series(series_id)
    if not series:raise ValueError('Unknown audiobook series')
    key=publication_key(series_id,edition=edition)
    old=json.loads(await db.get_state(key) or '{}')
    if old:return old  # Never downgrade a submitted or published delivery.
    plan={**pilot_metadata(series,edition=edition),'status':'pending','created_at':datetime.now(timezone.utc).isoformat(),
          'defer_reason':defer_reason,'comment_posted':False,'comment_pinned':False,
          'related_video_set':False}
    await db.set_state(key,json.dumps(plan,ensure_ascii=False))
    return plan


async def publish_longform_youtube(db,series_id,*,root=None,allow_longer_than_15=False,advanced_features=False,
                                  edition='full'):
    from app.longform_video import render_longform_audiobook_video,render_standard_audiobook_video
    from app.youtube_uploader import (async_playwright,ensure_chrome,upload_in_studio,
        wait_for_public_video,verify_persisted_video_details,post_first_comment,pin_author_comment,
        DailyUploadLimit)
    key=publication_key(series_id,edition=edition)
    async with media_slot():
        plan=await plan_longform(db,series_id,edition=edition)
        for field in ('title', 'description', 'comment_text'):
            if field in plan: plan[field] = sanitize_public_text(plan[field])
        if plan['status']=='uploaded':return plan
        plan['description'] = with_longform_cta(plan['description'])
        if plan.get('defer_reason'):return plan
        blocked=await youtube_blocked_until(db)
        if blocked:return {**plan,'status':'quota_blocked','retry_after':blocked}
        # Unknown or started submissions require reconciliation, never another upload.
        if plan['status'] not in ('pending','rendering','rendering_failed','rendered'):
            return {**plan,'needs_reconciliation':True}
        directory=Path(root or storage_cleanup.DATA_DIR/'longform').resolve()
        if not directory.is_relative_to(storage_cleanup.DATA_DIR.resolve()):raise ValueError('Longform media must remain within data/')
        path=directory/(f'series_{series_id}_standard.mp4' if edition=='standard' else f'series_{series_id}_chapters_1_2.mp4')
        series=await db.get_series(series_id)
        chapters=[await db.get_chapter(c['id']) for c in await db.list_chapters(series_id) if c['chapter_num'] in (1,2)]
        series={**series,'chapter_count':len(await db.list_chapters(series_id))}
        async def save(**values):
            plan.update(values);await db.set_state(key,json.dumps(plan,ensure_ascii=False))
        try:
            if plan.get('error'):
                plan.setdefault('previous_errors',[]).append(plan.pop('error'))
            await save(status='rendering')
            if edition=='standard':
                first=next(c for c in chapters if c['chapter_num']==1)
                prepared=await render_standard_audiobook_video(series,first,path)
                await save(**pilot_metadata(series,edition=edition,full_chapter=prepared['full_chapter']),
                    full_chapter=prepared['full_chapter'],selected_chars=prepared['selected_chars'],
                    source_chars=prepared['source_chars'],duration_budget=prepared['max_duration_seconds'])
            else:await render_longform_audiobook_video(series,chapters,path)
            from mutagen.mp4 import MP4
            duration=MP4(path).info.length
            await save(status='rendered',duration_seconds=duration,rendered_bytes=path.stat().st_size)
            if edition=='standard' and duration>=900:raise ValueError('Standard audiobook must be strictly under 15 minutes')
            if duration>900 and not allow_longer_than_15:
                await save(status='pending',defer_reason='YouTube: uploads over 15 minutes are not enabled')
                return plan
            async with async_playwright() as playwright:
                browser=await playwright.chromium.connect_over_cdp(await ensure_chrome(background=True))
                context=browser.contexts[0]
                page=await context.new_page()
                try:
                    async def started():await save(status='upload_started')
                    async def draft(value):await save(status=value['state'],video_id=value['video_id'],draft_url=value['url'])
                    async def submitted():await save(status='submitted')
                    receipt=await upload_in_studio(page,path,plan,plan['description'],kind='long',
                        timeout_seconds=1200,on_upload_started=started,on_draft=draft,on_submit=submitted)
                    # Record confirmed delivery before further UI actions. Audio/art never
                    # waits on comments or feature eligibility after arriving at YouTube.
                    await save(**receipt,status='uploaded',published_at=datetime.now(timezone.utc).isoformat())
                    removed=delete_video(path,root=directory)
                    await save(mp4_deleted=True,bytes_deleted=sum(r['bytes'] for r in removed))
                    try:
                        details=await verify_persisted_video_details(page,receipt['video_id'],plan['title'],plan['description'])
                        await save(**details)
                    except Exception as error:
                        await save(audience_verified=False,metadata_error=type(error).__name__)
                    try:
                        comment=await post_first_comment(page,receipt['video_id'],db=db,text=plan['comment_text'])
                        pinned=(await pin_author_comment(page,receipt['video_id'],plan['comment_text'],db=db)) if advanced_features else {
                            'comment_pinned':False,'pin_status':'pending_advanced_features'}
                        await save(**comment,**pinned)
                        await save(external_links_clickable=advanced_features)
                    except Exception as error:
                        await save(comment_error=type(error).__name__)
                finally:
                    await page.close()  # Only our dedicated tab; never close owner's Chrome.
            return plan
        except DailyUploadLimit as error:
            from datetime import timedelta
            until=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
            await db.set_state('youtube_quota_cooldown_until',until)
            await save(status='quota_blocked',retry_after=until,error=str(error))
            return plan
        except Exception as error:
            status='rendering_failed' if plan['status']=='rendering' else plan['status']
            await save(status=status,error=type(error).__name__+': '+str(error)[:500])
            return plan
        finally:
            # This is a disposable render cache. On ambiguity the persisted draft ID
            # prevents a second upload; inspect the server instead of keeping an MP4.
            if path.exists():
                delete_video(path,root=directory)
            if plan.get('status')=='uploaded' and not path.exists():
                await save(mp4_deleted=True)


async def reconcile_longform(db,series_id,*,edition='full'):
    """Read only persisted public state; no upload or Publish click is retried."""
    from app.youtube_uploader import (async_playwright,ensure_chrome,studio_page,
        wait_for_public_video,verify_persisted_video_details)
    key=publication_key(series_id,edition=edition)
    plan=json.loads(await db.get_state(key) or '{}')
    if not plan.get('video_id'):raise ValueError('No saved longform draft ID')
    async with media_slot():
        async with async_playwright() as playwright:
            browser=await playwright.chromium.connect_over_cdp(await ensure_chrome(background=True))
            page=await browser.contexts[0].new_page()
            try:
                receipt=await wait_for_public_video(page,plan['video_id'],plan['title'],kind='long',timeout_seconds=60)
                details=await verify_persisted_video_details(page,plan['video_id'],plan['title'],plan['description'])
                plan.update({**receipt,**details,'status':'uploaded'})
                plan.setdefault('published_at',datetime.now(timezone.utc).isoformat())
                await db.set_state(key,json.dumps(plan,ensure_ascii=False))
            finally:await page.close()
    return plan
