"""One render, two durable remote publications, zero retained MP4 and owner alert."""
import asyncio
import json
import html
import hashlib
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import (FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, InputPollOption,
    ReplyParameters, LinkPreviewOptions)
from app.shorts import SHORTS_DIR, ensure_short_video
from app.storage_cleanup import DATA_DIR, media_slot, enforce_zero_mp4_clutter
from app.youtube_uploader import upload_short_youtube, reconcile_youtube_publication
from app.notifications import notify_admins
from app.telegraph_seo import bot_link
from app.pipeline import failure_reason
from app.privacy import public_bot, anonymous_series_url, sanitize_public_text

STORY_QUESTION='⚔️ А как бы вы поступили на месте героя в этой главе?'
STORY_OPTIONS=(
    '🔥 Рискнуть всем и принять скрытое задание Системы',
    '🛡 Скрыть свой истинный ранг и изучить уязвимость',
    '📖 Сразу открыть следующую главу в @NovelPulseBot',
)
RAIL_TEASER_QUESTION='⚡ Как поступить инженеру с изобретением, которое может изменить ход войны?'
RAIL_TEASER_OPTIONS=(
    '⚡ Подать магию молний прямо в рельсотрон',
    '🛡 Скрыть прототип, пока королевство не узнало о нём',
    '📖 Читать дальше в @NovelPulseBot',
)
CULTIVATION_TEASER_QUESTION='🌱 Первая трава принесла награду Системы. Какой будет ваш следующий ход?'
CULTIVATION_TEASER_OPTIONS=(
    '🌱 Вырастить редкую траву и рискнуть ради новой награды',
    '🛡 Спрятать дар Системы и сначала изучить правила мира',
    '📖 Узнать её следующий шаг в @NovelPulseBot',
)


def campaign_teaser_poll(source):
    if source == 'tg_rail':
        return RAIL_TEASER_QUESTION, RAIL_TEASER_OPTIONS
    if source == 'tg_cultivation':
        return CULTIVATION_TEASER_QUESTION, CULTIVATION_TEASER_OPTIONS
    return STORY_QUESTION, STORY_OPTIONS

CHANNEL_SEARCH_DESCRIPTION=('📚 ЛитРПГ, Аудиокниги, Ранобэ и Веб-Новеллы на русском! '
    'Прокачка, Система, Исэкай, Попаданцы. 🎧 Слушай и читай бесплатно в боте: @NovelPulseBot')


def load_marketing_teaser(source, markdown_path=None):
    """Read one ready-to-post copy block by its exact Telegram attribution tag."""
    from app.analytics import is_telegram_campaign_source
    if not is_telegram_campaign_source(source):
        raise ValueError('Некорректная промо-метка')
    path = Path(markdown_path) if markdown_path else Path(__file__).resolve().parents[1] / 'marketing' / 'stage45_telegram_teasers.md'
    content = path.read_text(encoding='utf-8')
    for section in content.split('\n## ')[1:]:
        title, _, body = section.partition('\n')
        if f'https://t.me/NovelPulseBot?start={source}' in body:
            return body.strip()
    raise ValueError(f'В marketing/stage45_telegram_teasers.md нет тизера для {source}')


async def publish_campaign_teaser(db, bot, channel_id, *, series_id, source, teaser_text=None):
    """Publish one idempotent teaser and its native anonymous reply poll."""
    bot = public_bot(bot)
    from app.analytics import is_telegram_campaign_source
    if not is_telegram_campaign_source(source):
        raise ValueError('Некорректная промо-метка')
    key = f'campaign_teaser:{channel_id}:{source}'
    existing = json.loads(await db.get_state(key) or '{}')
    if existing.get('state') == 'sent':
        return existing
    series = await db.get_series(series_id)
    if not series:
        raise ValueError('Новелла для тизера не найдена')
    username = (await bot.me()).username
    if not username:
        raise ValueError('У бота нет публичного username')
    bot_url = f'https://t.me/{username}?start={source}'
    copy = load_marketing_teaser(source) if teaser_text is None else teaser_text.strip()
    if f'https://t.me/NovelPulseBot?start={source}' not in copy:
        raise ValueError('В утверждённом тизере нет нужной промо-метки')
    post = copy.replace(
        f'https://t.me/NovelPulseBot?start={source}', bot_url)
    if len(post) > 3500:
        raise ValueError('Текст тизера превышает безопасный лимит Telegram')
    async with db._lock:
        await db.connection.execute('BEGIN IMMEDIATE')
        try:
            row = await db._one('SELECT value FROM app_state WHERE key=?', (key,))
            prior = json.loads(row['value']) if row else {}
            if prior.get('state') == 'sent':
                await db.connection.commit()
                return prior
            if prior.get('state') in ('sending', 'uncertain'):
                raise RuntimeError('Публикация тизера требует сверки; повторная отправка исключена')
            if prior.get('state') == 'teaser_sent' and prior.get('poll_state') in ('sending', 'uncertain'):
                raise RuntimeError('Отправка опроса неоднозначна; сначала сверьте канал')
            await db.connection.execute('INSERT INTO app_state(key,value) VALUES (?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, json.dumps({'state': 'sending', 'series_id': series_id, 'source': source})))
            await db.connection.commit()
        except BaseException:
            await db.connection.rollback()
            raise

    message_id = prior.get('message_id')
    if not message_id:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='📖 Читать продолжение в боте ➡️', url=bot_url)],
            [InlineKeyboardButton(text='🌐 Читать в браузере',
                url=anonymous_series_url(series))],
        ])
        try:
            posted = await bot.send_message(channel_id, post, parse_mode=None, reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True))
        except BaseException as error:
            definite = isinstance(error, TelegramAPIError) and not isinstance(error, TelegramNetworkError)
            await db.set_state(key, json.dumps({'state': 'failed' if definite else 'uncertain',
                'series_id': series_id, 'source': source, 'error_class': type(error).__name__}))
            raise
        message_id = posted.message_id
        prior = {'state': 'teaser_sent', 'message_id': message_id, 'series_id': series_id,
                 'source': source, 'bot_url': bot_url,
                 'message_sent_at': datetime.now(timezone.utc).isoformat()}
        await db.set_state(key, json.dumps(prior, ensure_ascii=False))

    poll_state = prior.get('poll_state', 'pending')
    if poll_state == 'sent':
        return prior
    poll_key_state = {**prior, 'state': 'teaser_sent', 'poll_state': 'sending'}
    await db.set_state(key, json.dumps(poll_key_state, ensure_ascii=False))
    question, options = campaign_teaser_poll(source)
    try:
        poll = await bot.send_poll(channel_id, question=question,
            options=[InputPollOption(text=option) for option in options],
            is_anonymous=True,
            reply_parameters=ReplyParameters(message_id=message_id, allow_sending_without_reply=False))
    except BaseException as error:
        definite = isinstance(error, TelegramAPIError) and not isinstance(error, TelegramNetworkError)
        receipt = {**prior, 'state': 'teaser_sent', 'poll_state': 'failed' if definite else 'uncertain',
                   'poll_error_class': type(error).__name__}
        await db.set_state(key, json.dumps(receipt, ensure_ascii=False))
        raise
    receipt = {**prior, 'state': 'sent', 'poll_state': 'sent', 'poll_message_id': poll.message_id,
               'completed_at': datetime.now(timezone.utc).isoformat()}
    await db.set_state(key, json.dumps(receipt, ensure_ascii=False))
    return receipt


async def publish_raid_announcement(db,bot,channel_id,base_url):
    """One factual channel release; permission failure cannot create duplicate posts."""
    bot = public_bot(bot)
    from app.crawler_indexing import portal_base
    base=portal_base(base_url)
    description={'updated':False}
    try:
        description['updated']=bool(await bot.set_chat_description(channel_id,CHANNEL_SEARCH_DESCRIPTION))
    except TelegramAPIError as error:
        description['error']=type(error).__name__
    report=json.loads(await db.get_state('web_portal_report') or '{}')
    count=sum(n.endswith('.html') for n in report.get('files',[]))
    if not count:raise ValueError('Generate the public portal before announcing it')
    username=(await bot.get_me()).username
    key=f'stage37_raid_announcement:{channel_id}'
    async with db._lock:
        await db.connection.execute('BEGIN IMMEDIATE')
        try:
            row=await db._one('SELECT value FROM app_state WHERE key=?',(key,))
            old=json.loads(row['value']) if row else {}
            if old.get('state')=='sent':
                await db.connection.commit();return {**old,'description':description}
            if old.get('state') in ('sending','uncertain'):raise RuntimeError('Raid announcement delivery requires reconciliation')
            await db.connection.execute('INSERT INTO app_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key,json.dumps({'state':'sending'})))
            await db.connection.commit()
        except BaseException:await db.connection.rollback();raise
    text=('⚔️ Совместный Рейд — по +1 ключу обоим!\n\n'
        'Позовите нового читателя в NovelPulse. Вы получите +1 🔑 и +50 XP, а друг — +1 🔑 и +25 XP. '
        'Ключ открывает платную главу навсегда.\n\n'
        'Как участвовать: откройте бота → «Совместный Рейд» → отправьте другу персональную ссылку. '
        'Награды приходят после первого открытия главы 1 новым другом, один раз за подтверждённое чтение.\n\n'
        f'📚 Выбирайте историю: {count} страниц библиотеки, включая 24 жанровых маршрута с текстом и аудиопревью.\n'
        f'{base}\n\n🎧 RSS-подкаст: {report.get("podcast_episodes",0)} коротких аудиознакомств. '
        f'Полные доступные главы слушайте в боте.\n{base}podcast.xml\n\n'
        f'👉 Читать и слушать: @{username}')
    try:
        message=await bot.send_message(channel_id,text,parse_mode=None,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='⚔️ Найти напарника и читать бесплатно',url=f'https://t.me/{username}')],
                [InlineKeyboardButton(text='📚 Открыть библиотеку и подборки',url=base)]]))
    except BaseException as error:
        definite=isinstance(error,TelegramAPIError) and not isinstance(error,TelegramNetworkError)
        await db.set_state(key,json.dumps({'state':'failed' if definite else 'uncertain','error':type(error).__name__}))
        raise
    receipt={'state':'sent','message_id':message.message_id,'description':description,
        'portal_pages':count,'podcast_episodes':report.get('podcast_episodes',0)}
    await db.set_state(key,json.dumps(receipt))
    return receipt


async def publish_featured_review_post(db,bot,channel_id,series_id=5):
    """Feature consented reviews; editorial examples never count as reader ratings."""
    bot = public_bot(bot)
    series=await db.get_series(series_id)
    if not series:raise ValueError('Новелла не найдена')
    featured=await db.get_featured_reviews(series_id,limit=1)
    review=featured[0] if featured else None
    demonstration=review is None
    username=(await bot.get_me()).username
    link=bot_link(username,series_id)
    if review:
        name='@'+review['username'] if review.get('username') else 'Читатель без username'
        text=(f'💬 РЕЦЕНЗИЯ ЧИТАТЕЛЯ · «{series["title"][:220]}»\n\n'
            f'{"⭐️"*review["rating"]} {review["rating"]:.1f}/5.0 | Читатель: {name} (Ранг: {review["player_rank"]})\n\n')
        if review['review_text']:text+='«'+review['review_text']+'»\n\n'
        if review['favorite_quote']:text+='Любимая цитата: «'+review['favorite_quote']+'»\n\n'
    else:
        text=(f'💬 ПРИМЕР ОТЗЫВА · «{series["title"][:220]}»\n\n'
            '🧪 Демонстрационный текст редакции NovelPulse. Это пример формы, а не отзыв реального читателя.\n\n'
            '⭐️⭐️⭐️⭐️⭐️ 5.0/5.0 — пример оценки\n\n'
            '«Невероятный поворот в 4-й главе! Не ожидал, что законы физики так круто лягут на магию!»\n\n'
            'Прочитали новеллу? Оставьте собственный отзыв через /review в боте и получите +15 XP. '
            'Он может стать следующей рецензией канала, если вы разрешите публикацию.\n\n')
    text+='👉 Читать новеллу онлайн бесплатно: '+link
    key=f'featured_review_post:{channel_id}:{series_id}:'+ (str(review['id']) if review else 'editorial-example-v1')
    async with db._lock:
        await db.connection.execute('BEGIN IMMEDIATE')
        try:
            previous=await db._one('SELECT value FROM app_state WHERE key=?',(key,))
            prior=json.loads(previous['value']) if previous else {}
            if prior.get('state')=='sent':
                await db.connection.commit()
                return prior
            if prior.get('state') in ('sending','uncertain'):
                raise RuntimeError('Доставка рецензии требует сверки; повторная отправка исключена')
            await db.connection.execute('INSERT INTO app_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key,json.dumps({'state':'sending','demonstration':demonstration})))
            await db.connection.commit()
        except BaseException:
            await db.connection.rollback();raise
    try:
        message=await bot.send_message(channel_id,text,parse_mode=None,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='📖 Читать и оценить в боте',url=link)]]))
    except BaseException as error:
        state='failed' if isinstance(error,TelegramAPIError) and not isinstance(error,TelegramNetworkError) else 'uncertain'
        await db.set_state(key,json.dumps({'state':state,'error':type(error).__name__,'demonstration':demonstration}))
        raise
    receipt={'state':'sent','message_id':message.message_id,'series_id':series_id,
        'review_id':review['id'] if review else None,'demonstration':demonstration}
    await db.set_state(key,json.dumps(receipt))
    return receipt


async def publish_podcast_chapter_drop(db, bot, channel_id, series_id=5, chapter_num=1):
    """A replay-safe public free chapter; an ambiguous send is never retried blindly."""
    bot = public_bot(bot)
    from app.audiobook import render_chapter_audio, audio_title
    series = await db.get_series(series_id)
    rows = await db.list_chapters(series_id)
    record = next((c for c in rows if c['chapter_num']==chapter_num),None)
    if series is None or record is None or chapter_num>series['free_chapters_limit']:
        raise ValueError('В канал можно публиковать только опубликованную бесплатную главу')
    chapter = await db.get_chapter(record['id'])
    key = f'podcast_drop:{channel_id}:{series_id}:{chapter_num}'
    async with db._lock:
        await db.connection.execute('BEGIN IMMEDIATE')
        try:
            row = await db._one('SELECT value FROM app_state WHERE key=?',(key,))
            old = json.loads(row['value']) if row else {}
            if old.get('state') in ('sent','sending','uncertain'):
                await db.connection.commit()
                return old
            await db.connection.execute('INSERT INTO app_state(key,value) VALUES (?,?) ON CONFLICT(key) '
                'DO UPDATE SET value=excluded.value',(key,json.dumps({'state':'sending'})))
            await db.connection.commit()
        except BaseException:
            await db.connection.rollback()
            raise
    path = None
    sending = False
    try:
        username = (await bot.get_me()).username
        path = await render_chapter_audio(chapter,series)
        caption = (f"🎧 NovelPulse Audio · Премьера аудиокниги\n\n«{series['title'][:220]}»\n"
                   f"Глава {chapter_num}: {chapter['title'][:150]}\n\n"
                   '⚡ Магия молний встречает физику — слушайте первую главу целиком.\n'
                   '📖 Первые главы бесплатны. Продолжение — в боте, первая платная глава за 1 ⭐️!')
        sending = True
        result = await bot.send_audio(channel_id,FSInputFile(path),title=audio_title(chapter)[:128],
            performer='NovelPulse',caption=caption,parse_mode=None,request_timeout=180,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text='🎧 Слушать продолжение в боте ➡️',url=bot_link(username,series_id))]]))
        receipt = {'state':'sent','message_id':result.message_id,'series_id':series_id,'chapter_num':chapter_num,
                   'chapter_id':chapter['id'],'sent_at':datetime.now(timezone.utc).isoformat()}
        await db.set_state(key,json.dumps(receipt))
        return receipt
    except BaseException as error:
        definite = isinstance(error,TelegramAPIError) and not isinstance(error,TelegramNetworkError)
        await db.set_state(key,json.dumps({'state':'uncertain' if sending and not definite else 'failed',
                                          'error_class':type(error).__name__}))
        raise
    finally:
        if path is not None:
            Path(path).unlink(missing_ok=True)


def build_chapter_release_digest(new_series, updated_series, username, channel_url):
    """Public release note based only on the verified, published SQLite catalogue."""
    from app.telegraph_seo import bot_link
    lines = ['🔥 Свежий релиз глав!', '', f"📚 Новая новелла: {new_series['title']}"]
    if new_series.get('description'):
        lines.extend(['', new_series['description'][:450]])
    if new_series.get('telegraph_url'):
        lines.extend(['', f"⚡ Instant View: {new_series['telegraph_url']}"])
    lines.append(f"📖 Читать: {bot_link(username, new_series['id'])}")
    lines.extend(['', '✨ Новые главы уже в библиотеке:'])
    for item in updated_series:
        nums = item['chapter_nums']
        if nums:
            lines.append(f"• {item['title'][:150]} — главы {min(nums)}–{max(nums)}")
    lines.extend(['', 'Главы 7–10 открывают новые повороты в расширенной библиотеке.',
        '⚡ VIP на 24 часа — 19 ⭐️ · 👑 VIP на 7 дней — 49 ⭐️: доступ ко всем новеллам.',
        f"📢 Канал: {channel_url}"])
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = [[InlineKeyboardButton(text='📖 Читать новую новеллу', url=bot_link(username, new_series['id']))]]
    if new_series.get('telegraph_url'):
        keyboard.append([InlineKeyboardButton(text='⚡ Открыть Instant View', url=new_series['telegraph_url'])])
    for item in updated_series:
        if item['chapter_nums']:
            keyboard.append([InlineKeyboardButton(text=f"📚 {item['title'][:35]}",
                url=bot_link(username, item['id']))])
    return '\n'.join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _notify_updated_readers(db, bot, series):
    bot = public_bot(bot)
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    summary = {'sent': 0, 'failed': 0}
    for item in series:
        readers = await db.readers_to_notify(item['id'])
        for reader in readers:
            following = await db.next_chapter(item['id'], reader['read_chapter'])
            if following is None:
                continue
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text='📖 Продолжить чтение 👉', callback_data=f"chapter:{following['id']}")]])
            try:
                try:
                    await bot.send_message(reader['tg_id'],
                        f"🔥 Вышли новые главы новеллы «{item['title'][:180]}»! Продолжить чтение 👉",
                        parse_mode=None, reply_markup=markup, disable_notification=True)
                except TelegramRetryAfter as error:
                    if error.retry_after > 60:
                        raise
                    await asyncio.sleep(error.retry_after)
                    await bot.send_message(reader['tg_id'],
                        f"🔥 Вышли новые главы новеллы «{item['title'][:180]}»! Продолжить чтение 👉",
                        parse_mode=None, reply_markup=markup, disable_notification=True)
                await db.mark_notified(reader['tg_id'], item['id'], reader['latest_chapter'])
                summary['sent'] += 1
            except TelegramForbiddenError:
                await db.mark_notified(reader['tg_id'], item['id'], reader['latest_chapter'])
                summary['failed'] += 1
            except Exception:
                summary['failed'] += 1
            await asyncio.sleep(.05)
    return summary


async def publish_chapter_release_digest(db, bot, channel_id):
    """Post one idempotent release digest, notify existing readers, and refresh pinned #12."""
    bot = public_bot(bot)
    from app.telegraph_seo import channel_link
    from app.autopilot import build_navigator
    from aiogram.types import LinkPreviewOptions
    manifest = json.loads(await db.get_state('stage20_release_manifest') or '{}')
    new_id = manifest.get('new_series_id')
    updated_ids = manifest.get('updated_series_ids', [])
    if not new_id or not updated_ids:
        raise ValueError('Нет проверенного манифеста релиза Stage 20')
    new_series = await db.get_series(int(new_id))
    if not new_series or not new_series.get('telegraph_url'):
        raise ValueError('Для новой новеллы ещё нет Telegraph Instant View')
    updated = []
    for series_id in updated_ids:
        item = await db.get_series(int(series_id))
        if not item:
            continue
        chapters = await db.list_chapters(item['id'])
        nums = [c['chapter_num'] for c in chapters if c['chapter_num'] > 2]
        if nums:
            item['chapter_nums'] = nums
            updated.append(item)
    channel_url = channel_link(channel_id)
    username = (await bot.get_me()).username
    text, markup = build_chapter_release_digest(new_series, updated, username, channel_url)
    signature = hashlib.sha256(json.dumps({'new': new_series['id'],
        'chapters': [(i['id'], i['chapter_nums']) for i in updated]}, sort_keys=True).encode()).hexdigest()
    prior = await db.get_state('stage20_release_digest_receipt')
    receipt = json.loads(prior) if prior else {}
    if receipt.get('signature') != signature:
        sent = await bot.send_message(channel_id, text, parse_mode=None, reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True))
        receipt = {'signature': signature, 'message_id': sent.message_id}
        await db.set_state('stage20_release_digest_receipt', json.dumps(receipt))
    readers = await _notify_updated_readers(db, bot, updated)
    from app.telegraph_seo import HUB_TITLES
    hubs = json.loads(await db.get_state('seo_hubs') or '[]')
    series = await db.list_series()
    for item in series:
        item['chapter_count'] = len(await db.list_chapters(item['id']))
    deployment = json.loads(await db.get_state('public_web_deployment') or '{}')
    nav = build_navigator(series, username, hubs,
        deployment.get('base_url', '') if deployment.get('status') == 'live' else '',
        total_chapters=sum(item['chapter_count'] for item in series))
    chat = await bot.get_chat(channel_id)
    pinned = getattr(chat, 'pinned_message', None)
    if pinned is not None and pinned.message_id != 12:
        raise ValueError(f'Ожидался навигатор #12 в закрепе, Telegram показывает #{pinned.message_id}')
    await bot.edit_message_text(nav, chat_id=channel_id, message_id=12, parse_mode='HTML',
        link_preview_options=LinkPreviewOptions(is_disabled=True))
    await bot.pin_chat_message(chat_id=channel_id, message_id=12, disable_notification=True)
    navigator = {'message_id': 12, 'pinned': True,
        'digest': hashlib.sha256(nav.encode()).hexdigest(), 'state': 'sent'}
    await db.set_state(f'channel_navigator:{channel_id}', json.dumps(navigator))
    return {'message_id': receipt['message_id'], 'navigator_message_id': 12,
        'navigator_chapters': sum(item['chapter_count'] for item in series),
        'series_count': len(series), 'notified_readers': readers}


async def publish_stage31_catalog_refresh(db, bot, channel_id, new_series_id: int | list[int],
                                          updated_series: list[dict], *, milestone: str = '') -> dict:
    """Announce a verified catalog expansion and refresh the existing pinned navigator once."""
    bot = public_bot(bot)
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import LinkPreviewOptions
    from app.autopilot import build_navigator
    series = await db.list_series()
    new_ids = [new_series_id] if isinstance(new_series_id, int) else list(new_series_id)
    new_entries = [await db.get_series(sid) for sid in new_ids]
    if not new_entries or any(not item or not item.get('telegraph_url') for item in new_entries):
        raise ValueError('У новой новеллы должны быть опубликованная статья Telegraph и ссылка Instant View')
    if not set(new_ids).issubset({item['id'] for item in series}):
        raise ValueError('Нельзя анонсировать новеллу без опубликованных глав')
    if any(not item.get('telegraph_url') for item in series):
        raise ValueError('Нельзя обновить навигатор до публикации Instant View для всего каталога')
    username = (await bot.get_me()).username
    from app.telegraph_seo import bot_link, channel_link
    channel_url = channel_link(channel_id)
    lines = ['🔥 ' + (milestone or 'Свежий релиз в NovelPulse!')]
    for new_series in new_entries:
        lines.extend(['', f"📚 Новая новелла: «{new_series['title']}»"])
        if new_series.get('description'):
            lines.extend(['', new_series['description'][:450]])
        lines.extend(['', f"⚡ Instant View: {new_series['telegraph_url']}",
                      f"📖 Читать в боте: {bot_link(username, new_series['id'])}"])
    if updated_series:
        lines.extend(['', '⚡ Новые переведённые главы:'])
        for item in updated_series:
            nums = sorted(set(item.get('chapter_nums', [])))
            if nums:
                lines.append(f"• {item['title'][:130]} — главы {', '.join(map(str, nums))}")
    lines.extend(['', '⚡ VIP на 24 часа — 19 ⭐️ · 👑 VIP на 7 дней — 49 ⭐️',
                  f"📢 Новые главы и трейлеры: {channel_url}"])
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📖 ' + item['title'][:52], url=bot_link(username, item['id']))]
        for item in new_entries])
    signature = hashlib.sha256(json.dumps({
        'series_id': new_series_id,
        'chapters': [(item.get('id'), sorted(set(item.get('chapter_nums', [])))) for item in updated_series],
        'catalog': [(item['id'], len(await db.list_chapters(item['id']))) for item in series],
    }, sort_keys=True).encode()).hexdigest()
    key = 'stage35_catalog_release_receipt' if milestone else 'stage31_catalog_release_receipt'
    prior = json.loads(await db.get_state(key) or '{}')
    if prior.get('signature') == signature and prior.get('message_id'):
        message_id = prior['message_id']
    else:
        sent = await bot.send_message(channel_id, '\n'.join(lines), parse_mode=None,
            reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True))
        message_id = sent.message_id
        await db.set_state(key, json.dumps({'signature': signature, 'message_id': message_id,
            'state': 'sent', 'series_id': new_series_id}, ensure_ascii=False))
    hubs = json.loads(await db.get_state('seo_hubs') or '[]')
    deployment = json.loads(await db.get_state('public_web_deployment') or '{}')
    portal_url = deployment.get('base_url', '') if deployment.get('status') == 'live' else ''
    for item in series:
        item['chapter_count'] = len(await db.list_chapters(item['id']))
    total_chapters = sum(item['chapter_count'] for item in series)
    navigator = build_navigator(series, username, hubs, portal_url, total_chapters)
    chat = await bot.get_chat(channel_id)
    pinned = getattr(chat, 'pinned_message', None)
    if pinned is not None and pinned.message_id != 12:
        raise ValueError(f'Ожидался навигатор #12 в закрепе, Telegram показывает #{pinned.message_id}')
    try:
        await bot.edit_message_text(navigator, chat_id=channel_id, message_id=12, parse_mode='HTML',
            link_preview_options=LinkPreviewOptions(is_disabled=True))
    except TelegramBadRequest as error:
        if 'message is not modified' not in str(error).casefold():
            raise
    await bot.pin_chat_message(chat_id=channel_id, message_id=12, disable_notification=True)
    await db.set_state(f'channel_navigator:{channel_id}', json.dumps({
        'message_id': 12, 'pinned': True, 'digest': hashlib.sha256(navigator.encode()).hexdigest(), 'state': 'sent'}))
    return {'message_id': message_id, 'navigator_message_id': 12, 'series_count': len(series),
        'chapter_count': total_chapters}


async def publish_story_poll(db,bot,channel_id,short_id,video_message_id):
    """One native anonymous reply. Never retry a network-ambiguous submission."""
    bot = public_bot(bot)
    destination=str(channel_id)
    if not await db.claim_story_poll(short_id,destination,video_message_id):
        return await db.story_poll(short_id,destination)
    try:
        message=await bot.send_poll(channel_id,question=STORY_QUESTION,
            options=[InputPollOption(text=text) for text in STORY_OPTIONS],is_anonymous=True,
            reply_parameters=ReplyParameters(message_id=video_message_id,allow_sending_without_reply=False))
        await db.finish_story_poll(short_id,destination,'sent',message.message_id)
    except Exception as error:
        state='failed' if isinstance(error,TelegramAPIError) and not isinstance(error,TelegramNetworkError) else 'uncertain'
        await db.finish_story_poll(short_id,destination,state,error_class=type(error).__name__)
    return await db.story_poll(short_id,destination)


async def claim_autopilot_short(db, short_id=None):
    """A one-shot owner selection changes the episode, never its timer or receipts."""
    if short_id is None:
        override = await db.get_state('autopilot_next_short_id')
        if override:
            if not override.isascii() or not override.isdecimal() or int(override) <= 0:
                raise ValueError('Некорректный приоритетный Shorts')
            short_id = int(override)
            row = await db.get_short(short_id)
            if not row or row['status'] != 'pending':
                raise RuntimeError('Приоритетный Shorts не ожидает публикации; требуется сверка')
    return await db.claim_short(short_id)


async def _publish_short_pair(db, bot, settings, *, short_id=None, root=SHORTS_DIR, min_interval_hours=0):
    bot = public_bot(bot)
    if not settings.channel_id:
        raise ValueError('CHANNEL_ID обязателен для публикации в обе площадки')
    from app.youtube_uploader import youtube_blocked_until
    blocked=await youtube_blocked_until(db)
    if blocked:return {'status':'quota_blocked','retry_after':blocked,'reason':'Суточный лимит YouTube; повторная загрузка отложена'}
    root = Path(root).resolve()
    cleanup_root = DATA_DIR if root.is_relative_to(DATA_DIR.resolve()) else root
    async with nullcontext():
        last = await db.get_state('last_youtube_upload_at') or await db.get_state('last_paired_upload')
        if last and min_interval_hours and short_id is None:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
            if elapsed < min_interval_hours * 3600:
                return {'status': 'idle', 'reason': 'cooldown'}
        # Reconcile interrupted jobs first; never upload an ambiguous draft a second time.
        stranded = [s for s in await db.list_shorts() if s['status'] in ('sending', 'uncertain')]
        short = next((s for s in stranded if short_id is None or s['id'] == short_id), None)
        resume_draft = False
        if short:
            draft = json.loads(await db.get_state(f'youtube_draft:{short["id"]}') or '{}')
            if draft.get('state') not in ('submitted','draft'):
                return {'status': 'needs_review', 'short_id': short['id'], 'error': 'Незавершённый черновик требует сверки/возобновления'}
            if draft['state'] == 'submitted':
                await reconcile_youtube_publication(db, short['id'], root=root, manage_slot=False, keep_media=True)
            resume_draft = draft['state'] == 'draft'
            short['status'] = 'sending'
            short['_previous_status'] = 'pending'
        else:
            short = await claim_autopilot_short(db, short_id)
        if not short:
            return {'status': 'idle'}
        report = {'short_id': short['id'], 'title': short['title']}
        telegram_started = False
        try:
            receipts = [r for r in await db.short_publications() if r['short_id'] == short['id']]
            youtube = next((r for r in receipts if r['provider'] == 'youtube'), None)
            telegram = next((r for r in receipts if r['provider'] == 'telegram' and r['destination'] == str(settings.channel_id)), None)
            username = (await bot.get_me()).username
            path = await ensure_short_video(db, short, username, root=root)
            media_info=json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
            report['video_engine']=media_info.get('engine','legacy')
            if not youtube:
                upload = await upload_short_youtube(db, short_id=short['id'], claimed_short=short, root=root,
                    manage_slot=False, keep_media=True, background=True, settings=settings, rendered_path=path,
                    resume=resume_draft)
                report['youtube_attempt'] = upload
                if upload['status'] != 'uploaded':
                    if upload['status'] in ('login_required', 'failed'):
                        await db.finish_short(short['id'], 'pending')
                    raise RuntimeError(upload.get('error') or upload['status'])
                video_id = upload['video_id']
            else:
                video_id = youtube['remote_id']
            if not telegram:
                telegram_started = True
                message = await bot.send_video(settings.channel_id, FSInputFile(path),
                    caption=short['title'] + '\n\n' + bot_link(username, short['series_id']), parse_mode=None,
                    supports_streaming=True, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text='📖 Читать в боте ➡️', url=bot_link(username, short['series_id']))]]))
                message_id = message.message_id
                await db.record_short_publication(short['id'], 'telegram', str(settings.channel_id), str(message_id))
            else:
                message_id = int(telegram['remote_id'])
                await db.record_short_publication(short['id'], 'telegram', str(settings.channel_id), str(message_id))
            # End of both transfers: deleting earlier would force a second render.
            cleanup = enforce_zero_mp4_clutter(cleanup_root)
            poll=await publish_story_poll(db,bot,settings.channel_id,short['id'],message_id)
            report.update(status='uploaded', youtube_url=f'https://www.youtube.com/shorts/{video_id}',
                          video_id=video_id, telegram_message_id=message_id, cleanup=cleanup,
                          poll=poll,telegram_poll_message_id=poll.get('message_id'))
            comment=json.loads(await db.get_state('youtube_comment:'+video_id) or '{}')
            report.update(comment_posted=bool(comment.get('comment_posted')),
                          comment_status=comment.get('comment_status','not_attempted'))
            await db.set_state('last_paired_upload', datetime.now(timezone.utc).isoformat())
            if await db.get_state('autopilot_next_short_id') == str(short['id']):
                await db.set_state('autopilot_next_short_id', '')
            text = (f"🎬 Опубликован новый Shorts! {report['video_engine']}\n{short['title']}\n\n{report['youtube_url']}\n"
                    f"📢 Telegram channel message_id: {message_id}\n"
                    f"🗳 Опрос: message_id={poll.get('message_id') or 'не подтверждён'} ({poll['state']})\n"
                    f"💬 Первый комментарий: {'опубликован' if report['comment_posted'] else 'не подтверждён'} ({report['comment_status']})\n"
                    f"🧹 Локальный MP4 удалён (на диске: {cleanup['disk']['mp4_count']} MP4)")
            if settings.autopilot_worker:
                report['owner_alert_payload']={'text':text,'event_key':f'paired_upload:{short["id"]}'}
                from app.autopilot_process import queue_owner_alert
                await queue_owner_alert(db,text,report['owner_alert_payload']['event_key'])
            else:
                report['admin_alert'] = await notify_admins(bot, text, settings=settings, db=db, event_key=f'paired_upload:{short["id"]}')
        except Exception as error:
            row = await db.get_short(short['id'])
            if telegram_started and row['status'] == 'sending':
                state = 'pending' if isinstance(error, TelegramAPIError) and not isinstance(error, TelegramNetworkError) else 'uncertain'
                await db.finish_short(short['id'], state)
            elif row['status'] == 'sending' and 'youtube_attempt' not in report:
                await db.finish_short(short['id'], 'pending')
            report.update(status='partial', error=failure_reason(error))
            text=f"⚠️ Автопилот: ролик {short['id']} не завершён. {report['error']}. Повторные подтверждённые публикации исключены."
            key=f'paired_error:{short["id"]}:{datetime.now(timezone.utc).date()}'
            if settings.autopilot_worker:
                report['owner_alert_payload']={'text':text,'event_key':key}
                from app.autopilot_process import queue_owner_alert
                await queue_owner_alert(db,text,key)
            else:
                report['admin_alert']=await notify_admins(bot,text,settings=settings,db=db,event_key=key)
        finally:
            cleanup = enforce_zero_mp4_clutter(cleanup_root)
            previous = report.get('cleanup', {})
            cleanup['mp4_deleted'] += previous.get('mp4_deleted', 0)
            cleanup['bytes_deleted'] += previous.get('bytes_deleted', 0)
            report['cleanup'] = cleanup
        await db.set_state('paired_upload_last_report', json.dumps(report, ensure_ascii=False))
        return report


async def publish_short_pair(db, bot, settings, *, short_id=None, root=SHORTS_DIR, min_interval_hours=0):
    bot = public_bot(bot)
    root = Path(root).resolve()
    cleanup_root = DATA_DIR if root.is_relative_to(DATA_DIR.resolve()) else root
    async with media_slot():
        try:
            return await _publish_short_pair(db, bot, settings, short_id=short_id, root=root,
                                             min_interval_hours=min_interval_hours)
        finally:
            enforce_zero_mp4_clutter(cleanup_root)
