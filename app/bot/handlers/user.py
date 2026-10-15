"""Russian reader, catalog, deep links, pagination, and account help."""

import re
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import Settings
from app.db import Database
from app.text import split_text
from app.monetization import VIP_PLANS, VIP_LABELS, vip_options
from app.gamification import notify_levelups, status_window
from app.notifications import notify_admins

WELCOME = "📚 Добро пожаловать в NovelPulse! Читайте первые главы эксклюзивных переводов бесплатно и открывайте продолжение за Telegram Stars."
PEEK_LABEL = '🎲 Показать ещё отрывок бесплатно (1 раз)'


def deep_peek_excerpt(text: str) -> str:
    """900–950 characters for long chapters; always retain unrevealed story text."""
    text = text.strip()
    if len(text) <= 950:
        # Short demo chapters cannot supply 900 characters without revealing everything.
        end = max(1, min(len(text) - 1, int(len(text) * .8)))
        boundary = text.rfind(' ', 0, end)
        return text[:boundary if boundary > 0 else end].rstrip('.!?… \n') + '…'
    candidates = [i for i in range(900, 950) if text[i].isspace()
                  and text[i - 1] not in '.!?…»\n']
    keys = ('систем', 'ранг', 'смерт', 'кров', 'таймер', 'двер', '—', '«')
    end = max(candidates, key=lambda i: (sum(k in text[max(0, i-130):i].casefold() for k in keys), i)) if candidates else 949
    return text[:end].rstrip() + '…'


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def send_text(bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    chunks = split_text(text)
    for index, chunk in enumerate(chunks):
        await bot.send_message(chat_id, chunk, parse_mode=None,
                               reply_markup=markup if index == len(chunks) - 1 else None)


async def show_catalog(bot: Bot, chat_id: int, db: Database) -> None:
    series = await db.list_series()
    if not series:
        await bot.send_message(chat_id, "📚 Каталог пока пуст. Новые миры и переводы уже в пути!")
        return
    for offset in range(0, len(series), 50):
        rows = [[button(item["title"][:100], f"series:{item['id']}")] for item in series[offset:offset + 50]]
        rows.append([button("👑 Купить VIP-доступ", "vip")])
        await bot.send_message(chat_id, "📖 Каталог новелл\nВыберите мир, в который отправитесь сегодня:", reply_markup=keyboard(rows))


async def show_series(bot: Bot, chat_id: int, series_id: int, db: Database) -> None:
    series = await db.get_series(series_id)
    chapters = await db.list_chapters(series_id)
    if not series or not chapters:
        await bot.send_message(chat_id, "Эта новелла пока недоступна. Загляните в /catalog.")
        return
    await send_text(bot, chat_id, f"📚 {series['title']}\n\n{series['description']}")
    for offset in range(0, len(chapters), 50):
        rows = []
        for chapter in chapters[offset:offset + 50]:
            label = "Бесплатно" if chapter["chapter_num"] <= series["free_chapters_limit"] else f"{chapter['price_stars']} ⭐️"
            rows.append([button(f"{chapter['chapter_num']}. {chapter['title'][:65]} · {label}", f"chapter:{chapter['id']}")])
        rows.append([button("🔙 Назад к каталогу", "catalog")])
        await bot.send_message(chat_id, "Выберите главу — приключение ждёт:", reply_markup=keyboard(rows))


async def show_chapter(bot: Bot, chat_id: int, tg_id: int, chapter_id: int, db: Database,
                       *, page: int = 0, edit_message_id: int | None = None,
                       purchased_delivery: bool = False, deep_peek: bool = False,
                       edit_locked: bool = False, audio_locked: bool = False) -> None:
    purchased = await db.has_purchase(tg_id, chapter_id)
    chapter = await db.get_chapter(chapter_id, include_unpublished=purchased_delivery or purchased)
    if not chapter:
        await bot.send_message(chat_id, "Глава пока недоступна. Найдите новое приключение в /catalog.")
        return
    heading = f"{chapter['series_title']}\nГлава {chapter['chapter_num']}: {chapter['title']}\n\n"
    if await db.can_read(tg_id, chapter):
        pages = split_text(heading + chapter["content_text"], limit=3900)
        if page < 0 or page >= len(pages):
            await bot.send_message(chat_id, "Такой страницы нет. Откройте главу заново через /catalog.")
            return
        text = pages[page]
        if len(pages) > 1:
            text += f"\n\n📄 Страница {page + 1} из {len(pages)}"
        rows, navigation = [], []
        if page > 0:
            navigation.append(button(f"⬅️ Стр. {page}", f"page:{chapter_id}:{page - 1}"))
        if page + 1 < len(pages):
            navigation.append(button(f"Стр. {page + 2} ➡️", f"page:{chapter_id}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        if page == len(pages) - 1:
            following = await db.next_chapter(chapter["series_id"], chapter["chapter_num"])
            if following:
                rows.append([button("➡️ Следующая глава", f"chapter:{following['id']}")])
        rows.append([button('🎧 Слушать аудиокнигу', f'listen:{chapter_id}')])
        rows.append([button("🔙 Назад к каталогу", "catalog")])
        markup = keyboard(rows)
        if edit_message_id is not None:
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id,
                                            parse_mode=None, reply_markup=markup)
            except TelegramBadRequest as error:
                if "message is not modified" not in error.message.lower():
                    raise
        else:
            await bot.send_message(chat_id, text, parse_mode=None, reply_markup=markup)
        if chat_id == tg_id:
            await db.record_read(tg_id, chapter["series_id"], chapter["chapter_num"],completed=page==len(pages)-1)
            await notify_levelups(bot,db,tg_id)
    else:
        teaser = deep_peek_excerpt(chapter['content_text']) if deep_peek else chapter["content_text"][:300]
        if not deep_peek and len(chapter["content_text"]) > 300:
            teaser += "…"
        account = await db.get_user(tg_id)
        credits = account["free_unlock_credits"] if account else 0
        offer = await db.chapter_offer(tg_id, chapter)
        if offer["tripwire"]:
            discount = round((1 - 1 / offer["regular_price"]) * 100)
            label = f"🔥 Открыть за 1 ⭐️ (Скидка {discount}% новичку)"
            banner = ("\n\n🔥 Акция для новых читателей: откройте свою первую платную главу всего за 1 ⭐️ "
                      f"(вместо {offer['regular_price']} ⭐️)!")
        else:
            label = f"🔓 Открыть главу за {offer['price']} ⭐️"
            banner = ("\n\n💡 Вы уже открыли первую платную главу! Дальше выгоднее взять 👑 VIP на 7 дней (49 ⭐️) "
                      "и читать ВСЕ главы всех новелл без ограничений.") if offer["has_purchased"] else ""
        rows = []
        if not await db.has_used_peek(tg_id, chapter_id):
            rows.append([button(PEEK_LABEL, f'peek:{chapter_id}')])
        rows.append([button(label, f"buy:{chapter_id}")])
        if credits > 0:
            rows.append([button(f"🔑 Открыть за бонусный ключ (осталось: {credits})", f"credit:{chapter_id}")])
        rows.extend([[button(label,data)] for label,data in vip_options(account)])
        rows.extend([[button("🎁 Открыть бесплатно за друга", "referral")],
                     [button("👑 Купить VIP-доступ", "vip")], [button("🔙 Назад к каталогу", "catalog")]])
        if deep_peek:
            heading = f"{chapter['series_title'][:350]}\nГлава {chapter['chapter_num']}: {chapter['title'][:250]}\n\n"
            banner = ("\n\n🔥 Вы уже на середине главы! Чем закончится эта сцена? "
                      f"Откройте полную главу навсегда всего за {offer['price']} ⭐️ "
                      "(или 🎁 получите бесплатный ключ за 1 друга):")
        text = (heading + teaser + banner +
                "\n\n🔒 Самое интересное впереди! Откройте главу навсегда за Telegram Stars. "
                "Нажимая «Открыть», вы принимаете /terms. Вопросы об оплате: /paysupport.")
        if audio_locked:
            text = (f"🔒 Аудио-версия и полный текст доступны за {offer['price']} ⭐️ (или по VIP-подписке)!\n\n" + text)
        if edit_message_id is not None and edit_locked:
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=edit_message_id,
                    parse_mode=None, reply_markup=keyboard(rows))
            except TelegramBadRequest as error:
                if 'message is not modified' not in error.message.lower():
                    raise
        else:
            await send_text(bot, chat_id, text, keyboard(rows))
        if chat_id == tg_id:
            await db.log_paywall_view(tg_id, chapter_id)


async def listen(query: CallbackQuery, bot: Bot, db: Database, audio_jobs, cashier_lease=None) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    await db.ensure_user(query.from_user.id, query.from_user.username)
    chapter_id = int(query.data.split(':')[1])
    chapter = await db.get_chapter(chapter_id, include_unpublished=await db.has_purchase(query.from_user.id,chapter_id))
    if chapter is None:
        await query.message.answer('Глава пока недоступна. Откройте /catalog.')
        return
    if not await db.can_read(query.from_user.id, chapter):
        await show_chapter(bot,query.message.chat.id,query.from_user.id,chapter_id,db,audio_locked=True)
        return
    result = audio_jobs.start(bot,db,query.from_user.id,query.message.chat.id,chapter_id,lease=cashier_lease)
    await query.message.answer({'started':'🎧 Озвучиваю главу. Аудиокнига скоро появится здесь; пока можно продолжить чтение.',
        'duplicate':'⏳ Ваша аудиоглава уже готовится. Дождитесь её отправки.',
        'busy':'⏳ Сейчас готовятся другие аудиоглавы. Попробуйте через несколько минут.'}[result])


async def peek(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    await db.ensure_user(query.from_user.id, query.from_user.username)
    chapter_id = int(query.data.split(':')[1])
    claimed = await db.mark_peek_used(query.from_user.id, chapter_id)
    # Stale/repeated buttons return the ordinary paywall without revealing a second excerpt.
    await show_chapter(bot, query.message.chat.id, query.from_user.id, chapter_id, db,
        edit_message_id=query.message.message_id, deep_peek=claimed, edit_locked=True)


async def start(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings) -> None:
    if not message.from_user:
        return
    referred_by = int(command.args[4:]) if command.args and re.fullmatch(r"ref_[1-9][0-9]{0,17}", command.args) else None
    inviter, created = await db.ensure_user(message.from_user.id, message.from_user.username, referred_by, registration_details=True)
    if created and message.from_user.id not in settings.admin_ids:
        total = (await db.stats())['total_users']
        name = '@' + message.from_user.username if message.from_user.username else 'без username'
        await notify_admins(bot, f"👤 Новый читатель в NovelPulse! ID: {message.from_user.id} ({name}), "
            f"вход: {(command.args or '/start')[:128]}. Всего читателей: {total}",
            settings=settings, db=db, event_key=f'new_reader:{message.from_user.id}')
    if inviter:
        await notify_levelups(bot,db,inviter)
        try:
            await bot.send_message(inviter, "🎉 По вашей ссылке пришёл новый читатель! Вам начислен +1 бесплатный ключ к любой платной главе.")
        except TelegramAPIError:
            logging.getLogger(__name__).warning("Не удалось уведомить пригласившего; ключ сохранён")
    if command.args and re.fullmatch(r"ch_[1-9][0-9]{0,17}", command.args):
        await show_chapter(bot, message.chat.id, message.from_user.id, int(command.args[3:]), db)
    elif command.args and re.fullmatch(r"series_[1-9][0-9]{0,17}", command.args):
        await show_series(bot, message.chat.id, int(command.args[7:]), db)
    else:
        await message.answer(WELCOME, reply_markup=keyboard([
            [button("📖 Каталог новелл", "catalog")], [button("👑 Купить VIP-доступ", "vip")],
            [button("🎁 Читать бесплатно (Пригласить друга)", "referral")],
            [button("🎁 Дар Системы (Стрик)", "daily_gift")],
            [button('👤 Мой Ранг в Системе','profile')],
        ]))
        await show_catalog(bot, message.chat.id, db)


async def daily_gift(query: CallbackQuery, db: Database, bot: Bot) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    await db.ensure_user(query.from_user.id, query.from_user.username)
    result = await db.claim_daily_gift(query.from_user.id)
    await notify_levelups(bot,db,query.from_user.id)
    days = result['streak_days']
    if not result['claimed']:
        text = (f'⏳ Вы уже забрали сегодняшний дар Системы! Текущий стрик: {days}/3 дн. '
                'Возвращайтесь завтра!')
    elif result['rewarded']:
        text = ('🎉 [СИСТЕМА: НАГРАДА ПОЛУЧЕНА!]\n'
                'Вы заходили 3 дня подряд и получили +1 🔑 бонусный ключ к любой платной главе!')
    else:
        text = (f'⚡ [СИСТЕМА: ЕЖЕДНЕВНАЯ СИНХРОНИЗАЦИЯ {days}/3]\n'
                f'Ещё {3-days} дн. подряд — и вы получите бесплатный ключ к любой платной главе!\n'
                '🔥 Не хотите ждать? Откройте продолжение прямо сейчас за 1 ⭐️ '
                'или возьмите ⚡ VIP на 24 часа (19 ⭐️)!')
    await query.message.answer(text, reply_markup=keyboard([[button('📖 Каталог новелл', 'catalog')]]))


async def myid(message: Message) -> None:
    if message.from_user:
        await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


async def profile(message: Message, bot: Bot, db: Database) -> None:
    if not message.from_user:return
    await db.ensure_user(message.from_user.id,message.from_user.username)
    await notify_levelups(bot,db,message.from_user.id)
    user=await db.get_user(message.from_user.id)
    await message.answer(status_window(user),parse_mode=None,reply_markup=keyboard(
        [[button(label,data)] for label,data in vip_options(user)] + [[button('📖 Каталог новелл','catalog')]]))


async def profile_callback(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if not isinstance(query.message,Message):return
    await db.ensure_user(query.from_user.id,query.from_user.username)
    await notify_levelups(bot,db,query.from_user.id)
    user=await db.get_user(query.from_user.id)
    await query.message.answer(status_window(user),parse_mode=None,reply_markup=keyboard(
        [[button(label,data)] for label,data in vip_options(user)] + [[button('📖 Каталог новелл','catalog')]]))


async def catalog(message: Message, bot: Bot, db: Database) -> None:
    if message.from_user:
        await db.ensure_user(message.from_user.id, message.from_user.username)
        await show_catalog(bot, message.chat.id, db)


async def navigate(query: CallbackQuery, bot: Bot, db: Database) -> None:
    if not isinstance(query.message, Message):
        await query.answer("Откройте /start в личном чате с ботом.", show_alert=True)
        return
    await query.answer()
    await db.ensure_user(query.from_user.id, query.from_user.username)
    if query.data == "catalog":
        await show_catalog(bot, query.message.chat.id, db)
    elif query.data.startswith("page:"):
        _, chapter_id, page = query.data.split(":")
        await show_chapter(bot, query.message.chat.id, query.from_user.id, int(chapter_id), db,
                           page=int(page), edit_message_id=query.message.message_id)
    elif query.data.startswith("chapter:"):
        await show_chapter(bot, query.message.chat.id, query.from_user.id, int(query.data.split(":")[1]), db)
    else:
        await show_series(bot, query.message.chat.id, int(query.data.split(":")[1]), db)


async def terms(message: Message) -> None:
    await message.answer(
        "📜 Условия NovelPulse\n\nПокупка открывает указанную главу для вашего Telegram-аккаунта. "
        "Полная цена в Stars указана в счёте; автоматических списаний нет. "
        "VIP на 24 часа стоит 19 Stars, на 7 дней — 49 Stars, на 30 дней — 149 Stars; повторная покупка продлевает срок. "
        "Покупка VIP также означает согласие с этими условиями. Бонусный ключ открывает одну платную главу навсегда. "
        "Первые бесплатные главы и главы при действующем VIP доступны без оплаты. "
        "Нажимая «Открыть главу», вы принимаете эти условия. "
        "Если глава не пришла, оплата прошла дважды или нужен возврат, напишите владельцу через /paysupport. "
        "Сохраните квитанцию об оплате."
    )


def owner_links(settings: Settings) -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton(text="✉️ Написать владельцу", url=f"tg://user?id={tg_id}")]
            for tg_id in settings.admin_ids[:10]]


async def payment_support(message: Message, settings: Settings) -> None:
    rows = owner_links(settings)
    await message.answer(
        "По вопросам оплаты и возвратов напишите владельцу: укажите новеллу, номер главы и приложите квитанцию. "
        "Поддержка Telegram не решает вопросы покупок в этом боте."
        if rows else "Контакт владельца пока не настроен. Обратитесь к тому, кто поделился ботом, и сохраните квитанцию.",
        reply_markup=keyboard(rows) if rows else None,
    )


async def referral(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    await db.ensure_user(query.from_user.id, query.from_user.username)
    account = await db.get_user(query.from_user.id)
    me = await bot.get_me()
    await query.message.answer(
        "🎁 Пригласите нового читателя и получите ключ к любой платной главе!\n"
        f"Ваших ключей: {account['free_unlock_credits']}\n\n"
        f"Ваша ссылка: https://t.me/{me.username}?start=ref_{query.from_user.id}\n\n"
        "Ключ начисляется один раз за нового читателя, который впервые запускает бота по вашей ссылке."
    )


async def credit_unlock(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    await db.ensure_user(query.from_user.id, query.from_user.username)
    chapter_id = int(query.data.split(":")[1])
    try:
        inserted = await db.unlock_with_credit(query.from_user.id, chapter_id)
    except ValueError as error:
        await query.message.answer(str(error))
        return
    if inserted:
        await query.message.answer("🔑 Ключ использован! Глава открыта навсегда.")
    await show_chapter(bot, query.message.chat.id, query.from_user.id, chapter_id, db)


async def vip(query: CallbackQuery, db: Database) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await db.ensure_user(query.from_user.id,query.from_user.username)
        rows = [[button(label,data)] for label,data in vip_options(await db.get_user(query.from_user.id))]
        rows.append([button("🔙 Назад к каталогу", "catalog")])
        await query.message.answer(
            "👑 Все миры NovelPulse без ограничений! Выберите VIP-доступ. "
            "Если VIP уже активен, дни добавятся к его сроку. Автосписаний нет. "
            "Нажимая тариф, вы принимаете /terms.", reply_markup=keyboard(rows),
        )


def create_router() -> Router:
    router = Router(name="user")
    private = F.chat.type == "private"
    router.message.register(myid, Command("myid"))
    router.message.register(start, CommandStart(), private)
    router.message.register(catalog, Command("catalog"), private)
    router.message.register(profile, Command('profile'), private)
    router.message.register(terms, Command("terms"), private)
    router.message.register(payment_support, Command("paysupport", "support"), private)
    router.callback_query.filter(F.message.chat.type == "private")
    router.callback_query.register(vip, F.data == "vip")
    router.callback_query.register(referral, F.data == "referral")
    router.callback_query.register(daily_gift, F.data == "daily_gift")
    router.callback_query.register(profile_callback, F.data == 'profile')
    router.callback_query.register(listen, F.data.regexp(r'^listen:[1-9][0-9]{0,17}$'))
    router.callback_query.register(credit_unlock, F.data.regexp(r"^credit:[1-9][0-9]{0,17}$"))
    router.callback_query.register(peek, F.data.regexp(r'^peek:[1-9][0-9]{0,17}$'))
    router.callback_query.register(navigate, F.data.regexp(
        r"^(catalog|(?:series|chapter):[1-9][0-9]{0,17}|page:[1-9][0-9]{0,17}:[0-9]{1,6})$"))
    return router
