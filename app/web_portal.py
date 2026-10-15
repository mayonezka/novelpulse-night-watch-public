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

CSS = """
:root{color-scheme:dark;--ink:#eaf0ec;--muted:#a6b5b0;--green:#b9f27c;--card:#152522}
*{box-sizing:border-box}body{margin:0;background:#0a1513;color:var(--ink);font:18px/1.75 Georgia,serif}
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
.links{margin:60px 0;padding:30px;border-left:3px solid var(--green);background:var(--card)}.links a{display:block;margin:12px 0;font:16px/1.6 system-ui}
.reader{max-width:780px;margin:0 auto 100px}.chapter{padding:35px 0;border-top:1px solid #31413c}.chapter h2{font-size:28px;line-height:1.4}.chapter p{white-space:pre-line;overflow-wrap:anywhere}
.sticky{position:sticky;bottom:0;background:#14241ef5;border-top:1px solid #476038;padding:16px max(4%,calc((100% - 1120px)/2));display:flex;justify-content:space-between;align-items:center;gap:20px;backdrop-filter:blur(12px);font:14px/1.5 system-ui}
footer{padding:30px 0;color:var(--muted);font:13px/1.7 system-ui}.description{color:var(--muted)}
.audio-preview{margin:28px 0;padding:24px;border:1px solid #476038;border-radius:16px;background:var(--card);font:16px/1.6 system-ui}.audio-preview audio{display:block;width:100%;max-width:100%;margin:18px 0}.audio-preview summary{cursor:pointer;color:var(--muted)}
@media(max-width:650px){.grid{grid-template-columns:1fr}.hero{padding:30px 0 40px}.card{padding:24px}header{align-items:flex-start;flex-direction:column;gap:12px}.sticky{font-size:12px;padding:12px 4%;gap:12px}.sticky .cta{font-size:12px;padding:10px 13px}body{font-size:17px}.links{padding:22px}}
"""


def escape(value) -> str:
    return html.escape(str(value), quote=True)


def shell(title: str, description: str, body: str, *, canonical: str, channel_url: str,
          schema: dict | None = None, sticky: str = '') -> str:
    metadata = ''
    if canonical:
        metadata = f'<link rel="canonical" href="{escape(canonical)}"><meta property="og:url" content="{escape(canonical)}">'
    structured = ''
    if schema:
        # Prevent an imported </script> string from breaking out of JSON-LD.
        structured = '<script type="application/ld+json">' + json.dumps(schema, ensure_ascii=False).replace('<', '\\u003c') + '</script>'
    channel = f'<a href="{escape(channel_url)}">Telegram-канал ↗</a>' if channel_url else ''
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title><meta name="description" content="{escape(description[:300])}">
<meta property="og:title" content="{escape(title)}"><meta property="og:description" content="{escape(description[:300])}">
<meta property="og:type" content="{'book' if schema else 'website'}"><meta property="og:locale" content="ru_RU">
<meta name="theme-color" content="#0a1513">{metadata}{structured}<style>{CSS}</style></head>
<body><header><a class="brand" href="index.html">Novel<span>Pulse</span></a><nav><a href="index.html">Библиотека</a>{channel}</nav></header>
<main>{body}</main><footer>NovelPulse · Русские переводы веб-новелл. Первые главы бесплатно.<br>
Покупки глав бессрочны. VIP действует 7 дней и стоит 49 Telegram Stars.</footer>{sticky}</body></html>'''


def chapter_html(chapter: dict) -> str:
    paragraphs = ''.join(f'<p>{escape(p.strip())}</p>' for p in re.split(r'\n\s*\n', chapter['content_text']) if p.strip())
    return f'<section class="chapter"><h2>Глава {chapter["chapter_num"]}. {escape(chapter["title"])}</h2>{paragraphs}</section>'


def audio_preview_html(series, chapters, previews, link):
    """A small inline MP3 keeps static hosting self-contained and local storage empty."""
    import base64
    from app.audiobook import preview_fingerprint
    preview = previews.get(str(series['id']),{})
    first = next((c for c in chapters if c['id']==preview.get('chapter_id')),None)
    encoded = preview.get('audio_base64','')
    if first is None or preview.get('source_digest')!=preview_fingerprint(first):
        return f'<aside class="audio-preview"><h2>🎧 Слушайте в Telegram</h2><a href="{escape(link)}">Открыть аудиокнигу в боте</a></aside>'
    try:
        if not 100<=len(base64.b64decode(encoded,validate=True))<=260_000:
            raise ValueError('Preview outside size budget')
    except (ValueError,TypeError):
        return ''
    return ('<aside class="audio-preview"><h2>🎧 Послушайте начало истории</h2>'
            '<p>Короткий аудиофрагмент · русская нейронная озвучка NovelPulse Audio</p>'
            f'<audio controls preload="none" aria-label="Аудиофрагмент {escape(series["title"])}">'
            f'<source src="data:audio/mpeg;base64,{encoded}" type="audio/mpeg">'
            'Ваш браузер не поддерживает аудио. Откройте главу в Telegram.</audio>'
            f'<details><summary>Текст аудиофрагмента</summary><p>{escape(preview.get("transcript",""))}</p></details>'
            f'<a class="cta" href="{escape(link)}">🎧 Слушать полные главы в боте</a></aside>')


async def generate_web_portal(db: Database, bot_username: str, *, output_dir: str | Path = 'docs',
                               base_url: str | None = None, channel_id: str | int | None = None) -> dict:
    config = PipelineSettings(_env_file=None, web_base_url=base_url)
    base = config.web_base_url or 'http://localhost:8000/'
    public = config.web_base_url is not None
    channel = channel_link(channel_id) if isinstance(channel_id, str) and channel_id else ''
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    series_list = await db.list_series()
    hubs = json.loads(await db.get_state('seo_hubs') or '[]')
    previews = json.loads(await db.get_state('web_audio_previews') or '{}')
    chapter_previews=json.loads(await db.get_state('chapter_audio_previews') or '{}')
    cards, files = [], []
    for series in series_list:
        link = bot_link(bot_username, series['id'])
        filename = f"series_{series['id']}.html"
        published=await db.list_chapters(series['id'])
        chapters = [await db.get_chapter(c['id']) for c in published
                    if c['chapter_num'] <= min(2, series['free_chapters_limit'])]
        chapter_links=[]
        for row in published:
            chapter=await db.get_chapter(row['id'])
            chapter_name=f"series_{series['id']}_ch_{chapter['chapter_num']}.html"
            chapter_url=urljoin(base,chapter_name)
            direct=f"https://t.me/{bot_username}?start=ch_{chapter['id']}"
            title=f"{series['title']} — Глава {chapter['chapter_num']}: {chapter['title']} | Читать и слушать онлайн на русском"
            preview=chapter_previews.get(str(chapter['id']))
            if preview:
                audio=audio_preview_html(series,[chapter],{str(series['id']):preview},direct)
            else:
                audio=audio_preview_html(series,chapters,previews,direct)
                audio=audio.replace('Послушайте начало истории','Аудиознакомство с новеллой')
            excerpt=chapter['content_text'][:1200]
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
            body=(f'<div class="hero"><div class="eyebrow"><a href="index.html">Библиотека</a> / '
                  f'<a href="{filename}">{escape(series["title"])}</a></div><h1>Глава {chapter["chapter_num"]}: '
                  f'{escape(chapter["title"])}</h1><p>{escape(series["title"])}</p>{cta}</div>'
                  f'<article class="reader">{audio}<section class="chapter"><p>{escape(excerpt)}</p>'
                  '<blockquote>…Система скрывает исход этой сцены. Продолжение уже ждёт в боте.</blockquote>'
                  f'{cta}<p>🎧 Полная аудиоглава и текст — в Telegram. Первая платная глава: 1 ⭐️; VIP на 24 часа: 19 ⭐️.</p>'
                  '</section></article>')
            (directory/chapter_name).write_text(shell(title,excerpt[:300],body,canonical=chapter_url if public else '',
                channel_url=channel,schema=graph,sticky=f'<aside class="sticky">{cta}</aside>'),encoding='utf-8')
            files.append(chapter_name)
            chapter_links.append(f'<li><a href="{chapter_name}">Глава {chapter["chapter_num"]}: {escape(chapter["title"])}</a></li>')
        chapter_nav='<section class="links"><h2>Все главы: читать и слушать</h2><ol>'+''.join(chapter_links)+'</ol></section>'
        schema = {'@context': 'https://schema.org', '@type': 'Book', 'name': series['title'],
                  'inLanguage': 'ru', 'genre': ['ЛитРПГ', 'Веб-новелла'], 'description': series['description'],
                  'publisher': {'@type': 'Organization', 'name': 'NovelPulse'},
                  'sameAs': [url for url in (series['source_url'], series['telegraph_url']) if url]}
        if public:
            schema['url'] = urljoin(base, filename)
        article = (f'<div class="hero"><div class="eyebrow">Библиотека / ЛитРПГ и новые миры</div>'
                   f'<h1>{escape(series["title"])}</h1><p>{escape(series["description"])}</p>'
                   '<span class="pill">Первые главы бесплатно</span><span class="pill">Перевод на русский</span></div>'
                   '<article class="reader">' + chapter_nav + audio_preview_html(series,chapters,previews,link) + ''.join(chapter_html(c) for c in chapters) +
                   f'<div class="links"><h2>История продолжается…</h2><p>Следующая глава уже ждёт в Telegram. '
                   f'Новому читателю первая платная глава — всего 1 ⭐️.</p><a class="cta" href="{link}">Читать дальше в @{escape(bot_username)}</a></div></article>')
        sticky = f'<aside class="sticky"><span>🔥 Первая платная глава — 1 ⭐️<br>👑 VIP на все книги — 49 ⭐️ / 7 дней</span><a class="cta" href="{link}">Открыть продолжение ➡</a></aside>'
        target = directory / filename
        target.write_text(shell(series['title'] + ' — читать на русском | NovelPulse', series['description'], article,
            canonical=urljoin(base, filename) if public else '', channel_url=channel, schema=schema, sticky=sticky), encoding='utf-8')
        files.append(filename)
        iv = f'<a href="{escape(series["telegraph_url"])}">Instant View ↗</a>' if series['telegraph_url'] else ''
        cards.append(f'<section class="card"><div class="eyebrow">Мир {series["id"]:02d} · Первые главы бесплатно</div>'
                     f'<h2><a href="{filename}">{escape(series["title"])}</a></h2><p>{escape(complete_excerpt(series["description"], 550))}</p>'
                     f'<details><summary>Главы · {len(published)}</summary><ol>{"".join(chapter_links)}</ol></details>'
                     f'<div class="actions"><a class="cta" href="{link}">Читать в @{escape(bot_username)}</a>{iv}</div></section>')
    hub_links = ''.join(f'<a href="{escape(h["url"])}">{escape(h["title"])} ↗</a>' for h in hubs)
    body = ('<section class="hero"><div class="eyebrow">ЛитРПГ · Исэкай · Система</div>'
            '<h1>Ещё одна глава.<br>Ещё один новый мир.</h1><p>Герои нулевого ранга, опасные системы и приключения, '
            'от которых трудно оторваться. Откройте библиотеку русских переводов — первые главы читаются бесплатно.</p>'
            f'<span class="pill">{len(series_list)} новелл</span><span class="pill">Первая покупка — 1 ⭐️</span></section>'
            '<div class="grid">' + ''.join(cards) + '</div>' +
            f'<section class="links"><h2>Подборки для следующего приключения</h2>{hub_links}</section>')
    (directory / 'index.html').write_text(shell('NovelPulse — веб-новеллы и ЛитРПГ на русском',
        'Библиотека русских переводов: ЛитРПГ, Исэкай, Система. Первые главы бесплатно, продолжение в Telegram.',
        body, canonical=urljoin(base, 'index.html') if public else '', channel_url=channel), encoding='utf-8')
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
    report = {'series_count': len(series_list), 'files': files + ['sitemap.xml', 'robots.txt', 'DEPLOY.md'],
              'directory': str(directory.resolve()), 'base_url': base, 'public_base_configured': public}
    await db.set_state('web_portal_report', json.dumps(report, ensure_ascii=False))
    return report


async def _cli(args):
    settings = PipelineSettings()
    db = Database(settings.db_path)
    await db.open()
    try:
        report = await generate_web_portal(db, args.bot_username, output_dir=args.output_dir,
            base_url=args.base_url or settings.web_base_url, channel_id=args.channel)
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
