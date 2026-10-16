"""Shared, explicit two-sided raid offer. Reward amounts live in the XP ledger."""
from urllib.parse import urlencode

RAID_LABEL='⚔️ Совместный Рейд: По +1 ключу тебе и другу!'
RAID_SHARE_TEXT='⚔️ Вступай со мной в рейд в NovelPulse — после твоего первого открытия главы 1 нам ОБОИМ дадут по +1 бесплатному ключу к платным главам!'


def raid_share_url(username,tg_id):
    return 'https://t.me/share/url?'+urlencode({'url':f'https://t.me/{username}?start=raid_{tg_id}',
        'text':RAID_SHARE_TEXT})


async def announce_reading_reward(bot, db, tg_id, inviter):
    """Called only when the atomic chapter-one reward returns a new inviter."""
    if not inviter:return
    from aiogram.exceptions import TelegramAPIError
    from app.gamification import notify_levelups
    for target,text in (
        (inviter,'⚔️ Напарник открыл первую главу! Вам начислены +1 🔑 бесплатный ключ и +50 XP.'),
        (tg_id,'⚔️ Рейд подтверждён чтением! Вам начислены +1 🔑 бесплатный ключ и +25 XP. Ваш напарник получил +1 🔑 и +50 XP.')):
        try:await bot.send_message(target,text,parse_mode=None)
        except TelegramAPIError:pass
        await notify_levelups(bot,db,target)
