"""Useful reader navigation pages, enabled per catalog without inline media."""
from urllib.parse import urljoin

READER_PAGES = ('new_releases.html', 'audio_library.html', 'reading_guide.html')


def render_reader_sections(books, previews, username, base, channel_url):
    from app.web_portal import shell, escape
    from app.podcast_feed import podcast_assets
    available_audio = podcast_assets(books, previews)
    navigation = ('<nav><a href="new_releases.html">Новые поступления</a>'
        '<a href="audio_library.html">Аудиотека</a><a href="reading_guide.html">Как читать бесплатно</a></nav>')
    def reading_link(book):
        return f'https://t.me/{username}?start=web_s_{book["series"]["id"]}'
    arrivals = []
    for book in sorted(books, key=lambda b:b['series']['id'], reverse=True)[:2]:
        series = book['series']
        from app.promotional_text import complete_excerpt
        excerpt = complete_excerpt(book['chapters'][0]['content_text'],400) if book['chapters'] else ''
        iv = (f'<a href="{escape(series["telegraph_url"])}">Бесплатное начало в Instant View</a>'
              if series.get('telegraph_url') else '')
        arrivals.append(f'<article class="card"><div class="eyebrow">{escape(series["genre_title"])}</div>'
            f'<h2><a href="series_{series["id"]}.html">{escape(series["title"])}</a></h2>'
            f'<p>{escape(complete_excerpt(series["description"],400))}</p><blockquote>{escape(excerpt)}</blockquote>'
            f'<p>Опубликовано глав: {book["chapter_count"]}. Первые две — бесплатно.</p>'
            f'{iv}<a class="cta" href="{escape(reading_link(book))}">Начать читать в Telegram</a></article>')
    audio = []
    for book in books:
        series=book['series'];filename=f'podcast_{series["id"]}.mp3'
        preview = (f'<audio controls preload="none" src="{filename}" '
            f'aria-label="Начало новеллы {escape(series["title"])}"></audio>') if filename in available_audio else '<p>Полная озвучка — по запросу в боте.</p>'
        audio.append(f'<section class="audio-preview"><h2><a href="series_{series["id"]}.html">'
            f'{escape(series["title"])}</a></h2>{preview}<a class="cta" href="{escape(reading_link(book))}">'
            'Читать и слушать полные главы</a></section>')
    total=sum(book['chapter_count'] for book in books)
    guide=(f'<p>В библиотеке {len(books)} новелл и {total} опубликованных глав. Выберите книгу по жанру '
        'в каталоге, прочитайте первые две главы и послушайте короткое аудиопревью. Полный текст и '
        'озвучка продолжения доступны в Telegram-боте.</p>'
        '<h2>Как продолжить бесплатно</h2><p>На закрытой главе бот показывает суточный таймер: '
        'он позволяет открыть одну главу каждые 24 часа. Уже открытая глава остаётся доступной. '
        'Дождаться следующего открытия можно без оплаты.</p>'
        '<p>Бонусный ключ открывает одну платную главу. Получайте ключи за ежедневный стрик '
        'или подтверждённую подписку на канал. Совместный Рейд награждает вас и приглашённого друга '
        'после того, как друг впервые прочитает первую главу. Тест класса и карточка Stories дают XP.</p>'
        '<h2>Если хочется читать без ожидания</h2><p>Первая платная глава новому покупателю стоит '
        '1 Telegram Star. Обычная цена указана на кнопке каждой главы; купленный текст остаётся '
        'доступным бессрочно. VIP открывает весь каталог на срок подписки: 24 часа — 19 Stars, '
        '7 дней — 49 Stars, 30 дней — 149 Stars. Для читателей ранга E и выше бот предлагает '
        'суточный VIP за 9 Stars.</p>'
        '<h2>Как слушать и возвращаться к книге</h2><p>Откройте доступную главу и нажмите '
        '«Слушать аудиокнигу». Бот проверяет те же права доступа, что и для текста. '
        'Кнопка «Читать / Продолжить» возвращает к последней главе, а «Моя полка» хранит начатые книги. '
        'Профиль показывает ранг, XP, ключи и прогресс чтения.</p>'
        f'<a class="cta" href="https://t.me/{escape(username)}?start=web_s_1">Открыть библиотеку в боте</a>')
    definitions = (
        ('new_releases.html','Новые новеллы NovelPulse — свежие переводы и бесплатное начало',
         'Две последние новеллы каталога: жанры, описания, бесплатные отрывки и ссылки на полные главы в Telegram.',
         '<div class="grid">'+''.join(arrivals)+'</div>'),
        ('audio_library.html','Аудиотека NovelPulse — слушать русские веб-новеллы',
         'Короткие аудиопревью всех книг NovelPulse: выберите голос и историю, затем слушайте главы полностью в Telegram.',
         ''.join(audio)),
        ('reading_guide.html','Как читать NovelPulse бесплатно — таймер, ключи и VIP',
         'Практическое руководство NovelPulse: две бесплатные главы, суточный таймер, бонусные ключи, Stars и аудиокниги.',guide))
    return {filename:shell(title,description,
        f'<section class="hero"><h1>{escape(title)}</h1>{navigation}</section>'+body,
        canonical=urljoin(base,filename),channel_url=channel_url,
        schema={'@context':'https://schema.org','@type':'CollectionPage','name':title,'url':urljoin(base,filename)})
        for filename,title,description,body in definitions}
