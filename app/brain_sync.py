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
                "'factory_metrics','catalog_snapshot_generated_at')")}
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
    images = sum(1 for p in data_root.rglob('*') if p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp'))
    text = '\n'.join([
        ROLE, '', f'Срез UTC: {datetime.now(timezone.utc).isoformat()}',
        'Это передача контекста, а не новая авторизация на публикации. Сначала читай актуальные файлы и журнал.', '',
        'ЖИВЫЕ МЕТРИКИ',
        f"Новеллы: {catalog['count']}; опубликованные главы: {chapters['count']}; розничная стоимость: {chapters['retail']} ⭐️.",
        f"Читатели: {users['count']}; записи purchases (включая бонусные): {purchases['count']}; выручка: {purchases['stars']} ⭐️.",
        f"Shorts: pending={counts.get('pending',0)}, uploaded={counts.get('uploaded',0)}; остальные статусы: "
            + (', '.join(f'{k}={v}' for k,v in sorted(counts.items()) if k not in ('pending','uploaded')) or 'нет') + factory_note,
        f'YouTube cooldown UTC: {cooldown or "нет"}. Не загружать до истечения cooldown; максимум 4 за скользящие 24 ч.',
        f'Файлы рядом с SQLite: MP4={mp4}, MP3={mp3}, временные изображения={images}.',
        f"Последний сохранённый статус кассы: {cloud.get('active_node','нет данных')}; "
        f"cloud={cloud.get('cloud_runner',{}).get('mode','не подтверждён')}; heartbeat UTC={cloud.get('heartbeat_at','нет')}.",
        'Сохранённый статус не заменяет live-проверку GitHub/Watchdog.', '',
        'СТРОГИЕ ИНВАРИАНТЫ',
        '1. После каждой задачи 0 MP4, 0 MP3 и 0 временных изображений в data/. Генерация JIT → доставка → удаление.',
        '2. NovelPulse_Watchdog каждые 5 минут; один polling worker pythonw.exe, detached. '
        'Тяжёлое видео — изолированный subprocess. Историческая локальная dispatcher p95 ≈43 мс; это не гарантия сетевой задержки.',
        '3. Публичный Night-Watch остаётся HOT_STANDBY, cashier и каталог зашифрованы Fernet. '
        'Цель быстрого переключения ≈20 с; последнее контролируемое наблюдение Stage 21 — 37.5 с при graceful stop. '
        'Heartbeat 45 с, lease TTL 120 с, опрос 20 с. Не обещай 20 с при внезапном отключении.',
        '4. Viral Video Engine 2.0: 24 FPS, Shock-First с 0.0s без чтения заголовка, '
        'DmitryNeural + SvetlanaNeural, karaoke, Solo-Leveling HUD, Zero-402 art, MAX_YOUTUBE_UPLOADS_PER_24H=4.',
        '5. /brain_sync и /ceo_brief доступны только ADMIN_IDS. Не публиковать .env, токены, ID читателей или незашифрованную кассу.',
        '6. Стрик считается по UTC. Третий последовательный день даёт один ключ; повторное нажатие не даёт награду.', '',
        'РЕАЛИЗОВАННЫЕ ЭТАПЫ 1–23 (проверяй live-результаты по журналам соответствующего этапа)', *STAGES, '',
        'ПУБЛИЧНЫЕ ТОЧКИ ВХОДА',
        'Бот: https://t.me/NovelPulseBot · Канал: https://t.me/novelpulse_litrpg',
        'Портал: https://mayonezka.github.io/novelpulse-library/', '',
        'СЛЕДУЮЩИЕ ВЕКТОРЫ РОСТА',
        'Измерять конверсию peek → Stars → VIP и удержание D1/D3/D7; тестировать hooks по фактическим просмотрам.',
        'Углублять востребованные новеллы, измерять выдачу и использование streak-ключей.',
        'После cooldown дренировать очередь по pacer, сверять remote receipts и не дублировать Telegram-посты.',
        'Перед любым live-обещанием проверять watchdog, GitHub job, аренду, очередь, cooldown и остаточные media.',
        'Проверки Stage 22: ' + state.get('stage22_verification', 'см. data/stage22_full_tests.log и live-отчёт; ещё не зафиксированы.'), '',
    ])
    await asyncio.to_thread(_atomic_write, Path(output_path) if output_path is not None else HANDOFF_PATH, text)
    return text


async def brain_sync(message, db) -> None:
    text = await generate_orchestrator_handoff(db)
    for chunk in split_text(text):
        await message.answer(chunk, parse_mode=None)
