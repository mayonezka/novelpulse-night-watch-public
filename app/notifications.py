"""Owner-only push alerts with persistent per-recipient delivery receipts."""
import logging
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from app.config import Settings
from app.analytics import is_campaign_source, source_title

logger = logging.getLogger(__name__)


async def notify_admins(bot, text, *, settings=None, db=None, event_key=None, retry_failed=False):
    settings = settings or Settings()
    result = {'sent': [], 'errors': []}
    for admin_id in settings.admin_ids:
        try:
            if db and event_key and not await db.claim_admin_alert(event_key, admin_id, text, retry_failed=retry_failed):
                continue
            message = await bot.send_message(admin_id, text[:3900], parse_mode=None)
            if db and event_key:
                await db.finish_admin_alert(event_key, admin_id, 'sent', message.message_id)
            result['sent'].append({'admin_id': admin_id, 'message_id': message.message_id})
        except Exception as error:
            state = 'failed' if isinstance(error, TelegramAPIError) and not isinstance(error, TelegramNetworkError) else 'uncertain'
            if db and event_key:
                try:
                    await db.finish_admin_alert(event_key, admin_id, state)
                except Exception:
                    logger.warning('Could not persist failed owner alert receipt')
            result['errors'].append({'admin_id': admin_id, 'state': state, 'reason': type(error).__name__})
            logger.warning('Owner alert failed: %s', type(error).__name__)
    return result


async def retry_owner_alerts(db, bot, settings):
    reports = []
    for event in await db.failed_admin_alerts():
        reports.append(await notify_admins(bot, event['message_text'], db=db, settings=settings,
            event_key=event['event_key'], retry_failed=True))
    return reports


async def new_reader_alert(bot, db, tg_id, username, source, *, start_payload=None, settings=None):
    """Notify owners once for a registration, adding campaign attribution when present."""
    settings = settings or Settings()
    name = '@' + username if username else 'без username'
    if is_campaign_source(source):
        count = await db.funnel_source_reader_count(source)
        text = (f'🎯 Новый читатель по промо-метке {source}! Источник: {source_title(source)}; '
                f'Telegram ID: {tg_id} ({name}). Всего привлечено по этой метке: {count}.')
    else:
        total = (await db.stats())['total_users']
        text = (f'👤 Новый читатель в NovelPulse! ID: {tg_id} ({name}), '
                f'вход: {(start_payload or "/start")[:128]}. Всего читателей: {total}')
    return await notify_admins(bot, text, settings=settings, db=db,
        event_key=f'new_reader:{tg_id}')


async def revenue_alert(bot, db, message, payment, item, *, settings=None):
    summary = await db.dashboard_stats()
    name = '@' + message.from_user.username if message.from_user.username else f'ID {message.from_user.id}'
    return await notify_admins(bot,
        f"💰 КАССА ПОПОЛНЕНА! Получено +{payment.total_amount} ⭐️ от {name} ({item}). "
        f"Общая выручка: {summary['revenue']} ⭐️", settings=settings, db=db,
        event_key='payment:' + payment.telegram_payment_charge_id)
