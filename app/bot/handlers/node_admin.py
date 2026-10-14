"""Node administration available without any desktop/media dependencies."""

from datetime import datetime, timezone, timedelta

from aiogram import Bot, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import Message
from pydantic import ValidationError

from app.config import Settings
from app.db import Database
from app.errors import failure_reason
from app.node_sync import sync_catalog
from app.scheduling import upload_schedule, parse_utc
from app.startup import startup_status


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return bool(message.from_user and message.from_user.id in settings.admin_ids)


async def denied(message: Message):
    if message.from_user:
        await message.answer(f'⛔ Доступ запрещён. Ваш Telegram ID: {message.from_user.id}. '
            'Добавьте его в ADMIN_IDS в файле .env и перезапустите бота.')


async def node_status(message: Message, db: Database, settings: Settings):
    status = startup_status()
    schedule = await upload_schedule(db, settings)
    last = schedule['last_youtube_upload_at']
    if settings.node_mode == 'bot_24x7':
        last = await db.get_state('factory_last_youtube_upload_at') or last
        remaining = max(0, (parse_utc(last) + timedelta(hours=settings.autopilot_interval_hours)
            - datetime.now(timezone.utc)).total_seconds()) if last else None
        delay = 'на ПК; данные из последнего снимка' if remaining is None else f'на ПК: ~{int(remaining // 60)} мин (по снимку)'
    else:
        delay = 'выключено' if not schedule['enabled'] else ('пора публиковать / догнать' if schedule['due']
            else f"{int(schedule['seconds_remaining'] // 60)} мин")
    await message.answer(f"🖥 NODE_MODE: {settings.node_mode}\n"
        f"Автозапуск Windows: {'установлен' if status['installed'] else 'нет / не Windows'}\n"
        f"Последняя загрузка YouTube (UTC): {last or 'пока нет'}\nДо следующей: {delay}\n"
        f"Снимок каталога: {await db.get_state('catalog_snapshot_generated_at') or 'ещё не импортирован'}",
        parse_mode=None)


async def sync(message: Message, db: Database, settings: Settings):
    await message.answer('⏳ Синхронизирую каталог. Покупки, VIP и рефералы сохраняются.')
    try:
        result = await sync_catalog(db, settings)
        await message.answer(f"✅ Каталог обновлён: {result['series']} новелл, {result['chapters']} глав.")
    except ValidationError:
        await message.answer('❌ Некорректный формат снимка каталога. Требуется версия 1 от app.node_sync.')
    except Exception as error:
        await message.answer('❌ Синхронизация отклонена: ' + failure_reason(error), parse_mode=None)


def create_router():
    router = Router(name='node_admin')
    router.message.register(node_status, Command('node_status'), IsAdmin())
    router.message.register(sync, Command('sync_catalog'), IsAdmin())
    router.message.register(cloud_status, Command('cloud_status'), IsAdmin())
    router.message.register(denied, Command('node_status', 'sync_catalog', 'cloud_status'))
    return router


async def cloud_status(message: Message, db: Database, settings: Settings):
    """Includes verified watchdog/PID, Fernet state and public Actions billing mode."""
    from app.cloud_failover import cloud_summary
    await message.answer(await cloud_summary(db,settings),parse_mode=None)


async def phone_stats(message: Message, db: Database, settings: Settings):
    stats = await db.stats()
    series = await db.list_all_series()
    from app.cloud_failover import cloud_summary
    await message.answer(f"📊 Касса 24/7 · {len(series)} новелл\nЧитателей: {stats['total_users']}\n"
        f"Выручка сегодня (UTC): {stats['today_revenue']} ⭐️\nКупленных глав: {stats['unlocked_chapters']}\n"
        'Состояние узла: /node_status · Каталог: /sync_catalog\n' + await cloud_summary(db,settings))


async def phone_followup(message: Message, bot: Bot, db: Database):
    from app.retention import send_paywall_followups
    report = await send_paywall_followups(db, bot, min_age_seconds=3600)
    await message.answer(f"✅ Напоминания: отправлено {report['sent']}, ошибок {len(report['errors'])}.")


async def desktop_only(message: Message, settings: Settings):
    if message.from_user and message.from_user.id in settings.admin_ids:
        await message.answer('🖥 Эта команда выполняется на ПК в NODE_MODE=full. Лёгкая касса обслуживает чтение и оплату; '
            'ПК в youtube_factory публикует автоматически. /node_status · /sync_catalog')
    else:
        await denied(message)


def create_phone_router():
    router = Router(name='phone_admin')
    router.message.register(phone_stats, Command('stats', 'dashboard'), IsAdmin())
    router.message.register(phone_followup, Command('followup_now'), IsAdmin())
    router.message.register(denied, Command('stats', 'dashboard', 'followup_now'))
    router.message.register(desktop_only, Command('seed_demo', 'promo', 'import_trending', 'import_rr', 'post_channel',
        'traffic_pack', 'video_pack', 'notify_series', 'seo_pack', 'autopilot_now', 'seed_channel', 'growth_kit',
        'next_short', 'disk_status', 'autopublish_short'))
    return router
