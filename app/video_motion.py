"""Bounded-memory 24 FPS artwork camera, particles, HUD and karaoke compositor."""
import asyncio
import bisect
import math
import os
import random
import subprocess
from functools import lru_cache
import imageio_ffmpeg
from PIL import Image, ImageChops, ImageDraw, ImageFilter

SIZE=(720,1280)
FPS=24
POWER=('РАНГ','НУЛЕВОЙ','СИСТЕМ','СМЕРТ','УБИТЬ','МОГИЛ','РЕЛЬСОТРОН')


def hud_state(t,duration):
    # Include a true 0.0s frame in the encoded video (last timestamp is duration-1/FPS).
    end=max(1/FPS,duration-1/FPS)
    progress=min(1,max(0,t/end))
    remaining=0.0 if t>=end else max(0,duration-t)
    return {'remaining':remaining,'progress':progress,'percent':min(99,int(progress*99)),
            'crimson':remaining<=5,'flash':int(t*8)%2==0}


@lru_cache(maxsize=1)
def shader_layers():
    """Memory-only rune/lightning middle plane, defocused near plane and light shafts."""
    mid=Image.new('RGBA',SIZE)
    d=ImageDraw.Draw(mid)
    rng=random.Random(1800)
    for radius in (205,222,244):
        d.ellipse((360-radius,580-radius,360+radius,580+radius),outline=(34,210,255,65),width=3)
    for i in range(18):
        angle=i*math.tau/18
        x,y=360+232*math.cos(angle),580+232*math.sin(angle)
        d.line((x-6,y-11,x+7,y+9),fill=(255,205,81,100),width=2)
    for origin in (70,650):
        points=[(origin,190)]
        for y in range(220,980,43):points.append((origin+rng.randint(-50,50),y))
        d.line(points,fill=(65,174,255,130),width=4)
        d.line(points,fill=(214,243,255,185),width=1)
    mid=Image.alpha_composite(mid.filter(ImageFilter.GaussianBlur(7)),mid)
    near=Image.new('RGBA',SIZE)
    d=ImageDraw.Draw(near)
    for _ in range(22):
        x,y=rng.randrange(720),rng.randrange(1280);r=rng.randrange(8,28)
        d.ellipse((x-r,y-r,x+r,y+r),fill=(45,170,255,30),outline=(250,205,85,40),width=2)
    near=near.filter(ImageFilter.GaussianBlur(5))
    volume=Image.new('RGBA',SIZE)
    d=ImageDraw.Draw(volume)
    for x in (55,590):d.polygon([(x,0),(x+45,0),(x+240,1280),(x+90,1280)],fill=(55,110,180,18))
    volume=volume.filter(ImageFilter.GaussianBlur(36))
    vignette=Image.new('RGBA',SIZE,(1,3,12,0))
    vignette.putalpha(Image.radial_gradient('L').resize(SIZE).point(lambda a:int(a*.5)))
    volume=Image.alpha_composite(volume,vignette)
    return mid,near,volume


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


async def fetch_artwork(series=None,*,offline=False):
    # Compatibility argument is deliberately ignored; the provider accepts no book input.
    from app.art_provider import fetch_artwork as fixed_artwork
    return await fixed_artwork(offline=offline)


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
        self.episode=episode or 1
        self.magic,self.bokeh,self.vignette=shader_layers()
        from app.video_gen import _wrap,short_visual_hook
        for i,line in enumerate(_wrap(d,short_visual_hook(title),font(32,True),620)):
            d.text((42,270+i*43),line,font=font(32,True),fill='#FFD700',stroke_width=3,stroke_fill='black')
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
        image=image.convert('RGBA')
        # Three independently moving depth planes: art camera, runes/lightning, near bokeh.
        image=Image.alpha_composite(image,ImageChops.offset(self.magic,int(9*math.sin(t*.7)),int(6*math.cos(t*.5))))
        image=Image.alpha_composite(image,ImageChops.offset(self.bokeh,int(22*math.sin(t*.4)),int(-18*t)%1280))
        return Image.alpha_composite(image,self.vignette)

    def hud(self,t):
        state=hud_state(t,self.duration)
        layer=Image.new('RGBA',SIZE)
        d=ImageDraw.Draw(layer)
        color=(255,35,76,255) if state['crimson'] else (60,231,255,255)
        if state['crimson'] and not state['flash']:color=(149,21,52,255)
        d.rounded_rectangle((24,26,696,246),22,fill=(3,10,26,224),outline=color,width=3)
        d.text((48,43),f'[ СИСТЕМА · ЛИТРПГ · ЧАСТЬ {self.episode} ]',font=self.font(25,True),fill='#FFD966')
        d.text((48,87),f"ДО СБОЯ СИСТЕМЫ: {state['remaining']:.1f}s",font=self.font(31,True),fill=color,
               stroke_width=1,stroke_fill='black')
        d.text((48,139),f"СИНХРОНИЗАЦИЯ: {state['percent']}%",font=self.font(24,True),fill='#C4FAFF')
        d.rounded_rectangle((48,180,672,196),8,fill=(18,38,57,255),outline=(38,90,110,255),width=1)
        width=round(620*state['progress'])
        if width>0:
            d.rounded_rectangle((50,182,50+width,194),min(5,width/2),fill=(42,221,255,255))
            d.line((52,183,50+width,183),fill=(255,220,95,255),width=2)
        d.text((48,211),'[РАНГ: S-КЛАСС]   [УГРОЗА: ЭКСТРЕМАЛЬНАЯ]',font=self.font(19,True),fill='#FFD966')
        return layer

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
        image=Image.alpha_composite(image,self.hud(t))
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
