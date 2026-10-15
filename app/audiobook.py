"""Ephemeral Russian chapter audio; desktop/media packages are imported only on demand."""

import asyncio
import base64
import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path

from aiogram.enums import ChatAction
from aiogram.types import FSInputFile

AUDIO_DIR = Path(__file__).resolve().parents[1] / 'data' / 'audio_tmp'
logger = logging.getLogger(__name__)


def audio_title(chapter: dict) -> str:
    return f"Глава {chapter['chapter_num']}: {chapter['title']}"


def _tag(path: Path, chapter: dict, series: dict) -> None:
    from mutagen.id3 import ID3, TIT2, TPE1, TALB
    def clean(text):
        return re.sub(r'[\x00-\x1f\x7f]', ' ', str(text)).strip()
    tags = ID3()
    tags.add(TIT2(encoding=3, text=clean(audio_title(chapter))))
    tags.add(TPE1(encoding=3, text='NovelPulse Audio'))
    tags.add(TALB(encoding=3, text=clean(series['title'])))
    tags.save(path, v2_version=3)


async def render_chapter_audio(chapter: dict, series: dict, voice: str = 'ru-RU-DmitryNeural') -> Path:
    """Edge emits mono 24kHz / 48kbps MP3; ownership transfers to caller on success."""
    import edge_tts
    text = chapter['content_text'].strip()
    if not text or len(text) > 500_000:
        raise ValueError('Пустая или слишком длинная глава для озвучки')
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=AUDIO_DIR, prefix='chapter-', suffix='.mp3', delete=False) as stream:
        path = Path(stream.name)
    try:
        narration = audio_title(chapter) + '.\n\n' + text
        async with asyncio.timeout(900):
            await edge_tts.Communicate(narration, voice=voice, rate='+10%').save(str(path))
        if not path.is_file() or path.stat().st_size < 100:
            raise RuntimeError('Сервис озвучки вернул пустой аудиофайл')
        if path.stat().st_size >= 49_000_000:
            raise ValueError('Аудиоглава превышает лимит отправки Telegram')
        tagging = asyncio.create_task(asyncio.to_thread(_tag, path, chapter, series))
        try:
            await asyncio.shield(tagging)
        except asyncio.CancelledError:
            # A cancelled to_thread keeps running: wait for its file handle before unlink.
            await tagging
            raise
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class AudioJobs:
    """Bounded jobs let the sequential durable cashier acknowledge updates promptly."""
    def __init__(self):
        self.tasks = {}
        self.closed = False
        self.capacity = asyncio.Semaphore(2)

    def start(self, bot, db, tg_id, chat_id, chapter_id, *, lease=None) -> str:
        if self.closed or len(self.tasks) >= 4:
            return 'busy'
        if tg_id in self.tasks:
            return 'duplicate'
        task = asyncio.create_task(self._deliver(bot, db, tg_id, chat_id, chapter_id, lease),
                                   name=f'audiobook:{chapter_id}')
        self.tasks[tg_id] = task
        task.add_done_callback(lambda done: self.tasks.pop(tg_id, None))
        return 'started'

    async def _deliver(self, bot, db, tg_id, chat_id, chapter_id, lease):
        path = None
        try:
            async with self.capacity:
                if lease is not None:
                    await lease.assert_owner()
                chapter = await db.get_chapter(chapter_id, include_unpublished=await db.has_purchase(tg_id,chapter_id))
                if chapter is None or not await db.can_read(tg_id, chapter):
                    return
                series = await db.get_series(chapter['series_id'])
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
                path = await render_chapter_audio(chapter, series)
                # Revalidate the lease/VIP after synthesis; an old node must never send.
                if lease is not None:
                    await lease.assert_owner()
                if not await db.can_read(tg_id, chapter):
                    await bot.send_message(chat_id, '🔒 Срок VIP истёк. Откройте главу или продлите VIP через /catalog.')
                    return
                await bot.send_audio(chat_id, FSInputFile(path), title=audio_title(chapter)[:128],
                    performer='NovelPulse', caption=(f"🎧 «{series['title'][:220]}» — Глава {chapter['chapter_num']}\n"
                    f"📖 Продолжить чтение: /start ch_{chapter_id}"), parse_mode=None, request_timeout=180)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning('Audio chapter delivery failed: %s', type(error).__name__)
            try:
                if lease is not None:
                    await lease.assert_owner()
                await bot.send_message(chat_id, 'Не удалось доставить аудиоглаву. Попробуйте ещё раз чуть позже; текст доступен в /catalog.')
            except Exception:
                pass
        finally:
            if path is not None:
                Path(path).unlink(missing_ok=True)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()


def preview_fingerprint(chapter: dict) -> str:
    return hashlib.sha256((audio_title(chapter)+'\n'+chapter['content_text']).encode()).hexdigest()


async def prepare_web_audio_previews(db) -> dict:
    """Only public free-chapter snippets become data URIs; no MP3 retained on disk."""
    from app.promotional_text import complete_excerpt
    previews = json.loads(await db.get_state('web_audio_previews') or '{}')
    for series in await db.list_series():
        chapters = await db.list_chapters(series['id'])
        first = next((c for c in chapters if c['chapter_num'] <= series['free_chapters_limit']), None)
        if first is None:
            continue
        chapter = await db.get_chapter(first['id'])
        digest = preview_fingerprint(chapter)
        existing = previews.get(str(series['id']), {})
        if existing.get('source_digest') == digest and existing.get('audio_base64'):
            continue
        snippet = complete_excerpt(chapter['content_text'],350)
        path = None
        try:
            path = await render_chapter_audio({**chapter, 'content_text': snippet}, series)
            encoded = base64.b64encode(Path(path).read_bytes()).decode('ascii')
            if len(encoded) > 350_000:
                raise ValueError('Превью слишком велико для встроенного web-плеера')
            previews[str(series['id'])] = {'source_digest': digest, 'chapter_id': chapter['id'],
                'audio_base64': encoded, 'transcript': snippet}
            await db.set_state('web_audio_previews',json.dumps(previews,ensure_ascii=False))
        finally:
            if path is not None:
                Path(path).unlink(missing_ok=True)
    return {'previews': len(previews)}


async def prepare_chapter_audio_previews(db) -> dict:
    """Short, authorized chapter-specific excerpts; full paid audio stays in the bot."""
    from app.promotional_text import complete_excerpt
    previews=json.loads(await db.get_state('chapter_audio_previews') or '{}')
    rendered=0
    for series in await db.list_series():
        for row in await db.list_chapters(series['id']):
            chapter=await db.get_chapter(row['id']);key=str(chapter['id'])
            digest=preview_fingerprint(chapter)
            if previews.get(key,{}).get('source_digest')==digest:continue
            # Reuse an identical first-chapter preview from Stage 23, where available.
            books=json.loads(await db.get_state('web_audio_previews') or '{}')
            book=books.get(str(series['id']),{})
            if book.get('chapter_id')==chapter['id'] and book.get('source_digest')==digest:
                previews[key]=book
            else:
                snippet=complete_excerpt(chapter['content_text'][:1200],250)
                path=None
                try:
                    path=await render_chapter_audio({**chapter,'content_text':snippet},series)
                    encoded=base64.b64encode(Path(path).read_bytes()).decode('ascii')
                    if len(encoded)>350_000:raise ValueError('Слишком большой аудиофрагмент')
                    previews[key]={'source_digest':digest,'chapter_id':chapter['id'],
                        'audio_base64':encoded,'transcript':snippet}
                    rendered+=1
                finally:
                    if path is not None:Path(path).unlink(missing_ok=True)
            await db.set_state('chapter_audio_previews',json.dumps(previews,ensure_ascii=False))
            print(json.dumps({'chapter_audio_ready':chapter['id'],'cached':len(previews)}),flush=True)
    return {'previews':len(previews),'rendered':rendered}
