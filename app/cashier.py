"""Phone-only retention loop: no video or desktop imports."""

import asyncio
import logging

from app.retention import send_paywall_followups
from app.errors import failure_reason
from app.node_sync import sync_catalog


async def background_retention(db, bot, settings):
    while True:
        try:
            report = await send_paywall_followups(db, bot, min_age_seconds=3600)
            logging.getLogger(__name__).info('Касса: отправлено напоминаний %s', report['sent'])
        except Exception as error:
            logging.getLogger(__name__).warning('Касса: %s', failure_reason(error))
        await asyncio.sleep(settings.retention_interval_hours * 3600)


async def background_catalog_sync(db, settings):
    """A private git pull/file replacement becomes visible without restarting polling."""
    def fingerprint():
        try:
            info = settings.catalog_snapshot_path.stat()
            return info.st_mtime_ns, info.st_size
        except OSError:
            return None

    previous = None
    while True:
        try:
            stamp = fingerprint()
            if settings.catalog_snapshot_url or (stamp is not None and stamp != previous):
                result = await sync_catalog(db, settings, optional=True)
                logging.getLogger(__name__).info('Каталог кассы: %s', result)
                previous = fingerprint()
        except Exception as error:
            # No input snippets, URLs with query credentials or payment data in logs.
            logging.getLogger(__name__).warning('Каталог кассы сохранён: %s', type(error).__name__)
            previous = fingerprint()
        await asyncio.sleep(300 if settings.catalog_snapshot_url else 60)


async def background_cashier(db, bot, settings):
    async with asyncio.TaskGroup() as group:
        group.create_task(background_retention(db, bot, settings))
        group.create_task(background_catalog_sync(db, settings))
