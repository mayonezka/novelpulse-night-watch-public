"""Public library, free chapters and bounded 1200-character chapter landing excerpts."""

import argparse
import asyncio
import html
import json
import re
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from app.config import PipelineSettings
from app.db import Database
from app.telegraph_seo import bot_link, channel_link
from app.promotional_text import complete_excerpt
from app.seo_hubs import SEO_HUBS,select_books
from app.podcast_feed import write_podcast
from app.search import search_attributes, web_search_widget, WEB_SEARCH_SCRIPT

CSS = """
:root{color-scheme:dark;--ink:#eaf0ec;--muted:#a6b5b0;--green:#b9f27c;--card:#152522}
*{box-sizing:border-box}body{margin:0;background:#0a1513;color:var(--ink);font:18px/1.75 Georgia,serif}
[hidden]{display:none!important}.catalog-search{padding:25px;border:1px solid #476038;border-radius:18px;margin-bottom:30px;font:16px/1.6 system-ui}.catalog-search label{display:block;font-weight:700;font-size:23px}.catalog-search input{width:100%;padding:15px;margin:14px 0;border-radius:10px;border:1px solid #779b68;background:#0a1513;color:var(--ink);font:inherit}.trope-pills{display:flex;flex-wrap:wrap;gap:9px}.trope-pills button{font:inherit;color:var(--ink);background:#152522;border:1px solid #779b68;border-radius:22px;padding:10px;cursor:pointer}.trope-pills button[aria-pressed=true]{background:#b9f27c;color:#10200b}button:focus-visible,input:focus-visible{outline:3px solid #eabb67;outline-offset:3px}
a{color:var(--green);text-underline-offset:4px}header,main,footer{width:min(1120px,92%);margin:auto}
header{padding:30px 0;display:flex;justify-content:space-between;align-items:center;gap:20px;font:14px/1.5 system-ui}
.brand{font-size:24px;font-weight:800;letter-spacing:-1px;text-decoration:none}.brand span{color:var(--green)}
nav{display:flex;gap:18px;flex-wrap:wrap}.hero{padding:55px 0 65px;border-top:1px solid #294039}
.eyebrow{font:12px/1.6 system-ui;text-transform:uppercase;letter-spacing:3px;color:var(--green)}
h1{font-size:clamp(32px,5vw,64px);line-height:1.15;letter-spacing:-1.5px;margin:20px 0 24px;max-width:920px}
.hero p{color:var(--muted);max-width:700px}.pill{display:inline-block;font:13px/1.5 system-ui;border:1px solid #3c5147;border-radius:30px;padding:8px 15px;margin-right:8px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}.card{padding:30px;border:1px solid #31413c;border-radius:18px;background:linear-gradient(140deg,#1d3029,#10211e);display:flex;flex-direction:column;gap:12px}
.card h2{font-size:27px;line-height:1.35;margin:6px 0}.card p{font-size:16px;color:var(--muted);margin:0 0 15px}.actions{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:auto;font:14px/1.6 system-ui}
.cta{display:inline-block;background:var(--green);color:#10200b;padding:12px 19px;border-radius:10px;font:700 14px/1.5 system-ui;text-decoration:none}
.cliffhanger-cta{display:block;font-size:22px;padding:25px;background:#ffd45c;color:#161008;border:2px solid #fff1b6;box-shadow:0 0 28px #e9a52e44;margin:24px 0}.links{margin:60px 0;padding:30px;border-left:3px solid var(--green);background:var(--card)}.links a{display:block;margin:12px 0;font:16px/1.6 system-ui}
.reader{max-width:780px;margin:0 auto 100px}.chapter{padding:35px 0;border-top:1px solid #31413c}.chapter h2{font-size:28px;line-height:1.4}.chapter p{white-space:pre-line;overflow-wrap:anywhere}
.sticky{position:sticky;bottom:0;background:#14241ef5;border-top:1px solid #476038;padding:16px max(4%,calc((100% - 1120px)/2));display:flex;justify-content:space-between;align-items:center;gap:20px;backdrop-filter:blur(12px);font:14px/1.5 system-ui}
footer{padding:30px 0;color:var(--muted);font:13px/1.7 system-ui}.description{color:var(--muted)}
.audio-preview{margin:28px 0;padding:24px;border:1px solid #476038;border-radius:16px;background:var(--card);font:16px/1.6 system-ui}.audio-preview audio{display:block;width:100%;max-width:100%;margin:18px 0}.audio-preview summary{cursor:pointer;color:var(--muted)}
@media(max-width:650px){.grid{grid-template-columns:1fr}.hero{padding:30px 0 40px}.card{padding:24px}header{align-items:flex-start;flex-direction:column;gap:12px}.sticky{font-size:12px;padding:12px 4%;gap:12px}.sticky .cta{font-size:12px;padding:10px 13px}body{font-size:17px}.links{padding:22px}}
"""


def escape(value) -> str:
    return html.escape(str(value), quote=True)


def clean_chapter_title(chapter_num: int, title: str) -> str:
    """Catalog numbers own the heading; strip source labels, including parts 7.1/7.2."""
    if int(chapter_num) < 1:
        raise ValueError('Номер главы должен быть положительным')
    prefix = re.compile(r'^\s*глава\s+\d+(?:\.\d+)*(?=$|[\s.:;\-—–|])\s*[.:;\-—–|]*\s*', re.I)
    cleaned = str(title or '').strip()
    while True:
        remaining = prefix.sub('', cleaned, count=1)
        if remaining == cleaned:
            return cleaned
        cleaned = remaining.strip()


def chapter_heading(chapter: dict) -> str:
    title = clean_chapter_title(chapter['chapter_num'], chapter['title'])
    return f'Глава {chapter["chapter_num"]}' + (f'. {title}' if title else '')


def chapter_body(chapter: dict) -> str:
    """Hide a duplicated source heading only when the whole first line is a label."""
    text = chapter['content_text']
    lines = text.splitlines(keepends=True)
    if not lines:
        return text
    first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        return text
    first = lines[first_index].strip()
    cleaned = clean_chapter_title(chapter['chapter_num'], first)
    if cleaned != first and (not cleaned or cleaned == clean_chapter_title(chapter['chapter_num'], chapter['title'])):
        return ''.join(lines[first_index+1:]).lstrip('\r\n')
    return text


def cliffhanger_preview(text: str) -> str:
    """Keep a contiguous ~60% opening; prefer a tense sentence near the boundary."""
    target = int(len(text) * .60)
    if not target:
        return '…'
    low = int(len(text) * .54)
    candidates = list(re.finditer(r'[^.!?\n]+[.!?…]?', text[low:target]))
    tension = re.compile(r'систем|смерт|кров|таймер|двер|вдруг|не успел|шаг|—|«', re.I)
    dramatic = [m for m in candidates if tension.search(m.group())]
    end = low + dramatic[-1].end() if dramatic else target
    end = min(target, end)
    prefix = text[:end].rstrip()
    # Never leave half a word or reveal any text beyond the 60% budget.
    if end < len(text) and text[end:end+1].isalnum() and prefix[-1:].isalnum():
        prefix = prefix.rsplit(' ', 1)[0] if ' ' in prefix else ''
    return prefix.rstrip(' .!?…') + '…'


def shell(title: str, description: str, body: str, *, canonical: str, channel_url: str,
          schema: dict | None = None, sticky: str = '', rss: str = '', verification: str = '') -> str:
    metadata = ''
    if canonical:
        metadata = f'<link rel="canonical" href="{escape(canonical)}"><meta property="og:url" content="{escape(canonical)}">'
    structured = ''
    if schema:
        # Prevent an imported </script> string from breaking out of JSON-LD.
        structured = '<script type="application/ld+json">' + json.dumps(schema, ensure_ascii=False).replace('<', '\\u003c') + '</script>'
    verification_meta = f'<meta name="google-site-verification" content="{escape(verification.strip())}">' if verification.strip() else ''
    channel = f'<a href="{escape(channel_url)}">Telegram-канал ↗</a>' if channel_url else ''
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><meta name="description" content="{escape(description[:300])}">
<meta property="og:title" content="{escape(title)}"><meta property="og:description" content="{escape(description[:300])}">
<meta property="og:type" content="{'book' if schema else 'website'}"><meta property="og:locale" content="ru_RU">
<meta name="theme-color" content="#0a1513">{metadata}{structured}{rss}{verification_meta}<link rel="stylesheet" href="styles.css"></head>
<body><header><a class="brand" href="index.html">Novel<span>Pulse</span></a><nav><a href="index.html">Библиотека</a>{channel}</nav></header>
<main>{body}</main><footer>NovelPulse · Русские переводы веб-новелл. Первые главы бесплатно.<br>
Покупки глав бессрочны. VIP действует 7 дней и стоит 49 Telegram Stars.</footer>{sticky}</body></html>'''


def chapter_html(chapter: dict) -> str:
    paragraphs = ''.join(f'<p>{escape(p.strip())}</p>' for p in re.split(r'\n\s*\n', chapter_body(chapter)) if p.strip())
    return f'<section class="chapter"><h2>{escape(chapter_heading(chapter))}</h2>{paragraphs}</section>'


def audio_preview_html(series, chapters, previews, link, *, chapter_num=None):
    """Reuse the same validated public RSS enclosure on every page of this book."""
    from app.podcast_feed import podcast_assets
    preview = previews.get(str(series['id']), {})
    filename = f'podcast_{series["id"]}.mp3'
    if filename not in podcast_assets([{'series': series, 'chapters': chapters}], previews):
        return f'<aside class="audio-preview"><h2>🎧 Слушайте в Telegram</h2><a href="{escape(link)}">Открыть аудиокнигу в боте</a></aside>'
    later_chapter = chapter_num is not None and chapter_num >= 2
    heading = '🎧 Ознакомительный аудиотрейлер новеллы' if later_chapter else '🎧 Послушайте начало истории'
    description = ('Короткий аудиофрагмент первой главы · Полная аудиозапись этой главы доступна в Telegram-боте NovelPulse'
        if later_chapter else 'Короткий аудиофрагмент · русская нейронная озвучка NovelPulse Audio')
    return (f'<aside class="audio-preview"><h2>{heading}</h2>'
            f'<p>{description}</p>'
            f'<audio controls preload="none" aria-label="Аудиофрагмент первой главы {escape(series["title"])}">'
            f'<source src="{filename}" type="audio/mpeg">'
            'Ваш браузер не поддерживает аудио. Откройте главу в Telegram.</audio>'
            f'<details><summary>Текст аудиофрагмента</summary><p>{escape(preview.get("transcript",""))}</p></details>'
            f'<a class="cta" href="{escape(link)}">🎧 Слушать полные главы в боте</a></aside>')


async def generate_web_portal(db: Database, bot_username: str, *, output_dir: str | Path = 'docs',
                               base_url: str | None = None, channel_id: str | int | None = None,
                               google_site_verification: str = '') -> dict:
    config = PipelineSettings(_env_file=None, web_base_url=base_url)
    base = config.web_base_url or 'http://localhost:8000/'
    public = config.web_base_url is not None
    channel = channel_link(channel_id) if isinstance(channel_id, str) and channel_id else ''
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    series_list = await db.list_series()
    search_rows = {s['id']: s for s in await db.searchable_catalog()}
    hubs = json.loads(await db.get_state('seo_hubs') or '[]')
    previews = json.loads(await db.get_state('web_audio_previews') or '{}')
    reader_sections = await db.get_state('portal_reader_sections') == '1'
    original_full = await db.get_state('original_flagship_version') == '40'
    search_index = {}
    def indexed_attributes(row, kind):
        from bs4 import BeautifulSoup
        attrs = BeautifulSoup('<i '+search_attributes(row,kind)+'></i>', 'html.parser').i.attrs
        key = str(len(search_index))
        search_index[key] = {'search': attrs['data-search'], 'tropes': attrs['data-tropes']}
        return f'data-filter-item="{kind}" data-search-id="{key}"'
    (directory/'styles.css').write_text(CSS, encoding='utf-8')
    cards, files, books = [], [], []
    for series in series_list:
        link = f"https://t.me/{bot_username}?start=web_s_{series['id']}"
        filename = f"series_{series['id']}.html"
        published=await db.list_chapters(series['id'])
        chapters = [await db.get_chapter(c['id']) for c in published
                    if c['chapter_num'] <= min(2, series['free_chapters_limit'])]
        books.append({'series':series,'chapters':chapters,'chapter_count':len(published)})
        chapter_links=[]
        for row in published:
            chapter=await db.get_chapter(row['id'])
            chapter_name=f"series_{series['id']}_ch_{chapter['chapter_num']}.html"
            chapter_url=urljoin(base,chapter_name)
            direct=f"https://t.me/{bot_username}?start=web_ch_{series['id']}_{chapter['chapter_num']}"
            heading=chapter_heading(chapter)
            title=f"{series['title']} — {heading} | Читать бесплатно онлайн · Слушать аудиокнигу"
            audio=audio_preview_html(series,chapters,previews,direct,chapter_num=chapter['chapter_num'])
            free_gate = chapter['chapter_num'] <= min(2, series['free_chapters_limit'])
            source_body = chapter_body(chapter)
            excerpt = cliffhanger_preview(source_body) if free_gate else source_body[:1200]
            chapter_description=(f"{heading} — «{series['title']}»: ЛитРПГ новелла на русском. "
                "Читать бесплатно онлайн начало главы и слушать аудиокнигу в Telegram. "
                f"{source_body[:90]}")
            graph={'@context':'https://schema.org','@graph':[
                {'@type':'Chapter','@id':chapter_url+'#chapter','url':chapter_url,'name':title,
                 'position':chapter['chapter_num'],'inLanguage':'ru','description':excerpt[:250],
                 'isPartOf':{'@type':'Book','name':series['title'],'url':urljoin(base,filename)},
                 'potentialAction':{'@type':'ReadAction','target':direct}},
                {'@type':'BreadcrumbList','itemListElement':[
                    {'@type':'ListItem','position':1,'name':'Библиотека','item':urljoin(base,'index.html')},
                    {'@type':'ListItem','position':2,'name':series['title'],'item':urljoin(base,filename)},
                    {'@type':'ListItem','position':3,'name':f"Глава {chapter['chapter_num']}",'item':chapter_url}]}]}
            cta=f'<a class="cta" href="{escape(direct)}">📖 Читать продолжение в Telegram-боте (@{escape(bot_username)})</a>'
            if free_gate:
                cta=f'<a class="cta cliffhanger-cta" href="{escape(direct)}">🔥 Дочитать главу до конца и включить полную аудиоверсию бесплатно в Telegram-боте (в 1 клик)</a>'
            body=(f'<div class="hero"><div class="eyebrow"><a href="index.html">Библиотека</a> / '
                  f'<a href="{filename}">{escape(series["title"])}</a></div><h1>{escape(heading)}</h1>'
                  f'<p>{escape(series["title"])}</p>{cta}</div>'
                  f'<article class="reader">{audio}<section class="chapter"><p>{escape(excerpt)}</p>' +
                  '<blockquote>…Что произойдёт дальше? Полная глава уже ждёт в боте.</blockquote>' +
                  f'{cta}<p>🎧 Полная аудиоглава и текст — в Telegram. Первая платная глава: 1 ⭐️; VIP на 24 часа: 19 ⭐️.</p>'
                  '</section></article>')
            (directory/chapter_name).write_text(shell(title,chapter_description,body,canonical=chapter_url if public else '',
                channel_url=channel,schema=graph,sticky=f'<aside class="sticky">{cta}</aside>'),encoding='utf-8')
            files.append(chapter_name)
            chapter_links.append(f'<li><a href="{chapter_name}">{escape(heading)}</a></li>')
        chapter_nav='<section class="links"><h2>Все главы: читать и слушать</h2><ol>'+''.join(chapter_links)+'</ol></section>'
        schema = {'@context': 'https://schema.org', '@type': 'Book', 'name': series['title'],
                  'inLanguage': 'ru', 'genre': ['ЛитРПГ', 'Веб-новелла'], 'description': series['description'][:700],
                  'publisher': {'@type': 'Organization', 'name': 'NovelPulse'},
                  'sameAs': [url for url in (series['source_url'], series['telegraph_url']) if url]}
        if public:
            schema['url'] = urljoin(base, filename)
        article = (f'<div class="hero"><div class="eyebrow">Библиотека / ЛитРПГ и новые миры</div>'
                   f'<h1>{escape(series["title"])}</h1><p>{escape(complete_excerpt(series["description"],700))}</p>'
                   '<span class="pill">Первые главы бесплатно</span><span class="pill">Перевод на русский</span></div>'
                   '<article class="reader">' + chapter_nav + audio_preview_html(series,chapters,previews,link) + ''.join(chapter_html({**c, 'content_text': c['content_text'][:1200]}) + f'<a href="series_{series["id"]}_ch_{c["chapter_num"]}.html">Открыть страницу главы</a>' for c in chapters) +
                   f'<div class="links"><h2>История продолжается…</h2><p>Следующая глава уже ждёт в Telegram. '
                   f'Новому читателю первая платная глава — всего 1 ⭐️.</p><a class="cta" href="{link}">Читать дальше в @{escape(bot_username)}</a></div></article>')
        sticky = f'<aside class="sticky"><span>🔥 Первая платная глава — 1 ⭐️<br>👑 VIP на все книги — 49 ⭐️ / 7 дней</span><a class="cta" href="{link}">Открыть продолжение ➡</a></aside>'
        target = directory / filename
        target.write_text(shell(series['title'] + ' | ЛитРПГ новелла — читать бесплатно онлайн и слушать аудиокнигу',
            f'«{series["title"]}» — ЛитРПГ новелла на русском. Читать бесплатно онлайн первые главы, слушать аудиокнигу и продолжить в Telegram. ' + series['description'][:100], article,
            canonical=urljoin(base, filename) if public else '', channel_url=channel, schema=schema, sticky=sticky), encoding='utf-8')
        files.append(filename)
        iv = f'<a href="{escape(series["telegraph_url"])}">Instant View ↗</a>' if series['telegraph_url'] else ''
        cards.append(f'<section class="card" {indexed_attributes(search_rows[series["id"]], "book")}><div class="eyebrow">Мир {series["id"]:02d} · Первые главы бесплатно</div>'
                     f'<h2><a href="{filename}">{escape(series["title"])}</a></h2><p>{escape(complete_excerpt(series["description"], 120))}</p>'
                     f'<p><a href="{filename}">Оглавление · {len(published)} глав</a></p>'
                     f'<div class="actions"><a class="cta" href="{link}">Читать в @{escape(bot_username)}</a>{iv}</div></section>')
    genre_links=[]
    for slug,title,genres,intro in SEO_HUBS:
        name='seo_'+slug+'.html';selection=select_books(books,genres,slug=slug)
        entries=[];elements=[]
        for number,book in enumerate(selection,1):
            s=book['series'];link=f"https://t.me/{bot_username}?start=web_s_{s['id']}";book_name=f'series_{s["id"]}.html'
            excerpt=complete_excerpt(book['chapters'][0]['content_text'],400) if book['chapters'] else ''
            entries.append(f'<article class="card"><div class="eyebrow">{escape(s["genre_title"])} · {book["chapter_count"]} глав</div>'
                f'<h2><a href="{book_name}">{escape(s["title"])}</a></h2><p>{escape(complete_excerpt(s["description"],450))}</p>'
                f'<blockquote>{escape(excerpt)}</blockquote>'
                +audio_preview_html(s,book['chapters'],previews,link)+
                f'<div class="actions"><a class="cta" href="{link}">📖 Читать и слушать в @{escape(bot_username)}</a>'
                f'<a href="{book_name}">Оглавление и бесплатное начало</a></div></article>')
            elements.append({'@type':'ListItem','position':number,'url':urljoin(base,book_name),
                'item':{'@type':'Book','name':s['title'],'url':urljoin(base,book_name),'inLanguage':'ru'}})
        schema={'@context':'https://schema.org','@type':'ItemList','name':title,'numberOfItems':len(elements),'itemListElement':elements}
        related=''.join(f'<a href="seo_{other}.html">{escape(label)}</a>' for other,label,*_ in SEO_HUBS if other!=slug)
        body=(f'<section class="hero"><div class="eyebrow">Редакционный маршрут NovelPulse</div><h1>{escape(title)}</h1>'
            f'<p>{escape(intro)}</p><a class="cta" href="https://t.me/{escape(bot_username)}?start=web_s_1">📖 Открыть каталог в @{escape(bot_username)}</a>'
            '<p>Выбор основан на жанровых полках каталога, а не на рейтинге популярности. '
            'Сравните описание, бесплатный текст и звучание перед тем, как продолжить чтение.</p></section>'
            '<div class="grid">'+''.join(entries)+'</div><section class="links"><h2>Как читать дальше</h2>'
            f'<p>Откройте книгу в <a href="https://t.me/{escape(bot_username)}?start=web_s_1">@{escape(bot_username)}</a>. '
            'Для полного продолжения доступны ключи, суточный таймер, покупка главы и VIP. '
            'Первые главы и короткие аудиофрагменты помогают выбрать подходящий мир.</p>'
            '<a href="podcast.xml">RSS аудиопревью</a></section>'
            f'<section class="links"><h2>Другие читательские маршруты</h2>{related}</section>')
        (directory/name).write_text(shell(title+' | NovelPulse',intro,body,
            canonical=urljoin(base,name) if public else '',channel_url=channel,schema=schema),encoding='utf-8')
        files.append(name)
        hub_search={'title':title,'description':intro,'genre_slug':' '.join(genres),
            'chapter_titles':' '.join(search_rows[b['series']['id']]['chapter_titles'] or '' for b in selection)}
        genre_links.append(f'<a href="{name}" {indexed_attributes(hub_search, "hub")}>{escape(title)}</a>')
    dates=json.loads(await db.get_state('podcast_first_published') or '{}')
    audio_files=write_podcast(directory,books,previews,base,bot_username,dates)
    await db.set_state('podcast_first_published',json.dumps(dates))
    hub_links = ''.join(f'<a href="{escape(h["url"])}">{escape(h["title"])} ↗</a>' for h in hubs)
    reader_navigation = ''
    if reader_sections:
        from app.portal_sections import render_reader_sections
        for name, content in render_reader_sections(books,previews,bot_username,base,channel).items():
            (directory/name).write_text(content,encoding='utf-8')
            files.append(name)
        reader_navigation = ('<section class="links"><h2>Читателю</h2>'
            '<a href="new_releases.html">Новые поступления</a><a href="audio_library.html">Аудиотека</a>'
            '<a href="reading_guide.html">Как читать бесплатно: таймер, ключи и VIP</a></section>')
    body = ('<section class="hero"><div class="eyebrow">ЛитРПГ · Исэкай · Система</div>'
            '<h1>Ещё одна глава.<br>Ещё один новый мир.</h1><p>Герои нулевого ранга, опасные системы и приключения, '
            'от которых трудно оторваться. Откройте библиотеку русских переводов — первые главы читаются бесплатно.</p>'
            f'<span class="pill">{len(series_list)} новелл</span><span class="pill">Первая покупка — 1 ⭐️</span></section>'
            + reader_navigation + web_search_widget() + '<div class="grid">' + ''.join(cards) + '</div>' +
            f'<section class="links"><h2>24 маршрута: выбрать, читать и слушать</h2>{"".join(genre_links)}</section>'
            f'<section class="links"><h2>Подборки и подкаст</h2><a href="podcast.xml">🎧 RSS аудиопревью NovelPulse Audio</a>{hub_links}</section>' + '<script src="catalog.js" defer></script>')
    search_js = 'const index=' + json.dumps(search_index, ensure_ascii=False).replace('<','\\u003c') + ';\n' + "document.querySelectorAll('[data-search-id]').forEach(el=>{Object.assign(el.dataset,index[el.dataset.searchId]);});\n" + WEB_SEARCH_SCRIPT.removeprefix('<script>').removesuffix('</script>')
    (directory/'catalog.js').write_text(search_js, encoding='utf-8')
    (directory / 'index.html').write_text(shell('NovelPulse — веб-новеллы и ЛитРПГ на русском',
        'Библиотека русских переводов: ЛитРПГ, Исэкай, Система. Первые главы бесплатно, продолжение в Telegram.',
        body, verification=google_site_verification, canonical=urljoin(base, 'index.html') if public else '', channel_url=channel,
        rss=f'<link rel="alternate" type="application/rss+xml" title="NovelPulse Audio" href="{escape(urljoin(base,"podcast.xml"))}">'), encoding='utf-8')
    files.insert(0, 'index.html')
    ET.register_namespace('', 'http://www.sitemaps.org/schemas/sitemap/0.9')
    root = ET.Element('{http://www.sitemaps.org/schemas/sitemap/0.9}urlset')
    for filename in files:
        item = ET.SubElement(root, 'url')
        ET.SubElement(item, 'loc').text = urljoin(base, filename)
    ET.ElementTree(root).write(directory / 'sitemap.xml', encoding='utf-8', xml_declaration=True)
    (directory / 'robots.txt').write_text(f'User-agent: *\nAllow: /\nSitemap: {urljoin(base, "sitemap.xml")}\n', encoding='utf-8')
    (directory / 'DEPLOY.md').write_text(
        '# Публикация библиотеки\n\n'
        + ('Публичный адрес: ' + base if public else 'Публичный домен пока не задан. Sitemap использует локальный адрес предпросмотра; эти страницы ещё не доступны внешним поисковикам.')
        + '\n\nПредпросмотр из корня проекта: `.venv/Scripts/python.exe -m http.server 8000 --directory docs`.\n'
        'После выбора собственного домена задайте WEB_BASE_URL в .env и повторите '
        '`python -m app.web_portal --bot-username NovelPulseBot --channel @novelpulse_litrpg`.\n'
        'Размещайте только содержимое docs/. Не размещайте .env, data/, SQLite и приватный аккаунт Telegraph.\n'
        'Для IndexNow нужен собственный домен и проверочный ключ на нём; чужой домен telegra.ph отправлять нельзя.\n', encoding='utf-8')
    report = {'series_count': len(series_list), 'files': files + ['styles.css','catalog.js','podcast.xml',*audio_files,'sitemap.xml', 'robots.txt', 'DEPLOY.md'],
              'seo_pages':24,'podcast_episodes':len(audio_files),'podcast_audio_files':audio_files,
              'directory': str(directory.resolve()), 'base_url': base, 'public_base_configured': public}
    await db.set_state('web_portal_report', json.dumps(report, ensure_ascii=False))
    return report


async def _cli(args):
    settings = PipelineSettings()
    db = Database(settings.db_path)
    await db.open()
    try:
        report = await generate_web_portal(db, args.bot_username, output_dir=args.output_dir,
            base_url=args.base_url or settings.web_base_url, channel_id=args.channel,google_site_verification=settings.google_site_verification)
        print(json.dumps(report, ensure_ascii=True, indent=2))
    finally:
        await db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Статическая библиотека NovelPulse без секретов и платных глав')
    parser.add_argument('--bot-username', default='NovelPulseBot')
    parser.add_argument('--channel', default='')
    parser.add_argument('--output-dir', default='docs')
    parser.add_argument('--base-url')
    asyncio.run(_cli(parser.parse_args()))
