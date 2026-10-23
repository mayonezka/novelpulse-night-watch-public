"""Real Chrome/CDP Studio publishing with explicit audience and remote receipts."""

import asyncio
import json
import re
import subprocess
import os
import logging
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from datetime import datetime,timedelta,timezone

import httpx
from playwright.async_api import async_playwright, expect

CHROME = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
PROFILE = Path('browser_profiles/youtube').resolve()
NOT_KIDS = 'VIDEO_MADE_FOR_KIDS_NOT_MFK'
PUBLICATION_TIMEOUT_SECONDS = 180
CONFIRMATION_TIMEOUT_MS = 90_000
PUBLICATION_SIGNAL_PATTERN = re.compile(r'(Видео опубликовано|Video published|Обработка|Processing)',re.I)
logger=logging.getLogger(__name__)
UPLOAD_COMPLETE_PATTERN = re.compile(r'(Загрузка(?: видео)? завершена|Upload complete|Проверка завершена|Checks complete|Обработка завершена)', re.I)
DAILY_LIMIT_PATTERN=re.compile(r'Загрузка недоступна[\s\S]{0,180}24 час|Daily upload limit|upload this video.{0,50}24 hours',re.I)
from app.scheduling import MAX_YOUTUBE_UPLOADS_PER_24H, youtube_blocked_until
from app.privacy import sanitize_public_text, assert_public_safe, anonymous_series_url


class DailyUploadLimit(RuntimeError):
    pass


FIRST_COMMENT=('📖 А как бы вы поступили на месте героя в этот момент? Полные главы на русском языке уже ждут '
    'в нашем Телеграм-боте — просто вбейте в поиске Telegram: NovelPulseBot '
    '(или переходите в канал @novelpulse_litrpg) 🔥')
# YouTube renders emoji as <img>, so DOM text omits them even when the comment is posted.
COMMENT_MATCH=FIRST_COMMENT.removeprefix('📖 ').removesuffix(' 🔥')


async def post_first_comment(page,video_id,*,db=None,text=FIRST_COMMENT):
    """At most one submit; three pre-submit UI retries share a 15-second budget."""
    text = sanitize_public_text(text)
    assert_public_safe(text)
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):
        return {'comment_posted':False,'comment_status':'invalid_video_id'}
    key='youtube_comment:'+video_id
    previous=json.loads(await db.get_state(key) or '{}') if db else {}
    if previous.get('comment_posted'):return previous
    submitted=previous.get('comment_status') in ('submitting','uncertain')
    match_text=COMMENT_MATCH if text==FIRST_COMMENT else text.removeprefix('📖 ').strip()
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
                    existing=comments.locator('ytd-comment-thread-renderer').filter(has_text=match_text).first
                    if submitted or await existing.count():
                        await existing.wait_for(state='visible',timeout=1000)
                        return await save('posted',True)
                    placeholder=comments.locator('#placeholder-area').first
                    await placeholder.click(timeout=2000)
                    editor=comments.locator('#contenteditable-root[contenteditable="true"]').first
                    await editor.fill(text,timeout=1500)
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


async def find_existing_studio_video(page, short, draft=None):
    """Read visible Studio rows before any file transfer; ambiguous matches fail closed."""
    await page.goto('https://studio.youtube.com', wait_until='domcontentloaded')
    await page.get_by_role('button', name=re.compile(r'^(Создать|Create)$')).first.wait_for(state='visible')
    if '/channel/' not in page.url:
        raise RuntimeError('Studio channel not available')
    base = page.url.split('/channel/')[0]+'/channel/'+page.url.split('/channel/')[1].split('/')[0]
    await page.goto(base+'/videos/short',wait_until='domcontentloaded')
    await page.locator('ytcp-video-row').first.wait_for(state='visible')
    # Read the full title attribute, not a truncated title or description match.
    matches = page.locator('ytcp-video-row').filter(has=page.locator(f'a[href*="/video/{draft["video_id"]}/edit"]')) if draft and draft.get('video_id') else None
    if matches is None or not await matches.count():
        matches = page.locator('ytcp-video-row').filter(has_text=short['title'])
    count = await matches.count()
    if not count:
        if draft:
            raise RuntimeError('Saved upload is absent from visible Studio rows; no duplicate transfer allowed')
        return None
    if count != 1:
        raise RuntimeError('Multiple matching Studio videos; no duplicate transfer allowed')
    row = matches.first
    status = (await row.inner_text()).replace(short['title'],'')
    if re.search(r'Черновик|Draft',status):
        await row.get_by_text(re.compile(r'^(Редактировать черновик|Edit draft)$')).click()
        link=page.locator('ytcp-video-info a[href*="youtube"], ytcp-video-info a[href*="youtu.be"]').filter(visible=True).first
        video_id,url=await studio_video_reference(page,link)
        phase='draft_uploaded'
    else:
        link=row.locator('a[href*="/video/"][href*="/edit"]').first
        href=await link.get_attribute('href')
        match=re.search(r'/video/([A-Za-z0-9_-]{11})/edit',href or '')
        video_id=match[1] if match else None
        url='https://www.youtube.com/shorts/'+(video_id or '')
        if not re.search(r'Для всех|Открытый доступ|Public',status,re.I) or re.search(r'Черновик|Draft|Ограниченный доступ|Private',status,re.I):
            raise RuntimeError('Existing video is not public and is not an editable draft')
        phase='published'
    if not video_id or (draft and draft.get('video_id') and draft['video_id']!=video_id):
        raise RuntimeError('Existing Studio video ID mismatch')
    return {'video_id':video_id,'url':url,'state':'draft' if phase=='draft_uploaded' else 'published','phase':phase}


async def upload_in_studio(page, path, short, description, *, timeout_seconds=180, **kwargs):
    """One wall-clock budget covers all navigation, checks and publication waits."""
    async with asyncio.timeout(min(PUBLICATION_TIMEOUT_SECONDS, timeout_seconds)):
        return await _upload_in_studio(page,path,short,description,timeout_seconds=timeout_seconds,**kwargs)


async def _upload_in_studio(page, path, short, description, *, timeout_seconds=180, on_submit=None,
                           on_draft=None, on_upload_started=None, draft=None, kind='short'):
    # Longform uses embedded cover-like frames only. Never invoke a custom
    # thumbnail picker, feature verification dialog or a separate image upload.
    if kind == 'long':
        from app.longform_video import in_frame_longform_metadata
        short = in_frame_longform_metadata(short)
    short = {**short, 'title': sanitize_public_text(short['title'])}
    description = sanitize_public_text(description)
    assert_public_safe((short['title'], description))
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
        # Opening a draft updates the URL before its form is painted. Do not
        # navigate away merely because the first visibility probe was too early.
        await fields.nth(0).wait_for(state='visible',timeout=90000)
        current_id = None
        visible_link = page.locator('ytcp-video-info a[href*="youtube"], ytcp-video-info a[href*="youtu.be"]').filter(visible=True).first
        if ('udvid' in parse_qs(urlparse(page.url).query)
                or re.fullmatch(r'/video/[A-Za-z0-9_-]{11}/edit/?',urlparse(page.url).path)
                or await fields.nth(0).is_visible()):
            current_id,_ = await studio_video_reference(page,visible_link)
        if current_id != draft['video_id']:
            await page.goto('https://studio.youtube.com', wait_until='domcontentloaded')
            await page.get_by_role('button', name=re.compile(r'^(Создать|Create)$')).first.wait_for(state='visible')
            base = page.url.split('/channel/')[0] + '/channel/' + page.url.split('/channel/')[1].split('/')[0]
            await page.goto(base + '/videos/upload', wait_until='domcontentloaded')
            await page.get_by_role('tab', name='Shorts' if kind=='short' else re.compile(r'^(Видео|Videos)$'), exact=True).click()
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
        draft_id,draft_url=await studio_video_reference(page,video_link)
    except Exception:
        if await page.get_by_text(DAILY_LIMIT_PATTERN).filter(visible=True).count():
            raise DailyUploadLimit('YouTube установил суточный лимит: загрузка доступна после проверки канала владельцем или сброса лимита') from None
        raise
    if not draft_id:
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
    return await wait_for_publication_confirmation(page,draft_id,short['title'],
                                                  timeout_seconds=timeout_seconds,kind=kind)


async def wait_for_publication_confirmation(page,video_id,title='',*,timeout_seconds=180,kind='short'):
    """Tolerate a missing toast; verify the exact public row in a separate tab.

    Processing text and a saved ID are discovery signals, not public-delivery
    receipts. Never navigate the upload tab away from an in-flight Publish.
    """
    dialog=page.locator('ytcp-uploads-dialog').first
    tasks=[]
    try:
        if await dialog.is_visible():
            tasks=[asyncio.create_task(page.get_by_text(PUBLICATION_SIGNAL_PATTERN).filter(visible=True).first.wait_for(
                       state='visible',timeout=CONFIRMATION_TIMEOUT_MS)),
                   asyncio.create_task(dialog.wait_for(state='hidden',timeout=CONFIRMATION_TIMEOUT_MS))]
            # Either the broad completion/processing signal or a closed dialog
            # lets us check persisted visibility. A 90s toast timeout does too.
            await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            if not task.done():task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
    verification=await page.context.new_page()
    try:
        await verification.goto('https://studio.youtube.com',wait_until='domcontentloaded')
        await verification.get_by_role('button',name=re.compile(r'^(Создать|Create)$')).first.wait_for(state='visible')
        kwargs={'timeout_seconds':timeout_seconds}
        if kind!='short':kwargs['kind']=kind
        return await wait_for_public_video(verification,video_id,title,**kwargs)
    finally:
        await verification.close()


async def studio_video_reference(page,link):
    """A draft ID is identity only, never proof of public publication."""
    parsed=urlparse(page.url)
    if parsed.scheme=='https' and parsed.hostname=='studio.youtube.com':
        match=re.fullmatch(r'/video/([A-Za-z0-9_-]{11})/edit/?',parsed.path)
        value=match[1] if match else parse_qs(parsed.query).get('udvid',[''])[0]
        if re.fullmatch(r'[A-Za-z0-9_-]{11}',value):
            return value,'https://www.youtube.com/watch?v='+value
    try:
        await link.wait_for(state='visible',timeout=90000)
        url=await link.get_attribute('href')
        video_id=youtube_video_id(url)
        if video_id:return video_id,url
    except TimeoutError:
        pass
    except Exception as error:
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        if not isinstance(error,PlaywrightTimeout):raise
    parsed=urlparse(page.url)
    if parsed.scheme=='https' and parsed.hostname=='studio.youtube.com':
        match=re.fullmatch(r'/video/([A-Za-z0-9_-]{11})/edit/?',parsed.path)
        candidate=match[1] if match else parse_qs(parsed.query).get('udvid',[''])[0]
        if re.fullmatch(r'[A-Za-z0-9_-]{11}',candidate):
            return candidate,'https://www.youtube.com/watch?v='+candidate
    dialogs=page.locator('ytcp-uploads-dialog')
    ids=set()
    for dialog in await dialogs.all():
        if not await dialog.is_visible():continue
        for attr in ('video-id','data-video-id'):
            value=await dialog.get_attribute(attr)
            if value and re.fullmatch(r'[A-Za-z0-9_-]{11}',value):ids.add(value)
    if len(ids)==1:
        video_id=ids.pop();return video_id,'https://www.youtube.com/watch?v='+video_id
    raise RuntimeError('Studio video ID unavailable or ambiguous after 90s; duplicate upload forbidden')


def youtube_video_id(url):
    parsed=urlparse(url or '')
    if parsed.scheme!='https' or parsed.hostname not in ('youtube.com','www.youtube.com','youtu.be'):
        return None
    value=parse_qs(parsed.query).get('v',[''])[0] if parsed.path=='/watch' else parsed.path.rstrip('/').rsplit('/',1)[-1]
    return value if re.fullmatch(r'[A-Za-z0-9_-]{11}',value) else None


async def wait_for_public_video(page, video_id, title='', *, timeout_seconds=180, kind='short'):
    async with asyncio.timeout(min(PUBLICATION_TIMEOUT_SECONDS,timeout_seconds)):
        return await _wait_for_public_video(page,video_id,title,timeout_seconds=timeout_seconds,kind=kind)


async def _wait_for_public_video(page, video_id, title='', *, timeout_seconds=180, kind='short'):
    """Poll persisted Studio state; a processing dialog is not a publication receipt."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('Invalid YouTube video ID')
    if '/channel/' not in page.url:
        await page.goto('https://studio.youtube.com', wait_until='domcontentloaded')
        await page.get_by_role('button', name=re.compile(r'^(Создать|Create)$')).first.wait_for(state='visible')
    channel_url = page.url.split('/channel/')[0] + '/channel/' + page.url.split('/channel/')[1].split('/')[0]
    for _ in range(max(1, timeout_seconds // 5)):
        await page.goto(channel_url + '/videos/upload', wait_until='domcontentloaded')
        await page.get_by_role('tab', name='Shorts' if kind=='short' else re.compile(r'^(Видео|Videos)$'), exact=True).click()
        row = page.locator('ytcp-video-row').filter(has=page.locator(f'a[href*="/video/{video_id}/edit"]')).first
        try:
            await row.wait_for(state='visible', timeout=5000)
            text = await row.inner_text()
            status = text.replace(title, '') if title else text
            if (re.search(r'Для всех|Открытый доступ|Public', status,re.I)
                    and not re.search(r'Черновик|Draft|Ограниченный доступ|Private',status,re.I)):
                return {'video_id':video_id, 'url':('https://www.youtube.com/shorts/' if kind=='short' else 'https://www.youtube.com/watch?v=')+video_id,
                        'visibility':'public', 'audience':NOT_KIDS,
                        'processing':bool(re.search(r'Обрабатывается|Обработка|Processing|Uploading|Загрузка',status,re.I))}
        except Exception:
            pass
        await asyncio.sleep(5)
    raise RuntimeError('Запрос публикации принят, но готовый публичный ролик не подтверждён; новая загрузка запрещена')


async def verify_persisted_video_details(page, video_id, title, description):
    """Reopen persisted editor fields; never infer audience from a publish click."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',video_id):raise ValueError('Invalid video ID')
    await page.goto(f'https://studio.youtube.com/video/{video_id}/edit',wait_until='domcontentloaded')
    radio=page.locator(f'[name="{NOT_KIDS}"]').first
    await radio.wait_for(state='attached',timeout=20000)
    selected=await radio.evaluate("e=>e.checked===true||e.hasAttribute('checked')||e.getAttribute('aria-checked')==='true'")
    fields=page.locator('ytcp-social-suggestions-textbox #textbox')
    await fields.nth(0).wait_for(state='visible',timeout=15000)
    if not selected or (await fields.nth(0).inner_text()).strip()!=title.strip() \
            or (await fields.nth(1).inner_text()).strip()!=description.strip():
        raise RuntimeError('Persisted audience/title/description verification failed')
    return {'audience':NOT_KIDS,'audience_verified':True,'metadata_verified':True}


async def pin_author_comment(page, video_id, text, *, db=None):
    """Pin only the exact author comment; unavailable account features are explicit."""
    key='youtube_comment:'+video_id
    previous=json.loads(await db.get_state(key) or '{}') if db else {}
    if not previous.get('comment_posted'):
        return {'comment_pinned':False,'pin_status':'comment_not_confirmed'}
    try:
        async with asyncio.timeout(25):
            await page.goto('https://www.youtube.com/watch?v='+video_id,wait_until='domcontentloaded')
            comments=page.locator('ytd-comments#comments').first
            await comments.wait_for(state='attached',timeout=8000)
            await comments.scroll_into_view_if_needed()
            match=text.removeprefix('📖 ').strip()
            thread=comments.locator('ytd-comment-thread-renderer').filter(has_text=match).first
            await thread.wait_for(state='visible',timeout=8000)
            if await thread.get_by_text(re.compile(r'Закреплено|Pinned by')).count():
                result={'comment_pinned':True,'pin_status':'verified'}
            else:
                await thread.locator('#action-menu').first.click()
                await page.get_by_text(re.compile(r'^(Закрепить|Pin)$')).filter(visible=True).first.click(timeout=3000)
                # YouTube may present a confirmation sheet before pinning.
                dialog=page.get_by_role('dialog').filter(has_text=re.compile(r'Закрепить|Pin'))
                if await dialog.count():
                    await dialog.get_by_role('button',name=re.compile(r'^(Закрепить|Pin)$')).last.click()
                await thread.get_by_text(re.compile(r'Закреплено|Pinned by')).first.wait_for(state='visible',timeout=5000)
                result={'comment_pinned':True,'pin_status':'verified'}
    except Exception as error:
        result={'comment_pinned':False,'pin_status':'unavailable','pin_reason':type(error).__name__}
    if db:await db.set_state(key,json.dumps({**previous,**result}))
    return result


async def set_short_related_video(page, short_video_id, long_video_id, *, db=None):
    """Use the visible Related Video picker, then reload to verify persistence."""
    for value in (short_video_id,long_video_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',value):raise ValueError('Invalid YouTube video ID')
    result={'short_video_id':short_video_id,'long_video_id':long_video_id,'related_video_set':False}
    try:
        async with asyncio.timeout(45):
            await page.goto(f'https://studio.youtube.com/video/{short_video_id}/edit',wait_until='domcontentloaded')
            related=page.get_by_text(re.compile(r'^(Видео по теме|Related video)$')).first
            await related.wait_for(state='visible',timeout=15000)
            await related.click()
            dialog=page.get_by_role('dialog').filter(visible=True).last
            await dialog.wait_for(state='visible',timeout=5000)
            search=dialog.get_by_role('textbox').first
            await search.fill('https://www.youtube.com/watch?v='+long_video_id)
            choice=dialog.locator(f'[href*="{long_video_id}"], [data-video-id="{long_video_id}"]').first
            await choice.wait_for(state='visible',timeout=8000)
            await choice.click()
            done=dialog.get_by_role('button',name=re.compile(r'^(Готово|Done|Сохранить|Save)$'))
            if await done.count():await done.last.click()
            save=page.get_by_role('button',name=re.compile(r'^(Сохранить|Save)$')).first
            if await save.is_enabled():
                await save.click();await expect(save).to_be_disabled(timeout=15000)
            await page.reload(wait_until='domcontentloaded')
            await page.locator(f'a[href*="{long_video_id}"]').first.wait_for(state='attached',timeout=10000)
            result.update(related_video_set=True,status='verified')
    except Exception as error:
        result.update(status='unavailable',reason=type(error).__name__)
    if db:await db.set_state('youtube_related:'+short_video_id,json.dumps(result))
    return result


async def channel_feature_eligibility(page):
    """Read Studio's actual gates; never start phone/video/identity verification."""
    await page.goto('https://studio.youtube.com',wait_until='domcontentloaded')
    await page.get_by_role('button',name=re.compile(r'^(Настройки|Settings)$')).click()
    dialog=page.locator('#settings-dialog')
    try:
        await dialog.get_by_role('menuitem',name=re.compile(r'^(Канал|Channel)$')).click()
        await dialog.get_by_role('tab',name=re.compile(r'^(Доступность функций|Feature eligibility)$')).click()
        text=' '.join((await dialog.inner_text()).split())
        result={}
        for name,start,end in (('standard','1.','2.'),('intermediate','2.','3.'),('advanced','3.',None)):
            if start not in text:
                raise RuntimeError('Studio feature eligibility has not loaded')
            section=text.split(start,1)[-1]
            if end:section=section.split(end,1)[0]
            state=re.search(r'Включены|Enabled|Можно включить|Eligible',section,re.I)
            result[name]=bool(state and state[0].casefold() in ('включены','enabled'))
        return result
    finally:
        await dialog.get_by_role('button',name=re.compile(r'^(Закрыть|Close)$')).click()


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
        await db.set_state(f'youtube_draft:{short["id"]}', json.dumps({'state': 'upload_started','phase':'rendered'}))
    async def save_draft(value):
        nonlocal draft
        draft = {**value,'phase':'draft_uploaded' if value.get('state') in ('draft','submitted') else value.get('phase','rendered')}
        import json
        await db.set_state(f'youtube_draft:{short["id"]}', json.dumps(draft))
        if value.get('state')=='draft':
            await db.record_youtube_phase(short['id'],'draft_uploaded',recovered=resume)
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
                await db.set_state('youtube_audience_audit', json.dumps(audit))
            short = claimed_short or (await db.get_short(short_id) if resume else await db.claim_short(short_id, provider='youtube'))
            if not short:
                return {'status': 'idle', 'audience_audit': audit}
            short.setdefault('_previous_status','pending')
            saved = json.loads(await db.get_state(f'youtube_draft:{short["id"]}') or '{}')
            if saved.get('video_id') or saved.get('state') in ('draft','submitted','upload_started','published'):
                draft=saved
            path=Path(short['video_path']).resolve()
            try:
                if rendered_path is None and not draft:
                    path = await ensure_short_video(db, short, 'NovelPulseBot', root=root)
                elif rendered_path is not None:
                    from app.autopilot import video_ready
                    path = Path(rendered_path).resolve()
                    if not path.is_relative_to(Path(root).resolve()) or not video_ready(path):
                        raise ValueError('Нет проверенного JIT MP4 для передачи в YouTube')
                if not draft:
                    await db.set_state(f'youtube_draft:{short["id"]}',json.dumps({'state':'rendered','phase':'rendered'}))
                    await db.record_youtube_phase(short['id'],'rendered')
                from app.config import Settings
                settings = settings or Settings()
                series = await db.get_series(short['series_id'])
                description = sanitize_public_text(short['description']) + '\n\nЧитать продолжение в Telegram: https://t.me/novelpulse_litrpg и в боте https://t.me/NovelPulseBot\n' + anonymous_series_url(series)
                async with asyncio.timeout(PUBLICATION_TIMEOUT_SECONDS):
                    existing=await find_existing_studio_video(page,short,draft)
                    recovered=existing is not None
                    resume=resume or recovered
                    if existing and existing['phase']=='published':
                        receipt=await wait_for_public_video(page,existing['video_id'],short['title'])
                    else:
                        if existing:
                            draft=existing
                            await save_draft(existing)
                        elif draft:
                            raise RuntimeError('Unconfirmed prior transfer; new file upload forbidden')
                        receipt = await upload_in_studio(page, path, short, description, on_submit=mark_submitted,
                                                         on_draft=save_draft, on_upload_started=mark_upload_started, draft=draft)
                await db.record_short_publication(short['id'], 'youtube', 'studio', receipt['video_id'], finalize=not keep_media)
                await db.set_state(f'youtube_draft:{short["id"]}',json.dumps({'state':'published','phase':'published',**receipt}))
                await db.record_youtube_phase(short['id'],'published',recovered=recovered)
                removed = [] if keep_media else delete_video(path, root=root)
                comment=await try_first_comment(page,receipt['video_id'],db)
                return {'status': 'uploaded', 'short_id': short['id'], **receipt, **comment, 'audience_audit': audit,
                        'recovered_from_draft':recovered,
                        'bytes_deleted': sum(p['bytes'] for p in removed), 'mp4_deleted': sum(p['mp4'] for p in removed)}
            except DailyUploadLimit as error:
                await db.record_youtube_phase(short['id'],'error',error_class=type(error).__name__)
                until=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
                await db.set_state('youtube_upload_blocked_until',until)
                await db.set_state('youtube_quota_cooldown_until',until)
                await db.set_state(f'youtube_draft:{short["id"]}',json.dumps({**(draft or {}),'state':'quota_blocked','retry_after':until}))
                await db.finish_short(short['id'],'pending')
                removed=[] if keep_media else delete_video(path,root=root)
                return {'status':'quota_blocked','short_id':short['id'],'retry_after':until,'error':str(error),
                    'mp4_deleted':sum(p['mp4'] for p in removed)}
            except Exception as error:
                await db.record_youtube_phase(short['id'],'error',error_class=type(error).__name__)
                logger.warning('YouTube short_id=%s phase=%s failed: %s',short['id'],
                               (draft or {}).get('phase','rendered'),type(error).__name__)
                row = await db.get_short(short['id'])
                status = ('uploaded' if row['status'] == 'uploaded' else 'uncertain' if submitted
                          else 'sending' if draft or upload_started else short['_previous_status'])
                if row['status'] != 'uploaded' and status != 'sending':
                    await db.finish_short(short['id'], status)
                return {'status': 'uncertain' if submitted else 'resume_required' if draft or upload_started else 'failed', 'queue_status': status,
                        'error': (f'TimeoutError: публикация превысила {PUBLICATION_TIMEOUT_SECONDS} с; сохранённый черновик требует сверки'
                                  if isinstance(error,TimeoutError) else failure_reason(error)),
                        'short_id': short['id'], 'audience_audit': audit}
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
