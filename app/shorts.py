"""Three real neural trailers per novel and a resumable publishing queue."""

import asyncio
import hashlib
import json
import re
import unicodedata
from pathlib import Path

from app.autopilot import video_ready
from app.db import Database
from app.promotional_text import complete_excerpt
from app.telegraph_seo import bot_link, channel_link
from app.privacy import anonymous_series_url, assert_public_safe
from app.video_gen import EPISODE_LABELS, build_episode_scenes, build_drama_scenes, generate_episode_video, RENDERER_VERSION

SHORTS_DIR = Path('data/shorts_queue')


def shock_hook(series: dict, episode_num: int = 1) -> str:
    """Narrator copy only; formal titles/episode labels are never spoken as an intro."""
    title=series['title'].casefold()
    if 'очевидно' in title and 'святой' in title:
        return 'Он объявил себя святым — и заставил четырёх архангелов работать на себя!'
    if 'приручения' in title or 'самозанятый' in title:
        return 'Что делать, если Система выдала тебе ошейник питомца вместо оружия?!'
    if 'изменённом мире' in title or 'измененном мире' in title:
        return 'Тысячу людей выбросило в разлом, но правила выживания знают только единицы!'
    if 'рельсотрон' in title:
        return 'Пятилетний сирота соединил магию молний с физикой и собрал рельсотрон!'
    if 'садовниц' in title:
        return 'Она вырастила траву — и получила награду Системы! Что скрывает этот сад?!'
    if 'нулевого ранга' in title or 'нулевой ранг' in title:
        return 'Ему дали нулевой ранг — почему же Система боится его пробуждения?!'
    return 'Система предложила награду — но ценой ошибки станет твоя жизнь!'


def viral_theme(series: dict) -> tuple[tuple[str, str, str], str]:
    title = series['title'].casefold()
    if 'очевидно' in title and 'святой' in title:
        return ('Очевидно святой', 'Четыре архангела получили работу от смертного',
                'Святой или самозванец кто такой Коул'), '#фэнтези #новелла #shorts'
    if 'нулевого ранга' in title or 'нулевой ранг' in title:
        return ('Ему дали НУЛЕВОЙ РАНГ но Система дала сбой',
                'Система показала ему собственную могилу',
                'Его нулевой ранг скрывал секрет Системы'), '#литрпг #новелла #shorts'
    if 'садовниц' in title:
        return ('Духовная садовница бесконечные награды Системы',
                'Она вырастила траву и получила награду Системы',
                'Одна ошибка в саду Системы может стать последней'), '#литрпг #исэкай #shorts'
    if 'рельсотрон' in title:
        return ('Я собрал РЕЛЬСОТРОН с помощью магии молний',
                'Магия молний стала питанием для рельсотрона',
                'Пятилетний сирота создаёт оружие нового мира'), '#литрпг #магия #shorts'
    if 'изменённом мире' in title or 'измененном мире' in title:
        return ('Он проснулся в мире разломов классов и уровней',
                'Тысячи попаданцев борются за место в новом мире',
                'Новый мир изменил правила выживания'), '#литрпг #исэкай #shorts'
    if 'самозанятый' in title or 'приручения' in title:
        return ('Система выбрала ему класс УКРОТИТЕЛЬ',
                'Система сделала его собственным питомцем',
                'Ошейник и навыки есть а хозяина нет'), '#литрпг #система #shorts'
    clean_title = re.split(r'\s*[\[(]', series['title'], maxsplit=1)[0]
    return (clean_title + ' начало приключения', clean_title + ' Система меняет правила',
            clean_title + ' что будет дальше'), '#литрпг #новелла #shorts'


def sanitize_filename_title(value: str, limit: int = 86) -> str:
    value = unicodedata.normalize('NFKC', value)
    value = ''.join(' ' if unicodedata.category(c).startswith('C') else c for c in value)
    value = re.sub(r'[^\w\s#-]|_', ' ', value, flags=re.UNICODE)
    value = ' '.join(value.split()).strip(' .-')
    if not value:
        value = 'Новелла'
    if re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', value):
        value = 'Новелла ' + value
    if len(value) > limit:
        prefix = value[:limit + 1]
        value = prefix.rsplit(' ', 1)[0] if ' ' in prefix else value[:limit]
    return value.rstrip(' .-')


def viral_filename(series: dict, episode_num: int, *, collision_tag: str = '') -> str:
    if episode_num not in (1, 2, 3):
        raise ValueError('Episode number must be 1, 2 or 3')
    hooks, tags = viral_theme(series)
    collision_tag = sanitize_filename_title(collision_tag, 20) if collision_tag else ''
    suffix = f"{(' ' + collision_tag) if collision_tag else ''} Ч{episode_num} {tags}"
    budget = 90 - len('.mp4') - len(suffix)
    if budget < 8:
        raise ValueError('Filename suffix leaves no room for a Russian title')
    return sanitize_filename_title(hooks[episode_num - 1], budget) + suffix + '.mp4'


def archive_previous_assets(directory: Path, old: Path, target: Path) -> None:
    """Compatibility entry point: obsolete generated media is evicted, never archived."""
    root = directory.resolve()
    old, target = old.resolve(), target.resolve()
    if old == target or not old.is_relative_to(root) or old.parent != root:
        return
    from app.storage_cleanup import delete_video
    delete_video(old, root=root)


def short_copy(series: dict, episode_num: int, username: str, channel_url: str, portal_url: str = '') -> tuple[str, str]:
    title = viral_filename(series, episode_num)[:-4]
    description = (f"📚 {series['title']}\n{EPISODE_LABELS[episode_num]} — история продолжается в NovelPulse.\n\n"
                   "Первые две главы бесплатно. Первая платная глава новому читателю — 1 ⭐️.\n"
                   "⚡ VIP на 24 часа — 19 ⭐️ · 👑 VIP на 7 дней — 49 ⭐️: все книги.\n\n"
                   f"📖 Читать: {bot_link(username, series['id'])}\n"
                   + (f"📢 Канал: {channel_url}\n" if channel_url else '')
                   + f"🌐 Библиотека: {anonymous_series_url(series)}\n"
                   + "\n#NovelPulse #ЛитРПГ #Исэкай #ВебНовеллы #Shorts #TikTok #VKКлипы")
    assert_public_safe((title, description))
    return title, description


def episode_fingerprint(series, chapters, username, episode_num):
    value = {'version': RENDERER_VERSION, 'series_id': series['id'], 'title': series['title'], 'username': username,
             'episode_num': episode_num, 'scenes': build_drama_scenes(series, chapters, episode_num)}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def generate_shorts_queue(db: Database, username: str, channel_id: str | int | None, *,
                                 output_dir: str | Path = SHORTS_DIR, portal_url: str = '', progress=None) -> dict:
    """Plan metadata only. Videos are rendered immediately before actual delivery."""
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    channel_url = channel_link(channel_id) if isinstance(channel_id, str) else ''
    used_names = set()
    for series in await db.list_series():
        for episode in (1, 2, 3):
            name = viral_filename(series, episode)
            if name.casefold() in used_names:
                name = viral_filename(series, episode, collision_tag=f"Книга {series['id']}")
            used_names.add(name.casefold())
            path = directory / name
            _, description = short_copy(series, episode, username, channel_url, portal_url)
            await db.save_short(series['id'], episode, str(path), path.stem, description)
    rows = await db.list_shorts()
    lines = ['# NovelPulse · JIT-очередь', '',
             'MP4 создаётся только перед отправкой и удаляется после подтверждённой доставки.',
             'ПК: scripts/auto_upload_youtube.py · Telegram: /next_short или /autopublish_short', '']
    for row in rows:
        lines += [f"## {row['id']}. {row['title']}", f"Статус: {row['status']}", row['description'], '']
    (directory / 'README_UPLOAD.md').write_text('\n'.join(lines), encoding='utf-8')
    return {'generated': [], 'reused': [], 'errors': [], 'queue_count': len(rows), 'status': 'complete'}


async def ensure_short_video(db: Database, short: dict, username: str, *, root: Path = SHORTS_DIR) -> Path:
    """Caller holds media_slot until delivery; no completed episode is regenerated."""
    path = Path(short['video_path']).resolve()
    root = root.resolve()
    if not path.is_relative_to(root) or path.suffix.lower() != '.mp4':
        raise ValueError('Видео должно находиться внутри очереди MP4')
    if short['status'] in ('uploaded', 'sent', 'uncertain'):
        raise ValueError('Эпизод уже доставлен или требует сверки; повторная отправка запрещена')
    series = await db.get_series(short['series_id'])
    chapters = [await db.get_chapter(c['id']) for c in await db.list_chapters(series['id'])]
    fingerprint = episode_fingerprint(series, chapters, username, short['episode_num'])
    if video_ready(path, fingerprint):
        return path
    if any(p.resolve() != path for p in root.rglob('*.mp4')):
        raise ValueError('В очереди уже есть другой MP4. Выполните очистку JIT-кэша')
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            await generate_episode_video(series, chapters, username, short['episode_num'], path)
            metadata = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
            if metadata.get('mock_tts') or metadata.get('episode_num') != short['episode_num']:
                raise ValueError('Тестовая озвучка не допускается в рабочую очередь')
            metadata['input_digest'] = fingerprint
            path.with_suffix('.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
            if not video_ready(path, fingerprint):
                raise ValueError('Видео не прошло проверку контейнера/длительности')
            return path
        except ValueError:
            from app.storage_cleanup import delete_video
            delete_video(path, root=root)
            raise
        except Exception:
            from app.storage_cleanup import delete_video
            delete_video(path, root=root)
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)


async def publish_short_to_channel(db, bot, channel_id, *, short_id=None, root=SHORTS_DIR, min_interval_hours=0,defer_youtube=False):
    from datetime import datetime, timezone
    from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
    from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
    from app.storage_cleanup import media_slot, delete_video
    from app.pipeline import failure_reason
    receipts = await db.short_publications()
    if defer_youtube and short_id is not None:
        existing=next((r for r in receipts if r['short_id']==short_id and r['provider']=='telegram' and r['destination']==str(channel_id)),None)
        if existing:
            from app.publishing import publish_story_poll
            poll=await publish_story_poll(db,bot,channel_id,short_id,int(existing['remote_id']))
            return {'status':'uploaded','short_id':short_id,'message_id':int(existing['remote_id']),
                'poll':poll,'telegram_poll_message_id':poll.get('message_id'),'queue_status':(await db.get_short(short_id))['status'],
                'mp4_deleted':0,'bytes_deleted':0,'reused_receipt':True}
    last = [r for r in receipts if r['provider'] == 'telegram' and r['destination'] == str(channel_id)]
    if min_interval_hours and last:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last[-1]['published_at'].replace('Z','+00:00'))).total_seconds()
        if elapsed < min_interval_hours * 3600:
            return {'status': 'idle', 'reason': 'cooldown'}
    async with media_slot():
        short = await db.claim_short(short_id)
        if not short:
            return {'status': 'idle'}
        sent = None
        started_send = False
        try:
            username = (await bot.get_me()).username
            path = await ensure_short_video(db, short, username, root=root)
            caption = short['title'] + '\n\n' + bot_link(username, short['series_id'])
            started_send = True
            sent = await bot.send_video(channel_id, FSInputFile(path), caption=caption,
                parse_mode=None, supports_streaming=True, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text='📖 Читать в боте ➡️', url=bot_link(username, short['series_id']))]]))
            await db.record_short_publication(short['id'], 'telegram', str(channel_id), str(sent.message_id),finalize=not defer_youtube)
            removed = delete_video(path, root=root)
            from app.publishing import publish_story_poll
            poll=await publish_story_poll(db,bot,channel_id,short['id'],sent.message_id)
            if defer_youtube:await db.finish_short(short['id'],'pending')
            return {'status': 'uploaded', 'short_id': short['id'], 'message_id': sent.message_id,
                    'queue_status':'pending' if defer_youtube else 'uploaded',
                    'poll':poll,'telegram_poll_message_id':poll.get('message_id'),
                    'bytes_deleted': sum(p['bytes'] for p in removed), 'mp4_deleted': sum(p['mp4'] for p in removed)}
        except Exception as error:
            if not sent:
                status = 'uncertain' if started_send and (not isinstance(error, TelegramAPIError) or isinstance(error, TelegramNetworkError)) else 'pending'
                await db.finish_short(short['id'], status)
            else:
                status = 'uploaded' if (await db.get_short(short['id']))['status'] == 'uploaded' else 'uncertain'
                if status == 'uncertain':
                    await db.finish_short(short['id'], status, sent.message_id)
            return {'status': status, 'short_id': short['id'], 'message_id': sent.message_id if sent else None,
                    'error': failure_reason(error)}
