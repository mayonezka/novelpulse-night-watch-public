"""On-demand widescreen audiobook rendering. Never schedules or uploads a video."""
import asyncio
import os
import shutil
import subprocess
import tempfile
import html
import re
from pathlib import Path

LONGFORM_SPOKEN_CTA = 'Продолжение книги и следующие аудиоглавы уже доступны бесплатно. Нажмите на синюю ссылку в первом комментарии под этим видео!'
LONGFORM_OUTRO_SECONDS = 7.0

LONGFORM_CTA_FIRST_LINE = '👇 КЛИКАБЕЛЬНАЯ ССЫЛКА НА ПРОДОЛЖЕНИЕ — В ПЕРВОМ КОММЕНТАРИИ ПОД ВИДЕО!'


def with_longform_cta(description):
    """Keep the comment CTA first and never duplicate it on retries."""
    body = description.strip()
    while body.startswith(LONGFORM_CTA_FIRST_LINE):
        body = body[len(LONGFORM_CTA_FIRST_LINE):].lstrip()
    return LONGFORM_CTA_FIRST_LINE + '\n\n' + body


def longform_card_cta(chapter_count=0, first_unread=3):
    chapter_range = f'{first_unread}–{chapter_count}' if chapter_count >= first_unread else f'{first_unread}+'
    return ('ПРОДОЛЖЕНИЕ — В ПЕРВОМ КОММЕНТАРИИ ↓',
            f'Кликабельная ссылка на главы {chapter_range} · Поиск в Telegram: NovelPulseBot')


def narration_chunks(text):
    """Stay below Edge's 4096-byte implicit split without losing any text."""
    from app.text import split_text
    pending=split_text(text,1000);chunks=[]
    while pending:
        piece=pending.pop(0)
        if len(html.escape(piece).encode('utf-8'))>3000:
            pending[:0]=split_text(piece,max(2,len(piece)//2))
        else:chunks.append(piece)
    return chunks


async def _run(*args,timeout=180,cwd=None):
    import imageio_ffmpeg
    process=await asyncio.create_subprocess_exec(imageio_ffmpeg.get_ffmpeg_exe(),*map(str,args),
        cwd=cwd,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
        **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
    try:
        stdout,stderr=await asyncio.wait_for(process.communicate(),timeout)
    except BaseException:
        if process.returncode is None:process.kill();await process.wait()
        raise
    if process.returncode:raise RuntimeError('Longform FFmpeg: '+stderr.decode('utf-8',errors='replace')[-1400:])
    return stdout


LONGFORM_THUMBNAIL_MODE = 'in_frame_only'


def in_frame_longform_metadata(metadata):
    """Drop legacy external-image jobs before they reach the Studio uploader."""
    external = {'thumbnail_path', 'thumbnail_file', 'thumbnail_url', 'custom_thumbnail',
                'custom_thumbnail_path', 'upload_thumbnail'}
    return {**{key:value for key,value in metadata.items() if key not in external},
            'thumbnail_mode':LONGFORM_THUMBNAIL_MODE}


def longform_hook(series):
    """Short, fixed hooks remain readable in any auto-selected chapter/outro frame."""
    return {
        1: 'РАНГ 0: ОШИБКА СИСТЕМЫ',
        3: 'САД ДАЁТ БЕСКОНЕЧНЫЕ НАГРАДЫ',
        5: 'РЕЛЬСОТРОН В МИРЕ МАГИИ',
    }.get(series.get('id'), 'НОВЫЙ МИР НОВАЯ СИЛА')


def _card_font(size):
    from PIL import ImageFont
    for candidate in ('C:/Windows/Fonts/arialbd.ttf', 'C:/Windows/Fonts/segoeuib.ttf',
                      '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _card_lines(draw, text, size, width, max_lines, *, minimum=20):
    """Account for ink/stroke bounds, including long Cyrillic words."""
    for current in range(size, minimum-1, -2):
        font = _card_font(current)
        lines, line = [], ''
        for word in str(text).split():
            candidate = (line + ' ' + word).strip()
            if draw.textlength(candidate, font=font) > width and line:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        if len(lines) <= max_lines and all(draw.textlength(value, font=font) <= width for value in lines):
            return font, lines
    raise ValueError('Text cannot fit the audiobook frame')


def _card(series, chapter, path):
    from PIL import ImageDraw
    from app.longform_art import render_longform_art
    image = render_longform_art(series.get('id', 0))
    draw = ImageDraw.Draw(image)
    # A vector headphone icon avoids missing emoji glyphs on Windows/Linux fonts.
    draw.rounded_rectangle((48, 34, 445, 103), radius=18, fill='#da163e', outline='#ffdf59', width=3)
    draw.arc((70, 48, 112, 90), 180, 360, fill='white', width=5)
    for x in (68, 104):
        draw.rounded_rectangle((x, 68, x+10, 88), radius=4, fill='white')
    draw.text((132, 49), 'АУДИОКНИГА', font=_card_font(36), fill='white',
              stroke_width=2, stroke_fill='#671127')
    font, lines = _card_lines(draw, str(series['title']), 26, 1130, 2)
    for index, line in enumerate(lines):
        draw.text((56, 121+index*32), line, font=font, fill='#f1f5ff', stroke_width=2, stroke_fill='black')
    font, lines = _card_lines(draw, longform_hook(series), 112, 790, 3, minimum=82)
    # The fixed title region stays intact in ALL chapter cards and the outro card.
    line_height = 116
    top = 204 + (3-len(lines))*36
    for index, line in enumerate(lines):
        draw.text((58, top+index*line_height), line, font=font,
                  fill='#ffdc32' if index < len(lines)-1 else '#ffffff',
                  stroke_width=7, stroke_fill='#030305')
    draw.rounded_rectangle((78, 548, 1202, 562), radius=7, fill='#243c48')
    label = ('ПРОДОЛЖЕНИЕ ИСТОРИИ' if chapter['chapter_num'] == 0 else
             series.get('audio_label', f'ГЛАВА {chapter["chapter_num"]} · {chapter["title"]}'))
    font, lines = _card_lines(draw, label, 26, 1120, 1)
    draw.text((80, 570), lines[0], font=font, fill='#c9e9ff', stroke_width=2, stroke_fill='black')
    cta, detail = longform_card_cta(series.get('chapter_count', 0),series.get('first_unread',3))
    draw.rounded_rectangle((48, 610, 1232, 703), radius=18, fill='#0a101e', outline='#ffd447', width=2)
    draw.text((72, 622), cta, font=_card_font(28), fill='#ffdf59', stroke_width=1, stroke_fill='black')
    draw.text((72, 665), detail, font=_card_font(21), fill='white')
    image.save(path, 'PNG')


async def _speech(chapter,path,*,mock_tts=False):
    if mock_tts:
        import math,wave
        from array import array
        wav=path.with_suffix('.wav')
        with wave.open(str(wav),'wb') as stream:
            stream.setparams((1,2,24000,0,'NONE','not compressed'))
            stream.writeframes(array('h',(int(500*math.sin(2*math.pi*220*i/24000)) for i in range(24000))).tobytes())
        await _run('-y','-i',wav,path)
        return
    import edge_tts
    from app.audiobook import audio_title
    script = chapter['content_text'].strip() if chapter.get('narration_only') else audio_title(chapter)+'.\n\n'+chapter['content_text'].strip()
    chunks=narration_chunks(script)
    if len(chunks)>1:
        # Each bounded request has its own retry and file; a failed later chunk
        # cannot silently turn a partial chapter into a successful audiobook.
        with tempfile.TemporaryDirectory(prefix='speech-',dir=path.parent) as folder:
            work=Path(folder);semaphore=asyncio.Semaphore(2)
            async def piece(number,text):
                output=work/f'part_{number}.mp3'
                async with semaphore:
                    for attempt in range(4):
                        try:
                            kwargs={'voice':'ru-RU-DmitryNeural','rate':'+8%'}
                            if attempt%2:kwargs['boundary']='SentenceBoundary'
                            async with asyncio.timeout(180):
                                await edge_tts.Communicate(text,**kwargs).save(str(output))
                            if not output.exists() or output.stat().st_size<100:
                                raise RuntimeError('Empty neural narration chunk')
                            return output
                        except (edge_tts.exceptions.NoAudioReceived,TimeoutError,RuntimeError):
                            output.unlink(missing_ok=True)
                            if attempt==3:raise
                            await asyncio.sleep(.8*(attempt+1))
            tasks=[asyncio.create_task(piece(i,text)) for i,text in enumerate(chunks)]
            try:await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                raise
            (work/'parts.txt').write_text(''.join(f"file 'part_{i}.mp3'\n" for i in range(len(chunks))),encoding='ascii')
            await _run('-y','-f','concat','-safe','1','-i','parts.txt','-c','copy',path,cwd=work)
        return
    async with asyncio.timeout(900):
        try:await edge_tts.Communicate(script,voice='ru-RU-DmitryNeural',rate='+8%').save(str(path))
        except edge_tts.exceptions.NoAudioReceived:
            await edge_tts.Communicate(script,voice='ru-RU-DmitryNeural',rate='+8%',
                boundary='SentenceBoundary').save(str(path))


class DurationLimitExceeded(ValueError):
    def __init__(self,seconds):
        self.seconds=seconds
        super().__init__(f'Audiobook exceeds duration budget: {seconds:.3f}s')


async def measure_full_chapter_narration(chapters):
    """Measure real +8% speech without cutting text, rendering video or retaining audio."""
    import hashlib
    from mutagen.mp3 import MP3
    ordered=sorted(chapters,key=lambda c:c['chapter_num'])
    if [c['chapter_num'] for c in ordered]!=[1,2]:
        raise ValueError('Full pilot requires complete chapters 1 and 2')
    rows=[]
    with tempfile.TemporaryDirectory(prefix='novelpulse-longform-measure-') as folder:
        for chapter in ordered:
            if not chapter.get('content_text','').strip():
                raise ValueError('Empty chapter narration')
            path=Path(folder)/f'chapter_{chapter["chapter_num"]}.mp3'
            await _speech(chapter,path)
            seconds=MP3(path).info.length
            if seconds<=.25 or seconds>7200:
                raise ValueError('Invalid measured narration duration')
            rows.append({'chapter_id':chapter['id'],'chapter_num':chapter['chapter_num'],
                'chars':len(chapter['content_text']),'duration_seconds':seconds,
                'source_digest':hashlib.sha256(chapter['content_text'].encode()).hexdigest()})
    return {'chapters':rows,'speech_rate':'+8%','full_text':True,
            'outro_seconds':LONGFORM_OUTRO_SECONDS,
            'duration_seconds':sum(c['duration_seconds'] for c in rows)+LONGFORM_OUTRO_SECONDS,
            'temporary_audio_deleted':not Path(folder).exists()}


async def _render_audiobook_video(series,chapters,output_path,*,mock_tts=False,max_duration_seconds=None):
    """Render validated chapters at 1280x720/12fps, H.264/AAC; caller owns final MP4.

    All narration and frame intermediates live in a disposable system temp directory.
    mock_tts is test-only and cannot write to data/ or docs/.
    """
    if any(c.get('series_id',series['id'])!=series['id'] or not c.get('is_published',True)
        or not c.get('content_text','').strip() or len(c['content_text'])>500_000 for c in chapters):
        raise ValueError('Invalid audiobook chapter')
    target=Path(output_path).resolve()
    if target.suffix.lower()!='.mp4':raise ValueError('Longform output must be MP4')
    project=Path(__file__).resolve().parents[1]
    if mock_tts and any(target.is_relative_to(project/name) for name in ('data','docs')):
        raise ValueError('Test narration cannot enter production media')
    target.parent.mkdir(parents=True,exist_ok=True)
    from mutagen.mp3 import MP3
    with tempfile.TemporaryDirectory(prefix='novelpulse-longform-') as folder:
        work=Path(folder);durations=[]
        for chapter in chapters:
            n=chapter['chapter_num'];audio=work/f'audio_{n}.mp3'
            await _speech(chapter,audio,mock_tts=mock_tts)
            duration=MP3(audio).info.length
            if duration<=.25 or duration>7200:raise ValueError('Invalid audiobook duration')
            durations.append(duration)
            rendering=asyncio.create_task(asyncio.to_thread(_card,series,chapter,work/f'card_{n}.png'))
            try:await asyncio.shield(rendering)
            except asyncio.CancelledError:await rendering;raise
        # Reserve seven seconds before enforcing the standard channel's 15-minute cap.
        outro = {'chapter_num': 0, 'title': 'Продолжение — в первом комментарии',
                 'content_text': LONGFORM_SPOKEN_CTA, 'narration_only': True}
        await _speech(outro, work/'outro_raw.mp3', mock_tts=mock_tts)
        spoken_seconds = MP3(work/'outro_raw.mp3').info.length
        speed = spoken_seconds / LONGFORM_OUTRO_SECONDS
        # atempo accepts 0.5..2 per stage; chain bounded stages for any duration.
        factors = []
        while speed > 2: factors.append(2.0); speed /= 2
        while speed < .5: factors.append(.5); speed /= .5
        factors.append(speed)
        filters = ','.join(f'atempo={factor:.8f}' for factor in factors)
        await _run('-y', '-i', work/'outro_raw.mp3', '-af',
                   filters + ',apad', '-t', str(LONGFORM_OUTRO_SECONDS), work/'audio_0.mp3')
        _card(series, outro, work/'card_0.png')
        chapters = [*chapters, outro]
        durations.append(LONGFORM_OUTRO_SECONDS)
        total=sum(durations);offset=0
        if max_duration_seconds is not None and total>=max_duration_seconds:
            raise DurationLimitExceeded(total)
        for chapter,duration in zip(chapters,durations):
            n=chapter['chapter_num']
            visual=(f'[0:v]fps=12,format=yuv420p[base];color=c=0xb9f27c:s=1120x10:r=12[fill];'
                f'[base][fill]overlay=x=\'80-1120+1120*min((t+{offset:.6f})/{total:.6f},1)\':y=550:eval=frame:shortest=1,'
                'drawbox=x=0:y=550:w=80:h=10:color=0x0a1513:t=fill[v]')
            await _run('-y','-loop','1','-framerate','12','-i',work/f'card_{n}.png','-i',work/f'audio_{n}.mp3',
                '-filter_complex',visual,'-map','[v]','-map','1:a','-t',f'{duration:.6f}',
                '-c:v','libx264','-preset','veryfast','-crf','24','-pix_fmt','yuv420p','-c:a','aac',
                '-ar','48000','-ac','1','-shortest',work/f'clip_{n}.mp4',timeout=max(120,min(7200,duration*3)))
            offset+=duration
        (work/'concat.txt').write_text(''.join(f"file 'clip_{c['chapter_num']}.mp4'\n" for c in chapters),encoding='ascii')
        final=work/'audiobook.mp4'
        await _run('-y','-f','concat','-safe','1','-i','concat.txt','-c','copy','-movflags','+faststart',
            '-metadata','title='+str(series['title']),'-metadata','artist=NovelPulse Audio',final,cwd=work)
        with final.open('rb') as stream:header=stream.read(8)
        if final.stat().st_size<1000 or header[4:8]!=b'ftyp':raise RuntimeError('Invalid MP4 container')
        if max_duration_seconds is not None:
            from mutagen.mp4 import MP4
            actual=MP4(final).info.length
            if actual>=max_duration_seconds:raise DurationLimitExceeded(actual)
        partial=None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent,suffix='.partial',delete=False) as stream:partial=Path(stream.name)
            copying=asyncio.create_task(asyncio.to_thread(shutil.copyfile,final,partial))
            try:await asyncio.shield(copying)
            except asyncio.CancelledError:await copying;raise
            os.replace(partial,target)
        finally:
            if partial:partial.unlink(missing_ok=True)
    return target


async def render_longform_audiobook_video(series,chapters_1_and_2,output_path,*,mock_tts=False,chapter_numbers=(1,2)):
    """Existing full two-chapter edition; no abridgement or schedule changes."""
    chapters=sorted(chapters_1_and_2,key=lambda c:c['chapter_num'])
    expected=list(chapter_numbers)
    if expected!=[1,2] and not (series.get('id')==1 and expected==[3,4]):
        raise ValueError('Only original flagship may render Part 2 (chapters 3 and 4)')
    if len(chapters)!=2 or [c['chapter_num'] for c in chapters]!=expected:
        raise ValueError('Longform requires exactly the planned consecutive chapters')
    card={**series,'first_unread':expected[-1]+1}
    return await _render_audiobook_video(card,chapters,output_path,mock_tts=mock_tts)


def sentence_prefix(text,limit):
    """A contiguous opening ending at punctuation; never splice story scenes."""
    if len(text)<=limit:return text
    ends=[m.end() for m in re.finditer(r'[.!?…][»”\"\']*(?=\s|$)',text[:limit])]
    if not ends or ends[-1]<100:raise ValueError('No complete sentence within narration budget')
    return text[:ends[-1]].rstrip()


async def render_standard_audiobook_video(series,chapter,output_path,*,max_duration_seconds=870,mock_tts=False,
                                        excerpt_chars=None):
    """Real measured first-chapter pilot, comfortably below YouTube's 900s limit.

    Try the complete first chapter. If necessary reduce the contiguous excerpt at
    sentence boundaries and resynthesize at the same +8% rate; never accelerate or
    cut audio. Failed attempts remove all intermediate media automatically.
    """
    if not 30<=max_duration_seconds<=890:raise ValueError('Standard edition budget must be 30..890 seconds')
    if chapter['chapter_num']!=1:raise ValueError('Standard pilot requires chapter 1')
    original=chapter['content_text'].strip()
    if excerpt_chars is not None and (isinstance(excerpt_chars,bool) or not isinstance(excerpt_chars,int) or excerpt_chars<100):
        raise ValueError('Excerpt budget must be at least 100 characters')
    selected=sentence_prefix(original,excerpt_chars) if excerpt_chars is not None else original
    for attempt in range(4):
        full=selected==original
        current={**chapter,'content_text':selected}
        card={**series,'audio_label':'ГЛАВА 1 · '+('ПОЛНЫЙ ТЕКСТ' if full else 'ОЗНАКОМИТЕЛЬНЫЙ ФРАГМЕНТ')}
        try:
            path=await _render_audiobook_video(card,[current],output_path,mock_tts=mock_tts,
                max_duration_seconds=max_duration_seconds)
            from mutagen.mp4 import MP4
            return {'path':str(path),'duration_seconds':MP4(path).info.length,
                'full_chapter':full,'selected_chars':len(selected),'source_chars':len(original),
                'narration_text':selected,'speech_rate':'+8%','max_duration_seconds':max_duration_seconds}
        except DurationLimitExceeded as error:
            if attempt==3:raise
            selected=sentence_prefix(selected,int(len(selected)*min(.95,max_duration_seconds/error.seconds*.94)))
