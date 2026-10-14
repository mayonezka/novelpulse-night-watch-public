"""Bounded-memory 24 FPS artwork camera, particles, HUD and karaoke compositor."""
import asyncio
import bisect
import hashlib
import io
import math
import os
import random
import subprocess
from functools import lru_cache
from urllib.parse import quote
import httpx
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFilter, ImageOps

SIZE=(720,1280)
FPS=24
POWER=('РАНГ','НУЛЕВОЙ','СИСТЕМ','СМЕРТ','УБИТЬ','МОГИЛ','РЕЛЬСОТРОН')


def procedural_art(seed,scene=0):
    rng=random.Random(seed+scene)
    image=Image.new('RGB',SIZE)
    draw=ImageDraw.Draw(image)
    for y in range(1280):
        glow=math.exp(-((y-480)/330)**2)
        draw.line((0,y,720,y),fill=(int(5+17*glow),int(6+16*glow),int(19+36*glow)))
    haze=Image.new('RGBA',SIZE)
    glow=ImageDraw.Draw(haze)
    hue=(15,205,244,130) if scene%2==0 else (240,18,80,130)
    glow.ellipse((140,180,580,770),fill=hue)
    image=Image.alpha_composite(image.convert('RGBA'),haze.filter(ImageFilter.GaussianBlur(72)))
    draw=ImageDraw.Draw(image)
    for x in range(-30,760,46):
        top=rng.randint(430,800)
        draw.polygon([(x,1200),(x,top),(x+20,top-40),(x+38,top),(x+38,1200)],fill=(7,11,24,255))
        for y in range(top+30,1100,60):
            draw.line((x+7,y,x+11,y+18),fill=(*hue[:3],125),width=2)
    for radius in (200,220,243):
        draw.ellipse((360-radius,540-radius,360+radius,540+radius),outline=(*hue[:3],190),width=3)
    for a in range(0,360,20):
        angle=math.radians(a+scene*7)
        x,y=360+227*math.cos(angle),540+227*math.sin(angle)
        draw.line((x-8,y-10,x+6,y+8),fill=(250,197,74,255),width=3)
        draw.line((x+7,y-11,x-8,y+10),fill=(250,197,74,255),width=2)
    # Cloaked silhouette, layered shoulders, luminous blade and distant mountains.
    draw.polygon([(0,1030),(140,770),(260,990),(470,790),(720,1020),(720,1280),(0,1280)],fill=(7,10,24,255))
    draw.polygon([(266,1100),(303,694),(359,649),(412,696),(468,1130)],fill=(9,10,23,255),outline=(45,50,79,255))
    draw.ellipse((321,592,397,692),fill=(5,7,17,255),outline=(46,57,80,255),width=4)
    draw.line((344,642,373,640),fill=(*hue[:3],255),width=3)
    draw.line((442,745,504,1090),fill=(224,234,255,255),width=5)
    return image.convert('RGB')


async def fetch_artwork(series,*,offline=False):
    # External artwork requests contain fixed public prompts only. No catalogue text,
    # titles, descriptions, source URLs, reader IDs or catalogue-derived seeds leave here.
    seed=1600
    theme='mysterious rift world, dark anime lone adventurer, neon cyan magical class window'
    prompts=[f'{theme}, {view}, cinematic dark fantasy illustration, highly detailed, vertical composition, no text, no logo'
             for view in ('wide ruined city and portal','dramatic closeup hooded hero','huge glowing rune gate','looming shadow behind hero')]
    images=[]
    sources=[]
    async with httpx.AsyncClient(timeout=6,follow_redirects=True,headers={'User-Agent':'NovelPulse-Media/2.0'}) as client:
        for i,prompt in enumerate(prompts):
            art=None
            reason='explicit offline test'
            if not offline:
                try:
                    url='https://image.pollinations.ai/prompt/'+quote(prompt,safe='')
                    async with asyncio.timeout(6), client.stream('GET',url,params={'width':720,'height':1280,'nologo':'true','seed':seed+i}) as response:
                        response.raise_for_status()
                        data=bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data)>8_000_000:
                                raise ValueError('Image response exceeds 8 MB')
                    with Image.open(io.BytesIO(data)) as original:
                        if original.width*original.height>8_000_000:
                            raise ValueError('Image dimensions exceed limit')
                        art=ImageOps.fit(original.convert('RGB'),SIZE)
                    reason=None
                except Exception as error:
                    reason=type(error).__name__+(f' HTTP {error.response.status_code}' if isinstance(error,httpx.HTTPStatusError) else '')
            images.append(art if art is not None else await asyncio.to_thread(procedural_art,seed,i))
            sources.append({'provider':'pollinations' if art is not None else 'procedural','reason':reason})
    return images,sources


def caption_groups(words):
    groups=[]
    start=0
    while start<len(words):
        stop=start+1
        while stop<len(words) and words[stop]['segment']==words[start]['segment']:
            stop+=1
        run=words[start:stop]
        while run:
            n=4 if len(run)==4 else 2 if len(run)==2 else 3
            if len(run)==1 and groups and groups[-1][-1]['segment']==run[0]['segment']:
                groups[-1].append(run.pop())
            else:
                groups.append(run[:n]);run=run[n:]
        start=stop
    # A one-word dialogue interjection shares a card with adjacent timed words.
    for i in range(len(groups)-1,-1,-1):
        if len(groups[i])!=1 or len(groups)==1:
            continue
        if i+1<len(groups):
            if len(groups[i+1])<4:
                groups[i+1]=groups[i]+groups[i+1];groups.pop(i)
            else:
                groups[i].append(groups[i+1].pop(0))
        elif len(groups[i-1])<4:
            groups[i-1]+=groups.pop(i)
        else:
            groups[i].insert(0,groups[i-1].pop())
    return groups


class MotionRenderer:
    def __init__(self,art,words,segments,duration,title,username,episode,*,font):
        self.art=art;self.words=words;self.segments=segments;self.duration=duration
        self.groups=caption_groups(words)
        self.starts=[g[0]['start'] for g in self.groups]
        self.font=font
        self.title=title
        self.rng=random.Random(16)
        self.particles=[(self.rng.random()*720,self.rng.random()*1280,self.rng.uniform(0,6.28),self.rng.randint(1,3)) for _ in range(34)]
        self.chrome=Image.new('RGBA',SIZE)
        d=ImageDraw.Draw(self.chrome)
        d.rounded_rectangle((30,44,690,148),30,fill=(150,12,45,245),outline=(255,215,0,255),width=4)
        d.text((58,74),f'ЛИТРПГ · ЧАСТЬ {episode or 1}',font=font(39,True),fill='white',stroke_width=2,stroke_fill='black')
        from app.video_gen import _wrap,short_visual_hook
        for i,line in enumerate(_wrap(d,short_visual_hook(title),font(34,True),620)):
            d.text((42,175+i*44),line,font=font(34,True),fill='#FFD700',stroke_width=3,stroke_fill='black')
        d.rounded_rectangle((26,1000,694,1218),28,fill=(255,216,40,255),outline='white',width=4)
        d.ellipse((52,1034,84,1066),outline=(10,15,26),width=5)
        d.line((78,1062,94,1077),fill=(10,15,26),width=5)
        d.text((112,1030),'Вбей в поиске Telegram:',font=font(32,True),fill=(10,15,26))
        name=username.lstrip('@')
        f=font(53,True)
        d.text(((720-d.textlength(name,font=f))/2,1080),name,font=f,fill=(124,8,32))
        d.text((49,1161),'Первая платная глава — 1',font=font(30,True),fill=(10,15,26))
        d.polygon([(650+(17 if i%2==0 else 7)*math.cos(-math.pi/2+i*math.pi/5),
                    1181+(17 if i%2==0 else 7)*math.sin(-math.pi/2+i*math.pi/5))
                   for i in range(10)],fill=(124,8,32))

    def background(self,t):
        portion=self.duration/len(self.art)
        index=min(len(self.art)-1,int(t/portion))
        local=(t-index*portion)/portion
        zoom=1+.18*local
        width,height=720/zoom,1280/zoom
        x=(720-width)*(.5+.27*math.sin(local*math.pi))
        y=(1280-height)*(.45+.24*math.cos(local*math.pi))
        impacts=[0]+[s['start'] for s in self.segments if s['system']]
        shake=max((math.exp(-18*(t-a)) if 0<=t-a<.3 else 0 for a in impacts),default=0)
        dx=7*shake*math.sin(t*155);dy=5*shake*math.cos(t*129)
        box=(x+dx,y+dy,x+width+dx,y+height+dy)
        image=self.art[index].transform(SIZE,Image.Transform.EXTENT,box,Image.Resampling.BICUBIC)
        # Crossfade artwork boundaries and blend back to frame zero in the last 0.3s.
        if local>.9 and index+1<len(self.art):
            image=Image.blend(image,self.art[index+1],(local-.9)/.1)
        if t>self.duration-.3:
            image=Image.blend(image,self.art[0],min(1,(t-self.duration+.3)/.3))
        return image.convert('RGBA')

    def frame(self,t):
        image=self.background(t)
        layer=Image.new('RGBA',SIZE)
        d=ImageDraw.Draw(layer)
        for x,y,phase,radius in self.particles:
            x=(x+17*math.sin(2*math.pi*t/self.duration+phase))%720
            y=(y-1280*t/self.duration)%1280
            d.ellipse((x-radius*3,y-radius*3,x+radius*3,y+radius*3),fill=(40,180,255,24))
            d.ellipse((x-radius,y-radius,x+radius,y+radius),fill=(255,188,72,220))
        segment=next((s for s in self.segments if s['start']<=t<s['end']),None)
        if segment and segment['system']:
            pulse=int(170+70*math.sin(9*t)**2)
            d.rounded_rectangle((33,330,687,467),18,fill=(3,16,26,218),outline=(30,230,255,pulse),width=4)
            d.text((54,357),'[ СИСТЕМА: ОБНАРУЖЕН СБОЙ ]',font=self.font(27,True),fill='#FF3355',stroke_width=2,stroke_fill='black')
            line=415+int(30*math.sin(t*5))
            d.line((47,line,672,line),fill=(20,240,255,180),width=2)
        index=max(0,min(len(self.groups)-1,bisect.bisect_right(self.starts,t)-1))
        group=self.groups[index]
        active=next((w for w in group if w['start']<=t<w['end']),None)
        # Two lines at most, 2–4 words total. Active glyphs pop without shifting neighbours.
        rows=[];row=[];width=0
        for word in group:
            measured=self.font(66,True).getlength(word['text'])+22
            if row and width+measured>630:
                rows.append(row);row=[];width=0
            row.append(word);width+=measured
        if row: rows.append(row)
        y=650-len(rows)*43
        for row in rows:
            sizes=[]
            for word in row:
                scale=1.0
                if word is active:
                    p=(t-word['start'])/max(.001,word['end']-word['start'])
                    scale=1+.12*math.sin(math.pi*max(0,min(1,p)))
                sizes.append(round(58*scale))
            widths=[self.font(size,True).getlength(w['text']) for w,size in zip(row,sizes)]
            x=(720-sum(widths)-18*(len(row)-1))/2
            for word,size,width in zip(row,sizes,widths):
                power=any(word['text'].upper().strip('«»[]!.,?:—').startswith(p) for p in POWER)
                color=('#FF3355' if power else '#FFD700') if word is active else '#FFFFFF'
                f=self.font(size,True)
                d.text((x+4,y+5),word['text'],font=f,fill='black',stroke_width=5,stroke_fill='black')
                d.text((x,y),word['text'],font=f,fill=color,stroke_width=4,stroke_fill='black')
                x+=width+18
            y+=86
        image=Image.alpha_composite(image,layer)
        image=Image.alpha_composite(image,self.chrome)
        return image.convert('RGB')


def encode_motion(renderer,audio,target,*,timeout=240):
    """Only the MP4 is written: raw RGB frames go straight through ffmpeg stdin."""
    import time
    import tempfile
    args=[imageio_ffmpeg.get_ffmpeg_exe(),'-y','-v','error','-f','rawvideo','-pix_fmt','rgb24',
        '-s','720x1280','-r',str(FPS),'-i','pipe:0','-i',str(audio),'-map','0:v:0','-map','1:a:0',
        '-c:v','libx264','-preset','veryfast','-crf','23','-threads','2','-pix_fmt','yuv420p',
        '-c:a','aac','-b:a','144k','-movflags','+faststart','-t',f'{renderer.duration:.8f}','-shortest',str(target)]
    with tempfile.TemporaryFile() as errors:
        process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=errors,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        started=time.monotonic()
        try:
            for frame in range(math.ceil(renderer.duration*FPS)):
                if time.monotonic()-started>timeout:
                    raise TimeoutError('24 FPS render exceeded time budget')
                process.stdin.write(renderer.frame(frame/FPS).tobytes())
            process.stdin.close()
            code=process.wait(timeout=30)
            if code:
                errors.seek(0)
                raise RuntimeError('Motion ffmpeg: '+errors.read().decode(errors='replace')[-1500:])
        except BaseException:
            process.kill();process.wait()
            raise
