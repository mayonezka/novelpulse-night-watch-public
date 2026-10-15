"""Vertical Russian promo videos. Run: python -m app.video_gen [--series-id ID]."""

import argparse
import asyncio
import html
import json
import math
import os
import subprocess
import re
import shutil
import tempfile
import wave
from array import array
from pathlib import Path
from functools import lru_cache

import edge_tts
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

from app.promotional_text import complete_excerpt, dramatic_excerpt

VOICE = "ru-RU-DmitryNeural"
CTA = "Чем закончится история? Читай продолжение в телеграм-боте NovelPulse, ссылка в профиле и комментариях!"
SIZE = (720, 1280)
EPISODE_LABELS = {1: 'Часть 1 · Пробуждение', 2: 'Часть 2 · Вмешательство Системы', 3: 'Часть 3 · На грани смерти'}
EPISODE_CTA_PARTS = ('Продолжение истории уже в Телеграме —',
                     'просто вбей в поиск Телеграма NovelPulseBot',
                     'или переходи по ссылке в описании канала!')
EPISODE_CTA = ' '.join(EPISODE_CTA_PARTS)
RENDERER_VERSION = 5
LOOP_BRIDGE = 'И всё началось с того, что…'


def build_scenes(series: dict, chapters: list[dict]) -> list[str]:
    free = sorted((c for c in chapters if c["chapter_num"] <= series["free_chapters_limit"]),
                  key=lambda c: c["chapter_num"])
    if not free:
        raise ValueError("Для видео нужна опубликованная бесплатная глава")
    from app.shorts import shock_hook
    return [shock_hook(series),
            dramatic_excerpt(free[0]["content_text"], 125),
            dramatic_excerpt(free[-1]["content_text"], 125),
            "Чем закончится история?",
            "Читай продолжение в телеграм-боте NovelPulse, ссылка в профиле и комментариях!"]


def build_episode_scenes(series: dict, chapters: list[dict], episode_num: int) -> list[str]:
    if episode_num not in EPISODE_LABELS:
        raise ValueError('Episode number must be 1, 2 or 3')
    free = sorted((c for c in chapters if c['chapter_num'] <= min(2, series['free_chapters_limit'])),
                  key=lambda c: c['chapter_num'])
    if not free:
        raise ValueError('Для сериала нужна бесплатная глава')
    opening = re.split(r'\n\s*\n', free[0]['content_text'])[0]
    last_paragraphs = [p.strip() for p in re.split(r'\n\s*\n', free[-1]['content_text']) if p.strip()]
    if episode_num == 1:
        hook = 'Привычный мир исчезнет. Его правила больше не действуют.'
        first, second = complete_excerpt(opening, 78), dramatic_excerpt(free[0]['content_text'], 78)
        bridge = 'Это только начало. Что потребует новый мир?'
    elif episode_num == 2:
        hook = 'Система вмешалась. Теперь правила диктует она.'
        first = dramatic_excerpt('\n\n'.join(c['content_text'] for c in free), 78)
        second = dramatic_excerpt(free[-1]['content_text'], 78)
        if second == first:
            second = complete_excerpt(last_paragraphs[-1], 78)
        bridge = 'Следующий выбор может стать последним.'
    else:
        hook = 'Назад дороги нет. Цена ошибки стала слишком высокой.'
        first = complete_excerpt(last_paragraphs[-1], 78)
        # Only the already-public 300-character paywall teaser may appear in episode 3.
        third = next((c for c in chapters if c['chapter_num'] == 3), None)
        second = dramatic_excerpt(third['content_text'][:300], 78) if third else dramatic_excerpt(free[-1]['content_text'], 78)
        bridge = 'Сможет ли герой выжить? Первую платную главу открой всего за одну звезду.'
    from app.shorts import shock_hook
    return [shock_hook(series,episode_num),
            hook, first, second, bridge, *EPISODE_CTA_PARTS]


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False):
    names = (["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/tahomabd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
             if bold else ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for name in names:
        if Path(name).is_file():
            return ImageFont.truetype(name, size)
    raise RuntimeError("Не найден шрифт Arial/DejaVu Sans с поддержкой кириллицы")


def _wrap(draw, text, font, width):
    lines, line = [], ""
    for word in text.split():
        if draw.textlength(word, font=font) > width:
            if line:
                lines.append(line)
                line = ''
            fragment = ''
            for character in word:
                if fragment and draw.textlength(fragment + character, font=font) > width:
                    lines.append(fragment)
                    fragment = character
                else:
                    fragment += character
            line = fragment
            continue
        candidate = (line + " " + word).strip()
        if draw.textlength(candidate, font=font) > width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def mobile_subtitle_layout(draw, subtitle: str) -> dict:
    for size in range(52, 41, -2):
        font = _font(size, True)
        lines = _wrap(draw, subtitle, font, 604)
        if (len(lines) * (size + 17) <= 510
                and all(draw.textlength(line, font=font) <= 604 for line in lines)):
            return {'font_size': size, 'lines': lines, 'stroke_width': 3}
    raise ValueError('Subtitle is too dense for a readable mobile frame; split the scene')


def short_visual_hook(title: str) -> str:
    lower = title.casefold()
    if 'нулев' in lower:
        return 'НУЛЕВОЙ РАНГ. СИСТЕМА ДАЛА СБОЙ'
    if 'садовниц' in lower:
        return 'ЕЁ САД ВЫДАЁТ НАГРАДЫ СИСТЕМЫ'
    if 'рельсотрон' in lower:
        return 'МАГИЯ МОЛНИЙ + РЕЛЬСОТРОН'
    if 'изменённом' in lower or 'измененном' in lower:
        return 'НОВЫЙ МИР. НОВЫЕ ПРАВИЛА'
    if 'самозанят' in lower or 'приручения' in lower:
        return 'САМ СЕБЕ УКРОТИТЕЛЬ И ПИТОМЕЦ'
    return complete_excerpt(title, 60).upper()


def render_scene(title: str, subtitle: str, username: str, number: int, path: Path,
                 episode_label: str = '', total_scenes: int = 5):
    image = Image.new("RGB", SIZE)
    draw = ImageDraw.Draw(image)
    for y in range(SIZE[1]):
        glow = math.exp(-((y - 560) / 360) ** 2)
        draw.line((0, y, 720, y), fill=(int(9 + 12 * glow), int(12 + 18 * glow), int(24 + 40 * glow)))
    for x in range(-720, 1000, 90):
        draw.line((x, 0, x + 900, 1280), fill=(27, 37, 61), width=1)
    draw.rounded_rectangle((36, 56, 684, 154), 34, fill=(174, 24, 55), outline=(255, 208, 72), width=4)
    # Vector flame and magnifier avoid missing emoji glyphs in Windows Cyrillic fonts.
    draw.polygon([(67, 126), (59, 104), (76, 80), (78, 101), (92, 88), (107, 113), (98, 130)], fill=(255, 205, 53))
    part = re.search(r'Часть (\d)', episode_label)
    header = f"ЛИТРПГ · ЧАСТЬ {part.group(1)}" if part else 'NOVELPULSE · ЛИТРПГ'
    draw.text((121, 83), header, font=_font(34, True), fill='white', stroke_width=1, stroke_fill=(77, 9, 27))
    hook_font = _font(35, True)
    for i, line in enumerate(_wrap(draw, short_visual_hook(title), hook_font, 604)):
        draw.text((52, 178 + i * 46), line, font=hook_font, fill=(255, 216, 80), stroke_width=2, stroke_fill='black')
    layout = mobile_subtitle_layout(draw, subtitle)
    fontsize = layout['font_size']
    font, lines = _font(fontsize, True), layout['lines']
    height = len(lines) * (fontsize + 17)
    y = 589 - height / 2
    for line in lines:
        width = draw.textlength(line, font=font)
        draw.text(((720 - width) / 2 + 4, y + 5), line, font=font, fill='black', stroke_width=3, stroke_fill='black')
        draw.text(((720 - width) / 2, y), line, font=font, fill='white', stroke_width=3, stroke_fill='black')
        y += fontsize + 17
    draw.text((52, 871), f"{number:02d} / {total_scenes:02d}  ·  NOVELPULSE", font=_font(24, True), fill=(255, 214, 85))
    draw.rounded_rectangle((36, 934, 684, 1170), 30, fill=(255, 220, 76), outline='white', width=3)
    draw.ellipse((66, 969, 99, 1002), outline=(17, 24, 39), width=5)
    draw.line((94, 997, 109, 1012), fill=(17, 24, 39), width=6)
    draw.text((124, 964), 'Введи в поиске Telegram:', font=_font(33, True), fill=(17, 24, 39))
    name = username.lstrip('@')
    name_size = 52
    while draw.textlength(name, font=_font(name_size, True)) > 600 and name_size > 18:
        name_size -= 1
    name_font = _font(name_size, True)
    draw.text(((720 - draw.textlength(name, font=name_font)) / 2, 1014), name, font=name_font, fill=(120, 10, 31))
    draw.text((65, 1083), 'Продолжение книги в боте', font=_font(32, True), fill=(17, 24, 39))
    draw.text((65, 1125), '1-я платная глава 1', font=_font(31, True), fill=(17, 24, 39))
    draw.polygon([(385, 1122), (391, 1137), (407, 1138), (395, 1148), (399, 1164),
                  (385, 1155), (371, 1164), (375, 1148), (363, 1138), (379, 1137)], fill=(120, 10, 31))
    image.save(path)
    return layout


async def _ffmpeg(*args: str, timeout: float = 180) -> bytes:
    process = await asyncio.create_subprocess_exec(imageio_ffmpeg.get_ffmpeg_exe(), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise RuntimeError("ffmpeg: " + stderr.decode("utf-8", errors="replace")[-1800:])
    return stdout


async def _synthesize(script: str, path: Path, *, mock_tts: bool) -> list[dict]:
    if mock_tts:
        wav_path = path.with_suffix(".wav")
        with wave.open(str(wav_path), "wb") as output:
            output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            samples = array("h", (int(900 * math.sin(2 * math.pi * 220 * i / 24000)) for i in range(24 * 24000)))
            output.writeframes(samples.tobytes())
        await _ffmpeg("-y", "-i", str(wav_path), str(path))
        return []
    boundaries = []
    communicate = edge_tts.Communicate(script, voice=VOICE, rate="+10%", boundary="WordBoundary")
    with path.open("wb") as output:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                output.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                boundaries.append({"text": html.unescape(chunk["text"]),
                                   "start": chunk["offset"] / 10_000_000,
                                   "duration": chunk["duration"] / 10_000_000})
    if not path.exists() or path.stat().st_size == 0 or not boundaries:
        raise RuntimeError("TTS не вернул речь с временными метками; видео не создано")
    return boundaries


def build_drama_scenes(series, chapters, episode_num=None):
    scenes = build_episode_scenes(series, chapters, episode_num) if episode_num else build_scenes(series, chapters)
    # Use an actual free-chapter status/dialogue excerpt; do not invent book quotations.
    free = [c for c in chapters if c['chapter_num'] <= min(2,series['free_chapters_limit'])]
    text='\n\n'.join(c['content_text'] for c in free)
    box=re.search(r'┌[^\n]*Систем[^\n]*\n(.*?)└[^\n]*',text,re.I|re.S)
    bracket=re.search(r'\[[^\]]*(?:Систем|Статус|Уровень)[^\]]*\]',text,re.I)
    lines=[p.strip() for p in re.split(r'(?<=[.!?])\s+|\n+',text) if p.strip()]
    keys=('систем','разлом','врат','класс','навык','смерт','ошибк','ранг','таймер')
    candidates=[p for p in lines if len(p)<=130 and any(k in p.casefold() for k in keys)]
    status=(' '.join(box.group(1).replace('│','').split()) if box else bracket.group(0) if bracket else
            max(candidates,key=lambda p:sum(k in p.casefold() for k in keys)) if candidates else None)
    if status:
        excerpt = complete_excerpt(status,100).strip('«»[]')
        scenes[3 if episode_num else 2] = '«' + excerpt + '»'
    else:
        # Branded narrator interjection, explicitly outside the quoted source material.
        scenes.insert(2, '[NovelPulse: новый мир. Новые правила.]')
    return [*scenes, LOOP_BRIDGE]


async def generate_promo_video(series: dict, chapters: list[dict], bot_username: str,
                               output_path: str | Path, *, mock_tts: bool = False,
                               episode_num: int | None = None, test_duration: float | None = None) -> Path:
    """Viral Engine 2.0. No external media survives the temporary render workspace."""
    from app.voice_drama import segment_drama, synthesize_drama
    from app.video_motion import fetch_artwork, MotionRenderer, encode_motion, FPS
    from app.audio_sfx import mix_soundtrack
    if test_duration is not None and (not mock_tts or not .5 <= test_duration <= 30):
        raise ValueError('Short test duration requires explicit mock_tts')
    target=Path(output_path).resolve()
    target.parent.mkdir(parents=True,exist_ok=True)
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}',bot_username.lstrip('@')):
        raise ValueError('Некорректное имя Telegram-бота')
    scenes=build_drama_scenes(series,chapters,episode_num)
    segments=segment_drama(scenes)
    try:
        with tempfile.TemporaryDirectory(prefix='video-',dir=target.parent) as directory:
            work=Path(directory)
            speech,words,timings,original=await asyncio.wait_for(synthesize_drama(segments,work,_ffmpeg,
                mock_tts=mock_tts,mock_duration=test_duration or 24),timeout=240)
            duration=test_duration if test_duration else max(20.0,min(29.5,original))
            tempo=original/duration
            if not .5 <= tempo <= 2:
                raise ValueError('Озвучка выходит за пределы безопасного ускорения; сократите сценарий')
            for item in words+timings:
                item['start']/=tempo;item['end']/=tempo
            # Sample-accurate decoded PCM durations, not MP3 file sizes or text-length guesses.
            timings[-1]['end']=duration
            system_starts=[s['start'] for s in timings if s['voice']=='ru-RU-SvetlanaNeural']
            audio=await mix_soundtrack(speech,work,duration,system_starts,tempo,_ffmpeg)
            art,sources=await fetch_artwork(series,offline=mock_tts)
            renderer=MotionRenderer(art,words,timings,duration,series['title'],bot_username,episode_num,font=_font)
            rendered=work/'promo.mp4'
            # Shield the thread: cancellation waits until ffmpeg stops before wiping its workspace.
            task=asyncio.create_task(asyncio.to_thread(encode_motion,renderer,audio,rendered))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            if rendered.stat().st_size==0 or b'ftyp' not in rendered.read_bytes()[:32]:
                raise RuntimeError('Motion encoder did not create an MP4')
            await _ffmpeg('-v','error','-i',str(rendered),'-f','null','-')
            rendered.replace(target)
            metadata={'series_id':series['id'],'title':series['title'],'renderer_version':RENDERER_VERSION,
                'engine':'Viral Video Engine 2.0','mock_tts':mock_tts,'fps':FPS,'frames':math.ceil(duration*FPS),
                'width':720,'height':1280,'duration':duration,'original_audio_duration':original,'tempo':tempo,
                'voices':sorted(set(s['voice'] for s in timings)),'script':' '.join(scenes),'words':words,
                'hud':'live countdown + 0..99% sync + S-class threat badges','parallax_layers':3,
                'spoken_hook':scenes[0],'first_word_at_seconds':words[0]['start'],
                'speech_onset_seconds':timings[0].get('speech_onset_seconds',0)/tempo,
                'scenes':[{**s,'font_size':58,'stroke_width':4} for s in timings],'artwork':sources,
                'system_chimes':system_starts,'soundtrack':'procedural minor drone -18 dB + impact/HUD/ticks',
                'loop_bridge':LOOP_BRIDGE,'episode_num':episode_num,'episode_label':EPISODE_LABELS.get(episode_num,''),
                'search_prompt':f'Вбей в поиске Telegram: {bot_username.lstrip("@")}','bot_username':bot_username,
                'bytes':target.stat().st_size}
            target.with_suffix('.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


async def generate_episode_video(series,chapters,bot_username,episode_num,output_path,*,mock_tts=False,test_duration=None):
    return await generate_promo_video(series,chapters,bot_username,output_path,mock_tts=mock_tts,
                                      episode_num=episode_num,test_duration=test_duration)


async def _cli(args):
    from app.storage_cleanup import media_slot, DATA_DIR
    async with media_slot():
        target = Path(args.output).resolve()
        if any(p.resolve() != target for p in DATA_DIR.rglob('*.mp4')):
            raise ValueError('Уже есть активный MP4; сначала завершите отправку или очистите кэш')
        await _render_cli(args)


async def _render_cli(args):
    from aiogram import Bot
    from app.config import Settings
    from app.db import Database
    settings = Settings()
    db = Database(settings.db_path)
    await db.open()
    try:
        series = await db.get_series(args.series_id) if args.series_id else await db.latest_imported_series()
        if not series:
            raise ValueError("Нет импортированной новеллы для видео")
        chapters = [await db.get_chapter(c["id"]) for c in await db.list_chapters(series["id"])]
        username = args.bot_username
        if not username:
            async with Bot(settings.bot_token.get_secret_value()) as bot:
                username = (await bot.get_me()).username
        path = await generate_promo_video(series, chapters, username, args.output, mock_tts=args.mock_tts)
        print(json.dumps({"path": str(path), "bytes": path.stat().st_size, "title": series["title"]}, ensure_ascii=False))
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description="Вертикальный MP4 с русской нейронной озвучкой")
    parser.add_argument("--series-id", type=int)
    parser.add_argument("--bot-username", help="Если не указан, имя берётся через Telegram getMe")
    parser.add_argument("--output", default="data/promo_latest.mp4")
    parser.add_argument("--mock-tts", action="store_true", help="Только для изолированных тестов: тон вместо речи")
    args = parser.parse_args()
    try:
        asyncio.run(_cli(args))
    except Exception as error:
        parser.exit(1, f"Генерация не завершена: {type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()
