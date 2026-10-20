"""Admin tools with explicit permission diagnostics and promo generation."""

import asyncio
import logging
import json
import tempfile
from pathlib import Path
from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import FSInputFile, InlineKeyboardButton, Message

from app.bot.handlers.user import keyboard, send_text
from app.config import Settings
from app.db import Database
from app.brain_sync import brain_sync
from app.pipeline import failure_reason, fetch_trending_royalroad, run_pipeline, royalroad_fiction_url
from app.promotional_text import complete_excerpt, dramatic_excerpt
from app.video_gen import generate_promo_video
from app.autopilot import TRAFFIC_DIR, run_autopilot_cycle, publish_channel_showcase, video_ready
from app.telegraph_seo import publish_all, seo_teaser
from app.telegraph_seo import channel_link
from app.retention import send_paywall_followups
from app.shorts import SHORTS_DIR, ensure_short_video, publish_short_to_channel
from app.publishing import publish_short_pair
from app.storage_cleanup import disk_status as storage_status, media_slot, delete_video
from aiogram.exceptions import TelegramNetworkError

logger = logging.getLogger(__name__)


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return bool(message.from_user and message.from_user.id in settings.admin_ids)


async def denied(message: Message) -> None:
    if message.from_user:
        await message.answer(
            f"⛔ Доступ запрещён. Ваш Telegram ID: {message.from_user.id}. "
            "Добавьте его в ADMIN_IDS в файле .env и перезапустите бота."
        )


async def stats(message: Message, db: Database) -> None:
    result = await db.stats()
    await message.answer(
        f"📊 Читателей всего: {result['total_users']}\n"
        f"Выручка за сегодня (UTC): {result['today_revenue']} ⭐️\n"
        f"Купленных глав: {result['unlocked_chapters']}"
    )


async def seed_demo(message: Message, bot: Bot, db: Database) -> None:
    await db.ensure_user(message.from_user.id, message.from_user.username)
    series_id, chapters = await db.seed_demo()
    me = await bot.get_me()
    await message.answer(
        "🎮 Демо готово: «Система: Пробуждение Нулевого Ранга»!\n"
        "Главы 1 и 2 бесплатные, глава 3 — за 5 ⭐️.\n"
        f"Новелла: https://t.me/{me.username}?start=series_{series_id}\n"
        f"Начало: https://t.me/{me.username}?start=ch_{chapters[0]}\n"
        f"Проверить оплату: https://t.me/{me.username}?start=ch_{chapters[2]}\n"
        "Повторный /seed_demo обновляет демо; ID глав и покупки сохраняются."
    )


def promo_text(series: dict, hook: str, username: str) -> str:
    return (
        f"🔥 {series['title'][:180]}\n\n{hook[:500]}\n\n"
        "Что выберет герой, когда цена победы — всё, что ему дорого?\n"
        f"📖 Откройте новеллу в NovelPulse. Бесплатных глав на старте: {series['free_chapters_limit']}.\n"
        f"👉 https://t.me/{username}?start=series_{series['id']}\n\n"
        "#NovelPulse #ЛитРПГ #ВебНовеллы #ЧтоЧитать"
    )


async def promo(message: Message, bot: Bot, db: Database) -> None:
    series = await db.list_all_series()
    if not series:
        await message.answer("Пока нет новелл для промо. Добавьте перевод или запустите /seed_demo.")
        return
    me = await bot.get_me()
    for item in series:
        hook = await db.promo_hook(item["id"])
        await send_text(bot, message.chat.id, promo_text(item, hook or item["description"] or "Новый мир. Неизвестная Система. Один шанс изменить судьбу.", me.username))


async def perform_import(message: Message, db: Database, import_lock: asyncio.Lock, operation, progress: str) -> None:
    if import_lock.locked():
        await message.answer("⏳ Импорт уже выполняется. Дождитесь завершения текущей загрузки.")
        return
    async with import_lock:
        await message.answer(progress)
        try:
            results = await operation()
            if not isinstance(results, list):
                results = [results]
            if not results:
                await message.answer("Новых трендовых новелл не найдено: все доступные уже есть в базе.")
                return
            for result in results:
                series = await db.get_series(result.series_id)
                count = len(await db.list_chapters(result.series_id))
                await send_text(message.bot, message.chat.id,
                                f"✅ Импортировано: {series['title']} ({count} глав)\n"
                                f"Новых или обновлённых: {result.imported}. Режим перевода: {result.mode}.")
        except Exception as error:
            reason = failure_reason(error)
            logger.warning("Импорт не завершён: %s", reason)
            await send_text(message.bot, message.chat.id,
                            "❌ Импорт не завершён. Причина: " + reason[:2800] +
                            "\nСохранённые главы остаются в базе; /import_rr с тем же URL продолжит загрузку.")


async def import_trending(message: Message, db: Database, settings: Settings, import_lock: asyncio.Lock) -> None:
    async def report_failure(url: str, reason: str) -> None:
        await send_text(message.bot, message.chat.id, f"⚠️ Пропускаю {url}\nПричина: {reason[:2300]}\nПробую следующую новеллу.")
    await perform_import(message, db, import_lock,
                         lambda: fetch_trending_royalroad(1, 5, settings=settings, db=db, on_error=report_failure),
                         "⏳ Ищу трендовую новеллу на RoyalRoad и перевожу 5 глав...")


async def import_rr(message: Message, command: CommandObject, db: Database, settings: Settings,
                    import_lock: asyncio.Lock) -> None:
    args = (command.args or "").split()
    try:
        if not 1 <= len(args) <= 2:
            raise ValueError
        url = royalroad_fiction_url(args[0])
        count = int(args[1]) if len(args) == 2 else 5
        if not 1 <= count <= 100:
            raise ValueError
    except ValueError:
        await message.answer("Использование: /import_rr https://www.royalroad.com/fiction/123/название [число глав от 1 до 100]")
        return
    await perform_import(message, db, import_lock,
                         lambda: run_pipeline(url, settings, db=db, max_chapters=count, free_chapters_limit=2),
                         f"⏳ Загружаю RoyalRoad и перевожу до {count} глав...")


async def post_channel(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings) -> None:
    args = (command.args or "").split()
    channel = settings.channel_id
    series_id = None
    try:
        if len(args) == 2:
            channel = Settings.parse_channel_id(args[0])
            series_id = int(args[1])
        elif len(args) == 1:
            series_id = int(args[0])
        elif args:
            raise ValueError
        if series_id is not None and series_id < 1:
            raise ValueError
    except ValueError:
        await message.answer("Использование: /post_channel [ID новеллы] или /post_channel @channel_username ID_новеллы")
        return
    if channel is None:
        await message.answer("Укажите CHANNEL_ID в .env и перезапустите бота или используйте /post_channel @channel_username ID_новеллы.")
        return
    if series_id is None:
        series = await db.list_series()
        series_id = series[-1]["id"] if series else None
    series = await db.get_series(series_id) if series_id else None
    chapters = await db.list_chapters(series_id) if series else []
    first_two = [chapter for chapter in chapters if chapter["chapter_num"] in (1, 2)]
    if not series or len(first_two) != 2:
        await message.answer("Для поста нужна новелла с опубликованными главами 1 и 2.")
        return
    first = await db.get_chapter(first_two[0]["id"])
    second = await db.get_chapter(first_two[1]["id"])
    try:
        me = await bot.get_me()
        text = (f"🔥 {series['title'][:180]}\n\n"
                f"{first['content_text'][:600]}…\n\n"
                f"⚡ А дальше — выбор, который изменит всё:\n{second['content_text'][-500:]}\n\n"
                "Продолжение уже ждёт в NovelPulse. Какую цену заплатит герой?\n"
                "#NovelPulse #ЛитРПГ #ВебНовеллы")
        await bot.send_message(channel, text, parse_mode=None, reply_markup=keyboard([
            [InlineKeyboardButton(text="📖 Читать продолжение в боте ➡️", url=f"https://t.me/{me.username}?start=series_{series_id}")],
        ]))
    except TelegramAPIError:
        await message.answer("❌ Публикация не удалась. Проверьте канал и право бота публиковать сообщения в нём.")
        return
    await message.answer(f"✅ Тизер опубликован в {channel}.")


def build_traffic_pack(series: dict, opening: str, cliffhanger: str, username: str) -> tuple[str, str, str]:
    link = f"https://t.me/{username}?start=series_{series['id']}"
    title = series["title"][:180]
    opening = dramatic_excerpt(opening, 260)
    cliffhanger = dramatic_excerpt(cliffhanger, 300)
    voice_opening = complete_excerpt(opening, 100)
    voice_cliffhanger = complete_excerpt(cliffhanger, 130)
    hook = (f"1️⃣ Короткий хук для комментариев и чатов\n\n"
            f"«{title}» — история, где привычный мир перестаёт работать. "
            "Герой делает первый шаг, а опасность уже ждёт за поворотом. "
            "Первые главы и продолжение — в NovelPulse. "
            f"Узнайте, чем обернётся его выбор: {link}")
    script = (f"2️⃣ Сценарий Shorts / Reels / TikTok · 20 секунд\n\n"
              f"0–3 с · Экран: «{title}». Озвучка: «Что, если твой мир изменится за один миг?»\n"
              f"3–8 с · Экран: «Начало приключения». Озвучка: «{voice_opening}»\n"
              f"8–15 с · Экран: «А дальше…». Озвучка: «{voice_cliffhanger}»\n"
              "15–20 с · Экран: «Продолжение в NovelPulse — ссылка в профиле». "
              "Озвучка: «Заходи в бота по ссылке в профиле и читай дальше».\n\n"
              f"Ссылка для профиля: {link}")
    post = (f"3️⃣ Пост для Telegram-канала\n\n🔥 {title}\n\n{opening}\n\n"
            f"⚡ За миг до развязки в бесплатной главе:\n{cliffhanger}\n\n"
            f"Чем закончится этот выбор? Продолжение в NovelPulse:\n{link}\n\n"
            "#NovelPulse #ЛитРПГ #ВебНовеллы")
    return hook, script, post


async def traffic_pack(message: Message, bot: Bot, db: Database) -> None:
    series = await db.latest_imported_series()
    if not series:
        await message.answer("Нет импортированной новеллы с опубликованными главами. Сначала выполните /import_rr или /import_trending.")
        return
    chapters = await db.list_chapters(series["id"])
    free = [chapter for chapter in chapters if chapter["chapter_num"] <= series["free_chapters_limit"]]
    if not free:
        await message.answer("Для Traffic Pack нужна хотя бы одна опубликованная бесплатная глава.")
        return
    first = await db.get_chapter(free[0]["id"])
    last = await db.get_chapter(free[-1]["id"])
    me = await bot.get_me()
    formats = build_traffic_pack(series, first["content_text"], last["content_text"], me.username)
    for index, text in enumerate(formats):
        markup = keyboard([[InlineKeyboardButton(
            text="📖 Читать дальше в боте", url=f"https://t.me/{me.username}?start=series_{series['id']}")]]) if index == 2 else None
        await send_text(bot, message.chat.id, text, markup)


async def video_pack(message: Message, command: CommandObject, bot: Bot, db: Database,
                     video_lock: asyncio.Lock) -> None:
    try:
        await _video_pack(message, command, bot, db, video_lock)
    except Exception as error:
        await message.answer('❌ Видео: ' + failure_reason(error))


async def _video_pack(message: Message, command: CommandObject, bot: Bot, db: Database,
                      video_lock: asyncio.Lock) -> None:
    args = (command.args or "").split()
    try:
        if len(args) > 1 or (args and int(args[0]) < 1):
            raise ValueError
        series = await db.get_series(int(args[0])) if args else await db.latest_imported_series()
    except ValueError:
        await message.answer("Использование: /video_pack [ID новеллы]")
        return
    if not series:
        await message.answer("Новелла не найдена. Сначала выполните /import_trending или укажите ID.")
        return
    if video_lock.locked():
        await message.answer("⏳ Видео уже создаётся. Дождитесь завершения генерации.")
        return
    async with video_lock, media_slot():
        await message.answer("🎬 Генерирую вертикальное MP4-видео с русской озвучкой для Shorts/Reels/TikTok...")
        try:
            chapters = [await db.get_chapter(c["id"]) for c in await db.list_chapters(series["id"])]
            me = await bot.get_me()
            Path("data").mkdir(exist_ok=True)
            # Separate temporary output per request; retain it until Telegram upload completes.
            with tempfile.TemporaryDirectory(prefix="video-pack-", dir="data") as directory:
                path = await generate_promo_video(series, chapters, me.username, Path(directory) / "promo.mp4")
                await message.answer_video(FSInputFile(path), supports_streaming=True,
                    caption=f"🔥 {series['title'][:180]}\n\nЧем закончится история? Продолжение в NovelPulse!\n"
                    f"https://t.me/{me.username}?start=series_{series['id']}\n\n"
                    "#NovelPulse #ЛитРПГ #ВебНовеллы #Shorts #Reels #TikTok", parse_mode=None)
                delete_video(path)
        except Exception as error:
            logger.warning("Видео не создано: %s", failure_reason(error))
            await send_text(bot, message.chat.id, "❌ Видео не создано. Причина: " + failure_reason(error))


async def notify_series(message: Message, command: CommandObject, bot: Bot, db: Database,
                        notify_lock: asyncio.Lock) -> None:
    try:
        series_id = int(command.args or "")
        if series_id < 1:
            raise ValueError
    except ValueError:
        await message.answer("Использование: /notify_series ID_новеллы")
        return
    series = await db.get_series(series_id)
    if not series:
        await message.answer("Новелла не найдена.")
        return
    if notify_lock.locked():
        await message.answer("⏳ Рассылка уже выполняется.")
        return
    sent = failed = 0
    async with notify_lock:
        readers = await db.readers_to_notify(series_id)
        for reader in readers:
            following = await db.next_chapter(series_id, reader["read_chapter"])
            if following is None:
                # Publication can change while the notification batch is running.
                continue
            markup = keyboard([[InlineKeyboardButton(text="📖 Продолжить чтение 👉",
                                callback_data=f"chapter:{following['id']}")]])
            try:
                try:
                    await bot.send_message(reader["tg_id"],
                        f"🔥 Вышли новые главы новеллы «{series['title'][:180]}»! Продолжить чтение 👉",
                        parse_mode=None, reply_markup=markup, disable_notification=True)
                except TelegramRetryAfter as error:
                    if error.retry_after > 60:
                        raise
                    await asyncio.sleep(error.retry_after)
                    await bot.send_message(reader["tg_id"],
                        f"🔥 Вышли новые главы новеллы «{series['title'][:180]}»! Продолжить чтение 👉",
                        parse_mode=None, reply_markup=markup, disable_notification=True)
                await db.mark_notified(reader["tg_id"], series_id, reader["latest_chapter"])
                sent += 1
            except TelegramForbiddenError:
                # A blocked bot must not retry this same release on every command.
                await db.mark_notified(reader["tg_id"], series_id, reader["latest_chapter"])
                failed += 1
            except TelegramAPIError as error:
                logger.warning("Уведомление читателю %s: %s", reader["tg_id"], type(error).__name__)
                failed += 1
            await asyncio.sleep(0.05)
    await message.answer(f"✅ Уведомления: отправлено {sent}, недоставлено {failed}. Повторно эта публикация не рассылается успешно уведомлённым читателям.")


async def seo_pack(message: Message, bot: Bot, db: Database) -> None:
    username = (await bot.get_me()).username
    await message.answer("⏳ Готовлю живые SEO-ссылки и посты с Instant View...")
    results = await publish_all(db, username)
    if not results:
        await message.answer("Каталог пуст. Сначала выполните /seed_demo или /import_trending.")
    for item in results:
        if "error" in item:
            await send_text(bot, message.chat.id, f"❌ Новелла {item['id']}: {item['error']}")
            continue
        series = await db.get_series(item["id"])
        free = [c for c in await db.list_chapters(series["id"]) if c["chapter_num"] <= min(2, series["free_chapters_limit"])]
        chapter = await db.get_chapter(free[-1]["id"]) if free else None
        hook = dramatic_excerpt(chapter["content_text"] if chapter else series["description"])
        await send_text(bot, message.chat.id, seo_teaser(series, hook, username))


async def public_growth_links(db: Database, bot: Bot, settings: Settings) -> tuple[str, str]:
    deployment = json.loads(await db.get_state('public_web_deployment') or '{}')
    portal = settings.web_base_url or (deployment.get('base_url', '') if deployment.get('status') == 'live' else '')
    if not portal.startswith('https://'):
        portal = ''
    channel = channel_link(settings.channel_id) if isinstance(settings.channel_id, str) else ''
    if settings.channel_id and not channel:
        chat = await bot.get_chat(settings.channel_id)
        channel = channel_link('@' + chat.username) if chat.username else ''
    return portal, channel


async def funnel(message: Message, db: Database) -> None:
    from app.analytics import funnel_text
    await message.answer(funnel_text(await db.funnel_summary()),parse_mode=None)


async def weekly(message: Message, db: Database) -> None:
    from app.weekly import weekly_summary, weekly_text
    import html
    text=weekly_text(await weekly_summary(db))
    await message.answer('<pre>'+html.escape(text)+'</pre>',parse_mode='HTML')


async def dashboard(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    from app.analytics import funnel_text
    summary = await db.dashboard_stats()
    portal, channel = await public_growth_links(db, bot, settings)
    lines = ["📊 NovelPulse · Панель управления", "",
             f"🖥 NODE_MODE: {settings.node_mode} · /node_status · /sync_catalog",
             f"📚 Новелл: {summary['series_count']} · Опубликованных глав: {summary['chapters']}",
             f"Бесплатных: {summary['free']} · Платных: {summary['paid']}",
             f"Розничная стоимость платных глав: {summary['retail']} ⭐️ · VIP на 7 дней: 49 ⭐️",
             "Покупки глав бессрочны; VIP действует 7 дней.", "",
             f"👥 Пользователи: {summary['users']} · Рефералы: {summary['referrals']}",
             f"💰 Выручка за всё время: {summary['revenue']} ⭐️", "",
             '🌐 WEB_BASE_URL: ' + (portal or 'публичный HTTPS-адрес не настроен'),
             '📢 Канал: ' + (channel or 'публичная ссылка не настроена'), '',
             "⚡ SEO и видео:"]
    from app.cloud_failover import cloud_summary
    lines.insert(3,await cloud_summary(db,settings))
    for series in await db.list_all_series():
        path = TRAFFIC_DIR / f"series_{series['id']}.mp4"
        lines.extend([f"{series['id']}. {series['title'][:180]}",
                      series['telegraph_url'] or "SEO: ещё не опубликовано",
                      "🎬 JIT: создаётся перед отправкой и удаляется после доставки"])
    last = await db.get_state("autopilot_last_report")
    if last:
        report = json.loads(last)
        status = {"complete": "готово", "partial": "частично"}.get(report["status"], report["status"])
        lines.extend(["", f"🤖 Последний цикл: {report['finished_at']} · {status}",
                      f"Ошибок: {len(report['errors'])} · Канал: {report['channel']}"])
    lines.append(f"Фоновый интервал: {settings.autopilot_interval_hours:g} ч (0 = ручной запуск)")
    lines.append(disk_status_text())
    lines.extend(['',funnel_text(await db.funnel_summary(),compact=True),'Подробно: /funnel'])
    if summary['channel_posts']:
        states = {"pending": "ожидает сверки", "sent": "опубликовано", "failed": "ошибка", "uncertain": "требует сверки"}
        lines.append("Публикации: " + ", ".join(f"{states[p['state']]}: {p['count']}" for p in summary['channel_posts']))
    await send_text(bot, message.chat.id, "\n".join(lines))


async def autopilot_now(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    if db.autopilot_lock.locked():
        await message.answer("⏳ Автопилот уже работает. Статус доступен в /dashboard.")
        return
    await message.answer("🤖 Автопилот запущен: публикую SEO, готовлю MP4 и проверяю очередь канала...")
    try:
        report = await run_autopilot_cycle(db, bot, settings=settings)
        text = (f"{'✅' if report['status'] == 'complete' else '⚠️'} Цикл завершён.\n"
                f"Новых статей: {len(report['seo_created'])} · Новых видео: {len(report['videos_created'])}\n"
                f"Канал: {report['channel']}\nМанифест: data/traffic_drop/README_PUBLISH.md")
        text += f"\nНапоминаний: {report.get('followups', {}).get('sent', 0)}"
        if report.get('serial'):
            serial = report['serial']
            text += f"\nСериал: сообщение {serial['message_id']}" if serial.get('message_id') else "\nСериал: сейчас нет следующего эпизода или ещё не наступил интервал."
        for error in report["errors"]:
            step = {"seo": "статья", "video": "видео", "channel": "канал"}.get(error["step"], error["step"])
            text += f"\n❌ Новелла {error['id']} · {step}: {error['reason']}"
        await send_text(bot, message.chat.id, text)
    except Exception as error:
        await send_text(bot, message.chat.id, "❌ Автопилот не завершён: " + failure_reason(error))


async def seed_channel(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    if not settings.channel_id:
        await message.answer("Укажите CHANNEL_ID в .env и перезапустите бота.")
        return
    if db.autopilot_lock.locked():
        await message.answer("⏳ Подготовка канала уже выполняется.")
        return
    await message.answer("🎬 Готовлю витрину канала: тизеры, видеотрейлеры и закреплённый навигатор...")
    try:
        report = await publish_channel_showcase(db, bot, settings.channel_id)
        pin = (report.get("navigator") or {}).get("message_id")
        text = f"✅ Новых тизеров: {len(report['new_teasers'])} · Трейлеров: {len(report['new_videos'])}\nНавигатор: {pin or 'не опубликован'}"
        for error in report["errors"]:
            text += f"\n❌ {error['id']}: {error['reason']}"
        await send_text(bot, message.chat.id, text)
    except Exception as error:
        await send_text(bot, message.chat.id, "❌ Витрина канала не завершена: " + failure_reason(error))


async def followup_now(message: Message, bot: Bot, db: Database) -> None:
    await message.answer("⏳ Проверяю незавершённые открытия глав и отправляю одноразовые напоминания...")
    report = await send_paywall_followups(db, bot)
    text = f"✅ Напоминания: отправлено {report['sent']} · ошибок {len(report['errors'])}."
    for error in report['errors']:
        text += f"\n❌ Просмотр {error['view_id']}: {error['reason']}"
    await send_text(bot, message.chat.id, text)


async def growth_kit(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    portal, channel = await public_growth_links(db, bot, settings)
    navigator = json.loads(await db.get_state(f'channel_navigator:{settings.channel_id}') or '{}')
    pin_link = f"{channel}/{navigator['message_id']}" if channel and navigator.get('pinned') else ''
    counts = await db.followup_counts()
    lines = ['🚀 NovelPulse · Набор для роста', '', '☑ Канал: ' + (channel or 'публичная ссылка не настроена'),
             '☑ Закреплённый навигатор: ' + (pin_link or 'запустите /seed_channel'), '',
             '☑ SEO-подборки для подписей, форумов и профилей:']
    for hub in json.loads(await db.get_state('seo_hubs') or '[]'):
        lines.extend([hub['title'], hub['url']])
    lines.extend(['', '☑ Ролики для Shorts / TikTok / VK Клипов:'])
    for series in await db.list_series():
        path = TRAFFIC_DIR / f"series_{series['id']}.mp4"
        lines.extend([series['title'][:180], str(path.resolve()),
                      '🎬 JIT: /next_short или /autopublish_short · файл удаляется после отправки'])
    lines.extend(['', f"☑ Напоминания: ожидают {counts['pending']} · отправлено {counts['sent']}",
                  f"Ошибок доставки: {counts['failed']} · требуют сверки: {counts['uncertain']}",
                  'Статическая библиотека: ' + str(Path('docs/index.html').resolve())])
    if portal:
        lines.append('🌐 WEB_BASE_URL: ' + portal)
    shorts = await db.list_shorts()
    if shorts:
        lines.extend(['', f"🎬 Серийных роликов: {len(shorts)} · ждут доставки: {sum(s['status'] == 'pending' for s in shorts)}",
                      'Следующий ролик с телефона: /next_short · с ПК: make next-short'])
    await send_text(bot, message.chat.id, '\n'.join(lines))


async def next_short(message: Message, bot: Bot, db: Database) -> None:
    try:
        async with media_slot():
            await _next_short(message, bot, db)
    except Exception as error:
        await message.answer('❌ ' + failure_reason(error))


async def _next_short(message: Message, bot: Bot, db: Database) -> None:
    short = await db.claim_short()
    if not short:
        await message.answer('✅ Все подготовленные ролики уже доставлены. Новые эпизоды появятся после генерации очереди.')
        return
    delivered = None
    try:
        path = await ensure_short_video(db, short, (await bot.get_me()).username, root=SHORTS_DIR)
        delivered = await message.answer_video(FSInputFile(path), caption=short['title'],
                                                parse_mode=None, supports_streaming=True)
        await db.finish_short(short['id'], 'sent', delivered.message_id)
        delete_video(path, root=SHORTS_DIR)
        await send_text(bot, message.chat.id, short['title'] + '\n\n' + short['description'])
        await message.answer(f"✅ Ролик {short['id']} доставлен. После публикации на ПК: scripts/upload_assistant.py --mark-done {short['id']}")
    except Exception as error:
        if delivered:
            await db.finish_short(short['id'], 'sent', delivered.message_id)
            delete_video(path, root=SHORTS_DIR)
            prefix = f"Видео {short['id']} доставлено, но подпись/подтверждение не отправлены. Повторно видео не пересылаю. "
        else:
            state = 'pending' if isinstance(error, (ValueError, TelegramAPIError)) and not isinstance(error, TelegramNetworkError) else 'uncertain'
            await db.finish_short(short['id'], state)
            prefix = f"Не удалось доставить ролик {short['id']}. "
        await send_text(bot, message.chat.id, '❌ ' + prefix + failure_reason(error))


def disk_status_text():
    status = storage_status()
    return (f"💾 MP4 на диске: {status['mp4_count']} · {status['mp4_bytes'] / 1_000_000:.2f} МБ\n"
            f"Всего data/: {status['data_bytes'] / 1_000_000:.2f} МБ. Политика: отправить → удалить.")


async def disk_status(message: Message):
    await message.answer(disk_status_text())


async def autopublish_short(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings):
    if not settings.channel_id:
        await message.answer('Укажите CHANNEL_ID в .env и перезапустите бота.')
        return
    try:
        short_id = int(command.args) if command.args else None
        await message.answer('🎬 Публикую один эпизод в YouTube и Telegram; затем удалю MP4 и отправлю владельцу ссылки.')
        report = await publish_short_pair(db, bot, settings, short_id=short_id)
        await message.answer('✅ ' + json.dumps(report, ensure_ascii=False) if not report.get('error')
                             else '❌ ' + report['error'])
    except Exception as error:
        await message.answer('❌ ' + failure_reason(error))


async def send_weekly_digest(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    from app.digest import send_weekly_digest_broadcast
    mode = (command.args or "").strip().lower()
    if mode not in ("preview", "broadcast"):
        await message.answer(
            "Использование:\n"
            "/send_weekly_digest preview — тестовое превью дайджеста администратору\n"
            "/send_weekly_digest broadcast — живая рассылка дайджеста всем подходящим читателям"
        )
        return
    try:
        dry_run = mode == "preview"
        report = await send_weekly_digest_broadcast(
            db,
            bot,
            dry_run=dry_run,
            admin_id=message.from_user.id if message.from_user else None,
        )
        if dry_run:
            await message.answer(
                f"✅ Превью еженедельного дайджеста отправлено администратору (message_id={report.get('preview_message_id')})."
            )
        else:
            await message.answer(
                "📬 Рассылка «Дайджест недели NovelPulse» завершена:\n"
                f"• Подходящих читателей: {report['eligible']}\n"
                f"• Успешно доставлено: {report['sent']}\n"
                f"• Бот заблокирован / недоступен: {report['blocked']}\n"
                f"• Прочие ошибки: {report['failed']}"
            )
    except Exception as error:
        await message.answer("❌ " + failure_reason(error))


def create_router() -> Router:
    router = Router(name="admin")
    router.message.register(brain_sync, Command('brain_sync', 'ceo_brief'), IsAdmin())
    router.message.register(funnel, Command('funnel'), IsAdmin())
    router.message.register(weekly, Command('weekly'), IsAdmin())
    router.message.register(denied, Command('weekly'))
    router.message.register(denied, Command('funnel'))
    router.message.register(denied, Command('brain_sync', 'ceo_brief'))
    router.message.register(stats, Command("stats"), IsAdmin())
    router.message.register(seed_demo, Command("seed_demo"), IsAdmin())
    router.message.register(promo, Command("promo"), IsAdmin())
    router.message.register(import_trending, Command("import_trending"), IsAdmin())
    router.message.register(import_rr, Command("import_rr"), IsAdmin())
    router.message.register(post_channel, Command("post_channel"), IsAdmin())
    router.message.register(traffic_pack, Command("traffic_pack"), IsAdmin())
    router.message.register(video_pack, Command("video_pack"), IsAdmin())
    router.message.register(notify_series, Command("notify_series"), IsAdmin())
    router.message.register(seo_pack, Command("seo_pack"), IsAdmin())
    router.message.register(dashboard, Command("dashboard"), IsAdmin())
    router.message.register(autopilot_now, Command("autopilot_now"), IsAdmin())
    router.message.register(seed_channel, Command("seed_channel"), IsAdmin())
    router.message.register(followup_now, Command("followup_now"), IsAdmin())
    router.message.register(growth_kit, Command("growth_kit"), IsAdmin())
    router.message.register(next_short, Command("next_short"), IsAdmin())
    router.message.register(disk_status, Command("disk_status"), IsAdmin())
    router.message.register(autopublish_short, Command("autopublish_short"), IsAdmin())
    router.message.register(send_weekly_digest, Command("send_weekly_digest"), IsAdmin())
    router.message.register(denied, Command("stats", "seed_demo", "promo", "import_trending", "import_rr", "post_channel", "traffic_pack", "video_pack", "notify_series", "seo_pack", "dashboard", "autopilot_now", "seed_channel", "followup_now", "growth_kit", "next_short", "disk_status", "autopublish_short", "send_weekly_digest"))
    return router
