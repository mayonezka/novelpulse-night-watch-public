"""Public, stable enclosures from authorized free-chapter previews only."""
import base64
import re
from datetime import datetime,timezone
from email.utils import format_datetime
from io import BytesIO
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

ITUNES='http://www.itunes.com/dtds/podcast-1.0.dtd'
ATOM='http://www.w3.org/2005/Atom'


def valid_preview_bytes(data):
    if not isinstance(data,bytes) or not 100<=len(data)<=260_000:return False
    try:
        from mutagen.mp3 import MP3
        return .25<=MP3(BytesIO(data)).info.length<=60
    except Exception:
        return False


def podcast_assets(books,previews):
    from app.audiobook import preview_fingerprint
    assets={}
    for book in books:
        series=book['series'];preview=previews.get(str(series['id']),{})
        chapter=next((c for c in book['chapters'] if c['id']==preview.get('chapter_id')
            and c['chapter_num']<=series['free_chapters_limit']),None)
        if chapter is None or preview.get('source_digest')!=preview_fingerprint(chapter):continue
        try:data=base64.b64decode(preview.get('audio_base64',''),validate=True)
        except (ValueError,TypeError):continue
        if valid_preview_bytes(data):assets[f'podcast_{series["id"]}.mp3']=data
    return assets


async def catalog_podcast_assets(db):
    import json
    books=[]
    for series in await db.list_series():
        chapters=[await db.get_chapter(c['id']) for c in await db.list_chapters(series['id'])
            if c['chapter_num']<=min(2,series['free_chapters_limit'])]
        books.append({'series':series,'chapters':chapters})
    return podcast_assets(books,json.loads(await db.get_state('web_audio_previews') or '{}'))


def write_podcast(directory,books,previews,base,username,published_dates):
    ET.register_namespace('itunes',ITUNES);ET.register_namespace('atom',ATOM)
    root=ET.Element('rss',version='2.0');channel=ET.SubElement(root,'channel')
    def field(parent,name,text):
        ET.SubElement(parent,name).text=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',str(text))
    field(channel,'title','NovelPulse Audio — ЛитРПГ и веб-новеллы на русском')
    field(channel,'link',urljoin(base,'index.html'))
    field(channel,'description','Короткие русские аудиопревью. Полные доступные главы — в Telegram-боте NovelPulse.')
    field(channel,'language','ru-RU')
    field(channel,'{'+ITUNES+'}author','NovelPulse Audio')
    field(channel,'{'+ITUNES+'}explicit','false')
    field(channel,'{'+ITUNES+'}type','episodic')
    ET.SubElement(channel,'{'+ITUNES+'}category',text='Fiction')
    ET.SubElement(channel,'{'+ATOM+'}link',href=urljoin(base,'podcast.xml'),rel='self',type='application/rss+xml')
    assets=podcast_assets(books,previews)
    for name,data in assets.items():
        sid=int(name.removeprefix('podcast_').removesuffix('.mp3'))
        series=next(b['series'] for b in books if b['series']['id']==sid)
        preview=previews[str(sid)]
        (directory/name).write_bytes(data)
        item=ET.SubElement(channel,'item')
        field(item,'title',series['title']+' — аудиознакомство')
        link=f'https://t.me/{username}?start=series_{sid}'
        field(item,'link',link)
        field(item,'description',f'{series["description"][:700]}\n\nФрагмент бесплатной первой главы: '
            f'{preview.get("transcript","")}\n\nЧитать и слушать продолжение: {link}')
        guid=ET.SubElement(item,'guid',isPermaLink='false');guid.text=f'novelpulse:preview:{sid}:{preview["source_digest"]}'
        published_dates.setdefault(str(sid),datetime.now(timezone.utc).isoformat())
        stamp=datetime.fromisoformat(published_dates[str(sid)])
        field(item,'pubDate',format_datetime(stamp.astimezone(timezone.utc)))
        field(item,'{'+ITUNES+'}author','NovelPulse Audio')
        field(item,'{'+ITUNES+'}explicit','false')
        ET.SubElement(item,'enclosure',url=urljoin(base,name),length=str(len(data)),type='audio/mpeg')
    ET.ElementTree(root).write(directory/'podcast.xml',encoding='utf-8',xml_declaration=True)
    return list(assets)
