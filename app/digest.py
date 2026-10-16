"""Smart personalized Weekly Novelties push digest and anti-flood broadcast dispatcher."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db import Database

logger = logging.getLogger(__name__)

DIGEST_HEADER = "🗞 [ ДАЙДЖЕСТ НЕДЕЛИ NOVELPULSE ]"
DEFAULT_FALLBACK_HIT = "🎯 Начните приключение с нашего хита: «Рельсотрон магии молний»!"
WUF_REMINDER = "⏳ Ваш суточный бесплатный таймер активен — откройте 1 главу без оплаты!"
STREAK_REMINDER = "🎁 Не забудьте забрать ежедневный стрик (/start -> Дар Системы)"
VIP_CALLOUT = "⚡ Ночной VIP на 24 часа — всего 19 ⭐️ (или 9 ⭐️ со скидкой E-ранга)!"
ANTI_FLOOD_DELAY_SECONDS = 0.05
WEEKLY_DIGEST_INTERVAL = timedelta(days=7)
WEEKLY_DIGEST_STATE_KEY = "last_weekly_digest_broadcast_at"

FALLBACK_HITS = (
    ("Очевидно, святой", 6),
    ("Духовная садовница", 10),
    ("Рельсотрон магии молний", 8),
)


def _short_hit_title(title: str) -> str:
    lowered = title.lower()
    if "очевидно, святой" in lowered:
        return "Очевидно, святой"
    if "духовная садовница" in lowered:
        return "Духовная садовница"
    if "рельсотрон" in lowered:
        return "Рельсотрон магии молний"
    return title


def _hit_priority(item: dict[str, Any]) -> tuple[int, int, int]:
    lowered = (item.get("title") or "").lower()
    if "очевидно, святой" in lowered:
        rank = 3
    elif "духовная садовница" in lowered:
        rank = 2
    elif "рельсотрон" in lowered:
        rank = 1
    else:
        rank = 0
    return (rank, int(item.get("chapter_count") or 0), int(item.get("id") or 0))


def _digest_progress_bar(read_count: int, total: int) -> str:
    if total <= 0:
        return "[░░░░░░░░] Прочитано 0/0 глав"
    clamped = max(0, min(read_count, total))
    blocks = min(8, clamped * 8 // total)
    return f"[{'█' * blocks}{'░' * (8 - blocks)}] Прочитано {clamped}/{total} глав"


async def _top_catalog_hits(db: Database) -> tuple[list[tuple[str, int]], list[dict[str, Any]]]:
    series_list = await db.list_series()
    enriched: list[dict[str, Any]] = []
    for series in series_list:
        chapters = await db.list_chapters(series["id"])
        enriched.append({**series, "chapter_count": len(chapters), "chapters": chapters})
    ordered = sorted(enriched, key=_hit_priority, reverse=True)
    hits: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in ordered:
        display = _short_hit_title(item["title"])
        if display not in seen:
            seen.add(display)
            hits.append((display, item["chapter_count"]))
        if len(hits) == 3:
            break
    for fallback_title, fallback_count in FALLBACK_HITS:
        if len(hits) >= 3:
            break
        if fallback_title not in seen:
            seen.add(fallback_title)
            hits.append((fallback_title, fallback_count))
    return hits[:3], enriched


async def build_user_weekly_digest(user_row: dict[str, Any] | int | None,
                                   db: Database) -> tuple[str, InlineKeyboardMarkup]:
    """Build a personalized 3-section weekly retention digest and inline action keyboard."""
    if isinstance(user_row, int):
        user = await db.get_user(user_row) or {"tg_id": user_row}
    elif user_row is None:
        user = {"tg_id": 0}
    else:
        user = dict(user_row)
        if user.get("tg_id") and "last_read_series_id" not in user:
            stored = await db.get_user(int(user["tg_id"]))
            if stored:
                user = {**stored, **user}

    hits, enriched_series = await _top_catalog_hits(db)
    hit_lines = [f"• «{title}» — {count} глав" for title, count in hits]

    tg_id = int(user.get("tg_id") or 0)
    last_series_id = user.get("last_read_series_id")
    last_ch_id = user.get("last_read_chapter_id")
    series = await db.get_series(int(last_series_id)) if last_series_id else None

    if series:
        chapters = await db.list_chapters(series["id"])
        total = len(chapters)
        chapter = await db.get_chapter(int(last_ch_id)) if last_ch_id else None
        if chapter and chapter["series_id"] == series["id"]:
            last_ch = chapter["chapter_num"]
            resolved_ch_id = chapter["id"]
        else:
            progress_row = (await db._one(
                "SELECT chapter_num FROM reading_progress WHERE tg_id = ? AND series_id = ?",
                (tg_id, series["id"]),
            )) if tg_id else None
            if progress_row:
                last_ch = progress_row["chapter_num"]
                matched = next((c for c in chapters if c["chapter_num"] == last_ch),
                               chapters[-1] if chapters else None)
                resolved_ch_id = matched["id"] if matched else int(last_ch_id or 1)
            elif chapters:
                last_ch = chapters[0]["chapter_num"]
                resolved_ch_id = int(last_ch_id or chapters[0]["id"])
            else:
                last_ch = 1
                resolved_ch_id = int(last_ch_id or 1)

        read_count = 0
        if tg_id:
            lib = await db.library_progress(tg_id, series["id"])
            if lib:
                read_count = int(lib[0].get("read_count") or 0)
                if not last_ch_id and lib[0].get("resume_id"):
                    resolved_ch_id = int(lib[0]["resume_id"])
            if read_count == 0:
                progress_row = await db._one(
                    "SELECT chapter_num FROM reading_progress WHERE tg_id = ? AND series_id = ?",
                    (tg_id, series["id"]),
                )
                if progress_row:
                    read_count = sum(1 for c in chapters if c["chapter_num"] <= progress_row["chapter_num"])
        if read_count == 0 and user.get("chapters_read_count"):
            read_count = min(total, int(user["chapters_read_count"]))

        section_progress = (
            f"📖 Ваша текущая книга: «{series['title']}» (Глава {last_ch})\n"
            f"{_digest_progress_bar(read_count, total)}"
        )
    else:
        section_progress = DEFAULT_FALLBACK_HIT
        if last_ch_id:
            resolved_ch_id = int(last_ch_id)
        else:
            featured = next(
                (s for s in enriched_series if "рельсотрон" in s["title"].lower() or s["id"] == 5),
                enriched_series[0] if enriched_series else None,
            )
            featured_chapters = featured["chapters"] if featured else []
            resolved_ch_id = int(featured_chapters[0]["id"]) if featured_chapters else 1

    lines = [
        DIGEST_HEADER,
        "",
        "🔥 Хиты каталога:",
        *hit_lines,
        "",
        section_progress,
        "",
        "💎 Бесплатные бонусы и VIP:",
        WUF_REMINDER,
        STREAK_REMINDER,
        VIP_CALLOUT,
    ]
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить чтение", callback_data=f"read:{resolved_ch_id}")],
            [InlineKeyboardButton(text="📖 Каталог новинок", callback_data="catalog")],
            [InlineKeyboardButton(text="⚡ VIP-доступ", callback_data="vip_menu")],
        ]
    )
    return "\n".join(lines), markup


async def send_weekly_digest_broadcast(db: Database, bot: Bot, dry_run: bool = False,
                                       admin_id: int | None = None, *,
                                       now: datetime | None = None) -> dict[str, Any]:
    """Send personalized weekly digest to eligible users with polite 0.05s anti-flood pacing."""
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    timestamp = clock.isoformat()
    all_users = await db._all("SELECT * FROM users ORDER BY tg_id")
    eligible_users = await db.list_users_for_weekly_digest(now=clock)
    skipped = max(0, len(all_users) - len(eligible_users))

    if dry_run:
        sent = 0
        blocked = 0
        failed = 0
        preview_message_id = None
        if admin_id is not None:
            admin_user = await db.get_user(admin_id)
            if admin_user is None:
                await db.ensure_user(admin_id, None)
                admin_user = await db.get_user(admin_id)
            text, markup = await build_user_weekly_digest(admin_user, db)
            try:
                msg = await bot.send_message(admin_id, text, parse_mode=None, reply_markup=markup)
                preview_message_id = getattr(msg, "message_id", None)
                sent = 1
            except TelegramForbiddenError:
                blocked = 1
            except Exception as error:
                logger.warning("Weekly digest preview failed for %s: %s", admin_id, type(error).__name__)
                failed = 1
        return {
            "status": "preview",
            "triggered": False,
            "dry_run": True,
            "preview_message_id": preview_message_id,
            "eligible": len(eligible_users),
            "total_eligible": len(eligible_users),
            "sent": sent,
            "success": sent,
            "blocked": blocked,
            "failed": failed,
            "skipped": skipped,
            "timestamp": timestamp,
        }

    sent = 0
    blocked = 0
    failed = 0
    for index, user in enumerate(eligible_users):
        tg_id = int(user["tg_id"])
        text, markup = await build_user_weekly_digest(user, db)
        try:
            try:
                await bot.send_message(tg_id, text, parse_mode=None, reply_markup=markup)
            except TelegramRetryAfter as retry:
                if retry.retry_after > 60:
                    raise
                await asyncio.sleep(retry.retry_after)
                await bot.send_message(tg_id, text, parse_mode=None, reply_markup=markup)
            await db.mark_weekly_digest_sent(tg_id, timestamp)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramBadRequest as error:
            if any(token in error.message.lower() for token in ("blocked", "deactivated", "chat not found")):
                blocked += 1
            else:
                failed += 1
        except Exception as error:
            logger.warning("Weekly digest delivery failed for %s: %s", tg_id, type(error).__name__)
            failed += 1
        if index + 1 < len(eligible_users) or ANTI_FLOOD_DELAY_SECONDS > 0:
            await asyncio.sleep(ANTI_FLOOD_DELAY_SECONDS)

    await db.set_state(WEEKLY_DIGEST_STATE_KEY, timestamp)
    return {
        "status": "complete",
        "triggered": True,
        "dry_run": False,
        "eligible": len(eligible_users),
        "total_eligible": len(eligible_users),
        "sent": sent,
        "success": sent,
        "blocked": blocked,
        "failed": failed,
        "skipped": skipped,
        "timestamp": timestamp,
    }


async def weekly_digest_due(db: Database, *, now: datetime | None = None) -> bool:
    """Check whether 7 days have elapsed since the last weekly digest broadcast."""
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = clock - WEEKLY_DIGEST_INTERVAL
    last_broadcast = (await db.get_state(WEEKLY_DIGEST_STATE_KEY) or "").strip()
    if last_broadcast:
        try:
            parsed = datetime.fromisoformat(last_broadcast.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed <= cutoff:
                return True
        except ValueError:
            return True
        # Also allow broadcast if any previously-messaged user has aged past 7 days.
        users = await db._all("SELECT last_weekly_digest_at FROM users WHERE last_weekly_digest_at <> ''")
        for row in users:
            try:
                sent = datetime.fromisoformat(row["last_weekly_digest_at"].replace("Z", "+00:00"))
                if sent.tzinfo is None:
                    sent = sent.replace(tzinfo=timezone.utc)
                if sent <= cutoff:
                    return True
            except ValueError:
                continue
        return False
    return True


async def run_autopilot_weekly_digest(db: Database, bot: Bot, *,
                                      now: datetime | None = None) -> dict[str, Any]:
    """Trigger weekly digest broadcast in autopilot only when 7 days have elapsed."""
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not await weekly_digest_due(db, now=clock):
        return {
            "status": "cooldown",
            "triggered": False,
            "sent": 0,
            "success": 0,
            "blocked": 0,
            "failed": 0,
            "eligible": 0,
            "last_broadcast_at": await db.get_state(WEEKLY_DIGEST_STATE_KEY),
        }
    return await send_weekly_digest_broadcast(db, bot, dry_run=False, now=clock)
