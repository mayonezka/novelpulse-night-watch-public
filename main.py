"""Run the cashier and/or video factory according to NODE_MODE."""

import asyncio
import logging
import sys
from pathlib import Path
from contextlib import suppress

# pythonw has no console streams. Configure a bounded private log before SDK imports.
if Path(sys.executable).name.lower() == 'pythonw.exe':
    from logging.handlers import RotatingFileHandler
    directory = Path(__file__).resolve().parent / 'data'
    directory.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        handlers=[RotatingFileHandler(directory / 'polling.err.log', maxBytes=2_000_000,
                                      backupCount=3, encoding='utf-8')])
    def _detached_error(kind, value, traceback):
        logging.getLogger('daemon').critical('Detached process failed: %s', kind.__name__)
    sys.excepthook = _detached_error

from aiogram import Bot
from aiogram.types import BotCommand
from pydantic import ValidationError

from app.bot import create_dispatcher
from app.config import Settings
from app.db import Database
from app.instance import bot_instance
from app.scheduling import upload_schedule


async def background_autopilot(db, bot, settings):
    # Keep the desktop scheduler behind a lazy boundary for phone deployments.
    from app.autopilot import background_autopilot as desktop_loop
    from app.autopilot_process import owner_alert_loop
    alerts=asyncio.create_task(owner_alert_loop(db,bot,settings))
    try:await desktop_loop(db, bot, settings)
    finally:
        alerts.cancel()
        with suppress(asyncio.CancelledError):await alerts


def log_configuration(settings):
    # Public Actions logs must not reveal IDs when multiple IDs no longer match
    # GitHub's exact comma-separated secret masking. Keep PC startup diagnostics.
    if settings.cloud_role == 'cloud':
        logging.getLogger(__name__).info('ADMIN_IDS configured: %s owners (cloud IDs hidden)', len(settings.admin_ids))
    else:
        logging.getLogger(__name__).info("Конфигурация загружена из .env и окружения. ADMIN_IDS=%s", settings.admin_ids)
    logging.getLogger(__name__).info('NODE_MODE=%s', settings.node_mode)


async def run(settings: Settings | None = None, *, cashier_max_seconds: float | None = None,
              on_cashier_ready=None) -> dict | None:
    settings = settings or Settings()
    log_configuration(settings)
    db = Database(settings.db_path)
    await db.open()
    manager = None
    try:
        if settings.cloud_failover_enabled and settings.node_mode != 'youtube_factory':
            from app.cloud_failover import CloudFailover
            manager = await CloudFailover.create(db,settings)
            if not await manager.prepare():
                logging.getLogger(__name__).info('PC is online, cloud night-watch standing down')
                return
        async with Bot(token=settings.bot_token.get_secret_value()) as bot:
            from app.privacy import PublicPrivacyMiddleware
            bot.session.middleware(PublicPrivacyMiddleware(settings.channel_id))
            if settings.node_mode == 'bot_24x7':
                from app.node_sync import sync_catalog
                try:
                    result = await sync_catalog(db, settings, optional=True)
                    logging.getLogger(__name__).info('Начальный каталог: %s', result)
                except Exception as error:
                    from app.errors import failure_reason
                    logging.getLogger(__name__).warning('Каталог сохранён без изменений: %s',
                        'неверный формат снимка' if isinstance(error, ValidationError) else failure_reason(error))
            else:
                schedule = await upload_schedule(db, settings)
                logging.getLogger(__name__).info('Последняя YouTube загрузка UTC=%s; догнать сейчас=%s; до следующей=%s сек',
                    schedule['last_youtube_upload_at'], schedule['due'], schedule['seconds_remaining'])
            if settings.node_mode == 'youtube_factory':
                if settings.autopilot_interval_hours <= 0:
                    raise RuntimeError('youtube_factory requires AUTOPILOT_INTERVAL_HOURS > 0')
                # Sending messages is allowed; never take ownership of getUpdates/webhooks.
                await background_autopilot(db, bot, settings)
                return
            dispatcher = create_dispatcher(db, settings)
            dispatcher['cashier_lease'] = manager
            if manager is None:await db.backfill_player_progress()
            await bot.set_my_commands([
                BotCommand(command="start", description="Открыть NovelPulse"),
                BotCommand(command="catalog", description="Каталог новелл"),
                BotCommand(command="profile", description="Мой Ранг в Системе"),
                BotCommand(command="leaderboard", description="Зал Славы читателей"),
                BotCommand(command="search", description="Поиск новелл по сюжетам и словам"),
                BotCommand(command="quiz", description="Узнать свой скрытый класс"),
                BotCommand(command="review", description="Оценить новеллу и оставить отзыв"),
                BotCommand(command="myid", description="Узнать свой Telegram ID"),
                BotCommand(command="terms", description="Условия покупки"),
                BotCommand(command="paysupport", description="Помощь с оплатой"),
            ])
            await bot.delete_webhook(drop_pending_updates=False)
            tasks=[]
            from app.gamification import rank_alert_loop
            tasks.append(asyncio.create_task(rank_alert_loop(db,bot)))
            if settings.node_mode == 'bot_24x7':
                from app.cashier import background_cashier
                tasks.append(asyncio.create_task(background_cashier(db, bot, settings)))
            else:
                from app.cashier import background_retention
                tasks.append(asyncio.create_task(background_retention(db,bot,settings)))
                if settings.autopilot_interval_hours > 0:
                    tasks.append(asyncio.create_task(background_autopilot(db, bot, settings)))
            try:
                if manager is not None:
                    from app.cloud_failover import poll_with_lease
                    if on_cashier_ready:
                        await on_cashier_ready(manager.session)
                    await poll_with_lease(dispatcher,bot,db,manager,max_seconds=cashier_max_seconds)
                    return {'reason':manager.stop_reason,'epoch':manager.session,'role':manager.role}
                else:
                    await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types(),
                                                   close_bot_session=False)
            finally:
                await dispatcher['audio_jobs'].close()
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with suppress(asyncio.CancelledError):
                        await task
    finally:
        try:
            if manager is not None:
                await manager.shutdown()
        finally:
            await db.close()


async def supervise():
    while True:
        result=await run()
        if not result or result.get('role')!='pc' or result.get('reason') not in (
                'resume','lease_lost','conflict','heartbeat_unavailable','synchronization_failed'):
            return
        logging.getLogger(__name__).info('PC lease recovery: merge encrypted delta before resuming polling')
        await asyncio.sleep(1)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Do not log aiogram polling URLs, which include the bot token.
    logging.getLogger("aiogram.dispatcher").setLevel(logging.CRITICAL)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try:
        with bot_instance():
            asyncio.run(supervise())
    except RuntimeError as error:
        logging.getLogger(__name__).error('%s', error)
    except ValidationError:
        raise SystemExit("Ошибка конфигурации. Заполните .env по .env.example; проверьте BOT_TOKEN, ADMIN_IDS и настройки LLM.") from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
