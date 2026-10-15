"""Native Telegram Stars invoice, checkout validation, and delivery."""

import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from app.bot.handlers.user import show_chapter
from app.db import Database
from app.monetization import parse_vip_payload, vip_duration
from app.notifications import revenue_alert
from app.config import Settings

logger = logging.getLogger(__name__)


async def buy(query: CallbackQuery, bot: Bot, db: Database) -> None:
    if not isinstance(query.message, Message):
        await query.answer("Откройте главу в личном чате с ботом.", show_alert=True)
        return
    await query.answer()
    await db.ensure_user(query.from_user.id, query.from_user.username)
    chapter_id = int(query.data.split(":")[1])
    chapter = await db.get_chapter(chapter_id)
    if not chapter:
        await query.message.answer("Эта глава пока недоступна. Загляните в /catalog.")
        return
    if await db.can_read(query.from_user.id, chapter):
        await show_chapter(bot, query.message.chat.id, query.from_user.id, chapter_id, db)
        return
    try:
        order = await db.create_order(query.from_user.id, chapter_id)
    except ValueError as error:
        await query.message.answer(str(error))
        return
    await bot.send_invoice(
        chat_id=query.from_user.id,
        title=f"Глава {chapter['chapter_num']}: {chapter['title']}"[:32],
        description=(("Первая платная глава за 1 ⭐️! " if order["is_tripwire"] else "") +
                     f"Откройте главу новеллы «{chapter['series_title']}» навсегда. Вопросы об оплате: /paysupport.")[:255],
        payload=order["payload"], provider_token="", currency="XTR",
        prices=[LabeledPrice(label="Доступ к главе", amount=order["amount_stars"])],
        start_parameter=f"ch_{chapter_id}",
    )


async def buy_vip(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    await db.ensure_user(query.from_user.id, query.from_user.username)
    try:
        days, price = parse_vip_payload(query.data)
        await db.prepare_vip_order(query.from_user.id, query.data)
    except ValueError as error:
        await query.message.answer(str(error))
        return
    await bot.send_invoice(
        chat_id=query.from_user.id, title=f"VIP на {vip_duration(days)}",
        description=f"Все опубликованные главы NovelPulse на {vip_duration(days)}. Действующий VIP продлевается. Разовая покупка без автосписаний. Условия: /terms.",
        payload=query.data, provider_token="", currency="XTR",
        prices=[LabeledPrice(label=f"VIP на {vip_duration(days)}", amount=price)], start_parameter="vip",
    )


async def pre_checkout(query: PreCheckoutQuery, db: Database) -> None:
    try:
        if query.invoice_payload.startswith("vip:"):
            await db.approve_vip_order(query.from_user.id, query.invoice_payload, query.currency, query.total_amount, query.id)
        else:
            await db.approve_order(query.invoice_payload, query.from_user.id, query.currency, query.total_amount, query.id)
    except ValueError as error:
        await query.answer(ok=False, error_message=str(error))
        return
    except Exception:
        logger.exception("Checkout validation failed")
        await query.answer(ok=False, error_message="Оплата временно недоступна. Попробуйте немного позже.")
        return
    await query.answer(ok=True)


async def successful_payment(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    payment = message.successful_payment
    if not payment or not message.from_user:
        return
    await db.ensure_user(message.from_user.id, message.from_user.username)
    try:
        if payment.invoice_payload.startswith("vip:"):
            until, inserted = await db.record_vip_payment(
                message.from_user.id, payment.invoice_payload, payment.currency, payment.total_amount,
                payment.telegram_payment_charge_id,
            )
            expiration = datetime.fromisoformat(until.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
            if inserted:
                days, _ = parse_vip_payload(payment.invoice_payload)
                await revenue_alert(bot, db, message, payment, f'VIP на {vip_duration(days)}', settings=settings)
            await message.answer(
                f"👑 VIP {'активирован' if inserted else 'уже оплачен'}! Доступ ко всем опубликованным главам до {expiration}.\n"
                "Отправляйтесь в новый мир: /catalog"
            )
            return
        chapter_id, inserted = await db.record_payment(
            payment.invoice_payload, message.from_user.id, payment.currency,
            payment.total_amount, payment.telegram_payment_charge_id,
        )
    except ValueError:
        logger.error("Payment mismatch; inspect private payment records")
        await message.answer("Платёж требует проверки владельцем. Напишите через /paysupport и приложите квитанцию.")
        return
    if inserted:
        chapter = await db.get_chapter(chapter_id, include_unpublished=True)
        await revenue_alert(bot, db, message, payment,
                            f"Глава {chapter['chapter_num']} · {chapter['series_title']}", settings=settings)
        await message.answer("✅ Оплата получена! Глава открыта — приятного чтения!")
    await show_chapter(bot, message.chat.id, message.from_user.id, chapter_id, db, purchased_delivery=True)


def create_router() -> Router:
    router = Router(name="payments")
    router.callback_query.register(buy_vip, F.message.chat.type == "private", F.data.regexp(r"^vip:[0-9]{1,2}:[0-9]{1,3}$"))
    router.callback_query.register(buy, F.message.chat.type == "private", F.data.regexp(r"^buy:[1-9][0-9]{0,17}$"))
    router.pre_checkout_query.register(pre_checkout)
    router.message.register(successful_payment, F.successful_payment)
    return router
