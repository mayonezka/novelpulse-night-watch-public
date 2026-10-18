"""Lightweight, secret-free handoff for either orchestrator and either cashier node."""

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.text import split_text

ROOT = Path(__file__).resolve().parents[1]
HANDOFF_PATH = ROOT / 'ORCHESTRATOR_STATE.md'
ROLE = ('Ты — Главный ИИ-Оркестратор автономной фермы NovelPulse (F:/MyProjects/farm-unit-1). '
        'Владелец управляет тобой как директор, а код пишут воркеры Codex и Antigravity.')
MODEL_TRAFFIC_LIGHT = ("ВАЖНОЕ ПРАВИЛО ДЛЯ ОРКЕСТРАТОРА: В конце каждого ответа обязательно пиши владельцу строку "
    "'🚦 Режим модели на следующий шаг: ВКЛЮЧАЙ ⚡ FLASH' (для обычных отчётов и плановых шагов) или "
    "'🔴 ВКЛЮЧАЙ 🧠 ARGON' (только если возникла сложная архитектурная авария или проектируется новый модуль с нуля).")
STAGES = (
    '1. Telegram-каталог, чтение, SQLite и оплата Stars.',
    '2. Русский интерфейс, импорт и перевод, пагинация, промо.',
    '3. VIP, рефералы, ключи и автопостинг канала.',
    '4. Пакетный перевод с резервными провайдерами и traffic pack.',
    '5. Нейронная озвучка, вертикальный MP4, уведомления читателям.',
    '6. Telegraph, библиотека SEO и автопилот.',
    '7. Первая платная глава за 1 Star, SEO-хабы, навигатор канала.',
    '8. Напоминания о paywall, сериал в канале, HTML-портал.',
    '9. Публичный портал и очередь трёхчастных Shorts.',
    '10. Русские названия файлов, крупные субтитры, поиск Telegram.',
    '11. JIT-видео, CDP YouTube, удаление после доставки.',
    '12. Автопилот, push-алерты владельцу, пополнение контента.',
    '13. Автозапуск Windows, catch-up и разделение ролей NODE_MODE.',
    '14. Night-Watch, аренда polling и синхронизация кассы.',
    '15. Watchdog, скрытый pythonw.exe и Fernet-шифрование.',
    '16. Viral Video Engine 2.0: движение, karaoke, SFX, две роли озвучки.',
    '17. Тёплый HOT_STANDBY, relay workflow и отдельный процесс рендера.',
    '18. Резервные статические art-провайдеры, RPG HUD, первый комментарий.',
    '19. Shock-First, VIP 24 часа за 19 Stars, интерактивные опросы.',
    '20. Углубление каталога, релизный дайджест, pacer 4 YouTube / 24 ч.',
    '21. Одноразовый deep-peek платной главы, премьера «Очевидно, святой».',
    '22. /brain_sync, /ceo_brief, ежедневный стрик и возобновление очереди после cooldown.',
    '23. Аудиоглавы по запросу с проверкой доступа, подкаст в Telegram, HTML5-аудиопревью без локальных MP3.',
    '24. Ранги F→S, XP за уникальные события, VIP E-ранга за 9 Stars, страницы глав с аудио и JSON-LD.',
    '25. IndexNow: проверка публичного ключа, пакетная подача портала после деплоя, утренний аудит и правило Flash/Argon.',
    '26. Одноразовый ключ и +25 XP за проверенную подписку на канал; атомарная награда и синхронизация PC/Cloud.',
    '27. /quiz: четыре класса, одноразовый ключ +25 XP, пересылка результата и referral loop; YouTube после успеха каждые 6 часов.',
    '28. Жанровые полки, /start с быстрым возвратом, личная библиотека и честный прогресс завершённых глав; перенос в Cloud.',
    '29. Wait-for-Free: одна бесплатная глава каждые 24 часа, таймер paywall, +15 XP и атомарный перенос в Cloud.',
    '30. Персонализированный «Дайджест недели NovelPulse», анти-флуд рассылка 0.05с, /send_weekly_digest и автопилот 7 дней.',
    '31. Углубление каталога, новый RoyalRoad Trending, chapter SEO, IndexNow и обновление навигатора.',
    '32. Карточка ранга 9:16 для Telegram Stories и одноразовый сундук: +2 ключа и +50 XP.',
    '33. Главы 7–10 для серий 7–8, импорт девятой записи каталога, Chapter SEO, IndexNow и обновление навигатора.',
    '34. Win32 shutdown/suspend handover, PC heartbeat 20 с / lease 45 с и Night-Watch ETag polling 6 с.',
    '35. Каталог 10 новелл / 85 глав, углубление Series 9, два Trending-импорта, 96 HTML-страниц, IndexNow, навигатор и Fernet-синхронизация.',
    '36. Реальные оценки и отзывы, +15 XP однократно за книгу, разрешение на публикацию отзыва, зашифрованные черновики и облачная синхронизация.',
    '37. 24 жанровых SEO-маршрута, 120 HTML-страниц, RSS с постоянными аудиопревью, двусторонний Рейд, горизонтальный аудиоформат и поисковое описание канала.',
    '38. Зал Славы с приватными именами и отдельными учебными NPC, поиск по сюжетам и заголовкам в боте и на сайте, мост Telegraph → библиотека/жанры/RSS.',
    '39. Воронка наблюдаемых событий, четыре входа /start, XP-only quiz/Stories и Рейд после доставки главы 1; быстрые и полные тесты.',
    '40. Общие внешние MP3 и CSS/JS, web/RSS атрибуция, шесть полных оригинальных глав и технический SEO-аудит.',
    '41. Горизонтальные аудиокниги 1280×720, полные главы 1–2, темп +8%, yt_long атрибуция и отдельные статусы загрузки/закрепления/Related Video.',
    '42. Каталог 12 новелл / 108 глав: углубление Series 10–11, два Trending-импорта, лёгкий портал, IndexNow, навигатор и Fernet-синхронизация.',
    '43. Каталог 14 новелл / 128 глав, честный retail при цене 10 Stars, 170 страниц с аудиотекой и руководством, IndexNow, навигатор и Fernet-синхронизация.',
    '44. Замороженный каталог 14/128, чистые заголовки глав, честные подписи общего аудиотрейлера, Google verification и свежая наблюдаемая воронка.',
    '45. Второй горизонтальный аудиопилот стандартного канала: измеренный лимит менее 15 минут, естественная речь +8%, отдельная версия первой главы и три тизера для Telegram.',
)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.orchestrator-', suffix='.tmp', delete=False) as stream:
            name = stream.name
            stream.write(text)
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


async def generate_orchestrator_handoff(db, *, output_path: str | Path | None = None) -> str:
    """Snapshot aggregates only; never export reader identities, credentials or paid text."""
    async with db._lock:
        await db.connection.execute('BEGIN')
        try:
            catalog = await db._one('SELECT COUNT(*) AS count FROM series')
            chapters = await db._one('SELECT COUNT(*) AS count, COALESCE(SUM(CASE WHEN '
                'c.chapter_num>s.free_chapters_limit THEN c.price_stars ELSE 0 END),0) AS retail '
                'FROM chapters c JOIN series s ON s.id=c.series_id WHERE c.is_published=1')
            users = await db._one('SELECT COUNT(*) AS count FROM users')
            purchases = await db._one('SELECT COUNT(*) AS count,COALESCE(SUM(amount_stars),0) AS stars FROM purchases')
            shorts = await db._all('SELECT status,COUNT(*) AS count FROM shorts_queue GROUP BY status')
            state = {r['key']: r['value'] for r in await db._all("SELECT key,value FROM app_state WHERE key IN "
                "('youtube_quota_cooldown_until','youtube_upload_blocked_until','cloud_status','stage22_verification',"
                "'factory_metrics','catalog_snapshot_generated_at','stage24_verification','stage25_verification','stage26_verification','stage27_verification','stage28_verification','stage29_verification','stage30_verification','stage31_verification','stage32_verification','stage33_verification','stage34_verification','stage35_verification','stage36_verification','stage37_verification','stage38_verification','stage39_verification','stage40_verification','stage41_verification','stage42_verification','stage43_verification','stage44_verification','stage45_verification')")}
            await db.connection.commit()
        except BaseException:
            await db.connection.rollback()
            raise
    counts = {r['status']: r['count'] for r in shorts}
    cooldown = max((state.get(k, '') for k in ('youtube_quota_cooldown_until', 'youtube_upload_blocked_until')), default='')
    factory_note = ''
    if not counts and state.get('factory_metrics'):
        factory = json.loads(state['factory_metrics'])
        counts = {k: factory[k] for k in ('pending', 'uploaded')}
        cooldown = factory.get('cooldown_until') or cooldown
        factory_note = ' (снимок ПК UTC: ' + state.get('catalog_snapshot_generated_at', 'неизвестно') + ')'
    # Include only known non-sensitive status fields, never the whole state document.
    try:
        cloud = json.loads(state.get('cloud_status', '{}'))
    except ValueError:
        cloud = {}
    data_root = Path(db.path).resolve().parent
    mp4 = sum(1 for _ in data_root.rglob('*.mp4'))
    mp3 = sum(1 for _ in data_root.rglob('*.mp3'))
    png = sum(1 for p in data_root.rglob('*') if p.suffix.lower() == '.png')
    images = sum(1 for p in data_root.rglob('*') if p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'))
    from app.analytics import funnel_text
    funnel = funnel_text(await db.funnel_summary())
    text = '\n'.join([
        ROLE, '', MODEL_TRAFFIC_LIGHT, '', f'Срез UTC: {datetime.now(timezone.utc).isoformat()}',
        'Это передача контекста, а не новая авторизация на публикации. Сначала читай актуальные файлы и журнал.', '',
        'ЖИВЫЕ МЕТРИКИ',
        f"Новеллы: {catalog['count']}; опубликованные главы: {chapters['count']}; розничная стоимость: {chapters['retail']} ⭐️.",
        f"Читатели: {users['count']}; записи purchases (включая бонусные): {purchases['count']}; выручка: {purchases['stars']} ⭐️.",
        f"Shorts: pending={counts.get('pending',0)}, uploaded={counts.get('uploaded',0)}; остальные статусы: "
            + (', '.join(f'{k}={v}' for k,v in sorted(counts.items()) if k not in ('pending','uploaded')) or 'нет') + factory_note,
        f'YouTube cooldown UTC: {cooldown or "нет"}. Не загружать до истечения cooldown; максимум 4 за скользящие 24 ч.',
        f'Файлы рядом с SQLite: MP4={mp4}, MP3={mp3}, PNG={png}, временные изображения={images}.',
        f"Последний сохранённый статус кассы: {cloud.get('active_node','нет данных')}; "
        f"cloud={cloud.get('cloud_runner',{}).get('mode','не подтверждён')}; heartbeat UTC={cloud.get('heartbeat_at','нет')}.",
        'Сохранённый статус не заменяет live-проверку GitHub/Watchdog.', '',
        funnel, '',
        'Текущая экономика Stage 39: quiz и Stories — только +50 XP; Рейд — ключи обоим после доставки главы 1. Старые ключи сохранены. /start — четыре входа; бонусы в профиле.', '',
        'СТРОГИЕ ИНВАРИАНТЫ',
        '1. После каждой задачи 0 MP4, 0 MP3, 0 PNG и 0 временных изображений в data/. '
        'Story-карточки создаются вне репозитория, доставляются и удаляются в finally.',
        '2. NovelPulse_Watchdog каждые 5 минут; один polling worker pythonw.exe, detached. '
        'Тяжёлое видео — изолированный subprocess. Историческая локальная dispatcher p95 ≈43 мс; это не гарантия сетевой задержки.',
        '3. Публичный Night-Watch остаётся HOT_STANDBY, cashier и каталог зашифрованы Fernet. '
        'Win32 shutdown/logoff/suspend hook ограничивает освобождение PC lease двумя секундами; '
        'heartbeat 20 с, TTL 45 с, cloud ETag polling 6 с. Фактический takeover указывай только по live-замеру.',
        '4. Viral Video Engine 2.0: 24 FPS, Shock-First с 0.0s без чтения заголовка, '
        'DmitryNeural + SvetlanaNeural, karaoke, Solo-Leveling HUD, Zero-402 art, MAX_YOUTUBE_UPLOADS_PER_24H=4.',
        '5. /brain_sync и /ceo_brief доступны только ADMIN_IDS. Не публиковать .env, токены, ID читателей или незашифрованную кассу.',
        '6. Стрик считается по UTC. Третий последовательный день даёт один ключ; повторное нажатие не даёт награду.', '',
        f'РЕАЛИЗОВАННЫЕ ЭТАПЫ 1–{len(STAGES)} (проверяй live-результаты по журналам соответствующего этапа)', *STAGES, '',
        'ПУТИВНЫЕ ТОЧКИ ВХОДА' if False else 'ПУБЛИЧНЫЕ ТОЧКИ ВХОДА',
        'Бот: https://t.me/NovelPulseBot · Канал: https://t.me/novelpulse_litrpg',
        'Портал: https://novelpulse-books.github.io/novelpulse-library/', '',
        'СЛЕДУЮЩИЕ ВЕКТОРЫ РОСТА',
        'Измерять конверсию peek → Stars → VIP и удержание D1/D3/D7; тестировать hooks по фактическим просмотрам.',
        'Углублять востребованные новеллы, измерять выдачу и использование streak-ключей.',
        'После cooldown дренировать очередь по pacer, сверять remote receipts и не дублировать Telegram-посты.',
        'Перед любым live-обещанием проверять watchdog, GitHub job, аренду, очередь, cooldown и остаточные media.',
        'Проверки Stage 22: ' + state.get('stage22_verification', 'см. data/stage22_full_tests.log и live-отчёт; ещё не зафиксированы.'), '',
        'Проверки Stage 24: ' + state.get('stage24_verification','ещё не зафиксированы; см. data/stage24_full_tests.log и live-отчёт.'), '',
        'Проверки Stage 25: ' + state.get('stage25_verification','ещё не зафиксированы; см. data/stage25_audit.json и live-отчёт.'), '',
        'Проверки Stage 26: ' + state.get('stage26_verification','ещё не зафиксированы; см. data/stage26_full_tests.log и live-отчёт.'), '',
        'Проверки Stage 27: ' + state.get('stage27_verification','ещё не зафиксированы; см. data/stage27_full_tests.log и live-отчёт.'), '',
        'Проверки Stage 28: ' + state.get('stage28_verification','ещё не зафиксированы; см. data/stage28_full_tests.log и live-отчёт.'), '',
        'Проверки Stage 29: ' + state.get('stage29_verification','ещё не зафиксированы; см. data/stage29_full_tests.log и live-отчёт.'), '',
        'Проверки Stage 30: ' + state.get('stage30_verification','ещё не зафиксированы; см. data/stage30_full_tests.log и live-отчёт.'), '',
        'Проверки Stage 31: ' + state.get('stage31_verification','ещё не зафиксированы; см. data/stage31_live_report.json и live-отчёт.'), '',
        'Проверки Stage 32: ' + state.get('stage32_verification','ещё не зафиксированы; см. data/stage32_full_tests.log и live-отчёт.'), '',
        'Проверки Stage 34: ' + state.get('stage34_verification','ещё не зафиксированы; см. data/stage34_full_tests.log и live-отчёт.'), '',
        'Проверки Stage 35: ' + state.get('stage35_verification','ещё не зафиксированы; см. data/stage35_live_report.json.'), '',
        'Проверки Stage 36: ' + state.get('stage36_verification','ещё не зафиксированы; см. data/stage36_live_report.json.'), '',
        'Проверки Stage 37: ' + state.get('stage37_verification','ещё не зафиксированы; см. data/stage37_live_report.json.'), '',
        'Проверки Stage 44: ' + state.get('stage44_verification','ещё не зафиксированы; см. data/stage44_live_report.json.'), '',
        'Проверки Stage 45: ' + state.get('stage45_verification','подготовка; см. data/stage45_prepare_report.json.'), '',
        'Проверки Stage 43: ' + state.get('stage43_verification','ещё не зафиксированы; см. data/stage43_live_report.json.'), '',
        'Проверки Stage 42: ' + state.get('stage42_verification','ещё не зафиксированы; см. data/stage42_live_report.json.'), '',
        'Проверки Stage 41: ' + state.get('stage41_verification','ещё не зафиксированы; см. data/stage41_live_report.json.'), '',
        'Проверки Stage 40: ' + state.get('stage40_verification','ещё не зафиксированы; см. data/stage40_live_report.json.'), '',
        'Проверки Stage 39: ' + state.get('stage39_verification','ещё не зафиксированы; см. data/stage39_live_report.json.'), '',
        'Проверки Stage 38: ' + state.get('stage38_verification','ещё не зафиксированы; см. data/stage38_live_report.json.'), '',
    ])
    await asyncio.to_thread(_atomic_write, Path(output_path) if output_path is not None else HANDOFF_PATH, text)
    return text


async def brain_sync(message, db) -> None:
    text = await generate_orchestrator_handoff(db)
    for chunk in split_text(text):
        await message.answer(chunk, parse_mode=None)
