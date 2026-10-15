"""aiogram v3 bot assembly."""

import asyncio

from aiogram import Dispatcher

from app.config import Settings
from app.db import Database


def create_dispatcher(db: Database, settings: Settings) -> Dispatcher:
    if settings.node_mode == 'youtube_factory':
        raise RuntimeError('youtube_factory cannot own Telegram polling')
    from app.bot.handlers import payments, user
    from app.bot.handlers import node_admin
    if settings.node_mode == 'bot_24x7':
        admin_router = node_admin.create_phone_router()
    else:
        from app.bot.handlers import admin
        admin_router = admin.create_router()
    from app.audiobook import AudioJobs
    dispatcher = Dispatcher(db=db, settings=settings, audio_jobs=AudioJobs(), import_lock=asyncio.Lock(),
                            video_lock=asyncio.Lock(), notify_lock=asyncio.Lock())
    dispatcher.include_routers(node_admin.create_router(), admin_router, payments.create_router(), user.create_router())
    return dispatcher
