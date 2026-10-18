"""Russian reader, catalog, deep links, pagination, and account help."""

import re
import logging
from pathlib import Path
from urllib.parse import urlencode

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    InlineQuery,InlineQueryResultArticle,InputTextMessageContent,InlineQueryResultsButton,FSInputFile)

from app.config import Settings
from app.db import Database, GENRES, wuf_status
from app.text import split_text
from app.monetization import VIP_PLANS, VIP_LABELS, vip_options
from app.gamification import notify_levelups, status_window, leaderboard_window
from app.search import TROPES, TROPE_IDS, search_catalog
from app.notifications import new_reader_alert
from app.quiz import question_card,result_card,recommended_series,referral_text
from app.reviews import rating_line,review_button,register_handlers as register_reviews
from app.referrals import RAID_LABEL,raid_share_url,announce_reading_reward
from app.analytics import acquisition_source, EXTERNAL_CAMPAIGN_SOURCES

WELCOME = "📚 Добро пожаловать в NovelPulse! Читайте первые главы эксклюзивных переводов бесплатно и открывайте продолжение за Telegram Stars."
PEEK_LABEL = '🎲 Показать ещё отрывок бесплатно (1 раз)'
SUBSCRIPTION_LABEL = '🎁 Получить +1 ключ за подписку на канал'


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


def render_player_story_card(user_row: dict, series_title: str = ''):
    """Load Pillow only for a share-card request so the phone bot stays lightweight."""
    from app.story_card import render_player_story_card as render
    return render(user_row, series_title)


def subscription_reward_rows(user: dict | None) -> list[list[InlineKeyboardButton]]:
    return [[button(SUBSCRIPTION_LABEL, 'claim_sub_key')]] if user and not user.get('channel_sub_reward_claimed', 0) else []


def profile_markup(user: dict) -> InlineKeyboardMarkup:
    return keyboard([
        [button('🔖 Моя полка','library')], [button('🔮 Тест класса (+50 XP)','quiz')],
        [button('🏆 Зал Славы','leaderboard')], [button('🎁 Стрик','daily_gift')],
        [button('📸 Карточка Stories (+50 XP)','share_story_card')],
        [button('⚔️ Совместный Рейд','referral')],
        *subscription_reward_rows(user), *[[button(label,data)] for label,data in vip_options(user)],
        [button('📖 Каталог новелл','catalog')]])


async def send_text(bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    chunks = split_text(text)
    for index, chunk in enumerate(chunks):
        await bot.send_message(chat_id, chunk, parse_mode=None,
                               reply_markup=markup if index == len(chunks) - 1 else None)


def progress_bar(read_count: int, total: int) -> str:
    read_count=max(0,min(read_count,total))
    blocks=min(8,read_count*8//total) if total else 0
    return f"[{'█'*blocks}{'░'*(8-blocks)}] {read_count}/{total} глав"


def genre_rows() -> list[list[InlineKeyboardButton]]:
    items=[button(label,f'genre:{slug}') for slug,(_,label) in GENRES.items()]
    return [items[i:i+2] for i in range(0,len(items),2)]+[[button('🎧 С аудио','genre:audio')]]


async def show_catalog(bot: Bot, chat_id: int, db: Database, genre: str | None=None) -> None:
    series = await db.list_series()
    if not series and genre is None:
        await bot.send_message(chat_id, "📚 Каталог пока пуст. Новые миры и переводы уже в пути!")
        return
    if genre and genre!='audio':
        series=[s for s in series if s['genre_slug']==genre or genre=='isekai' and s['genre_slug']=='comedy_isekai']
    heading='📖 Каталог новелл\nВыберите мир, в который отправитесь сегодня:'
    if genre:
        heading='📖 Каталог новелл · '+('🎧 С аудио\nАудиоглавы доступны по запросу в читалке.' if genre=='audio' else GENRES[genre][0])
    if not series:
        await bot.send_message(chat_id,heading+'\nНа этой полке пока нет новелл.',reply_markup=keyboard([[button('🔙 Назад к каталогу','catalog')]]))
        return
    for offset in range(0, len(series), 50):
        rows = genre_rows() if genre is None else []
        rows.append([button('🔍 Поиск по сюжетам', 'search')])
        rows.extend([[button(item["title"][:100], f"series:{item['id']}")] for item in series[offset:offset + 50]])
        if genre:rows.append([button('🔙 Назад к каталогу','catalog')])
        rows.append([button('🔖 Моя полка','library')])
        rows.append([button("👑 Купить VIP-доступ", "vip")])
        await bot.send_message(chat_id,heading,reply_markup=keyboard(rows))


async def show_library(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if not isinstance(query.message,Message):return
    await db.ensure_user(query.from_user.id,query.from_user.username)
    books=await db.library_progress(query.from_user.id,only_started=True)
    if not books:
        await query.message.answer('У вас пока нет начатых книг. Выберите новеллу в каталоге!',
            reply_markup=keyboard([[button('📖 Каталог новелл','catalog')]]))
        return
    for book in books:
        rows=[]
        if book['resume_id']:rows.append([button('▶️ Продолжить чтение',f"read:{book['resume_id']}")])
        rows.append([button('📖 Все главы',f"series:{book['id']}")])
        await query.message.answer(f"📖 «{book['title']}» · Прочитано {book['read_count']}/{book['total_chapters']} глав\n"
            +progress_bar(book['read_count'],book['total_chapters']),reply_markup=keyboard(rows),parse_mode=None)
    await query.message.answer('🔖 Ваша личная полка',reply_markup=keyboard([[button('🔙 Назад к каталогу','catalog')]]))


async def show_series(bot: Bot, chat_id: int, series_id: int, db: Database) -> None:
    series = await db.get_series(series_id)
    chapters = await db.list_chapters(series_id)
    if not series or not chapters:
        await bot.send_message(chat_id, "Эта новелла пока недоступна. Загляните в /catalog.")
        return
    progress=await db.library_progress(chat_id,series_id)
    summary=progress_bar(progress[0]['read_count'],progress[0]['total_chapters']) if progress else ''
    average,count=await db.get_series_rating_stats(series_id)
    await send_text(bot, chat_id, f"📚 {series['title']}\n{series['genre_title']}\n{summary}\n"
        f"{rating_line(average,count)}\n\n{series['description']}",keyboard([[review_button(series_id)]]))
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
            average,count=await db.get_series_rating_stats(chapter['series_id'])
            text+='\n\n'+rating_line(average,count)
            following = await db.next_chapter(chapter["series_id"], chapter["chapter_num"])
            if following:
                rows.append([button("➡️ Следующая глава", f"chapter:{following['id']}")])
            rows.append([review_button(chapter['series_id'])])
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
            inviter=await db.record_read(tg_id, chapter["series_id"], chapter["chapter_num"],completed=page==len(pages)-1)
            await announce_reading_reward(bot,db,tg_id,inviter)
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
        rows.extend(subscription_reward_rows(account))
        rows.append([button('📸 Карточка Stories (+50 XP)', 'share_story_card')])
        if not await db.has_used_peek(tg_id, chapter_id):
            rows.append([button(PEEK_LABEL, f'peek:{chapter_id}')])
        timer = wuf_status(account['last_wuf_unlock_at'] if account else '')
        rows.append([button(timer['label'], f'wuf_unlock:{chapter_id}' if timer['available'] else 'wuf_cooldown_info')])
        rows.append([button(label, f"buy:{chapter_id}")])
        if credits > 0:
            rows.append([button(f"🔑 Открыть за бонусный ключ (осталось: {credits})", f"credit:{chapter_id}")])
        rows.extend([[button(label,data)] for label,data in vip_options(account)])
        rows.extend([[button(RAID_LABEL, "referral")],
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
            await db.record_funnel_event(tg_id,'view_paywall',chapter['series_id'],chapter_id)
            await db.remember_chapter(tg_id,chapter_id)


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
    await db.remember_chapter(query.from_user.id,chapter_id)
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
    referred_by = int(command.args.split('_')[1]) if command.args and re.fullmatch(r"(?:ref|raid)_[1-9][0-9]{0,17}", command.args) else None
    inviter, created = await db.ensure_user(message.from_user.id, message.from_user.username, referred_by, registration_details=True)
    source = acquisition_source(command.args)
    await db.record_funnel_event(message.from_user.id,'bot_start',source=source)
    if created and message.from_user.id not in settings.admin_ids:
        await new_reader_alert(bot, db, message.from_user.id, message.from_user.username,
            source, start_payload=command.args, settings=settings)
    if inviter:
        await message.answer('⚔️ Приглашение в Рейд принято! Откройте первую главу — тогда вы и ваш напарник получите по +1 🔑. Напарнику +50 XP, вам +25 XP.')
    if command.args in EXTERNAL_CAMPAIGN_SOURCES:
        await show_series(bot, message.chat.id, 1, db)
    elif command.args and re.fullmatch(r"yt_long_s_[1-9][0-9]{0,17}", command.args):
        await show_series(bot, message.chat.id, int(command.args[10:]), db)
    elif command.args and re.fullmatch(r"web_ch_[1-9][0-9]{0,17}", command.args):
        await show_chapter(bot, message.chat.id, message.from_user.id, int(command.args[7:]), db)
    elif command.args and re.fullmatch(r"(?:web_s|rss_s)_[1-9][0-9]{0,17}", command.args):
        await show_series(bot, message.chat.id, int(command.args[6:]), db)
    elif command.args and re.fullmatch(r"ch_[1-9][0-9]{0,17}", command.args):
        await show_chapter(bot, message.chat.id, message.from_user.id, int(command.args[3:]), db)
    elif command.args and re.fullmatch(r"series_[1-9][0-9]{0,17}", command.args):
        await show_series(bot, message.chat.id, int(command.args[7:]), db)
    elif command.args == 'quiz':
        await quiz_command(message,bot,db)
    else:
        resume=await db.resume_info(message.from_user.id)
        series=await db.list_series()
        read_target=f"read:{resume['id']}" if resume else (f"series:{series[0]['id']}" if series else 'catalog')
        welcome=WELCOME + ('\n\n'+progress_bar(resume['read_count'],resume['total_chapters']) if resume else '')
        await message.answer(welcome, reply_markup=keyboard([
            [button('▶️ Читать / Продолжить',read_target)],
            [button(f'📚 Каталог ({len(series)} книг)','catalog')],
            [button('🎧 Аудиокниги','genre:audio')],
            [button('👤 Профиль и Бонусы','profile')],
        ]))


async def display_quiz(message: Message, bot: Bot, db: Database, user: dict, *, edit=False) -> None:
    if user['quiz_completed']:
        me=await bot.get_me()
        series=recommended_series(user['quiz_class_result'],await db.list_series())
        # Owner selected Telegram Share; it works without BotFather inline mode.
        text,markup=result_card(user,series,me.username,inline_enabled=False)
    else:
        text,markup=question_card(user)
    if edit:
        try:
            await message.edit_text(text,reply_markup=markup,parse_mode=None)
        except TelegramBadRequest as error:
            if 'message is not modified' not in error.message.lower():raise
    else:
        await message.answer(text,reply_markup=markup,parse_mode=None)


async def quiz_command(message: Message, bot: Bot, db: Database) -> None:
    if not message.from_user:return
    await db.ensure_user(message.from_user.id,message.from_user.username)
    user=await db.begin_quiz(message.from_user.id)
    await display_quiz(message,bot,db,user)


async def quiz_callback(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if not isinstance(query.message,Message):return
    await db.ensure_user(query.from_user.id,query.from_user.username)
    if query.data=='quiz':
        user=await db.begin_quiz(query.from_user.id)
    else:
        _,session,index,choice=query.data.split(':')
        try:
            result=await db.answer_quiz(query.from_user.id,session,int(index),int(choice))
        except ValueError:
            await query.message.answer('Эта кнопка устарела. Откройте /quiz, чтобы продолжить тест.')
            return
        user=result['user']
        if not user['quiz_session'] and not user['quiz_completed']:
            await query.message.answer('Откройте /quiz, чтобы начать тест Системы.')
            return
    await display_quiz(query.message,bot,db,user,edit=True)
    if user['quiz_completed']:await notify_levelups(bot,db,query.from_user.id)


async def quiz_inline(query: InlineQuery, bot: Bot, db: Database) -> None:
    user=await db.get_user(query.from_user.id)
    if not user or not user['quiz_completed']:
        await query.answer([],is_personal=True,cache_time=0,
            button=InlineQueryResultsButton(text='🔮 Узнать свой класс',start_parameter='quiz'))
        return
    me=await bot.get_me()
    text=referral_text(user['quiz_class_result'],me.username,query.from_user.id)
    article=InlineQueryResultArticle(id=f'quiz-{query.from_user.id}',title='🔮 '+user['quiz_class_result'],
        description='Мой скрытый класс S-ранга. Пройди тест и узнай свой!',
        input_message_content=InputTextMessageContent(message_text=text,parse_mode=None),
        reply_markup=keyboard([[InlineKeyboardButton(text='🔮 Узнать свой скрытый класс',
            url=f'https://t.me/{me.username}?start=ref_{query.from_user.id}')]]))
    await query.answer([article],is_personal=True,cache_time=0)


async def claim_sub_key(query: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    tg_id = query.from_user.id
    await db.ensure_user(tg_id, query.from_user.username)
    user = await db.get_user(tg_id)
    # Read the clicked paywall's context, including the back button on the subscribe prompt.
    selected_chapter = None
    for row in query.message.reply_markup.inline_keyboard if query.message.reply_markup else []:
        for item in row:
            match = re.fullmatch(r'(?:buy|credit|peek|chapter):([1-9][0-9]{0,17})', item.callback_data or '')
            if match:
                chapter = await db.get_chapter(int(match[1]))
                if chapter and not await db.can_read(tg_id, chapter):
                    selected_chapter = chapter
                    break
        if selected_chapter:
            break
    if user['channel_sub_reward_claimed']:
        text = '🔑 Вы уже получили подарок за подписку. Ваши ключи и ранг: /profile.'
    else:
        if settings.channel_id is None:
            await query.message.answer('⏳ Проверка подписки временно недоступна. Попробуйте позже.')
            return
        try:
            member = await bot.get_chat_member(chat_id=settings.channel_id, user_id=tg_id)
        except TelegramAPIError as error:
            logging.getLogger(__name__).warning('Subscription verification deferred: %s', type(error).__name__)
            await query.message.answer('⏳ Не удалось проверить подписку. Попробуйте ещё раз чуть позже.',
                reply_markup=keyboard([[button('✅ Проверить ещё раз', 'claim_sub_key')]]))
            return
        subscribed = member.status in ('creator', 'administrator', 'member') or (
            member.status == 'restricted' and member.is_member)
        if not subscribed:
            channel = settings.channel_id if isinstance(settings.channel_id, str) and settings.channel_id.startswith('@') else None
            if channel is None:
                try:
                    chat = await bot.get_chat(settings.channel_id)
                    channel = '@' + chat.username if chat.username else None
                    link = 'https://t.me/' + chat.username if chat.username else chat.invite_link
                except TelegramAPIError:
                    link = None
            else:
                link = 'https://t.me/' + channel[1:]
            rows = [[InlineKeyboardButton(text='📢 Перейти в канал', url=link)]] if link else []
            rows.append([button('✅ Я подписался, проверить!', 'claim_sub_key')])
            if selected_chapter:
                rows.append([button('🔙 Вернуться к выбранной главе', f"chapter:{selected_chapter['id']}")])
            await query.message.answer(f'📢 Подпишитесь на наш канал {channel or "NovelPulse"}, '
                'чтобы не пропускать новые аудио-главы, а затем нажмите кнопку ниже 👇', reply_markup=keyboard(rows))
            return
        claimed = await db.claim_channel_subscription_reward(tg_id)
        text = ('🎉 Подписка подтверждена! Вам начислен +1 🔑 бесплатный ключ к любой платной главе и +25 XP!'
            if claimed else '🔑 Вы уже получили подарок за подписку. Ваши ключи и ранг: /profile.')
    account = await db.get_user(tg_id)
    chapter = (selected_chapter or await db.latest_locked_paywall(tg_id)) if account['free_unlock_credits'] > 0 else None
    rows = [[button('🔑 Открыть главу бонусным ключом', f"credit:{chapter['id']}")]] if chapter else []
    rows.append([button('📖 Каталог новелл', 'catalog')])
    await query.message.answer(text, reply_markup=keyboard(rows))
    await notify_levelups(bot, db, tg_id)


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


async def show_leaderboard(bot: Bot, chat_id: int, tg_id: int, db: Database) -> None:
    standings = await db.get_leaderboard(viewer_tg_id=tg_id)
    resume = await db.resume_info(tg_id)
    await send_text(bot, chat_id, leaderboard_window(standings), keyboard([
        [button('⚔️ Призвать друга (+50 XP)', 'referral')],
        [button('⚡ Взять VIP (+100 XP)', 'vip')],
        [button('📖 Читать дальше (+10 XP)', f"read:{resume['id']}" if resume else 'catalog')],
        [button('👤 Мой Ранг в Системе', 'profile')]]))


async def leaderboard(message: Message, bot: Bot, db: Database) -> None:
    if not message.from_user:
        return
    await db.ensure_user(message.from_user.id, message.from_user.username)
    await show_leaderboard(bot, message.chat.id, message.from_user.id, db)


async def leaderboard_callback(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if isinstance(query.message, Message):
        await db.ensure_user(query.from_user.id, query.from_user.username)
        await show_leaderboard(bot, query.message.chat.id, query.from_user.id, db)


async def show_search(bot: Bot, chat_id: int, db: Database, query: str = '', trope: str | None = None) -> None:
    rows = [[button(label, f'trope:{slug}')] for slug, label, *_ in TROPES]
    rows.append([button('🔙 Назад к каталогу', 'catalog')])
    title = next((label for slug, label, *_ in TROPES if slug == trope), '🔍 Поиск по сюжетам')
    await bot.send_message(chat_id, title + '\nВыберите троп или введите /search молния. Можно искать по названию главы.',
        parse_mode=None, reply_markup=keyboard(rows))
    if not query.strip() and trope is None:
        return
    results = search_catalog(await db.searchable_catalog(), query, trope)
    await bot.send_message(chat_id, f'📚 Найдено новелл: {len(results)}' if results else
        'Ничего не найдено. Попробуйте другой запрос или сюжетный фильтр.', parse_mode=None)
    # Telegram's message and keyboard budgets stay bounded as the catalog grows.
    for series in results[:25]:
        actions = [button('📖 Читать', f"series:{series['id']}")]
        if series['first_chapter_id']:
            actions.append(button('🎧 Слушать', f"listen:{series['first_chapter_id']}"))
        await bot.send_message(chat_id, f"📖 «{series['title'][:220]}»\n{series['genre_title']}\n{series['description'][:500]}",
            parse_mode=None, reply_markup=keyboard([actions]))
    if len(results) > 25:
        await bot.send_message(chat_id, 'Показаны первые 25 новелл. Уточните запрос через /search.')


async def search_command(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    await show_search(bot, message.chat.id, db, (command.args or '')[:120])


async def search_callback(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    trope = query.data.split(':', 1)[1] if query.data.startswith('trope:') else None
    if trope is not None and trope not in TROPE_IDS:
        return
    await show_search(bot, query.message.chat.id, db, trope=trope)


async def profile(message: Message, bot: Bot, db: Database) -> None:
    if not message.from_user:return
    await db.ensure_user(message.from_user.id,message.from_user.username)
    await notify_levelups(bot,db,message.from_user.id)
    user=await db.get_user(message.from_user.id)
    await message.answer(status_window(user),parse_mode=None,reply_markup=profile_markup(user))


async def profile_callback(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if not isinstance(query.message,Message):return
    await db.ensure_user(query.from_user.id,query.from_user.username)
    await notify_levelups(bot,db,query.from_user.id)
    user=await db.get_user(query.from_user.id)
    await query.message.answer(status_window(user),parse_mode=None,reply_markup=profile_markup(user))


async def share_story_card(query: CallbackQuery, bot: Bot, db: Database) -> None:
    """Send the user's temporary rank card and remove it even if Telegram fails."""
    await query.answer()
    if not isinstance(query.message, Message):
        return
    tg_id = query.from_user.id
    await db.ensure_user(tg_id, query.from_user.username)
    user = await db.get_user(tg_id)
    path = render_player_story_card(user)
    try:
        bot_user = await bot.get_me()
        share_url = 'https://t.me/share/url?' + urlencode({
            'url': f'https://t.me/{bot_user.username}?start=ref_{tg_id}',
            'text': f"Мой ранг в Системе: {user['player_rank']}-ранг! Читай бесплатно в NovelPulse",
        })
        markup = keyboard([
            [InlineKeyboardButton(text='📲 Опубликовать в Stories', url=share_url)],
            [button('🎁 Забрать 50 XP', 'claim_story_chest')],
        ])
        await bot.send_photo(
            chat_id=query.message.chat.id,
            photo=FSInputFile(path),
            caption=('🏆 Ваша персональная карточка ранга готова!\n'
                'Опубликуйте её в своих Telegram Stories, чтобы друзья узнали ваш ранг в Системе.\n\n'
                '🎁 Нажмите кнопку ниже, чтобы забрать награду:'),
            reply_markup=markup,
        )
    finally:
        Path(path).unlink(missing_ok=True)


async def claim_story_chest(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    await db.ensure_user(query.from_user.id, query.from_user.username)
    claimed = await db.claim_story_reward(query.from_user.id)
    if claimed:
        text = '🎉 [СУНДУК ЗА ИСТОРИЮ ОТКРЫТ!]\nВам начислено +50 XP к Рангу Игрока! Приятного чтения!'
        await notify_levelups(bot, db, query.from_user.id)
    else:
        text = 'Вы уже получили награду за публикацию истории!'
    await query.message.answer(text, parse_mode=None)


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
    elif query.data.startswith('genre:'):
        await show_catalog(bot,query.message.chat.id,db,query.data.split(':')[1])
    elif query.data.startswith("page:"):
        _, chapter_id, page = query.data.split(":")
        await show_chapter(bot, query.message.chat.id, query.from_user.id, int(chapter_id), db,
                           page=int(page), edit_message_id=query.message.message_id)
    elif query.data.startswith(("chapter:",'read:')):
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
        "⚔️ Совместный Рейд: вам и новому другу — по +1 🔑 бесплатному ключу!\n"
        "Вы получите +50 XP, друг — +25 XP.\n"
        f"Ваших ключей: {account['free_unlock_credits']}\n\n"
        f"Ваша ссылка: https://t.me/{me.username}?start=raid_{query.from_user.id}\n\n"
        "Награды начисляются один раз, когда новый друг откроет главу 1 по вашей ссылке.",
        reply_markup=keyboard([[InlineKeyboardButton(text='📤 Позвать напарника в рейд',
            url=raid_share_url(me.username,query.from_user.id))]])
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


async def wuf_unlock(query: CallbackQuery, bot: Bot, db: Database) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    await db.ensure_user(query.from_user.id, query.from_user.username)
    chapter_id = int(query.data.split(':')[1])
    try:
        inserted = await db.unlock_with_wuf(query.from_user.id, chapter_id)
    except ValueError as error:
        await query.message.answer(str(error))
        return
    if inserted:
        await query.message.answer('🎉 Вы открыли главу бесплатно по суточному таймеру! '
            'Следующий бесплатный доступ откроется через 24 часа. Приятного чтения!')
    await show_chapter(bot, query.message.chat.id, query.from_user.id, chapter_id, db)


async def wuf_cooldown_info(query: CallbackQuery, db: Database) -> None:
    account = await db.get_user(query.from_user.id)
    timer = wuf_status(account['last_wuf_unlock_at'] if account else '')
    text = ('⏳ Бесплатная глава уже доступна! Откройте нужную главу заново.'
            if timer['available'] else timer['label'])
    await query.answer(text, show_alert=True)


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
    router.message.register(leaderboard, Command('leaderboard'), private)
    router.message.register(search_command, Command('search'), private)
    router.message.register(quiz_command, Command('quiz'), private)
    router.message.register(terms, Command("terms"), private)
    router.message.register(payment_support, Command("paysupport", "support"), private)
    router.callback_query.filter(F.message.chat.type == "private")
    router.callback_query.register(leaderboard_callback, F.data == 'leaderboard')
    router.callback_query.register(search_callback, F.data.regexp(r'^search$|^trope:(?:dungeons|technology|pets|hidden|cultivation|crafting)$'))
    router.callback_query.register(vip, F.data.in_(("vip", "vip_menu")))
    router.callback_query.register(referral, F.data == "referral")
    router.callback_query.register(daily_gift, F.data == "daily_gift")
    router.callback_query.register(claim_sub_key, F.data == 'claim_sub_key')
    router.callback_query.register(quiz_callback, F.data=='quiz')
    router.callback_query.register(show_library,F.data=='library')
    router.callback_query.register(share_story_card,F.data=='share_story_card')
    router.callback_query.register(claim_story_chest,F.data=='claim_story_chest')
    router.callback_query.register(quiz_callback, F.data.regexp(r'^qz:[0-9a-f]{8}:[0-3]:[0-3]$'))
    router.inline_query.register(quiz_inline)
    router.callback_query.register(profile_callback, F.data == 'profile')
    router.callback_query.register(listen, F.data.regexp(r'^listen:[1-9][0-9]{0,17}$'))
    router.callback_query.register(credit_unlock, F.data.regexp(r"^credit:[1-9][0-9]{0,17}$"))
    router.callback_query.register(wuf_unlock, F.data.regexp(r'^wuf_unlock:[1-9][0-9]{0,17}$'))
    router.callback_query.register(wuf_cooldown_info, F.data=='wuf_cooldown_info')
    router.callback_query.register(peek, F.data.regexp(r'^peek:[1-9][0-9]{0,17}$'))
    router.callback_query.register(navigate, F.data.regexp(
        r"^(catalog|genre:(?:litrpg|cultivation|isekai|science_magic|taming|comedy_isekai|audio)|(?:series|chapter|read):[1-9][0-9]{0,17}|page:[1-9][0-9]{0,17}:[0-9]{1,6})$"))
    register_reviews(router,private)
    return router
