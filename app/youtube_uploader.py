"""Real Chrome/CDP Studio publishing with explicit audience and remote receipts."""

import asyncio
import json
import re
import subprocess
import os
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime,timedelta,timezone

import httpx
from playwright.async_api import async_playwright, expect

CHROME = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
PROFILE = Path('browser_profiles/youtube').resolve()
NOT_KIDS = 'VIDEO_MADE_FOR_KIDS_NOT_MFK'
UPLOAD_COMPLETE_PATTERN = re.compile(r'(Загрузка(?: видео)? завершена|Upload complete|Проверка завершена|Checks complete|Обработка завершена)', re.I)
DAILY_LIMIT_PATTERN=re.compile(r'Загрузка недоступна[\s\S]{0,180}24 час|Daily upload limit|upload this video.{0,50}24 hours',re.I)
MAX_YOUTUBE_UPLOADS_PER_24H = 4


class DailyUploadLimit(RuntimeError):
    pass


async def youtube_blocked_until(db):
    now=datetime.now(timezone.utc)
    active=[]
    for key in ('youtube_quota_cooldown_until','youtube_upload_blocked_until'):
        value=await db.get_state(key)
        if value:
            try:
                until=datetime.fromisoformat(value.replace('Z','+00:00'))
                if until.tzinfo is None:until=until.replace(tzinfo=timezone.utc)
                if until>now:active.append(until)
            except ValueError:
                continue
    uploads=await db._all("SELECT published_at FROM short_publications WHERE provider='youtube' AND julianday(published_at)>=julianday('now')-1 ORDER BY julianday(published_at)")
    if len(uploads)>=MAX_YOUTUBE_UPLOADS_PER_24H:
        oldest=datetime.fromisoformat(uploads[-MAX_YOUTUBE_UPLOADS_PER_24H]['published_at'].replace('Z','+00:00'))
        if oldest.tzinfo is None:oldest=oldest.replace(tzinfo=timezone.utc)
        until=oldest+timedelta(hours=24)
        if until>now:
            active.append(until)
    if not active:
        return None
    effective=max(active).isoformat()
    if await db.get_state('youtube_quota_cooldown_until') != effective:
        await db.set_state('youtube_quota_cooldown_until',effective)
    return effective
FIRST_COMMENT=('📖 А как бы вы поступили на месте героя в этот момент? Полные главы на русском языке уже ждут '
    'в нашем Телеграм-боте — просто вбейте в поиске Telegram: NovelPulseBot '
    '(или переходите в канал @novelpulse_litrpg) 🔥')
# YouTube renders emoji as <img>, so DOM text omits them even when the comment is posted.
COMMENT_MATCH=FIRST_COMMENT.removeprefix('📖 ').removesuffix(' 🔥')


async def post_first_comment(page,video_id,*,db=None):
    """At most one submit; three pre-submit UI retries share a 15-second budget."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):
        return {'comment_posted':False,'comment_status':'invalid_video_id'}
    key='youtube_comment:'+video_id
    previous=json.loads(await db.get_state(key) or '{}') if db else {}
    if previous.get('comment_posted'):return previous
    submitted=previous.get('comment_status') in ('submitting','uncertain')
    async def save(state,posted=False):
        result={'comment_posted':posted,'comment_status':state,'video_id':video_id}
        if db:await db.set_state(key,json.dumps(result))
        return result
    try:
        async with asyncio.timeout(15):
            await page.goto('https://www.youtube.com/watch?v='+video_id,wait_until='domcontentloaded',timeout=6000)
            for attempt in range(3):
                try:
                    comments=page.locator('ytd-comments#comments').first
                    await comments.wait_for(state='attached',timeout=2500)
                    await comments.scroll_into_view_if_needed(timeout=1500)
                    existing=comments.locator('ytd-comment-thread-renderer').filter(has_text=COMMENT_MATCH).first
                    if submitted or await existing.count():
                        await existing.wait_for(state='visible',timeout=1000)
                        return await save('posted',True)
                    placeholder=comments.locator('#placeholder-area').first
                    await placeholder.click(timeout=2000)
                    editor=comments.locator('#contenteditable-root[contenteditable="true"]').first
                    await editor.fill(FIRST_COMMENT,timeout=1500)
                    submit=comments.locator('#submit-button').get_by_role('button').first
                    await submit.wait_for(state='visible',timeout=1000)
                    # Persist BEFORE clicking. A timeout after submit is never auto-reposted.
                    await save('submitting')
                    submitted=True
                    await submit.click(timeout=1500)
                    await existing.wait_for(state='visible',timeout=3000)
                    return await save('posted',True)
                except Exception:
                    if submitted and attempt==2:return await save('uncertain')
                    if attempt<2:await asyncio.sleep(.5)
        return await save('unavailable')
    except Exception as error:
        result=await save('uncertain' if submitted else 'unavailable')
        result['comment_reason']=type(error).__name__
        return result


async def try_first_comment(page,video_id,db):
    """Comment persistence/UI failures cannot downgrade a verified public upload."""
    try:return await post_first_comment(page,video_id,db=db)
    except Exception as error:
        return {'comment_posted':False,'comment_status':'unavailable','comment_reason':type(error).__name__}


class LoginRequired(RuntimeError):
    pass


def validate_cdp_url(url):
    parsed = urlparse(url)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.port != 9222
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('CDP is restricted to localhost:9222')
    return url.rstrip('/')


async def ensure_chrome(cdp_url='http://127.0.0.1:9222', *, launch=True, background=False):
    url = validate_cdp_url(cdp_url)
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        try:
            response = await client.get(url + '/json/version')
            response.raise_for_status()
            if response.json().get('Browser', '').startswith('Chrome/'):
                return url
        except (httpx.HTTPError, ValueError):
            pass
        if not launch or not CHROME.is_file():
            raise LoginRequired('Chrome CDP недоступен. Запустите Chrome с отдельным профилем и портом 9222.')
        PROFILE.mkdir(parents=True, exist_ok=True)
        # Actual installed Chrome; no copied cookies, modified TLS or insecure login flags.
        startup = None
        if background and os.name == 'nt':
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 6  # SW_MINIMIZE, real installed Chrome with the saved login.
        subprocess.Popen([str(CHROME), '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9222',
                          '--user-data-dir=' + str(PROFILE), '--no-first-run', 'https://studio.youtube.com'], startupinfo=startup)
        for _ in range(20):
            await asyncio.sleep(.5)
            try:
                response = await client.get(url + '/json/version')
                response.raise_for_status()
                if response.json().get('Browser', '').startswith('Chrome/'):
                    return url
            except (httpx.HTTPError, ValueError):
                pass
    raise LoginRequired('Chrome не открыл локальный порт 9222')


async def studio_page(browser):
    pages = [p for context in browser.contexts for p in context.pages]
    page = next((p for p in pages if p.url.startswith('https://studio.youtube.com')), None)
    if page is None:
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
    await page.goto('https://studio.youtube.com', wait_until='domcontentloaded', timeout=60000)
    try:
        await page.get_by_role('button', name=re.compile(r'^(Создать|Create)$')).first.wait_for(state='visible', timeout=15000)
    except Exception as error:
        raise LoginRequired('В отдельном Chrome-профиле нужен ручной вход в YouTube Studio. После входа повторите команду.') from error
    if 'accounts.google.com' in page.url:
        raise LoginRequired('Требуется ручной вход в Google; автоматический ввод пароля не выполняется.')
    return page


async def minimize_studio(browser, page):
    session = await page.context.new_cdp_session(page)
    try:
        target = await session.send('Target.getTargetInfo')
        window = await session.send('Browser.getWindowForTarget', {'targetId': target['targetInfo']['targetId']})
        await session.send('Browser.setWindowBounds', {'windowId': window['windowId'], 'bounds': {'windowState': 'minimized'}})
    finally:
        await session.detach()


async def select_not_kids(page):
    radio = page.locator(f'[name="{NOT_KIDS}"]').first
    await radio.wait_for(state='visible', timeout=20000)
    await radio.click()
    if not await radio.evaluate("e => e.checked === true || e.hasAttribute('checked') || e.getAttribute('aria-checked') === 'true'"):
        raise RuntimeError('YouTube не подтвердил выбор «Нет, это видео не для детей»')


async def audit_existing_audience(page, shorts):
    """Reopen each saved video and verify the server-persisted audience selection."""
    channel_url = page.url.split('/channel/')[0] + '/channel/' + page.url.split('/channel/')[1].split('/')[0] if '/channel/' in page.url else ''
    results = []
    if not channel_url:
        return [{'status': 'unavailable', 'reason': 'Страница канала не найдена'}]
    for short in shorts:
        try:
            await page.goto(channel_url + '/videos/upload', wait_until='domcontentloaded')
            await page.get_by_role('tab', name='Shorts', exact=True).click()
            matches = page.locator('ytcp-video-row').filter(has_text=short['title'][:20])
            await matches.first.wait_for(state='visible', timeout=15000)
            if await matches.count() != 1:
                raise RuntimeError('Название неоднозначно; проверьте видео вручную')
            row = matches.first
            await row.wait_for(state='visible', timeout=15000)
            link = row.locator('a[href*="/video/"][href*="/edit"]').first
            await link.click()
            await select_not_kids(page)
            save = page.get_by_role('button', name=re.compile(r'^(Сохранить|Save)$')).first
            if await save.is_enabled():
                await save.click()
                await expect(save).to_be_disabled(timeout=20000)
            await page.reload(wait_until='domcontentloaded')
            radio = page.locator(f'[name="{NOT_KIDS}"]').first
            await radio.wait_for(state='visible')
            checked = await radio.evaluate("e => e.checked === true || e.hasAttribute('checked') || e.getAttribute('aria-checked') === 'true'")
            if not checked:
                raise RuntimeError('Настройка аудитории не сохранилась')
            video_id = page.url.split('/video/')[1].split('/')[0]
            results.append({'short_id': short['id'], 'status': 'verified_not_kids', 'video_id': video_id})
        except Exception as error:
            results.append({'short_id': short['id'], 'status': 'needs_review', 'reason': type(error).__name__})
    return results


async def upload_in_studio(page, path, short, description, *, timeout_seconds=900, on_submit=None,
                           on_draft=None, on_upload_started=None, draft=None):
    if not draft:
        await page.goto('https://studio.youtube.com', wait_until='domcontentloaded')
        await page.get_by_role('button', name=re.compile(r'^(Создать|Create)$')).first.click()
        await page.get_by_text(re.compile(r'^(Добавить видео|Upload videos)$')).filter(visible=True).first.click()
        chooser = page.locator('input[type=file]').first
        await chooser.wait_for(state='attached')
        if on_upload_started:
            await on_upload_started()
        await chooser.set_input_files(str(path))
    else:
        fields = page.locator('ytcp-social-suggestions-textbox #textbox')
        current_id = None
        visible_link = page.locator('ytcp-video-info a[href*="youtube"], ytcp-video-info a[href*="youtu.be"]').filter(visible=True).first
        if await fields.nth(0).is_visible() and await visible_link.count():
            current_href = await visible_link.get_attribute('href')
            current_id = urlparse(current_href).path.rstrip('/').rsplit('/', 1)[-1]
        if current_id != draft['video_id']:
            await page.goto('https://studio.youtube.com', wait_until='domcontentloaded')
            await page.get_by_role('button', name=re.compile(r'^(Создать|Create)$')).first.wait_for(state='visible')
            base = page.url.split('/channel/')[0] + '/channel/' + page.url.split('/channel/')[1].split('/')[0]
            await page.goto(base + '/videos/upload', wait_until='domcontentloaded')
            await page.get_by_role('tab', name='Shorts', exact=True).click()
            await page.locator(f'a[href="/video/{draft["video_id"]}/edit"]').filter(visible=True).first.click()
    fields = page.locator('ytcp-social-suggestions-textbox #textbox')
    await fields.nth(0).wait_for(state='visible')
    await fields.nth(0).fill(short['title'])
    await fields.nth(1).fill(description)
    if await fields.nth(0).inner_text() != short['title']:
        raise RuntimeError('YouTube не сохранил точное название')
    await select_not_kids(page)
    video_link = page.locator('ytcp-video-info a[href*="youtube"], ytcp-video-info a[href*="youtu.be"]').filter(visible=True).first
    try:
        await video_link.wait_for(state='visible')
    except Exception:
        if await page.get_by_text(DAILY_LIMIT_PATTERN).filter(visible=True).count():
            raise DailyUploadLimit('YouTube установил суточный лимит: загрузка доступна после проверки канала владельцем или сброса лимита') from None
        raise
    draft_url = await video_link.get_attribute('href')
    parsed = urlparse(draft_url)
    draft_id = parsed.path.rstrip('/').rsplit('/', 1)[-1]
    if parsed.hostname not in ('youtube.com', 'www.youtube.com', 'youtu.be') or not re.fullmatch(r'[A-Za-z0-9_-]{11}', draft_id):
        raise RuntimeError('YouTube не вернул ID черновика')
    if draft and draft_id != draft['video_id']:
        raise RuntimeError('Открыт другой черновик; публикация отменена')
    if on_draft:
        await on_draft({'video_id': draft_id, 'url': draft_url, 'state': 'draft'})
    await page.get_by_text(UPLOAD_COMPLETE_PATTERN).filter(visible=True).first.wait_for(
        state='visible', timeout=timeout_seconds * 1000)
    for _ in range(3):
        await page.get_by_role('button', name=re.compile(r'^(Далее|Next)$')).first.click()
    public = page.locator('[name="PUBLIC"]').first
    await public.wait_for(state='visible')
    await public.click()
    if not await public.evaluate("e => e.checked === true || e.hasAttribute('checked') || e.getAttribute('aria-checked') === 'true'"):
        raise RuntimeError('Открытый доступ не выбран')
    if on_submit:
        await on_submit()  # From here an ambiguous failure MUST NOT trigger a duplicate.
    await page.get_by_role('button', name=re.compile(r'^(Опубликовать|Publish)$')).first.click()
    return await wait_for_public_video(page, draft_id, short['title'], timeout_seconds=timeout_seconds)


async def wait_for_public_video(page, video_id, title='', *, timeout_seconds=900):
    """Poll persisted Studio state; a processing dialog is not a publication receipt."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Invalid YouTube video ID')
    if '/channel/' not in page.url:
        await page.goto('https://studio.youtube.com', wait_until='domcontentloaded')
        await page.get_by_role('button', name=re.compile(r'^(Создать|Create)$')).first.wait_for(state='visible')
    channel_url = page.url.split('/channel/')[0] + '/channel/' + page.url.split('/channel/')[1].split('/')[0]
    for _ in range(max(1, timeout_seconds // 5)):
        await page.goto(channel_url + '/videos/upload', wait_until='domcontentloaded')
        await page.get_by_role('tab', name='Shorts', exact=True).click()
        row = page.locator('ytcp-video-row').filter(has=page.locator(f'a[href*="/video/{video_id}/edit"]')).first
        try:
            await row.wait_for(state='visible', timeout=5000)
            text = await row.inner_text()
            status = text.replace(title, '') if title else text
            if (re.search(r'Для всех|Открытый доступ|Public', status)
                    and re.search(r'Опубликовано|Published', status)
                    and not re.search(r'Обрабатывается|Обработка|Processing|Uploading|Загрузка', status, re.I)):
                return {'video_id':video_id, 'url':'https://www.youtube.com/shorts/'+video_id,
                        'visibility':'public', 'audience':NOT_KIDS}
        except Exception:
            pass
        await asyncio.sleep(5)
    raise RuntimeError('Запрос публикации принят, но готовый публичный ролик не подтверждён; новая загрузка запрещена')


async def upload_short_youtube(db, *, short_id=None, cdp_url='http://127.0.0.1:9222', launch=True, root=None,
                               resume=False, claimed_short=None, manage_slot=True, keep_media=False,
                               background=False, settings=None, rendered_path=None):
    from app.shorts import SHORTS_DIR, ensure_short_video
    from app.storage_cleanup import media_slot, delete_video
    from app.pipeline import failure_reason
    root = root or SHORTS_DIR
    blocked=await youtube_blocked_until(db)
    if blocked:return {'status':'quota_blocked','retry_after':blocked,'error':'Суточный лимит YouTube: ожидается ручная проверка канала или сброс лимита'}
    submitted = False
    upload_started = False
    draft = None
    async def mark_upload_started():
        nonlocal upload_started
        upload_started = True
        import json
        await db.set_state(f'youtube_draft:{short["id"]}', json.dumps({'state': 'upload_started'}))
    async def save_draft(value):
        nonlocal draft
        draft = value
        import json
        await db.set_state(f'youtube_draft:{short["id"]}', json.dumps(draft))
    async def mark_submitted():
        nonlocal submitted
        submitted = True
        await save_draft({**draft, 'state': 'submitted'})
    async with media_slot() if manage_slot else nullcontext():
        async with async_playwright() as playwright:
            try:
                url = await ensure_chrome(cdp_url, launch=launch, background=background)
                browser = await playwright.chromium.connect_over_cdp(url)
                pages = [page for context in browser.contexts for page in context.pages]
                page = next((p for p in pages if p.url.startswith('https://studio.youtube.com')), None) if resume else None
                if page is None:
                    page = await studio_page(browser)
                if background:
                    await minimize_studio(browser, page)
            except LoginRequired as error:
                return {'status': 'login_required', 'error': str(error), 'audience_audit': 'not_accessible'}
            previous = [s for s in await db.list_shorts() if s['series_id'] == 1 and s['episode_num'] in (1, 2)] if claimed_short is None else []
            audit = [{'status': 'resume_existing_draft'}] if resume else await audit_existing_audience(page, previous)
            if not resume:
                import json
                await db.set_state('youtube_audience_audit', json.dumps(audit))
            if resume:
                import json
                draft = json.loads(await db.get_state(f'youtube_draft:{short_id}') or '{}')
                short = await db.get_short(short_id)
                if not draft or draft.get('state') != 'draft' or not short or short['status'] != 'sending':
                    raise ValueError('Возобновление допускается только для подтверждённого неопубликованного черновика')
                short['_previous_status'] = 'uploaded' if any(r['short_id'] == short_id for r in await db.short_publications()) else 'pending'
            else:
                short = claimed_short or await db.claim_short(short_id, provider='youtube')
            if not short:
                return {'status': 'idle', 'audience_audit': audit}
            try:
                if rendered_path is None:
                    path = await ensure_short_video(db, short, 'NovelPulseBot', root=root)
                else:
                    from app.autopilot import video_ready
                    path = Path(rendered_path).resolve()
                    if not path.is_relative_to(Path(root).resolve()) or not video_ready(path):
                        raise ValueError('Нет проверенного JIT MP4 для передачи в YouTube')
                from app.config import Settings
                settings = settings or Settings()
                description = short['description'] + '\n\nЧитать продолжение в Telegram: https://t.me/novelpulse_litrpg и в боте https://t.me/NovelPulseBot\n' + (settings.web_base_url or '')
                receipt = await upload_in_studio(page, path, short, description, on_submit=mark_submitted,
                                                 on_draft=save_draft, on_upload_started=mark_upload_started, draft=draft)
                await db.record_short_publication(short['id'], 'youtube', 'studio', receipt['video_id'], finalize=not keep_media)
                removed = [] if keep_media else delete_video(path, root=root)
                comment=await try_first_comment(page,receipt['video_id'],db)
                return {'status': 'uploaded', 'short_id': short['id'], **receipt, **comment, 'audience_audit': audit,
                        'bytes_deleted': sum(p['bytes'] for p in removed), 'mp4_deleted': sum(p['mp4'] for p in removed)}
            except DailyUploadLimit as error:
                until=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
                await db.set_state('youtube_upload_blocked_until',until)
                await db.set_state('youtube_quota_cooldown_until',until)
                await db.set_state(f'youtube_draft:{short["id"]}',json.dumps({'state':'quota_blocked','retry_after':until}))
                await db.finish_short(short['id'],'pending')
                removed=[] if keep_media else delete_video(path,root=root)
                return {'status':'quota_blocked','short_id':short['id'],'retry_after':until,'error':str(error),
                    'mp4_deleted':sum(p['mp4'] for p in removed)}
            except Exception as error:
                row = await db.get_short(short['id'])
                status = ('uploaded' if row['status'] == 'uploaded' else 'uncertain' if submitted
                          else 'sending' if draft or upload_started else short['_previous_status'])
                if row['status'] != 'uploaded' and status != 'sending':
                    await db.finish_short(short['id'], status)
                return {'status': 'uncertain' if submitted else 'resume_required' if draft or upload_started else 'failed', 'queue_status': status,
                        'error': failure_reason(error), 'short_id': short['id'], 'audience_audit': audit}
            # Leaving the context disconnects Playwright; never close the owner's Chrome.


async def reconcile_youtube_publication(db, short_id, *, root=None, manage_slot=True, keep_media=False):
    """Read a submitted video's saved public state. Never uploads or clicks Publish."""
    import json
    from app.shorts import SHORTS_DIR
    from app.storage_cleanup import media_slot, delete_video
    root = root or SHORTS_DIR
    async with media_slot() if manage_slot else nullcontext():
        short = await db.get_short(short_id)
        draft = json.loads(await db.get_state(f'youtube_draft:{short_id}') or '{}')
        if not short or not re.fullmatch(r'[A-Za-z0-9_-]{11}', draft.get('video_id', '')) or draft.get('state') != 'submitted':
            raise ValueError('Нет сохранённого отправленного черновика для сверки')
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(await ensure_chrome(launch=False))
            page = await studio_page(browser)
            video_id = draft['video_id']
            await wait_for_public_video(page, video_id, short['title'])
            await db.record_short_publication(short_id, 'youtube', 'studio', video_id, finalize=not keep_media)
            removed = [] if keep_media else delete_video(short['video_path'], root=root)
            comment=await try_first_comment(page,video_id,db)
            return {'status': 'uploaded', 'short_id': short_id, 'video_id': video_id,
                    **comment,
                    'url': 'https://youtube.com/shorts/' + video_id, 'visibility': 'public',
                    'bytes_deleted': sum(p['bytes'] for p in removed), 'mp4_deleted': sum(p['mp4'] for p in removed)}


async def audit_youtube_channel(db):
    import json
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(await ensure_chrome(launch=False))
        page = await studio_page(browser)
        rows = [s for s in await db.list_shorts() if s['series_id'] == 1 and s['episode_num'] in (1, 2, 3)]
        result = await audit_existing_audience(page, rows)
        await db.set_state('youtube_audience_audit', json.dumps(result))
        for item in result:
            if item['status'] == 'verified_not_kids':
                await db.record_short_publication(item['short_id'], 'youtube', 'studio', item['video_id'])
        return result
