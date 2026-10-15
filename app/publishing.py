"""One render, two durable remote publications, zero retained MP4 and owner alert."""
import asyncio
import json
import html
import hashlib
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, InputPollOption, ReplyParameters
from app.shorts import SHORTS_DIR, ensure_short_video
from app.storage_cleanup import DATA_DIR, media_slot, enforce_zero_mp4_clutter
from app.youtube_uploader import upload_short_youtube, reconcile_youtube_publication
from app.notifications import notify_admins
from app.telegraph_seo import bot_link
from app.pipeline import failure_reason

STORY_QUESTION='⚔️ А как бы вы поступили на месте героя в этой главе?'
STORY_OPTIONS=(
    '🔥 Рискнуть всем и принять скрытое задание Системы',
    '🛡 Скрыть свой истинный ранг и изучить уязвимость',
    '📖 Сразу открыть следующую главу в @NovelPulseBot',
)


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


async def publish_story_poll(db,bot,channel_id,short_id,video_message_id):
    """One native anonymous reply. Never retry a network-ambiguous submission."""
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


async def _publish_short_pair(db, bot, settings, *, short_id=None, root=SHORTS_DIR, min_interval_hours=0):
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
            short = await db.claim_short(short_id)
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
    root = Path(root).resolve()
    cleanup_root = DATA_DIR if root.is_relative_to(DATA_DIR.resolve()) else root
    async with media_slot():
        try:
            return await _publish_short_pair(db, bot, settings, short_id=short_id, root=root,
                                             min_interval_hours=min_interval_hours)
        finally:
            enforce_zero_mp4_clutter(cleanup_root)
