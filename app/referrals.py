"""Shared, explicit two-sided raid offer. Reward amounts live in the XP ledger."""
from urllib.parse import urlencode

RAID_LABEL='⚔️ Совместный Рейд: По +1 ключу тебе и другу!'
RAID_SHARE_TEXT='⚔️ Вступай со мной в рейд в NovelPulse — нам ОБОИМ сразу дадут по +1 бесплатному ключу к платным главам!'


def raid_share_url(username,tg_id):
    return 'https://t.me/share/url?'+urlencode({'url':f'https://t.me/{username}?start=raid_{tg_id}',
        'text':RAID_SHARE_TEXT})
