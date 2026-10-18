"""On-demand widescreen audiobook rendering. Never schedules or uploads a video."""
import asyncio
import os
import shutil
import subprocess
import tempfile
import html
import re
from pathlib import Path

LONGFORM_CTA_FIRST_LINE = '👇 КЛИКАБЕЛЬНАЯ ССЫЛКА НА ПРОДОЛЖЕНИЕ — В ПЕРВОМ КОММЕНТАРИИ ПОД ВИДЕО!'


def with_longform_cta(description):
    """Keep the comment CTA first and never duplicate it on retries."""
    body = description.strip()
    while body.startswith(LONGFORM_CTA_FIRST_LINE):
        body = body[len(LONGFORM_CTA_FIRST_LINE):].lstrip()
    return LONGFORM_CTA_FIRST_LINE + '\n\n' + body


def longform_card_cta(chapter_count=0):
    chapter_range = f'3–{chapter_count}' if chapter_count >= 3 else '3+'
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


def _card(series,chapter,path):
    from PIL import Image,ImageDraw,ImageFont
    candidates=('C:/Windows/Fonts/arialbd.ttf','C:/Windows/Fonts/segoeuib.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')
    def font(size):
        for candidate in candidates:
            try:return ImageFont.truetype(candidate,size)
            except OSError:pass
        return ImageFont.load_default(size=size)
    image=Image.new('RGB',(1280,720));draw=ImageDraw.Draw(image)
    for y in range(720):draw.line((0,y,1280,y),fill=(8+int(12*y/720),16+int(12*y/720),28+int(16*y/720)))
    for radius in (125,190,255):
        draw.ellipse((950-radius,300-radius,950+radius,300+radius),outline=(26,59,73),width=2)
    draw.rounded_rectangle((50,35,1230,685),radius=24,outline=(59,109,116),width=2)
    draw.text((80,65),'NOVELPULSE AUDIO · АУДИОКНИГА',font=font(24),fill='#b9f27c')
    def wrapped(text,size,width,max_lines):
        f=font(size);lines=[];line=''
        for word in text.split():
            value=(line+' '+word).strip()
            if draw.textlength(value,font=f)>width and line:lines.append(line);line=word
            else:line=value
        if line:lines.append(line)
        if len(lines)>max_lines:
            if size>20:return wrapped(text,size-2,width,max_lines)
            lines=lines[:max_lines];lines[-1]=lines[-1][:60]+'…'
        return f,lines
    f,lines=wrapped(str(series['title']),46,1100,3)
    for i,line in enumerate(lines):draw.text((80,135+i*58),line,font=f,fill='#f6d878',stroke_width=1,stroke_fill='black')
    draw.text((80,345),series.get('audio_label',f'ГЛАВА {chapter["chapter_num"]} / 2'),font=font(30),fill='#70dcff')
    f,lines=wrapped(str(chapter['title']),32,1100,2)
    for i,line in enumerate(lines):draw.text((80,400+i*43),line,font=f,fill='white')
    draw.rounded_rectangle((78,548,1202,562),radius=7,fill='#243c48')
    cta, detail = longform_card_cta(series.get('chapter_count',0))
    draw.text((80,590),cta,font=font(30),fill='#b9f27c')
    draw.text((80,636),detail,font=font(22),fill='#e3ece9')
    image.save(path,'PNG')


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
    script=audio_title(chapter)+'.\n\n'+chapter['content_text'].strip()
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


async def render_longform_audiobook_video(series,chapters_1_and_2,output_path,*,mock_tts=False):
    """Existing full two-chapter edition; no abridgement or schedule changes."""
    chapters=sorted(chapters_1_and_2,key=lambda c:c['chapter_num'])
    if len(chapters)!=2 or [c['chapter_num'] for c in chapters]!=[1,2]:
        raise ValueError('Longform requires exactly chapters 1 and 2')
    return await _render_audiobook_video(series,chapters,output_path,mock_tts=mock_tts)


def sentence_prefix(text,limit):
    """A contiguous opening ending at punctuation; never splice story scenes."""
    if len(text)<=limit:return text
    ends=[m.end() for m in re.finditer(r'[.!?…][»”\"\']*(?=\s|$)',text[:limit])]
    if not ends or ends[-1]<100:raise ValueError('No complete sentence within narration budget')
    return text[:ends[-1]].rstrip()


async def render_standard_audiobook_video(series,chapter,output_path,*,max_duration_seconds=870,mock_tts=False):
    """Real measured first-chapter pilot, comfortably below YouTube's 900s limit.

    Try the complete first chapter. If necessary reduce the contiguous excerpt at
    sentence boundaries and resynthesize at the same +8% rate; never accelerate or
    cut audio. Failed attempts remove all intermediate media automatically.
    """
    if not 30<=max_duration_seconds<=890:raise ValueError('Standard edition budget must be 30..890 seconds')
    if chapter['chapter_num']!=1:raise ValueError('Standard pilot requires chapter 1')
    original=chapter['content_text'].strip();selected=original
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
