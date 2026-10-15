"""One render, two durable remote publications, zero retained MP4 and owner alert."""
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from app.shorts import SHORTS_DIR, ensure_short_video
from app.storage_cleanup import DATA_DIR, media_slot, enforce_zero_mp4_clutter
from app.youtube_uploader import upload_short_youtube, reconcile_youtube_publication
from app.notifications import notify_admins
from app.telegraph_seo import bot_link
from app.pipeline import failure_reason


async def _publish_short_pair(db, bot, settings, *, short_id=None, root=SHORTS_DIR, min_interval_hours=0):
    if not settings.channel_id:
        raise ValueError('CHANNEL_ID обязателен для публикации в обе площадки')
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
            report.update(status='uploaded', youtube_url=f'https://www.youtube.com/shorts/{video_id}',
                          video_id=video_id, telegram_message_id=message_id, cleanup=cleanup)
            await db.set_state('last_paired_upload', datetime.now(timezone.utc).isoformat())
            text = (f"🎬 Опубликован новый Shorts! {report['video_engine']}\n{short['title']}\n\n{report['youtube_url']}\n"
                    f"📢 Telegram channel message_id: {message_id}\n"
                    f"🧹 Локальный MP4 удалён (на диске: {cleanup['disk']['mp4_count']} MP4)")
            if settings.autopilot_worker:
                report['owner_alert_payload']={'text':text,'event_key':f'paired_upload:{short["id"]}'}
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
