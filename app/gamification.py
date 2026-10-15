"""Fixed RPG economy; XP sources are server-side events, never callback amounts."""
import asyncio
import logging

XP_EVENTS = {'read': 10, 'checkin': 25, 'referral': 50, 'purchase': 100, 'bootstrap': 0}
RANKS = (
    ('F', 0, 'Новичок без класса', 0), ('E', 50, 'Кандидат Системы', 0),
    ('D', 150, 'Пробуждённый', 1), ('C', 350, 'Боец Разлома', 1),
    ('B', 750, 'Охотник за артефактами', 2), ('A', 1500, 'Мастер Подземелий', 0),
    ('S', 3000, 'Владыка Системы', 0),
)


def rank_for_xp(xp: int) -> dict:
    if type(xp) is not int or xp < 0:
        raise ValueError('XP must be a non-negative integer')
    index = max(i for i,r in enumerate(RANKS) if xp >= r[1])
    rank, threshold, title, _ = RANKS[index]
    return {'rank':rank,'title':title,'threshold':threshold,
            'xp_to_next':RANKS[index+1][1]-xp if index+1<len(RANKS) else 0}


def status_window(user: dict) -> str:
    rank = rank_for_xp(user['player_xp'])
    name = '@'+user['username'] if user.get('username') else 'Читатель'
    text = (f"🛡 [ ОКНО СТАТУСА: {name[:70]} ]\n"
        f"🏅 Ранг: {rank['rank']} — {rank['title']}\n"
        f"⚡ Опыт: {user['player_xp']} XP (до следующего ранга: {rank['xp_to_next']} XP)\n"
        f"📖 Прочитано глав: {user['chapters_read_count']} · 🔥 Стрик: {user['streak_days']}/3 дн.\n"
        f"🔑 Доступные ключи: {user['free_unlock_credits']}")
    if user['player_xp'] >= 50:
        text += '\n\n⚡ Секрет E-ранга: VIP на 24 часа за 9 ⭐️ вместо 19 ⭐️!'
    if rank['rank']=='S':text+='\n👑 Максимальный ранг достигнут!'
    return text


async def notify_levelups(bot, db, tg_id):
    rewards = await db.claim_rank_banners(tg_id)
    if not rewards:return
    lines = ['🎉 [ СИСТЕМА: ПОВЫШЕНИЕ РАНГА! ]']
    for row in rewards:
        rank = next(r for r in RANKS if r[0]==row['rank'])
        benefit = ('⚡ Секретный VIP на 24 часа — 9 ⭐️ вместо 19 ⭐️!' if rank[0]=='E' else
                   f"+{rank[3]} 🔑 бесплатных ключей!" if rank[3] else 'Новый титул Системы открыт!')
        lines.append(f'{rank[0]}-ранг — {rank[2]}\n{benefit}')
    try:
        await bot.send_message(tg_id,'\n\n'.join(lines)+'\n\nВаш статус: /profile',parse_mode=None)
        await db.finish_rank_banners(tg_id,[r['rank'] for r in rewards])
    except Exception as error:
        # Claimed/ambiguous sends are not spammed again; rewards are already durable.
        logging.getLogger(__name__).warning('Rank banner not confirmed: %s',type(error).__name__)


async def rank_alert_loop(db, bot):
    while True:
        try:
            async with db._lock:
                users = await db._all('SELECT DISTINCT tg_id FROM rank_awards WHERE notified=0 LIMIT 20')
            for user in users:await notify_levelups(bot,db,user['tg_id'])
        except Exception as error:
            logging.getLogger(__name__).warning('Rank outbox deferred: %s',type(error).__name__)
        await asyncio.sleep(10)
