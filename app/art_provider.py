"""Fixed-query SFW artwork only. No catalog, settings, credentials or text input."""
import asyncio
import io
import json
import re
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

SIZE=(720,1280)
WALLHAVEN_URL='https://wallhaven.cc/api/v1/search?q=fantasy+magic&categories=010&purity=100&atleast=720x1280&sorting=favorites'
SAFEBOORU_URL='https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1&limit=20&tags=scenery+magic+rating%3Asafe'
SAFEBOORU_DARK_URL='https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1&limit=20&tags=dark+fantasy+solo+rating%3Asafe'
SEARCHES=(('wallhaven',WALLHAVEN_URL),('safebooru',SAFEBOORU_URL),('safebooru',SAFEBOORU_DARK_URL))
IMAGE_HOSTS=frozenset(('w.wallhaven.cc','th.wallhaven.cc','safebooru.org','img.safebooru.org'))
MAX_BYTES=8_000_000


def safe_image_url(value):
    """Provider CDN paths only; no redirects, query strings or arbitrary hosts."""
    if not isinstance(value,str):return False
    try:
        parsed=urlsplit(value)
        return (parsed.scheme=='https' and parsed.hostname in IMAGE_HOSTS and
            not parsed.username and not parsed.password and parsed.port in (None,443) and
            not parsed.query and not parsed.fragment and '..' not in parsed.path and
            bool(re.fullmatch(r'/[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png|webp)',parsed.path,re.I)))
    except ValueError:return False


def candidates(provider,payload):
    rows=payload.get('data',[]) if provider=='wallhaven' and isinstance(payload,dict) else payload
    if not isinstance(rows,list):return []
    result=[]
    for item in rows[:20]:
        if not isinstance(item,dict):continue
        if provider=='wallhaven':
            if item.get('purity')!='sfw':continue
            urls=[item.get('path'),(item.get('thumbs') or {}).get('large')]
        else:
            if item.get('rating') not in ('s','safe','g','general'):continue
            urls=[item.get('file_url'),item.get('sample_url')]
            # The documented legacy API returns a directory and image filename.
            directory,image=str(item.get('directory','')),str(item.get('image',''))
            if directory.isdigit() and re.fullmatch(r'[A-Za-z0-9_.-]+\.(?:jpg|jpeg|png|webp)',image,re.I):
                urls.append('https://safebooru.org/images/'+directory+'/'+image)
        chosen=next((url for url in urls if safe_image_url(url)),None)
        if chosen and chosen not in result:result.append(chosen)
    return result


def grade_art(image):
    image=ImageOps.fit(image.convert('RGB'),SIZE,method=Image.Resampling.LANCZOS)
    image=ImageEnhance.Contrast(image).enhance(1.22)
    image=ImageEnhance.Color(image).enhance(.78)
    overlay=Image.new('RGBA',SIZE)
    draw=ImageDraw.Draw(overlay)
    import math
    for y in range(SIZE[1]):
        # A calm dark subtitle lane, deep blue shadows, lighter edges of the scene.
        alpha=int(48+118*math.exp(-((y-650)/200)**2))
        draw.line((0,y,720,y),fill=(2,7,22,alpha))
    return Image.alpha_composite(image.convert('RGBA'),overlay).convert('RGB')


async def fetch_bytes(client,url,limit=MAX_BYTES):
    async with client.stream('GET',url) as response:
        response.raise_for_status()
        content=bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content)>limit:raise ValueError('Artwork response exceeds byte limit')
    return bytes(content)


def decode_image(data):
    with Image.open(io.BytesIO(data)) as source:
        if source.width*source.height>8_000_000 or min(source.size)<100:
            raise ValueError('Artwork dimensions outside limits')
        source.load()
        return grade_art(source)


async def fetch_artwork(*,offline=False,count=4):
    """Finite no-key failover; images stay in memory and are never cached on disk."""
    if not 1<=count<=4:raise ValueError('Artwork count must be 1..4')
    images,sources,failures=[],[],[]
    if not offline:
        async with httpx.AsyncClient(timeout=6,follow_redirects=False,trust_env=False,
                headers={'User-Agent':'NovelPulse-Art/3.0 (fixed SFW scenery queries)'}) as client:
            for provider,url in SEARCHES:
                try:
                    async with asyncio.timeout(10):
                        payload=json.loads(await fetch_bytes(client,url,1_000_000))
                        urls=candidates(provider,payload)[:count-len(images)]
                        if not urls:raise ValueError('No safe artwork candidates')
                        async def download(image_url):
                            return await asyncio.to_thread(decode_image,await fetch_bytes(client,image_url))
                        results=await asyncio.gather(*(download(u) for u in urls),return_exceptions=True)
                        for result in results:
                            if isinstance(result,BaseException):
                                failures.append({'provider':provider,'reason':type(result).__name__})
                            else:
                                images.append(result)
                                sources.append({'provider':provider,'reason':None,'query':'fixed SFW scenery'})
                    if len(images)==count:break
                except Exception as error:
                    reason=type(error).__name__
                    if isinstance(error,httpx.HTTPStatusError):reason+=' HTTP '+str(error.response.status_code)
                    failures.append({'provider':provider,'reason':reason})
    while len(images)<count:
        # Lazy boundary avoids circular imports and keeps the fallback reusable in tests.
        from app.video_motion import procedural_art
        images.append(await asyncio.to_thread(lambda i=len(images):grade_art(procedural_art(1800,i))))
        sources.append({'provider':'procedural','reason':'explicit offline test' if offline else 'Free APIs unavailable',
                        'failures':list(failures),'layers':3})
    return images,sources
