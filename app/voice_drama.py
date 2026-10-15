"""Edge neural dialogue with per-word metadata and an explicit sentence fallback."""
import html
import re
import wave
from array import array
from pathlib import Path
import math
import edge_tts

NARRATOR='ru-RU-DmitryNeural'
SYSTEM='ru-RU-SvetlanaNeural'


def trim_initial_silence(clip,events):
    """24kHz mono s16le: remove codec/TTS pre-roll, keeping a 2ms consonant lead-in."""
    samples=array('h');samples.frombytes(clip)
    if samples.itemsize!=2:raise RuntimeError('Expected 16-bit PCM')
    first=next((i for i,value in enumerate(samples) if abs(value)>=120),None)
    if first is None:raise RuntimeError('Neural hook contains no audible speech')
    cut=max(0,first-48)
    shifted=[{**e,'start':max(0,e['start']-cut/24000),'end':max(.001,e['end']-cut/24000)} for e in events]
    # First word is visible in the first frame, with the opening impact, not after a title card.
    shifted[0]['start']=0.0
    return samples[cut:].tobytes(),shifted,(first-cut)/24000,cut/24000


def segment_drama(scenes):
    segments=[]
    for scene in scenes:
        for part in re.split(r'(«[^»]+»|\[[^\]]+\])',scene):
            text=part.strip()
            if not text:
                continue
            if not re.search(r'\w',text):
                if segments: segments[-1]['text']+=text
                continue
            system=bool(re.match(r'^[«\[]|^(?:Система[:.!]|Статус:|Уровень:|Ранг:)',text,re.I))
            female=bool(re.search(r'—.*(?:сказала|прошептала|спросила|ответила|крикнула)',text,re.I))
            segments.append({'text':text,'voice':SYSTEM if system or female else NARRATOR,
                'rate':'+8%' if system or female else '+12%','system':system,'scene':len(segments)})
    return segments


def sentence_words(text,start,duration):
    words=text.split()
    total=sum(max(1,len(w)) for w in words)
    result=[]
    cursor=start
    for word in words:
        length=duration*max(1,len(word))/max(1,total)
        result.append({'text':word,'start':cursor,'end':cursor+length,'source':'SentenceBoundary'})
        cursor+=length
    return result


async def neural_clip(segment,path):
    last_error=None
    for boundary in ('WordBoundary','SentenceBoundary'):
        try:
            events=[]
            communicate=edge_tts.Communicate(segment['text'],voice=segment['voice'],rate=segment['rate'],boundary=boundary)
            with Path(path).open('wb') as output:
                async for event in communicate.stream():
                    if event['type']=='audio':
                        output.write(event['data'])
                    elif event['type'] in ('WordBoundary','SentenceBoundary'):
                        start=event['offset']/10_000_000
                        duration=event['duration']/10_000_000
                        text=html.unescape(event['text'])
                        if event['type']=='WordBoundary':
                            events.append({'text':text,'start':start,'end':start+duration,'source':'WordBoundary'})
                        else:
                            events.extend(sentence_words(text,start,duration))
            if not Path(path).stat().st_size or not events:
                raise RuntimeError('Edge TTS returned no audio/timing metadata')
            return events
        except Exception as error:
            last_error=error
    raise RuntimeError('Neural TTS failed: '+type(last_error).__name__) from None


async def synthesize_drama(segments,directory,ffmpeg,*,mock_tts=False,mock_duration=24):
    root=Path(directory)
    pcm=bytearray()
    words=[]
    timings=[]
    total_words=sum(len(s['text'].split()) for s in segments)
    for index,segment in enumerate(segments):
        onset,trimmed=0.0,0.0
        start=len(pcm)/48000
        if mock_tts:
            duration=mock_duration*len(segment['text'].split())/max(1,total_words)
            samples=array('h',(int(1400*math.sin(2*math.pi*(220 if segment['voice']==NARRATOR else 330)*i/24000))
                               for i in range(round(duration*24000))))
            clip=samples.tobytes()
            events=sentence_words(segment['text'],0,duration)
            for e in events: e['source']='mock'
        else:
            path=root/f'voice{index}.mp3'
            events=await neural_clip(segment,path)
            clip=await ffmpeg('-v','error','-i',str(path),'-f','s16le','-ac','1','-ar','24000','pipe:1')
            if index==0:clip,events,onset,trimmed=trim_initial_silence(clip,events)
        pcm.extend(clip)
        end=len(pcm)/48000
        if end<=start:
            raise RuntimeError('Empty neural speech segment')
        timings.append({**segment,'start':start,'end':end,'speech_onset_seconds':onset,
                        'leading_trim_seconds':trimmed})
        for e in events:
            a=max(start,min(end-.001,start+e['start']))
            b=max(a+.001,min(end,start+e['end']))
            words.append({**e,'start':a,'end':b,'voice':segment['voice'],'system':segment['system'],'segment':index})
    speech=root/'speech.wav'
    with wave.open(str(speech),'wb') as stream:
        stream.setparams((1,2,24000,0,'NONE','not compressed'))
        stream.writeframes(pcm)
    return speech,words,timings,len(pcm)/48000
