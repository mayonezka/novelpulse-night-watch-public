"""One follow-up per privately viewed locked chapter, with persistent send claims."""

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db import Database
from app.errors import failure_reason


async def send_paywall_followups(db: Database, bot: Bot, *, min_age_seconds: float = 0) -> dict:
    report = {"sent": 0, "suppressed": 0, "errors": []}
    recipients = set()
    for view in await db.pending_paywalls(min_age_seconds):
        # Multiple abandoned chapters do not produce a burst of messages to one reader.
        if view['tg_id'] in recipients or not await db.claim_followup(view['id']):
            continue
        recipients.add(view['tg_id'])
        try:
            chapter = await db.get_chapter(view['chapter_id'])
            if not chapter or await db.can_read(view['tg_id'], chapter):
                await db.finish_followup(view['id'], 'suppressed')
                report['suppressed'] += 1
                continue
            offer = await db.chapter_offer(view['tg_id'], chapter)
            price_label = ("🔥 Открыть продолжение за 1 ⭐️" if not offer['has_purchased']
                           else f"🔓 Открыть главу за {offer['price']} ⭐️")
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=price_label, callback_data=f"buy:{chapter['id']}")],
                [InlineKeyboardButton(text="👑 Взять VIP на все книги (49 ⭐️)", callback_data="vip:7:49")],
                [InlineKeyboardButton(text="🎁 Открыть бесплатно за друга", callback_data="referral")]])
            text = (f"📚 Вы остановились на самом интересном месте в «{chapter['series_title'][:180]}»!\n\n"
                    f"Ещё один фрагмент главы {chapter['chapter_num']}:\n\n{chapter['content_text'][300:550]}\n\n"
                    "Что скрывается дальше? Вернитесь к истории — продолжение уже ждёт.\n"
                    "Нажимая «Открыть», вы принимаете /terms. Помощь: /paysupport.")
            async def deliver():
                return await bot.send_message(view['tg_id'], text, parse_mode=None,
                                              reply_markup=markup, disable_notification=True)
            try:
                result = await deliver()
            except TelegramRetryAfter as error:
                if error.retry_after > 60:
                    await db.finish_followup(view['id'], 'pending')
                    raise
                await asyncio.sleep(error.retry_after)
                result = await deliver()
            await db.finish_followup(view['id'], 'sent', result.message_id)
            report['sent'] += 1
        except Exception as error:
            # Never retry an ambiguous send: Telegram may already have delivered it.
            state = ('blocked' if isinstance(error, TelegramForbiddenError) else
                     'pending' if isinstance(error, TelegramRetryAfter) else
                     'uncertain' if isinstance(error, (TelegramNetworkError, OSError)) else 'failed')
            await db.finish_followup(view['id'], state)
            report['errors'].append({"view_id": view['id'], "state": state, "reason": failure_reason(error)})
        await asyncio.sleep(0.05)
    return report
