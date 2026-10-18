"""Restart-safe SEO/video preparation, reader follow-ups and episodic channel feeding."""

import argparse
import asyncio
import hashlib
import json
import logging
import html
import os
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from app.config import Settings
from app.db import Database
from app.pipeline import failure_reason
from app.promotional_text import dramatic_excerpt
from app.telegraph_seo import bot_link, publish_series, publish_seo_cluster, seo_teaser
from app.video_gen import generate_promo_video
from app.retention import send_paywall_followups
from app.text import split_text
from app.privacy import public_bot, sanitize_public_text

logger = logging.getLogger(__name__)
TRAFFIC_DIR = Path("data/traffic_drop")


def video_fingerprint(series: dict, chapters: list[dict], username: str) -> str:
    values = {"version": 1, "title": series["title"], "id": series["id"], "username": username,
              "free_limit": series["free_chapters_limit"], "chapters": [
                  {"num": c["chapter_num"], "text": c["content_text"]}
                  for c in sorted(chapters, key=lambda c: c["chapter_num"])
                  if c["chapter_num"] <= series["free_chapters_limit"]]}
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def video_ready(path: Path, fingerprint: str | None = None) -> bool:
    try:
        with path.open("rb") as source:
            valid_header = b"ftyp" in source.read(32)
        metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        return (valid_header and path.stat().st_size == metadata["bytes"] and not metadata["mock_tts"]
                and 20 <= metadata["duration"] <= 30
                and (fingerprint is None or metadata.get("input_digest") == fingerprint))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def ready_caption(series: dict, username: str) -> str:
    return (f"🔥 {series['title'][:180]}\n\nЧем закончится история? Читайте первые главы бесплатно в NovelPulse!\n"
            f"📖 {bot_link(username, series['id'])}\n"
            f"⚡ Статья с двумя главами: {series['telegraph_url']}\n"
            "👑 VIP на 7 дней — 49 ⭐️\n\n#NovelPulse #ЛитРПГ #ВебНовеллы #Shorts #Reels #TikTok")


async def write_manifest(db: Database, username: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["# NovelPulse — Traffic Drop", "",
             "Режим JIT: ролик создаётся перед отправкой и удаляется после подтверждения. Подписи можно копировать целиком.",
             "В соцсетях разместите ссылку на бота в профиле/комментариях. В Telegram ссылка Telegraph открывает статью с Instant View.",
             "Покупки глав остаются навсегда; VIP действует 7 дней.", ""]
    for series in await db.list_all_series():
        path = directory / f"series_{series['id']}.mp4"
        lines.extend([f"## {series['id']}. {series['title']}", "",
                      f"Видео: [{path.name}]({path.name}) · " + (f"{path.stat().st_size} байт" if video_ready(path) else "JIT · локальный MP4 не хранится"),
                      f"Статья: {series['telegraph_url'] or 'не опубликована'}",
                      f"Бот: {bot_link(username, series['id'])}", "", "Готовая подпись:", "", ready_caption(series, username), ""])
    target = directory / "README_PUBLISH.md"
    temporary = directory / "README_PUBLISH.md.tmp"
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(target)
    return target


async def _run_autopilot_cycle(db: Database, bot: Bot, *, settings: Settings | None = None,
                              output_dir: str | Path = TRAFFIC_DIR) -> dict:
    bot = public_bot(bot)
    settings = settings or Settings()
    directory = Path(output_dir)
    async with db.autopilot_lock:
        report = {"started_at": datetime.now(timezone.utc).isoformat(), "seo_created": [],
                  "videos_created": [], "series": [], "errors": [], "channel": "не настроен"}
        username = (await bot.get_me()).username
        from app.notifications import retry_owner_alerts
        report['owner_alert_retries'] = [] if settings.autopilot_worker else await retry_owner_alerts(db, bot, settings)
        directory.mkdir(parents=True, exist_ok=True)
        for series in await db.list_all_series():
            item = {"id": series["id"], "title": series["title"]}
            try:
                url = await publish_series(db, series["id"], username)
                item["telegraph_url"] = url
                if not series["telegraph_url"]:
                    report["seo_created"].append(series["id"])
            except Exception as error:
                report["errors"].append({"id": series["id"], "step": "seo", "reason": failure_reason(error)})
            item['video_policy'] = 'JIT: generate, deliver, delete'
            report["series"].append(item)
        report["manifest"] = str((await write_manifest(db, username, directory)).resolve())
        if settings.channel_id is not None:
            channel = settings.channel_id
            report["channel"] = "нет неопубликованных новелл"
            series = await db.latest_unposted_series(channel)
            if series:
                if await db.claim_channel_post(channel, series["id"]):
                    try:
                        chapters = await db.list_chapters(series["id"])
                        free = [c for c in chapters if c["chapter_num"] <= series["free_chapters_limit"]]
                        chapter = await db.get_chapter(free[-1]["id"])
                        result = await bot.send_message(channel, seo_teaser(series, dramatic_excerpt(chapter["content_text"]), username),
                            parse_mode=None, link_preview_options=LinkPreviewOptions(url=series["telegraph_url"]),
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                                text="📖 Читать продолжение в боте ➡️", url=bot_link(username, series["id"]))]]))
                        await db.finish_channel_post(channel, series["id"], "sent", result.message_id)
                        report["channel"] = f"опубликована новелла {series['id']}, сообщение {result.message_id}"
                    except Exception as error:
                        # Ambiguous network failures must not result in duplicate public posts.
                        state = "failed" if isinstance(error, TelegramAPIError) and not isinstance(error, TelegramNetworkError) else "uncertain"
                        await db.finish_channel_post(channel, series["id"], state)
                        report["channel"] = "ошибка публикации: " + failure_reason(error)
                        report["errors"].append({"id": series["id"], "step": "channel", "reason": failure_reason(error), "state": state})
        try:
            report["followups"] = (await send_paywall_followups(db, bot, min_age_seconds=3600)
                if settings.node_mode != 'youtube_factory' else {'sent': 0, 'errors': [], 'owner': 'bot_24x7'})
            report["errors"].extend({"id": item['view_id'], "step": "followup", **item}
                                    for item in report["followups"]["errors"])
        except Exception as error:
            report["errors"].append({"id": 0, "step": "followup", "reason": failure_reason(error)})
        try:
            from app.digest import run_autopilot_weekly_digest
            report["weekly_digest"] = (await run_autopilot_weekly_digest(db, bot)
                if settings.node_mode != 'youtube_factory' else {'triggered': False, 'owner': 'bot_24x7'})
        except Exception as error:
            report["errors"].append({"id": 0, "step": "weekly_digest", "reason": failure_reason(error)})
        if settings.channel_id is not None:
            try:
                report["serial"] = await publish_next_serial(db, bot, settings.channel_id,
                    min_interval_hours=max(settings.autopilot_interval_hours, 1))
                if report["serial"].get("error"):
                    report["errors"].append({"id": report["serial"].get("series_id", 0), "step": "serial",
                                            "reason": report["serial"]["error"]})
            except Exception as error:
                report["errors"].append({"id": 0, "step": "serial", "reason": failure_reason(error)})
        if settings.channel_id is not None:
            try:
                from app.shorts import generate_shorts_queue
                from app.publishing import publish_short_pair
                from app.replenishment import replenish_content_queue
                from app.youtube_uploader import MAX_YOUTUBE_UPLOADS_PER_24H, youtube_blocked_until
                from app.shorts import SHORTS_DIR
                report['youtube_pacer'] = {'max_uploads_per_24h': MAX_YOUTUBE_UPLOADS_PER_24H,
                    'cooldown_until': await youtube_blocked_until(db)}
                shorts_dir = SHORTS_DIR if directory.resolve() == TRAFFIC_DIR.resolve() else directory / 'shorts_queue'
                if not json.loads(await db.get_state('replenishment_checkpoint') or '{}'):
                    await generate_shorts_queue(db, username, settings.channel_id, portal_url=settings.web_base_url or '', output_dir=shorts_dir)
                try:
                    report['replenishment'] = await replenish_content_queue(db, bot, settings, output_dir=shorts_dir)
                except Exception as error:
                    report['errors'].append({'id': 0, 'step': 'replenishment', 'reason': failure_reason(error)})
                report['short'] = await publish_short_pair(db, bot, settings,
                    root=shorts_dir,
                    min_interval_hours=max(settings.autopilot_interval_hours, 1))
                if report['short'].get('error'):
                    report['errors'].append({'id': report['short'].get('short_id', 0), 'step': 'short',
                                             'reason': report['short']['error']})
                if sum(s['status'] == 'pending' for s in await db.list_shorts()) < 3:
                    try:
                        report['replenishment_after_upload'] = await replenish_content_queue(db, bot, settings, output_dir=shorts_dir)
                    except Exception as error:
                        report['errors'].append({'id': 0, 'step': 'replenishment', 'reason': failure_reason(error)})
            except Exception as error:
                report['errors'].append({'id': 0, 'step': 'short', 'reason': failure_reason(error)})
        try:
            from app.storage_cleanup import media_slot, enforce_zero_mp4_clutter
            async with media_slot():
                cleanup_root = Path('data') if directory.resolve() == TRAFFIC_DIR.resolve() else directory
                report['cleanup'] = enforce_zero_mp4_clutter(cleanup_root)
        except Exception as error:
            report['errors'].append({'id': 0, 'step': 'cleanup', 'reason': failure_reason(error)})
        if report['errors']:
            from app.notifications import notify_admins
            await notify_admins(bot, '⚠️ Цикл автопилота завершён с ошибками: ' +
                '; '.join(f"{e['step']}: {e['reason']}" for e in report['errors'])[:3000], settings=settings)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            from app.node_sync import export_catalog
            snapshot_path = settings.catalog_snapshot_path if directory.resolve() == TRAFFIC_DIR.resolve() else directory / 'catalog_snapshot.json'
            report['catalog_snapshot'] = await export_catalog(db, snapshot_path)
        except Exception as error:
            report['errors'].append({'id': 0, 'step': 'catalog_snapshot', 'reason': failure_reason(error)})
        report["status"] = "complete" if not report["errors"] else "partial"
        await db.set_state("autopilot_last_report", json.dumps(report, ensure_ascii=False))
        return report


async def run_autopilot_cycle(db: Database, bot: Bot, *, settings: Settings | None = None,
                              output_dir: str | Path = TRAFFIC_DIR) -> dict:
    """Cleanup also runs when startup/network failures escape the inner cycle."""
    bot = public_bot(bot)
    settings=settings or Settings()
    if settings.cloud_failover_enabled and not settings.autopilot_worker:
        from app.autopilot_process import run_isolated_cycle
        try:
            return await run_isolated_cycle(db,bot,settings,output_dir)
        finally:
            await refresh_handoff(db, output_dir)
    directory = Path(output_dir)
    cleanup_root = Path('data') if directory.resolve() == TRAFFIC_DIR.resolve() else directory
    try:
        return await _run_autopilot_cycle(db, bot, settings=settings, output_dir=output_dir)
    finally:
        from app.storage_cleanup import media_slot, enforce_zero_mp4_clutter
        try:
            async with media_slot():
                enforce_zero_mp4_clutter(cleanup_root)
        except RuntimeError:
            # Another valid job owns the slot; never remove its active transfer.
            logger.info('Очистка отложена: другой процесс ещё отправляет MP4')
        finally:
            await refresh_handoff(db, output_dir)


async def refresh_handoff(db, output_dir):
    """Production updates the root prompt; isolated test DBs never overwrite it."""
    from app.brain_sync import generate_orchestrator_handoff, ROOT
    target = None if Path(db.path).resolve().parent == ROOT/'data' else Path(output_dir)/'ORCHESTRATOR_STATE.md'
    try:
        await generate_orchestrator_handoff(db, output_path=target)
    except Exception:
        logger.exception('Не удалось обновить ORCHESTRATOR_STATE.md')


async def background_autopilot(db: Database, bot: Bot, settings: Settings) -> None:
    bot = public_bot(bot)
    if settings.autopilot_interval_hours <= 0:
        return
    while True:
        if (Path(db.path).resolve().parent/'autopilot.pause').exists():
            await db.set_state('autopilot_pacer_status', json.dumps({'state': 'paused', 'pid': os.getpid(),
                'checked_at': datetime.now(timezone.utc).isoformat()}))
            await asyncio.sleep(5)
            continue
        from app.scheduling import upload_schedule
        schedule = await upload_schedule(db, settings)
        await db.set_state('autopilot_pacer_status', json.dumps({'state': 'running' if schedule['due'] else 'armed',
            'pid': os.getpid(), 'checked_at': datetime.now(timezone.utc).isoformat(),
            'next_upload_at': schedule['next_upload_at'], 'cooldown_until': schedule['cooldown_until'],
            'max_check_interval_seconds': 60}))
        if not schedule['due']:
            # One catch-up publication, not a burst for every missed overnight slot.
            # Re-read cooldown/queue every minute; resume at expiry, even after edits.
            await asyncio.sleep(min(60, schedule['seconds_remaining']))
            continue
        try:
            report = await run_autopilot_cycle(db, bot, settings=settings)
            if settings.channel_id and not settings.cloud_failover_enabled:
                chat = await bot.get_chat(settings.channel_id)
                if chat.username:
                    await publish_seo_cluster(db, (await bot.get_me()).username, "@" + chat.username)
                showcase = await publish_channel_showcase(db, bot, settings.channel_id)
                logger.info("Витрина канала: %s; ошибок: %s", showcase["navigator"], len(showcase["errors"]))
            logger.info("Автопилот: %s; ошибок: %s", report["status"], len(report["errors"]))
        except Exception as error:
            logger.warning("Автопилот: %s", failure_reason(error))
        schedule = await upload_schedule(db, settings)
        # Failed/empty cycles use the normal interval rather than spinning while overdue.
        await asyncio.sleep(min(60, schedule['seconds_remaining']) if not schedule['due']
                            else settings.autopilot_interval_hours * 3600)


def build_navigator(series: list[dict], username: str, hubs: list[dict], portal_url: str = '',
                    total_chapters: int | None = None) -> str:
    lines = ["🧭 <b>Навигатор по библиотеке NovelPulse</b>", "",
             "🔥 Первая платная глава новому читателю — всего 1 ⭐️!", "",
             "Две первые главы бесплатны. Выберите свой мир:"]
    if total_chapters is not None:
        lines.insert(2, f"📚 {len(series)} новелл · {total_chapters} глав")
    if portal_url:
        from app.config import PipelineSettings
        portal_url = PipelineSettings(_env_file=None, web_base_url=portal_url).web_base_url
        lines.extend(['', f'<a href="{html.escape(portal_url, quote=True)}">🌐 Веб-библиотека NovelPulse · читать в браузере</a>'])
    for number, item in enumerate(series, 1):
        lines.extend([f"\n{number}. <a href=\"{bot_link(username, item['id'])}\">{html.escape(item['title'][:130])}</a>",
                      f"<a href=\"{html.escape(item['telegraph_url'], quote=True)}\">⚡ Читать бесплатно · Instant View</a>"])
    lines.extend(["", "👑 VIP на 7 дней — 49 ⭐️: все главы всех новелл.", "Покупки глав бессрочны; VIP действует 7 дней."])
    for label, hub in zip(("Топ-5 новелл", "Система и попаданцы", "Полный каталог"), hubs):
        lines.append(f"<a href=\"{html.escape(hub['url'], quote=True)}\">🧭 {label}</a>")
    text = sanitize_public_text("\n".join(lines))
    # Telegram limits rendered text after HTML entities are parsed. The URLs in
    # link attributes do not consume the 4096-character message allowance.
    from html.parser import HTMLParser
    class VisibleText(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
        def handle_data(self, data):
            self.parts.append(data)
    visible = VisibleText()
    visible.feed(text)
    if len(''.join(visible.parts).encode("utf-16-le")) // 2 > 4096:
        raise ValueError("Навигатор превышает лимит Telegram; сократите названия новелл")
    return text


async def _channel_request(operation):
    try:
        return await operation()
    except TelegramRetryAfter as error:
        if error.retry_after > 60:
            raise
        await asyncio.sleep(error.retry_after)
        return await operation()


def _send_failure_state(error):
    return "failed" if isinstance(error, TelegramAPIError) and not isinstance(error, TelegramNetworkError) else "uncertain"


def serial_excerpt(text: str) -> str:
    """Keep 1200–1800 characters, preferring a complete sentence or paragraph."""
    excerpt = text[:1799]
    if len(text) <= 1800:
        return excerpt.strip()
    endings = [i + 1 for i, char in enumerate(excerpt) if char in '.!?»\n' and i >= 1200]
    boundary = endings[-1] if endings else excerpt.rfind(' ', 1200)
    return excerpt[:boundary if boundary >= 1200 else 1799].rstrip() + '…'


async def publish_next_serial(db: Database, bot: Bot, channel_id: str | int, *,
                              min_interval_hours: float = 0) -> dict:
    bot = public_bot(bot)
    chapter = await db.claim_serial_chapter(channel_id, min_interval_hours)
    if chapter is None:
        return {"status": "idle"}
    report = {"series_id": chapter['series_id'], "chapter_id": chapter['id']}
    try:
        username = (await bot.get_me()).username
        text = (f"📖 Читаем в ленте · {chapter['series_title'][:180]} (Глава {chapter['chapter_num']})\n\n"
                f"{serial_excerpt(chapter['content_text'])}\n\n"
                "─── На этом месте история обрывается… ───\n"
                "🔥 Что случится дальше? Первые главы бесплатны, первая платная глава новому читателю — 1 ⭐️!")
        text = split_text(text, limit=3900)[0]
        result = await _channel_request(lambda: bot.send_message(channel_id, text, parse_mode=None,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="🔥 Читать дальше в боте (первая платная глава — 1 ⭐️) ➡️",
                url=bot_link(username, chapter['series_id']))]])))
        await db.finish_serial_post(channel_id, chapter['id'], 'sent', result.message_id)
        report.update(status='sent', message_id=result.message_id, title=chapter['series_title'],
                      chapter_num=chapter['chapter_num'])
    except Exception as error:
        state = _send_failure_state(error)
        await db.finish_serial_post(channel_id, chapter['id'], state)
        report.update(status=state, error=failure_reason(error))
    return report


async def publish_channel_showcase(db: Database, bot: Bot, channel_id: str | int, *,
                                    output_dir: str | Path = TRAFFIC_DIR) -> dict:
    """Publish missing teasers/videos, then create or update one persistently tracked pin."""
    bot = public_bot(bot)
    async with db.autopilot_lock:
        username = (await bot.get_me()).username
        series = await db.list_series()
        hubs = json.loads(await db.get_state("seo_hubs") or "[]")
        report = {"channel_id": channel_id, "new_teasers": [], "new_videos": [], "navigator": None, "errors": []}
        for item in series:
            if not item["telegraph_url"]:
                report["errors"].append({"id": item["id"], "reason": "Не готова SEO-статья"})
                continue
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="📖 Читать продолжение в боте ➡️", url=bot_link(username, item["id"]))]])
            if await db.claim_channel_post(channel_id, item["id"]):
                try:
                    free = [c for c in await db.list_chapters(item["id"]) if c["chapter_num"] <= 2]
                    chapter = await db.get_chapter(free[-1]["id"])
                    text = seo_teaser(item, dramatic_excerpt(chapter["content_text"]), username)
                    text += "\n\n🔥 Первая платная глава новому читателю — всего 1 ⭐️!"
                    sent = await _channel_request(lambda: bot.send_message(channel_id, text, parse_mode=None,
                        reply_markup=markup, link_preview_options=LinkPreviewOptions(url=item["telegraph_url"])))
                    await db.finish_channel_post(channel_id, item["id"], "sent", sent.message_id)
                    report["new_teasers"].append({"series_id": item["id"], "message_id": sent.message_id})
                except Exception as error:
                    await db.finish_channel_post(channel_id, item["id"], _send_failure_state(error))
                    report["errors"].append({"id": item["id"], "reason": failure_reason(error)})
                await asyncio.sleep(1.1)
            # Already delivered trailers are represented by receipts, not local media.
            if await db.claim_channel_video(channel_id, item['id']):
                from app.storage_cleanup import media_slot, delete_video
                started_send = False
                try:
                    async with media_slot():
                        path = Path(output_dir) / f"series_{item['id']}.mp4"
                        chapters = [await db.get_chapter(c['id']) for c in await db.list_chapters(item['id'])]
                        if not video_ready(path, video_fingerprint(item, chapters, username)):
                            await generate_promo_video(item, chapters, username, path)
                        if not video_ready(path):
                            raise ValueError('Не готов MP4 с настоящей нейронной озвучкой')
                        started_send = True
                        sent = await _channel_request(lambda: bot.send_video(channel_id, FSInputFile(path),
                            caption=ready_caption(item, username)[:1000], parse_mode=None,
                            reply_markup=markup, supports_streaming=True))
                        await db.finish_channel_video(channel_id, item['id'], 'sent', sent.message_id)
                        delete_video(path, root=Path(output_dir))
                        report['new_videos'].append({'series_id': item['id'], 'message_id': sent.message_id})
                except Exception as error:
                    # A stored sent receipt wins over a subsequent cleanup failure.
                    receipts = await db.channel_showcase_messages(channel_id)
                    if not any(r['series_id'] == item['id'] and r['state'] == 'sent' for r in receipts['videos']):
                        await db.finish_channel_video(channel_id, item['id'], _send_failure_state(error) if started_send else 'failed')
                    report['errors'].append({'id': item['id'], 'reason': failure_reason(error)})
                await asyncio.sleep(1.1)
        if series and all(s["telegraph_url"] for s in series):
            key = f"channel_navigator:{channel_id}"
            navigator = json.loads(await db.get_state(key) or "{}")
            if navigator.get("state") == "failed" and not navigator.get("message_id"):
                navigator = {}
            try:
                deployment = json.loads(await db.get_state('public_web_deployment') or '{}')
                text = build_navigator(series, username, hubs, deployment.get('base_url', '') if deployment.get('status') == 'live' else '')
                digest = hashlib.sha256(text.encode()).hexdigest()
                if not navigator:
                    navigator = {"state": "pending", "digest": digest, "pinned": False}
                    await db.set_state(key, json.dumps(navigator))
                    try:
                        sent = await _channel_request(lambda: bot.send_message(channel_id, text, parse_mode="HTML",
                            link_preview_options=LinkPreviewOptions(is_disabled=True)))
                    except Exception as error:
                        navigator["state"] = _send_failure_state(error)
                        await db.set_state(key, json.dumps(navigator))
                        raise
                    navigator.update(message_id=sent.message_id, state="sent")
                    await db.set_state(key, json.dumps(navigator))
                elif not navigator.get("message_id"):
                    raise ValueError("Предыдущая отправка навигатора не подтверждена; проверьте канал перед повторной публикацией")
                elif navigator.get("digest") != digest:
                    await _channel_request(lambda: bot.edit_message_text(text, chat_id=channel_id,
                        message_id=navigator["message_id"], parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)))
                    navigator["digest"] = digest
                    await db.set_state(key, json.dumps(navigator))
                if not navigator.get("pinned"):
                    await _channel_request(lambda: bot.pin_chat_message(chat_id=channel_id,
                        message_id=navigator["message_id"], disable_notification=True))
                    navigator["pinned"] = True
                    await db.set_state(key, json.dumps(navigator))
                report["navigator"] = navigator
            except Exception as error:
                report["navigator"] = navigator
                report["errors"].append({"id": "navigator", "reason": failure_reason(error)})
        report["messages"] = await db.channel_showcase_messages(channel_id)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        await db.set_state("channel_showcase_last_report", json.dumps(report, ensure_ascii=False))
        return report


async def _cli(args):
    settings = Settings()
    db = Database(settings.db_path)
    await db.open()
    try:
        async with Bot(settings.bot_token.get_secret_value()) as bot:
            report = await run_autopilot_cycle(db, bot, settings=settings, output_dir=args.output_dir)
        Path("data").mkdir(exist_ok=True)
        Path("data/stage6_autopilot_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0 if report["status"] == "complete" else 1
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Подготовка SEO/MP4 и один пост в настроенный канал")
    parser.add_argument("--output-dir", default=str(TRAFFIC_DIR))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_cli(args)))
