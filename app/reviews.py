"""Durable, private-chat review forms and truthful reader rating presentation."""
import re
from aiogram import F
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from app.gamification import notify_levelups

REVIEW_LABEL='⭐️ Оставить отзыв (+15 XP)'


def review_button(series_id):
    return InlineKeyboardButton(text=REVIEW_LABEL,callback_data=f'write_review:{series_id}')


def rating_line(average,count):
    if not count:
        return '⭐️ Оценок пока нет — станьте первым читателем с отзывом!'
    return f'⭐️ Рейтинг читателей: {average:.1f}/5.0 ({count} отзывов)'


def markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_form(bot,db,tg_id,chat_id,series_id):
    draft=await db.begin_review(tg_id,series_id)
    series=await db.get_series(series_id)
    await bot.send_message(chat_id,f'⭐️ Ваша оценка новеллы «{series["title"][:250]}»?\n'
        '+15 XP за первый отзыв об этой книге. Оценку можно изменить без повторной награды.',parse_mode=None,
        reply_markup=markup([[InlineKeyboardButton(text=f'⭐ {n}',
            callback_data=f'review_rate:{series_id}:{draft["token"]}:{n}') for n in range(1,6)]]))


async def review_command(message: Message,command: CommandObject,bot,db):
    if not message.from_user:return
    await db.ensure_user(message.from_user.id,message.from_user.username)
    if command.args:
        if not re.fullmatch(r'[1-9][0-9]{0,17}',command.args):
            await message.answer('Используйте /review или /review <номер новеллы>.')
            return
        try:await start_form(bot,db,message.from_user.id,message.chat.id,int(command.args))
        except ValueError as error:await message.answer(str(error))
        return
    series=await db.list_series()
    await message.answer('⭐️ Какую новеллу оценим?' if series else 'Каталог пока пуст. Загляните позже.',
        reply_markup=markup([[InlineKeyboardButton(text=s['title'][:80],callback_data=f'write_review:{s["id"]}')]
            for s in series[:50]]) if series else None)


async def write_review(query: CallbackQuery,bot,db):
    if not isinstance(query.message,Message):return
    await db.ensure_user(query.from_user.id,query.from_user.username)
    await query.answer()
    try:await start_form(bot,db,query.from_user.id,query.message.chat.id,int(query.data.split(':')[1]))
    except ValueError as error:await query.message.answer(str(error))


async def choose_rating(query: CallbackQuery,db):
    if not isinstance(query.message,Message):return
    _,sid,token,score=query.data.split(':')
    if not await db.rate_review_draft(query.from_user.id,int(sid),token,int(score)):
        await query.answer('Эта форма устарела. Начните заново через /review.',show_alert=True)
        return
    await query.answer()
    await query.message.answer('💬 Напишите короткое впечатление одним предложением (до 500 символов). '
        'Для любимой цитаты начните сообщение с «Цитата:».\n\n'
        'Отзыв сохранится у книги. Публикацию с вашим именем в канале можно разрешить отдельной кнопкой после сохранения.',
        reply_markup=markup([[InlineKeyboardButton(text='Пропустить текст, оставить только оценку',
            callback_data=f'review_save:{sid}:{token}')],
            [InlineKeyboardButton(text='Отменить отзыв',callback_data=f'review_cancel:{token}')]]))


class ReviewTextFilter(Filter):
    async def __call__(self,message: Message,db):
        if not message.from_user or not message.text or message.text.startswith('/'):
            return False
        draft=await db.get_review_draft(message.from_user.id)
        return {'review_draft':draft} if draft and draft['rating'] is not None else False


async def save_form(bot,db,tg_id,chat_id,draft,text='',quote=''):
    review=await db.add_or_update_review(tg_id,draft['series_id'],draft['rating'],text,quote,
        draft_token=draft['token'])
    rows=[[InlineKeyboardButton(text='📖 Вернуться к новелле',callback_data=f'series:{review["series_id"]}')]]
    if review['review_text'] or review['favorite_quote']:
        rows.append([InlineKeyboardButton(text='💬 Разрешить публикацию отзыва в канале',
            callback_data=f'review_feature:{review["id"]}')])
    await bot.send_message(chat_id,'🎉 Спасибо за отзыв! Вам начислено +15 XP к Рангу Игрока!'
        if review['xp_awarded'] else '🎉 Отзыв обновлён! Награда за эту новеллу уже получена.',
        reply_markup=markup(rows))
    await notify_levelups(bot,db,tg_id)


async def review_text(message: Message,bot,db,review_draft):
    raw=message.text.strip()
    quote=re.match(r'^цитата\s*:\s*(.+)$',raw,re.I|re.S)
    try:
        await save_form(bot,db,message.from_user.id,message.chat.id,review_draft,
            '' if quote else raw,quote[1].strip() if quote else '')
    except ValueError as error:
        await message.answer(str(error))


async def skip_text(query: CallbackQuery,bot,db):
    if not isinstance(query.message,Message):return
    _,sid,token=query.data.split(':')
    draft=await db.get_review_draft(query.from_user.id)
    if not draft or draft['series_id']!=int(sid) or draft['token']!=token or draft['rating'] is None:
        await query.answer('Форма уже сохранена или устарела. Откройте /review.',show_alert=True)
        return
    await query.answer()
    try:await save_form(bot,db,query.from_user.id,query.message.chat.id,draft)
    except ValueError as error:await query.message.answer(str(error))


async def cancel_form(query: CallbackQuery,db):
    draft=await db.get_review_draft(query.from_user.id)
    if draft and draft['token']==query.data.split(':')[1]:
        await db.cancel_review(query.from_user.id,draft['token'])
    await query.answer('Отзыв отменён.')


async def feature_review(query: CallbackQuery,db):
    result=await db.feature_own_review(query.from_user.id,int(query.data.split(':')[1]))
    await query.answer('Спасибо! Вы разрешили публикацию отзыва с вашим именем в канале.' if result
        else 'Отзыв не найден или пока не содержит текста.',show_alert=True)


def register_handlers(router,private):
    router.message.register(review_command,Command('review'),private)
    router.message.register(review_text,private,ReviewTextFilter())
    router.callback_query.register(write_review,F.data.regexp(r'^write_review:[1-9][0-9]{0,17}$'))
    router.callback_query.register(choose_rating,F.data.regexp(r'^review_rate:[1-9][0-9]{0,17}:[0-9a-f]{8}:[1-5]$'))
    router.callback_query.register(skip_text,F.data.regexp(r'^review_save:[1-9][0-9]{0,17}:[0-9a-f]{8}$'))
    router.callback_query.register(cancel_form,F.data.regexp(r'^review_cancel:[0-9a-f]{8}$'))
    router.callback_query.register(feature_review,F.data.regexp(r'^review_feature:[1-9][0-9]{0,17}$'))
