"""SQLite persistence with versioned migrations and serialized transactions."""

import asyncio
import secrets
import sqlite3
from functools import wraps
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from app.monetization import parse_vip_payload


def durable_write(method):
    @wraps(method)
    async def invoke(self, *args, **kwargs):
        before=None
        if self.durability_hook is not None:
            # Read a committed baseline, never another coroutine's uncommitted clock.
            async with self._lock:
                before=(await self._one('SELECT revision FROM cashier_clock WHERE id=1'))['revision']
        result = await method(self, *args, **kwargs)
        if self.durability_hook is not None:
            async with self._lock:
                after=(await self._one('SELECT revision FROM cashier_clock WHERE id=1'))['revision']
            if before is None or after!=before:
                await self.durability_hook()
        return result
    return invoke


MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS users (
        tg_id INTEGER PRIMARY KEY,
        username TEXT,
        balance_stars INTEGER NOT NULL DEFAULT 0 CHECK(balance_stars >= 0),
        is_vip_until TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE TABLE IF NOT EXISTS series (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        free_chapters_limit INTEGER NOT NULL DEFAULT 0 CHECK(free_chapters_limit >= 0)
    );
    CREATE TABLE IF NOT EXISTS chapters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id INTEGER NOT NULL REFERENCES series(id),
        chapter_num INTEGER NOT NULL CHECK(chapter_num > 0),
        title TEXT NOT NULL,
        content_text TEXT NOT NULL,
        price_stars INTEGER NOT NULL DEFAULT 1 CHECK(price_stars >= 1),
        is_published INTEGER NOT NULL DEFAULT 0 CHECK(is_published IN (0, 1)),
        UNIQUE(series_id, chapter_num)
    );
    CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER NOT NULL REFERENCES users(tg_id),
        chapter_id INTEGER NOT NULL REFERENCES chapters(id),
        amount_stars INTEGER NOT NULL CHECK(amount_stars > 0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX IF NOT EXISTS purchases_user_chapter ON purchases(tg_id, chapter_id);
    CREATE INDEX IF NOT EXISTS purchases_created_at ON purchases(created_at);
    """,
    """
    ALTER TABLE purchases ADD COLUMN telegram_payment_charge_id TEXT;
    CREATE UNIQUE INDEX purchases_charge ON purchases(telegram_payment_charge_id);
    CREATE TABLE payment_orders (
        payload TEXT PRIMARY KEY,
        tg_id INTEGER NOT NULL REFERENCES users(tg_id),
        chapter_id INTEGER NOT NULL REFERENCES chapters(id),
        amount_stars INTEGER NOT NULL CHECK(amount_stars > 0),
        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'approved', 'paid')),
        pre_checkout_id TEXT,
        approved_at TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    """,
    """
    ALTER TABLE series ADD COLUMN source_url TEXT;
    ALTER TABLE series ADD COLUMN translation_mode TEXT NOT NULL DEFAULT 'manual';
    CREATE UNIQUE INDEX series_source_url ON series(source_url);
    ALTER TABLE chapters ADD COLUMN source_url TEXT;
    ALTER TABLE chapters ADD COLUMN translation_mode TEXT NOT NULL DEFAULT 'manual';
    CREATE UNIQUE INDEX chapters_source_url ON chapters(series_id, source_url);
    """,
    """
    ALTER TABLE purchases RENAME TO purchases_stage3_legacy;
    CREATE TABLE purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER NOT NULL REFERENCES users(tg_id),
        chapter_id INTEGER NOT NULL,
        amount_stars INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        telegram_payment_charge_id TEXT,
        kind TEXT NOT NULL DEFAULT 'chapter' CHECK(kind IN ('chapter', 'vip', 'bonus')),
        vip_days INTEGER,
        vip_until TEXT,
        chapter_ref INTEGER GENERATED ALWAYS AS (NULLIF(chapter_id, 0)) VIRTUAL REFERENCES chapters(id),
        CHECK((kind = 'vip' AND chapter_id = 0 AND amount_stars > 0 AND vip_days IN (7, 30))
           OR (kind = 'chapter' AND chapter_id > 0 AND amount_stars > 0)
           OR (kind = 'bonus' AND chapter_id > 0 AND amount_stars = 0))
    );
    INSERT INTO purchases(id, tg_id, chapter_id, amount_stars, created_at, telegram_payment_charge_id)
        SELECT id, tg_id, chapter_id, amount_stars, created_at, telegram_payment_charge_id FROM purchases_stage3_legacy;
    DROP TABLE purchases_stage3_legacy;
    CREATE INDEX purchases_user_chapter ON purchases(tg_id, chapter_id);
    CREATE INDEX purchases_created_at ON purchases(created_at);
    CREATE UNIQUE INDEX purchases_charge ON purchases(telegram_payment_charge_id);
    CREATE TABLE vip_orders (
        tg_id INTEGER NOT NULL REFERENCES users(tg_id),
        payload TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'approved', 'paid')),
        pre_checkout_id TEXT,
        approved_at TEXT,
        PRIMARY KEY(tg_id, payload)
    );
    """,
    """
    CREATE TABLE reading_progress (
        tg_id INTEGER NOT NULL REFERENCES users(tg_id),
        series_id INTEGER NOT NULL REFERENCES series(id),
        chapter_num INTEGER NOT NULL CHECK(chapter_num > 0),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        PRIMARY KEY(tg_id, series_id)
    );
    CREATE TABLE reader_notifications (
        tg_id INTEGER NOT NULL REFERENCES users(tg_id),
        series_id INTEGER NOT NULL REFERENCES series(id),
        chapter_num INTEGER NOT NULL,
        sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        PRIMARY KEY(tg_id, series_id)
    );
    """,
    """
    CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE channel_posts (
        channel_id TEXT NOT NULL,
        series_id INTEGER NOT NULL REFERENCES series(id),
        state TEXT NOT NULL CHECK(state IN ('pending', 'sent', 'failed', 'uncertain')),
        message_id INTEGER,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        PRIMARY KEY(channel_id, series_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS channel_videos (
        channel_id TEXT NOT NULL,
        series_id INTEGER NOT NULL REFERENCES series(id),
        state TEXT NOT NULL CHECK(state IN ('pending', 'sent', 'failed', 'uncertain')),
        message_id INTEGER,
        PRIMARY KEY(channel_id, series_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS paywall_views (
        id INTEGER PRIMARY KEY,
        tg_id INTEGER NOT NULL REFERENCES users(tg_id),
        chapter_id INTEGER NOT NULL REFERENCES chapters(id),
        series_id INTEGER NOT NULL REFERENCES series(id),
        viewed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        reminded INTEGER NOT NULL DEFAULT 0 CHECK(reminded IN (0,1)),
        followup_state TEXT NOT NULL DEFAULT 'pending'
            CHECK(followup_state IN ('pending','sending','sent','suppressed','blocked','failed','uncertain')),
        reminded_at TEXT,
        message_id INTEGER,
        UNIQUE(tg_id, chapter_id)
    );
    CREATE INDEX IF NOT EXISTS paywall_pending ON paywall_views(reminded, viewed_at);
    CREATE TABLE IF NOT EXISTS channel_serial_posts (
        channel_id TEXT NOT NULL,
        chapter_id INTEGER NOT NULL REFERENCES chapters(id),
        series_id INTEGER NOT NULL REFERENCES series(id),
        state TEXT NOT NULL CHECK(state IN ('pending','sent','failed','uncertain')),
        message_id INTEGER,
        posted_at TEXT,
        PRIMARY KEY(channel_id, chapter_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS shorts_queue (
        id INTEGER PRIMARY KEY,
        series_id INTEGER NOT NULL REFERENCES series(id),
        episode_num INTEGER NOT NULL CHECK(episode_num BETWEEN 1 AND 3),
        video_path TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','sending','sent','uploaded','uncertain')),
        uploaded_at TEXT,
        delivered_at TEXT,
        message_id INTEGER,
        UNIQUE(series_id, episode_num)
    );
    CREATE INDEX IF NOT EXISTS shorts_pending ON shorts_queue(status, episode_num, series_id);
    """,
    """
    CREATE TABLE IF NOT EXISTS short_publications (
        short_id INTEGER NOT NULL REFERENCES shorts_queue(id),
        provider TEXT NOT NULL CHECK(provider IN ('youtube','telegram')),
        destination TEXT NOT NULL,
        remote_id TEXT NOT NULL,
        published_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY(short_id, provider, destination)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_alerts (
        event_key TEXT NOT NULL, admin_id INTEGER NOT NULL, message_text TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('sending','sent','failed','uncertain')),
        message_id INTEGER, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY(event_key,admin_id)
    );
    """,
    """
    INSERT INTO app_state(key,value)
        SELECT 'last_youtube_upload_at', MAX(published_at) FROM short_publications
        WHERE provider='youtube' HAVING MAX(published_at) IS NOT NULL
        ON CONFLICT(key) DO NOTHING;
    """,
    """
    CREATE TABLE IF NOT EXISTS cashier_clock(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0);
    INSERT INTO cashier_clock(id,revision) VALUES(1,0) ON CONFLICT(id) DO NOTHING;
    CREATE TABLE IF NOT EXISTS processed_updates(update_id INTEGER PRIMARY KEY, processed_at TEXT NOT NULL);
    """,
    """
    CREATE TABLE IF NOT EXISTS catalog_clock(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0);
    INSERT INTO catalog_clock(id,revision) VALUES(1,0) ON CONFLICT(id) DO NOTHING;
    """,
    """
    CREATE TABLE purchases_stage19 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER NOT NULL REFERENCES users(tg_id),
        chapter_id INTEGER NOT NULL,
        amount_stars INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        telegram_payment_charge_id TEXT,
        kind TEXT NOT NULL DEFAULT 'chapter' CHECK(kind IN ('chapter', 'vip', 'bonus')),
        vip_days INTEGER,
        vip_until TEXT,
        chapter_ref INTEGER GENERATED ALWAYS AS (NULLIF(chapter_id, 0)) VIRTUAL REFERENCES chapters(id),
        CHECK((kind = 'vip' AND chapter_id = 0 AND amount_stars > 0 AND vip_days IN (1, 7, 30))
           OR (kind = 'chapter' AND chapter_id > 0 AND amount_stars > 0)
           OR (kind = 'bonus' AND chapter_id > 0 AND amount_stars = 0))
    );
    INSERT INTO purchases_stage19(id,tg_id,chapter_id,amount_stars,created_at,telegram_payment_charge_id,kind,vip_days,vip_until)
        SELECT id,tg_id,chapter_id,amount_stars,created_at,telegram_payment_charge_id,kind,vip_days,vip_until FROM purchases;
    DROP TABLE purchases;
    ALTER TABLE purchases_stage19 RENAME TO purchases;
    CREATE INDEX purchases_user_chapter ON purchases(tg_id, chapter_id);
    CREATE INDEX purchases_created_at ON purchases(created_at);
    CREATE UNIQUE INDEX purchases_charge ON purchases(telegram_payment_charge_id);
    CREATE TABLE IF NOT EXISTS short_story_polls (
        short_id INTEGER NOT NULL REFERENCES shorts_queue(id),
        destination TEXT NOT NULL,
        video_message_id INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('sending','sent','failed','uncertain')),
        message_id INTEGER,
        error_class TEXT,
        PRIMARY KEY(short_id,destination)
    );
    """,
    """
    ALTER TABLE paywall_views ADD COLUMN peek_used INTEGER NOT NULL DEFAULT 0 CHECK(peek_used IN (0,1));
    DROP TRIGGER IF EXISTS cashier_paywall_views_update;
    """,
    """
    ALTER TABLE users ADD COLUMN streak_days INTEGER NOT NULL DEFAULT 0 CHECK(streak_days BETWEEN 0 AND 2);
    ALTER TABLE users ADD COLUMN last_checkin_date TEXT NOT NULL DEFAULT '';
    DROP TRIGGER IF EXISTS cashier_users_update;
    """,
    """
    CREATE TABLE IF NOT EXISTS xp_events (
        tg_id INTEGER NOT NULL REFERENCES users(tg_id), event_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('read','checkin','referral','purchase','bootstrap')),
        amount INTEGER NOT NULL CHECK(amount>=0), PRIMARY KEY(tg_id,event_key)
    );
    CREATE TABLE IF NOT EXISTS rank_awards (
        tg_id INTEGER NOT NULL REFERENCES users(tg_id), rank TEXT NOT NULL,
        keys_awarded INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0 CHECK(notified IN (0,1,2)),
        PRIMARY KEY(tg_id,rank)
    );
    DROP TRIGGER IF EXISTS cashier_users_update;
    """,
)


def active_vip(until: str | None) -> bool:
    if not until:
        return False
    try:
        expiry = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry > datetime.now(timezone.utc)
    except ValueError:
        return False


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self.seo_lock = asyncio.Lock()
        self.autopilot_lock = asyncio.Lock()
        self.durability_hook = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database has not been opened")
        return self._connection

    async def open(self) -> None:
        if self._connection is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.path, isolation_level=None)
        self.connection.row_factory = aiosqlite.Row
        try:
            await self.connection.execute("PRAGMA foreign_keys = ON")
            await self.connection.execute("PRAGMA journal_mode = WAL")
            await self.connection.execute("PRAGMA busy_timeout = 5000")
            await self.connection.execute("BEGIN IMMEDIATE")
            row = await self._one("PRAGMA user_version")
            version = row["user_version"]
            if version > len(MIGRATIONS):
                raise RuntimeError("Database schema is newer than this application")
            for index in range(version, len(MIGRATIONS)):
                if index == 17:
                    columns = {row['name'] for row in await self._all('PRAGMA table_info(users)')}
                    for name, definition in (
                        ('referred_by','INTEGER'),
                        ('free_unlock_credits','INTEGER NOT NULL DEFAULT 0 CHECK(free_unlock_credits>=0)'),
                        ('streak_days','INTEGER NOT NULL DEFAULT 0 CHECK(streak_days BETWEEN 0 AND 2)'),
                        ('last_checkin_date',"TEXT NOT NULL DEFAULT ''"),
                        ('player_xp','INTEGER NOT NULL DEFAULT 0 CHECK(player_xp>=0)'),
                        ('player_rank',"TEXT NOT NULL DEFAULT 'F' CHECK(player_rank IN ('F','E','D','C','B','A','S'))"),
                        ('chapters_read_count','INTEGER NOT NULL DEFAULT 0 CHECK(chapters_read_count>=0)')):
                        if name not in columns:
                            await self.connection.execute(f'ALTER TABLE users ADD COLUMN {name} {definition}')
                if index == 16:
                    columns = {row['name'] for row in await self._all('PRAGMA table_info(users)')}
                    if 'streak_days' not in columns:
                        await self.connection.execute('ALTER TABLE users ADD COLUMN streak_days INTEGER NOT NULL DEFAULT 0 CHECK(streak_days BETWEEN 0 AND 2)')
                    if 'last_checkin_date' not in columns:
                        await self.connection.execute("ALTER TABLE users ADD COLUMN last_checkin_date TEXT NOT NULL DEFAULT ''")
                    await self.connection.execute('DROP TRIGGER IF EXISTS cashier_users_update')
                    await self.connection.execute(f'PRAGMA user_version = {index + 1}')
                    continue
                if index == 15:
                    columns = {row['name'] for row in await self._all('PRAGMA table_info(paywall_views)')}
                    if 'peek_used' in columns:
                        await self.connection.execute('DROP TRIGGER IF EXISTS cashier_paywall_views_update')
                        await self.connection.execute(f'PRAGMA user_version = {index + 1}')
                        continue
                if index == 6:
                    columns = {row["name"] for row in await self._all("PRAGMA table_info(payment_orders)")}
                    if "is_tripwire" not in columns:
                        await self.connection.execute("ALTER TABLE payment_orders ADD COLUMN is_tripwire INTEGER NOT NULL DEFAULT 0 CHECK(is_tripwire IN (0,1))")
                    if "base_price" not in columns:
                        await self.connection.execute("ALTER TABLE payment_orders ADD COLUMN base_price INTEGER")
                        await self.connection.execute("UPDATE payment_orders SET base_price = amount_stars")
                if index == 5:
                    columns = {row["name"] for row in await self._all("PRAGMA table_info(series)")}
                    if "telegraph_url" not in columns:
                        await self.connection.execute("ALTER TABLE series ADD COLUMN telegraph_url TEXT NOT NULL DEFAULT ''")
                if index == 3:
                    columns = {row["name"] for row in await self._all("PRAGMA table_info(users)")}
                    if "referred_by" not in columns:
                        await self.connection.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
                    if "free_unlock_credits" not in columns:
                        await self.connection.execute("ALTER TABLE users ADD COLUMN free_unlock_credits INTEGER NOT NULL DEFAULT 0 CHECK(free_unlock_credits >= 0)")
                statement = ""
                for line in MIGRATIONS[index].splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        await self.connection.execute(statement)
                        statement = ""
                await self.connection.execute(f"PRAGMA user_version = {index + 1}")
            from app.cloud_state import TABLES
            for table in (*TABLES, 'series', 'chapters'):
                columns = [r['name'] for r in await self._all(f'PRAGMA table_info({table})')]
                changed = ' OR '.join(f'OLD.{c} IS NOT NEW.{c}' for c in columns)
                for event in ('INSERT', 'UPDATE', 'DELETE'):
                    condition = f' WHEN {changed}' if event == 'UPDATE' else ''
                    clock = 'catalog_clock' if table in ('series','chapters') else 'cashier_clock'
                    prefix = 'catalog' if table in ('series','chapters') else 'cashier'
                    await self.connection.execute(f'CREATE TRIGGER IF NOT EXISTS {prefix}_{table}_{event.lower()} '
                        f'AFTER {event} ON {table}{condition} BEGIN '
                        f'UPDATE {clock} SET revision=revision+1 WHERE id=1; END')
            await self.connection.commit()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @durable_write
    async def claim_daily_gift(self, tg_id: int, *, now: datetime | None = None) -> dict:
        """One UTC check-in per reader per date, serialized across connections/nodes."""
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            raise ValueError('Check-in clock must be timezone-aware')
        today = clock.astimezone(timezone.utc).date()
        async with self._lock:
            await self.connection.execute('BEGIN IMMEDIATE')
            try:
                user = await self._one('SELECT * FROM users WHERE tg_id=?', (tg_id,))
                if user is None:
                    raise ValueError('Reader must register before checking in')
                last = user['last_checkin_date']
                # A future date must not enable duplicate rewards after clock rollback.
                if last and last >= today.isoformat():
                    result = {'claimed': False, 'rewarded': False, 'streak_days': user['streak_days']}
                else:
                    consecutive = last == (today - timedelta(days=1)).isoformat()
                    day = user['streak_days'] + 1 if consecutive else 1
                    reward = day == 3
                    await self.connection.execute(
                        'UPDATE users SET streak_days=?,last_checkin_date=?,free_unlock_credits=free_unlock_credits+? WHERE tg_id=?',
                        (0 if reward else day, today.isoformat(), int(reward), tg_id))
                    await self._award_xp(tg_id, 'checkin:'+today.isoformat(), 'checkin')
                    result = {'claimed': True, 'rewarded': reward, 'streak_days': 0 if reward else day}
                await self.connection.commit()
                return result
            except BaseException:
                await self.connection.rollback()
                raise

    async def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        async with self.connection.execute(sql, args) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        async with self.connection.execute(sql, args) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def _award_xp(self, tg_id: int, key: str, kind: str) -> bool:
        """Called only within the source event's BEGIN IMMEDIATE transaction."""
        from app.gamification import XP_EVENTS, RANKS, rank_for_xp
        amount = XP_EVENTS[kind]
        cursor = await self.connection.execute('INSERT INTO xp_events(tg_id,event_key,kind,amount) '
            'VALUES (?,?,?,?) ON CONFLICT(tg_id,event_key) DO NOTHING',(tg_id,key,kind,amount))
        if cursor.rowcount != 1:return False
        user = await self._one('SELECT player_xp FROM users WHERE tg_id=?',(tg_id,))
        xp = user['player_xp']+amount
        await self.connection.execute('UPDATE users SET player_xp=?,player_rank=?,chapters_read_count=chapters_read_count+? WHERE tg_id=?',
            (xp,rank_for_xp(xp)['rank'],int(kind=='read'),tg_id))
        for rank, threshold, _, keys in RANKS[1:]:
            if xp < threshold:break
            award = await self.connection.execute('INSERT INTO rank_awards(tg_id,rank,keys_awarded) VALUES (?,?,?) '
                'ON CONFLICT(tg_id,rank) DO NOTHING',(tg_id,rank,keys))
            if award.rowcount == 1 and keys:
                await self.connection.execute('UPDATE users SET free_unlock_credits=free_unlock_credits+? WHERE tg_id=?',(keys,tg_id))
        return True

    @durable_write
    async def backfill_player_progress(self) -> int:
        """Bootstrap each pre-RPG reader once from available legacy progress/receipts."""
        async with self._lock:
            await self.connection.execute('BEGIN IMMEDIATE')
            try:
                users = await self._all("SELECT * FROM users u WHERE NOT EXISTS(SELECT 1 FROM xp_events e WHERE e.tg_id=u.tg_id AND e.event_key='bootstrap')")
                for user in users:
                    tg_id=user['tg_id']
                    await self._award_xp(tg_id,'bootstrap','bootstrap')
                    # Legacy progress stored a high-water mark, not per-chapter receipts.
                    reads=await self._all('SELECT c.id FROM reading_progress r JOIN chapters c ON c.series_id=r.series_id '
                        'AND c.chapter_num<=r.chapter_num WHERE r.tg_id=? AND c.is_published=1',(tg_id,))
                    for row in reads:await self._award_xp(tg_id,f"read:{row['id']}",'read')
                    payments=await self._all('SELECT id,telegram_payment_charge_id FROM purchases WHERE tg_id=? AND amount_stars>0',(tg_id,))
                    for row in payments:
                        await self._award_xp(tg_id,'purchase:'+(row['telegram_payment_charge_id'] or f"legacy:{row['id']}"),'purchase')
                    referrals=await self._all('SELECT tg_id FROM users WHERE referred_by=?',(tg_id,))
                    for row in referrals:await self._award_xp(tg_id,f"referral:{row['tg_id']}",'referral')
                    if user['last_checkin_date']:
                        await self._award_xp(tg_id,'checkin:'+user['last_checkin_date'],'checkin')
                await self.connection.commit()
                return len(users)
            except BaseException:
                await self.connection.rollback();raise

    @durable_write
    async def claim_rank_banners(self, tg_id):
        async with self._lock:
            await self.connection.execute('BEGIN IMMEDIATE')
            try:
                rows=await self._all('SELECT * FROM rank_awards WHERE tg_id=? AND notified=0 ORDER BY rowid',(tg_id,))
                await self.connection.execute('UPDATE rank_awards SET notified=1 WHERE tg_id=? AND notified=0',(tg_id,))
                await self.connection.commit();return rows
            except BaseException:
                await self.connection.rollback();raise

    @durable_write
    async def finish_rank_banners(self, tg_id, ranks):
        async with self._lock:
            await self.connection.executemany('UPDATE rank_awards SET notified=2 WHERE tg_id=? AND rank=? AND notified=1',
                                            [(tg_id,rank) for rank in ranks])

    async def _validate_vip_discount(self,tg_id,price):
        if price==9:
            user=await self._one('SELECT player_xp FROM users WHERE tg_id=?',(tg_id,))
            if user is None or user['player_xp']<50:
                raise ValueError('Секретный VIP за 9 ⭐️ доступен с E-ранга (50 XP).')

    @durable_write
    async def ensure_user(self, tg_id: int, username: str | None, referred_by: int | None = None, *,
                          registration_details: bool = False):
        """Award a referral only on first registration; return the credited inviter."""
        async with self._lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                existing = await self._one("SELECT tg_id FROM users WHERE tg_id = ?", (tg_id,))
                inviter = None
                if not existing and referred_by and referred_by != tg_id:
                    inviter = await self._one("SELECT tg_id FROM users WHERE tg_id = ?", (referred_by,))
                if existing:
                    await self.connection.execute("UPDATE users SET username = ? WHERE tg_id = ?", (username, tg_id))
                else:
                    await self.connection.execute("INSERT INTO users(tg_id, username, referred_by) VALUES (?, ?, ?)",
                                                   (tg_id, username, referred_by if inviter else None))
                    await self._award_xp(tg_id,'bootstrap','bootstrap')
                if inviter:
                    await self.connection.execute("UPDATE users SET free_unlock_credits = free_unlock_credits + 1 WHERE tg_id = ?", (referred_by,))
                    await self._award_xp(referred_by,f'referral:{tg_id}','referral')
                await self.connection.commit()
                result = referred_by if inviter else None
                return (result, existing is None) if registration_details else result
            except BaseException:
                await self.connection.rollback()
                raise

    async def get_user(self, tg_id: int) -> dict[str, Any] | None:
        async with self._lock:
            return await self._one("SELECT * FROM users WHERE tg_id = ?", (tg_id,))

    @durable_write
    async def record_read(self, tg_id: int, series_id: int, chapter_num: int, *, completed: bool = True) -> None:
        """Called only after an accessible chapter was delivered in a private chat."""
        async with self._lock:
            await self.connection.execute('BEGIN IMMEDIATE')
            try:
                chapter = await self._one('SELECT id FROM chapters WHERE series_id=? AND chapter_num=?',(series_id,chapter_num))
                if chapter is None or not await self._can_read(tg_id,await self._chapter(chapter['id'])):
                    raise ValueError('Нельзя начислить XP за недоступную главу')
                await self.connection.execute(
                    "INSERT INTO reading_progress(tg_id, series_id, chapter_num) VALUES (?, ?, ?) "
                    "ON CONFLICT(tg_id, series_id) DO UPDATE SET "
                    "chapter_num = MAX(reading_progress.chapter_num, excluded.chapter_num), "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')", (tg_id, series_id, chapter_num))
                if completed:await self._award_xp(tg_id,f"read:{chapter['id']}",'read')
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback();raise

    async def readers_to_notify(self, series_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            return await self._all(
                "SELECT r.tg_id, r.chapter_num AS read_chapter, MAX(c.chapter_num) AS latest_chapter "
                "FROM reading_progress r JOIN chapters c ON c.series_id = r.series_id AND c.is_published = 1 "
                "LEFT JOIN reader_notifications n ON n.tg_id = r.tg_id AND n.series_id = r.series_id "
                "WHERE r.series_id = ? GROUP BY r.tg_id "
                "HAVING r.chapter_num < MAX(c.chapter_num) AND COALESCE(n.chapter_num, 0) < MAX(c.chapter_num)",
                (series_id,))

    async def mark_notified(self, tg_id: int, series_id: int, chapter_num: int) -> None:
        async with self._lock:
            await self.connection.execute(
                "INSERT INTO reader_notifications(tg_id, series_id, chapter_num) VALUES (?, ?, ?) "
                "ON CONFLICT(tg_id, series_id) DO UPDATE SET "
                "chapter_num = MAX(reader_notifications.chapter_num, excluded.chapter_num), "
                "sent_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')", (tg_id, series_id, chapter_num))

    @durable_write
    async def unlock_with_credit(self, tg_id: int, chapter_id: int) -> bool:
        async with self._lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                chapter = await self._chapter(chapter_id)
                if not chapter or not chapter["is_published"]:
                    raise ValueError("Эта глава пока недоступна.")
                if await self._can_read(tg_id, chapter):
                    await self._suppress_paywalls(tg_id, chapter_id)
                    await self.connection.commit()
                    return False
                checkout = await self._one(
                    "SELECT payload FROM payment_orders WHERE tg_id = ? AND chapter_id = ? AND state = 'approved' "
                    "AND datetime(approved_at) > datetime('now', '-10 minutes')", (tg_id, chapter_id))
                if checkout:
                    raise ValueError("Оплата этой главы уже обрабатывается. Дождитесь её завершения.")
                async with self.connection.execute(
                    "UPDATE users SET free_unlock_credits = free_unlock_credits - 1 WHERE tg_id = ? AND free_unlock_credits > 0", (tg_id,)
                ) as cursor:
                    if cursor.rowcount != 1:
                        raise ValueError("Бонусных ключей пока нет. Пригласите друга, чтобы получить ключ!")
                await self.connection.execute("INSERT INTO purchases(tg_id, chapter_id, amount_stars, kind) VALUES (?, ?, 0, 'bonus')", (tg_id, chapter_id))
                await self._suppress_paywalls(tg_id, chapter_id)
                await self.connection.commit()
                return True
            except BaseException:
                await self.connection.rollback()
                raise

    @durable_write
    async def prepare_vip_order(self, tg_id: int, payload: str) -> None:
        _, price = parse_vip_payload(payload)
        async with self._lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                await self._validate_vip_discount(tg_id,price)
                recent = await self._one("SELECT payload FROM vip_orders WHERE tg_id = ? AND payload = ? AND state = 'approved' AND datetime(approved_at) > datetime('now', '-10 minutes')", (tg_id, payload))
                if recent:
                    raise ValueError("Этот платёж VIP уже обрабатывается. Дождитесь его завершения.")
                await self.connection.execute(
                    "INSERT INTO vip_orders(tg_id, payload) VALUES (?, ?) ON CONFLICT(tg_id, payload) DO UPDATE SET "
                    "state = 'pending', pre_checkout_id = NULL, approved_at = NULL", (tg_id, payload))
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    @durable_write
    async def approve_vip_order(self, tg_id: int, payload: str, currency: str, amount: int, query_id: str) -> None:
        _, price = parse_vip_payload(payload)
        if currency != "XTR" or amount != price:
            raise ValueError("Валюта или сумма не соответствует тарифу VIP.")
        async with self._lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                await self._validate_vip_discount(tg_id,price)
                order = await self._one("SELECT * FROM vip_orders WHERE tg_id = ? AND payload = ?", (tg_id, payload))
                if not order or order["state"] == "paid":
                    raise ValueError("Откройте меню VIP и запросите новый счёт.")
                if order["state"] == "approved" and order["pre_checkout_id"] != query_id:
                    recent = await self._one("SELECT payload FROM vip_orders WHERE tg_id = ? AND payload = ? AND datetime(approved_at) > datetime('now', '-10 minutes')", (tg_id, payload))
                    if recent:
                        raise ValueError("Этот счёт VIP уже обрабатывается.")
                await self.connection.execute(
                    "UPDATE vip_orders SET state = 'approved', pre_checkout_id = ?, approved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE tg_id = ? AND payload = ?",
                    (query_id, tg_id, payload))
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    @durable_write
    async def record_vip_payment(self, tg_id: int, payload: str, currency: str, amount: int, charge_id: str) -> tuple[str, bool]:
        days, price = parse_vip_payload(payload)
        if currency != "XTR" or amount != price or not charge_id:
            raise ValueError("Платёж не соответствует тарифу VIP.")
        async with self._lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                user = await self._one("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
                prior = await self._one("SELECT * FROM purchases WHERE telegram_payment_charge_id = ?", (charge_id,))
                if prior:
                    if (prior["tg_id"], prior["kind"], prior["vip_days"], prior["amount_stars"]) != (tg_id, "vip", days, price):
                        raise ValueError("Этот платёж уже связан с другой покупкой.")
                    if user and active_vip(user["is_vip_until"]):
                        await self._suppress_paywalls(tg_id)
                    await self.connection.commit()
                    return user["is_vip_until"] or prior["vip_until"], False
                order = await self._one("SELECT * FROM vip_orders WHERE tg_id = ? AND payload = ?", (tg_id, payload))
                if not user or not order or order["state"] != "approved":
                    raise ValueError("Платёж VIP не соответствует одобренному счёту.")
                now = datetime.now(timezone.utc)
                base = now
                if active_vip(user["is_vip_until"]):
                    base = datetime.fromisoformat(user["is_vip_until"].replace("Z", "+00:00"))
                    if base.tzinfo is None:
                        base = base.replace(tzinfo=timezone.utc)
                    base = max(base.astimezone(timezone.utc), now)
                until = (base + timedelta(days=days)).isoformat()
                await self.connection.execute("UPDATE users SET is_vip_until = ? WHERE tg_id = ?", (until, tg_id))
                await self.connection.execute(
                    "INSERT INTO purchases(tg_id, chapter_id, amount_stars, telegram_payment_charge_id, kind, vip_days, vip_until) VALUES (?, 0, ?, ?, 'vip', ?, ?)",
                    (tg_id, price, charge_id, days, until))
                await self.connection.execute("UPDATE vip_orders SET state = 'paid' WHERE tg_id = ? AND payload = ?", (tg_id, payload))
                await self._award_xp(tg_id,'purchase:'+charge_id,'purchase')
                await self._suppress_paywalls(tg_id)
                await self.connection.commit()
                return until, True
            except BaseException:
                await self.connection.rollback()
                raise

    async def list_series(self) -> list[dict[str, Any]]:
        async with self._lock:
            return await self._all(
                "SELECT s.* FROM series s WHERE EXISTS "
                "(SELECT 1 FROM chapters c WHERE c.series_id = s.id AND c.is_published = 1) "
                "ORDER BY s.id"
            )

    async def get_series(self, series_id: int) -> dict[str, Any] | None:
        async with self._lock:
            return await self._one("SELECT * FROM series WHERE id = ?", (series_id,))

    async def list_all_series(self) -> list[dict[str, Any]]:
        async with self._lock:
            return await self._all("SELECT * FROM series ORDER BY id")

    async def set_telegraph_url(self, series_id: int, url: str) -> None:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "telegra.ph" or not parsed.path.strip("/") or parsed.query or parsed.fragment:
            raise ValueError("Telegraph вернул некорректный адрес статьи")
        async with self._lock:
            await self.connection.execute("UPDATE series SET telegraph_url = ? WHERE id = ?", (url, series_id))

    async def get_state(self, key: str) -> str | None:
        async with self._lock:
            row = await self._one("SELECT value FROM app_state WHERE key = ?", (key,))
            return row["value"] if row else None

    async def set_state(self, key: str, value: str) -> None:
        async with self._lock:
            await self.connection.execute("INSERT INTO app_state(key, value) VALUES (?, ?) "
                                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    async def latest_unposted_series(self, channel_id: str | int) -> dict | None:
        async with self._lock:
            return await self._one(
                "SELECT s.* FROM series s LEFT JOIN channel_posts p ON p.series_id = s.id AND p.channel_id = ? "
                "WHERE s.telegraph_url != '' AND (p.state IS NULL OR p.state = 'failed') "
                "AND EXISTS (SELECT 1 FROM chapters c WHERE c.series_id = s.id AND c.is_published = 1) "
                "ORDER BY s.id DESC LIMIT 1", (str(channel_id),))

    async def claim_channel_post(self, channel_id: str | int, series_id: int) -> bool:
        async with self._lock:
            async with self.connection.execute(
                "INSERT INTO channel_posts(channel_id, series_id, state) VALUES (?, ?, 'pending') "
                "ON CONFLICT(channel_id, series_id) DO UPDATE SET state = 'pending', "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE channel_posts.state = 'failed'",
                (str(channel_id), series_id)) as cursor:
                return cursor.rowcount == 1

    async def finish_channel_post(self, channel_id: str | int, series_id: int, state: str, message_id: int | None = None) -> None:
        if state not in ("sent", "failed", "uncertain"):
            raise ValueError("Некорректный статус публикации")
        async with self._lock:
            await self.connection.execute(
                "UPDATE channel_posts SET state = ?, message_id = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE channel_id = ? AND series_id = ? AND state = 'pending'",
                (state, message_id, str(channel_id), series_id))

    async def claim_channel_video(self, channel_id: str | int, series_id: int) -> bool:
        async with self._lock:
            async with self.connection.execute(
                "INSERT INTO channel_videos(channel_id,series_id,state) VALUES (?,?,'pending') "
                "ON CONFLICT(channel_id,series_id) DO UPDATE SET state = 'pending' WHERE channel_videos.state = 'failed'",
                (str(channel_id), series_id)) as cursor:
                return cursor.rowcount == 1

    async def finish_channel_video(self, channel_id: str | int, series_id: int, state: str, message_id: int | None = None):
        if state not in ("sent", "failed", "uncertain"):
            raise ValueError("Некорректный статус видео")
        async with self._lock:
            await self.connection.execute("UPDATE channel_videos SET state = ?, message_id = ? WHERE channel_id = ? AND series_id = ? AND state = 'pending'",
                                          (state, message_id, str(channel_id), series_id))

    async def channel_showcase_messages(self, channel_id: str | int) -> dict:
        async with self._lock:
            return {"teasers": await self._all("SELECT series_id, state, message_id FROM channel_posts WHERE channel_id = ?", (str(channel_id),)),
                    "videos": await self._all("SELECT series_id, state, message_id FROM channel_videos WHERE channel_id = ?", (str(channel_id),))}

    async def dashboard_stats(self) -> dict:
        async with self._lock:
            counts = await self._one("SELECT COUNT(*) AS series_count FROM series")
            chapters = await self._one(
                "SELECT COUNT(*) AS chapters, COALESCE(SUM(c.chapter_num <= s.free_chapters_limit),0) AS free, "
                "COALESCE(SUM(c.chapter_num > s.free_chapters_limit),0) AS paid, "
                "COALESCE(SUM(CASE WHEN c.chapter_num > s.free_chapters_limit THEN c.price_stars ELSE 0 END),0) AS retail "
                "FROM chapters c JOIN series s ON s.id = c.series_id WHERE c.is_published = 1")
            readers = await self._one("SELECT COUNT(*) AS users, COUNT(referred_by) AS referrals FROM users")
            revenue = await self._one("SELECT COALESCE(SUM(amount_stars),0) AS revenue FROM purchases")
            channel = await self._all("SELECT state, COUNT(*) AS count FROM channel_posts GROUP BY state")
            return {**counts, **chapters, **readers, **revenue, "channel_posts": channel}

    async def latest_imported_series(self) -> dict[str, Any] | None:
        async with self._lock:
            return await self._one(
                "SELECT s.* FROM series s WHERE s.source_url IS NOT NULL "
                "AND EXISTS(SELECT 1 FROM chapters c WHERE c.series_id = s.id AND c.is_published = 1) ORDER BY s.id DESC LIMIT 1")

    async def promo_hook(self, series_id: int) -> str | None:
        async with self._lock:
            row = await self._one(
                "SELECT c.content_text FROM chapters c JOIN series s ON s.id = c.series_id "
                "WHERE s.id = ? AND c.is_published = 1 "
                "ORDER BY (c.chapter_num > s.free_chapters_limit) DESC, c.chapter_num LIMIT 1", (series_id,)
            )
            return row["content_text"][:300] + "…" if row else None

    async def has_purchase(self, tg_id: int, chapter_id: int) -> bool:
        async with self._lock:
            return await self._one("SELECT id FROM purchases WHERE tg_id = ? AND chapter_id = ? LIMIT 1",
                                   (tg_id, chapter_id)) is not None

    async def _has_user_purchased(self, tg_id: int) -> bool:
        return await self._one("SELECT id FROM purchases WHERE tg_id = ? LIMIT 1", (tg_id,)) is not None

    async def has_user_purchased(self, tg_id: int) -> bool:
        async with self._lock:
            return await self._has_user_purchased(tg_id)

    async def chapter_offer(self, tg_id: int, chapter: dict) -> dict:
        async with self._lock:
            first = not await self._has_user_purchased(tg_id)
            discounted = first and chapter["price_stars"] > 1
            return {"price": 1 if discounted else chapter["price_stars"],
                    "regular_price": chapter["price_stars"], "tripwire": discounted, "has_purchased": not first}

    async def series_by_source(self, source_url: str) -> dict[str, Any] | None:
        async with self._lock:
            return await self._one("SELECT * FROM series WHERE source_url = ?", (source_url,))

    async def chapter_by_source(self, series_id: int, source_url: str) -> dict[str, Any] | None:
        async with self._lock:
            return await self._one("SELECT * FROM chapters WHERE series_id = ? AND source_url = ?",
                                   (series_id, source_url))

    async def save_series(self, title: str, slug: str, description: str, source_url: str, mode: str, free_chapters_limit: int = 3) -> int:
        if free_chapters_limit < 0:
            raise ValueError("Лимит бесплатных глав не может быть отрицательным")
        async with self._lock:
            await self.connection.execute(
                "INSERT INTO series(title, slug, description, free_chapters_limit, source_url, translation_mode) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_url) DO UPDATE SET title = excluded.title, description = excluded.description, "
                "translation_mode = excluded.translation_mode WHERE "
                "(series.translation_mode IN ('mock', 'passthrough') AND excluded.translation_mode IN ('gtx', 'llm')) "
                "OR (series.translation_mode = 'gtx' AND excluded.translation_mode = 'llm')",
                (title, slug, description, free_chapters_limit, source_url, mode)
            )
            row = await self._one("SELECT id FROM series WHERE source_url = ?", (source_url,))
            return row["id"]

    async def save_pipeline_chapter(self, series_id: int, chapter_num: int, title: str,
                                    content: str, source_url: str, mode: str, published: bool) -> int:
        if chapter_num < 1 or not title.strip() or not content.strip():
            raise ValueError("Номер главы должен быть положительным; название и текст не могут быть пустыми")
        async with self._lock:
            await self.connection.execute(
                "INSERT INTO chapters(series_id, chapter_num, title, content_text, price_stars, "
                "is_published, source_url, translation_mode) VALUES (?, ?, ?, ?, 10, ?, ?, ?) "
                "ON CONFLICT(series_id, source_url) DO UPDATE SET title = excluded.title, content_text = excluded.content_text, "
                "translation_mode = excluded.translation_mode, is_published = excluded.is_published "
                "WHERE (chapters.translation_mode IN ('mock', 'passthrough') AND excluded.translation_mode IN ('gtx', 'llm')) "
                "OR (chapters.translation_mode = 'gtx' AND excluded.translation_mode = 'llm')",
                (series_id, chapter_num, title, content, int(published), source_url, mode),
            )
            row = await self._one("SELECT id FROM chapters WHERE series_id = ? AND source_url = ?", (series_id, source_url))
            return row["id"]

    async def list_chapters(self, series_id: int) -> list[dict[str, Any]]:
        async with self._lock:
            return await self._all(
                "SELECT id, chapter_num, title, price_stars FROM chapters "
                "WHERE series_id = ? AND is_published = 1 ORDER BY chapter_num", (series_id,)
            )

    async def _chapter(self, chapter_id: int) -> dict[str, Any] | None:
        return await self._one(
            "SELECT c.*, s.title AS series_title, s.free_chapters_limit "
            "FROM chapters c JOIN series s ON s.id = c.series_id WHERE c.id = ?", (chapter_id,)
        )

    async def get_chapter(self, chapter_id: int, *, include_unpublished: bool = False) -> dict[str, Any] | None:
        async with self._lock:
            chapter = await self._chapter(chapter_id)
            return chapter if chapter and (include_unpublished or chapter["is_published"]) else None

    async def next_chapter(self, series_id: int, chapter_num: int) -> dict[str, Any] | None:
        async with self._lock:
            return await self._one(
                "SELECT id FROM chapters WHERE series_id = ? AND chapter_num > ? "
                "AND is_published = 1 ORDER BY chapter_num LIMIT 1", (series_id, chapter_num)
            )

    async def _can_read(self, tg_id: int, chapter: dict[str, Any]) -> bool:
        if chapter["chapter_num"] <= chapter["free_chapters_limit"]:
            return True
        user = await self._one("SELECT is_vip_until FROM users WHERE tg_id = ?", (tg_id,))
        if user and active_vip(user["is_vip_until"]):
            return True
        return await self._one(
            "SELECT id FROM purchases WHERE tg_id = ? AND chapter_id = ? LIMIT 1",
            (tg_id, chapter["id"]),
        ) is not None

    async def can_read(self, tg_id: int, chapter: dict[str, Any]) -> bool:
        async with self._lock:
            return await self._can_read(tg_id, chapter)

    @durable_write
    async def create_order(self, tg_id: int, chapter_id: int) -> dict[str, Any]:
        async with self._lock:
            chapter = await self._chapter(chapter_id)
            if not chapter or not chapter["is_published"]:
                raise ValueError("Эта глава пока недоступна.")
            if await self._can_read(tg_id, chapter):
                raise ValueError("У вас уже есть доступ к этой главе.")
            payload = "chapter_" + secrets.token_hex(16)
            tripwire = not await self._has_user_purchased(tg_id) and chapter["price_stars"] > 1
            amount = 1 if tripwire else chapter["price_stars"]
            await self.connection.execute(
                "INSERT INTO payment_orders(payload, tg_id, chapter_id, amount_stars, is_tripwire, base_price) VALUES (?, ?, ?, ?, ?, ?)",
                (payload, tg_id, chapter_id, amount, int(tripwire), chapter["price_stars"]),
            )
            await self.connection.commit()
            return {"payload": payload, "amount_stars": amount, "is_tripwire": tripwire}

    @durable_write
    async def approve_order(self, payload: str, tg_id: int, currency: str, amount: int, query_id: str) -> None:
        async with self._lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                order = await self._one("SELECT * FROM payment_orders WHERE payload = ?", (payload,))
                if (not order or order["tg_id"] != tg_id or currency != "XTR"
                        or order["amount_stars"] != amount or order["state"] == "paid"):
                    raise ValueError("Некорректный счёт. Откройте главу и попробуйте снова.")
                recent = await self._one(
                    "SELECT payload FROM payment_orders WHERE payload = ? "
                    "AND state = 'approved' AND datetime(approved_at) > datetime('now', '-10 minutes')",
                    (payload,),
                )
                if recent and order["pre_checkout_id"] != query_id:
                    raise ValueError("Этот счёт уже обрабатывается.")
                chapter = await self._chapter(order["chapter_id"])
                if not chapter or not chapter["is_published"] or chapter["price_stars"] != order["base_price"]:
                    raise ValueError("Глава или цена изменились. Запросите новый счёт.")
                if order["is_tripwire"]:
                    if amount != 1 or await self._has_user_purchased(tg_id):
                        raise ValueError("Акция доступна только для первой покупки. Запросите новый счёт.")
                    reserved = await self._one(
                        "SELECT payload FROM payment_orders WHERE tg_id = ? AND is_tripwire = 1 "
                        "AND state = 'approved' AND payload != ? AND datetime(approved_at) > datetime('now', '-10 minutes')",
                        (tg_id, payload))
                    if reserved:
                        raise ValueError("Оплата первой главы уже обрабатывается. Дождитесь завершения.")
                elif amount != chapter["price_stars"]:
                    raise ValueError("Некорректная цена главы.")
                if await self._can_read(tg_id, chapter):
                    raise ValueError("У вас уже есть доступ к этой главе.")
                other = await self._one(
                    "SELECT payload FROM payment_orders WHERE tg_id = ? AND chapter_id = ? "
                    "AND state = 'approved' AND payload != ? "
                    "AND datetime(approved_at) > datetime('now', '-10 minutes')",
                    (tg_id, chapter["id"], payload),
                )
                if other:
                    raise ValueError("Другая оплата этой главы уже обрабатывается.")
                await self.connection.execute(
                    "UPDATE payment_orders SET state = 'approved', pre_checkout_id = ?, "
                    "approved_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE payload = ?",
                    (query_id, payload),
                )
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    @durable_write
    async def record_payment(self, payload: str, tg_id: int, currency: str, amount: int, charge_id: str) -> tuple[int, bool]:
        """Return (chapter_id, newly_recorded); commit access before delivering text."""
        async with self._lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                order = await self._one("SELECT * FROM payment_orders WHERE payload = ?", (payload,))
                if (not order or order["tg_id"] != tg_id or currency != "XTR"
                        or order["amount_stars"] != amount or not charge_id
                        or order["state"] not in ("approved", "paid")):
                    raise ValueError("Payment does not match an approved invoice.")
                prior = await self._one(
                    "SELECT * FROM purchases WHERE telegram_payment_charge_id = ?", (charge_id,)
                )
                if prior:
                    if (prior["tg_id"], prior["chapter_id"], prior["amount_stars"]) != (tg_id, order["chapter_id"], amount):
                        raise ValueError("Payment charge is associated with another purchase.")
                    await self._suppress_paywalls(tg_id, order["chapter_id"])
                    await self.connection.commit()
                    return order["chapter_id"], False
                if order["state"] == "paid":
                    raise ValueError("Invoice already settled with a different payment charge.")
                await self.connection.execute(
                    "INSERT INTO purchases(tg_id, chapter_id, amount_stars, telegram_payment_charge_id) "
                    "VALUES (?, ?, ?, ?)", (tg_id, order["chapter_id"], amount, charge_id),
                )
                await self.connection.execute("UPDATE payment_orders SET state = 'paid' WHERE payload = ?", (payload,))
                await self._award_xp(tg_id,'purchase:'+charge_id,'purchase')
                await self._suppress_paywalls(tg_id, order["chapter_id"])
                await self.connection.commit()
                return order["chapter_id"], True
            except BaseException:
                await self.connection.rollback()
                raise

    async def _suppress_paywalls(self, tg_id: int, chapter_id: int | None = None) -> None:
        """Called inside the same transaction that grants access."""
        sql = ("UPDATE paywall_views SET reminded = 1, "
               "followup_state = CASE WHEN followup_state = 'sent' THEN 'sent' ELSE 'suppressed' END "
               "WHERE tg_id = ?")
        args = (tg_id,)
        if chapter_id is not None:
            sql += " AND chapter_id = ?"
            args += (chapter_id,)
        await self.connection.execute(sql, args)

    async def has_used_peek(self, tg_id: int, chapter_id: int) -> bool:
        async with self._lock:
            row = await self._one('SELECT peek_used FROM paywall_views WHERE tg_id=? AND chapter_id=?',
                                  (tg_id, chapter_id))
            return bool(row and row['peek_used'])

    @durable_write
    async def mark_peek_used(self, tg_id: int, chapter_id: int) -> bool:
        """Atomically consume one preview, checkpointing before the reader sees it."""
        async with self._lock:
            await self.connection.execute('BEGIN IMMEDIATE')
            try:
                chapter = await self._chapter(chapter_id)
                if not chapter or not chapter['is_published'] or await self._can_read(tg_id, chapter):
                    await self.connection.commit()
                    return False
                cursor = await self.connection.execute(
                    'INSERT INTO paywall_views(tg_id,chapter_id,series_id,peek_used) VALUES (?,?,?,1) '
                    'ON CONFLICT(tg_id,chapter_id) DO UPDATE SET peek_used=1 WHERE paywall_views.peek_used=0',
                    (tg_id, chapter_id, chapter['series_id']))
                claimed = cursor.rowcount == 1
                await self.connection.commit()
                return claimed
            except BaseException:
                await self.connection.rollback()
                raise

    async def log_paywall_view(self, tg_id: int, chapter_id: int) -> None:
        async with self._lock:
            chapter = await self._chapter(chapter_id)
            if not chapter or not chapter["is_published"] or await self._can_read(tg_id, chapter):
                return
            await self.connection.execute(
                "INSERT INTO paywall_views(tg_id, chapter_id, series_id) VALUES (?, ?, ?) "
                "ON CONFLICT(tg_id, chapter_id) DO UPDATE SET "
                "viewed_at = CASE WHEN paywall_views.reminded = 0 THEN excluded.viewed_at ELSE paywall_views.viewed_at END",
                (tg_id, chapter_id, chapter["series_id"]))

    async def pending_paywalls(self, min_age_seconds: float = 0, limit: int = 100) -> list[dict]:
        async with self._lock:
            return await self._all(
                "SELECT * FROM paywall_views WHERE reminded = 0 "
                "AND julianday(viewed_at) <= julianday('now') - ? / 86400.0 ORDER BY viewed_at, id LIMIT ?",
                (max(0, min_age_seconds), limit))

    @durable_write
    async def claim_followup(self, view_id: int) -> bool:
        """A persistent claim prevents duplicate sends across concurrent processes/restarts."""
        async with self._lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                view = await self._one("SELECT * FROM paywall_views WHERE id = ? AND reminded = 0", (view_id,))
                claimed = False
                if view:
                    chapter = await self._chapter(view["chapter_id"])
                    if not chapter or not chapter["is_published"] or await self._can_read(view["tg_id"], chapter):
                        await self._suppress_paywalls(view["tg_id"], view["chapter_id"])
                    else:
                        await self.connection.execute(
                            "UPDATE paywall_views SET reminded = 1, followup_state = 'sending' WHERE id = ?", (view_id,))
                        claimed = True
                await self.connection.commit()
                return claimed
            except BaseException:
                await self.connection.rollback()
                raise

    @durable_write
    async def finish_followup(self, view_id: int, state: str, message_id: int | None = None) -> None:
        if state not in ('sent', 'suppressed', 'blocked', 'failed', 'uncertain', 'pending'):
            raise ValueError("Invalid follow-up state")
        async with self._lock:
            # A purchase may race a confirmed send. Preserve the actual delivery receipt.
            allowed = "('sending','suppressed')" if state == 'sent' else "('sending')"
            await self.connection.execute(
                "UPDATE paywall_views SET followup_state = ?, reminded = ?, message_id = ?, "
                "reminded_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND followup_state IN " + allowed,
                (state, int(state != 'pending'), message_id, view_id))

    async def followup_counts(self) -> dict:
        async with self._lock:
            # Reconcile access granted by imports/manual VIP changes as well as normal payments.
            for view in await self._all("SELECT * FROM paywall_views WHERE reminded = 0"):
                chapter = await self._chapter(view["chapter_id"])
                if not chapter or not chapter["is_published"] or await self._can_read(view["tg_id"], chapter):
                    await self._suppress_paywalls(view["tg_id"], view["chapter_id"])
            return await self._one(
                "SELECT COUNT(*) AS total, COALESCE(SUM(reminded = 0), 0) AS pending, "
                "COALESCE(SUM(followup_state = 'sent'), 0) AS sent, "
                "COALESCE(SUM(followup_state IN ('sending','uncertain')), 0) AS uncertain, "
                "COALESCE(SUM(followup_state IN ('blocked','failed')), 0) AS failed FROM paywall_views")

    async def claim_serial_chapter(self, channel_id: str | int, min_interval_hours: float = 0) -> dict | None:
        async with self._lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                channel = str(channel_id)
                busy = await self._one("SELECT 1 FROM channel_serial_posts WHERE channel_id = ? AND state IN ('pending','uncertain')", (channel,))
                recent = await self._one(
                    "SELECT 1 FROM channel_serial_posts WHERE channel_id = ? AND state = 'sent' "
                    "AND julianday(posted_at) > julianday('now') - ? / 24.0", (channel, min_interval_hours))
                chapter = None
                if not busy and not recent:
                    # Short demo chapters cannot supply a 1200-character reading episode.
                    chapter = await self._one(
                        "SELECT c.*, s.title AS series_title, s.free_chapters_limit FROM chapters c "
                        "JOIN series s ON s.id = c.series_id "
                        "LEFT JOIN channel_serial_posts p ON p.channel_id = ? AND p.chapter_id = c.id "
                        "WHERE c.is_published = 1 AND c.chapter_num <= s.free_chapters_limit "
                        "AND length(c.content_text) >= 1200 AND (p.chapter_id IS NULL OR p.state = 'failed') "
                        "ORDER BY s.id, c.chapter_num LIMIT 1", (channel,))
                    if chapter:
                        await self.connection.execute(
                            "INSERT INTO channel_serial_posts(channel_id, chapter_id, series_id, state) VALUES (?, ?, ?, 'pending') "
                            "ON CONFLICT(channel_id, chapter_id) DO UPDATE SET state = 'pending'",
                            (channel, chapter['id'], chapter['series_id']))
                await self.connection.commit()
                return chapter
            except BaseException:
                await self.connection.rollback()
                raise

    async def finish_serial_post(self, channel_id: str | int, chapter_id: int, state: str,
                                 message_id: int | None = None) -> None:
        if state not in ('sent', 'failed', 'uncertain'):
            raise ValueError("Invalid serial state")
        async with self._lock:
            await self.connection.execute(
                "UPDATE channel_serial_posts SET state = ?, message_id = ?, "
                "posted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE channel_id = ? AND chapter_id = ?",
                (state, message_id, str(channel_id), chapter_id))

    async def save_short(self, series_id: int, episode_num: int, video_path: str, title: str, description: str) -> int:
        if episode_num not in (1, 2, 3) or not title.strip() or not description.strip():
            raise ValueError('Invalid short episode')
        async with self._lock:
            await self.connection.execute(
                "INSERT INTO shorts_queue(series_id, episode_num, video_path, title, description) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(series_id, episode_num) DO UPDATE SET video_path = excluded.video_path, "
                "title = excluded.title, description = excluded.description WHERE shorts_queue.status != 'sending'",
                (series_id, episode_num, str(Path(video_path).resolve()), title, description))
            row = await self._one('SELECT id FROM shorts_queue WHERE series_id = ? AND episode_num = ?', (series_id, episode_num))
            return row['id']

    async def next_short(self) -> dict | None:
        async with self._lock:
            return await self._one("SELECT * FROM shorts_queue WHERE status = 'pending' ORDER BY episode_num, series_id, id LIMIT 1")

    async def get_short(self, short_id: int) -> dict | None:
        async with self._lock:
            return await self._one('SELECT * FROM shorts_queue WHERE id = ?', (short_id,))

    async def list_shorts(self) -> list[dict]:
        async with self._lock:
            return await self._all('SELECT * FROM shorts_queue ORDER BY episode_num, series_id, id')

    async def claim_short(self, short_id: int | None = None, *, provider: str | None = None) -> dict | None:
        async with self._lock:
            await self.connection.execute('BEGIN IMMEDIATE')
            try:
                busy = await self._one("SELECT id FROM shorts_queue WHERE status = 'sending' LIMIT 1")
                row = None if busy else await self._one(
                    "SELECT * FROM shorts_queue s WHERE (status = 'pending' OR (? = 'youtube' AND status = 'uploaded' "
                    "AND EXISTS (SELECT 1 FROM short_publications p WHERE p.short_id=s.id AND provider='telegram'))) "
                    "AND (? IS NULL OR id = ?) AND (? IS NULL OR NOT EXISTS "
                    "(SELECT 1 FROM short_publications p WHERE p.short_id=s.id AND p.provider=?)) "
                    "ORDER BY episode_num, series_id, id LIMIT 1", (provider, short_id, short_id, provider, provider))
                if row:
                    await self.connection.execute("UPDATE shorts_queue SET status = 'sending' WHERE id = ?", (row['id'],))
                    row['_previous_status'] = row['status']
                    row['status'] = 'sending'
                await self.connection.commit()
                return row
            except BaseException:
                await self.connection.rollback()
                raise

    async def finish_short(self, short_id: int, status: str, message_id: int | None = None) -> None:
        if status not in ('pending', 'sent', 'uncertain', 'uploaded'):
            raise ValueError('Invalid delivery status')
        async with self._lock:
            await self.connection.execute(
                "UPDATE shorts_queue SET status = ?, message_id = ?, "
                "delivered_at = CASE WHEN ? = 'sent' THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now') ELSE delivered_at END "
                "WHERE id = ? AND status = 'sending'", (status, message_id, status, short_id))

    async def mark_short_uploaded(self, short_id: int) -> None:
        async with self._lock:
            row = await self._one('SELECT status FROM shorts_queue WHERE id = ?', (short_id,))
            if not row:
                raise ValueError('Ролик с этим ID не найден')
            if row['status'] in ('sending', 'uncertain'):
                raise ValueError('Сначала сверяйте незавершённую доставку ролика')
            await self.connection.execute(
                "UPDATE shorts_queue SET status = 'uploaded', "
                "uploaded_at = COALESCE(uploaded_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) WHERE id = ?", (short_id,))
        from app.storage_cleanup import DATA_DIR, delete_video
        short = await self.get_short(short_id)
        if Path(short['video_path']).resolve().is_relative_to(DATA_DIR.resolve()):
            delete_video(short['video_path'], root=DATA_DIR)

    async def record_short_publication(self, short_id: int, provider: str, destination: str, remote_id: str, *, finalize=True) -> None:
        """Durably commit a verified remote receipt before removing the local asset."""
        if provider not in ('youtube', 'telegram') or not remote_id:
            raise ValueError('A confirmed remote publication ID is required')
        async with self._lock:
            await self.connection.execute('BEGIN IMMEDIATE')
            try:
                receipt = await self.connection.execute(
                    'INSERT INTO short_publications(short_id,provider,destination,remote_id) VALUES (?,?,?,?) '
                    'ON CONFLICT(short_id,provider,destination) DO NOTHING', (short_id, provider, destination, remote_id))
                if provider == 'youtube' and receipt.rowcount == 1:
                    await self.connection.execute(
                        'INSERT INTO app_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        ('last_youtube_upload_at', datetime.now(timezone.utc).isoformat()))
                if finalize:
                    await self.connection.execute("UPDATE shorts_queue SET status='uploaded', "
                        "uploaded_at=COALESCE(uploaded_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) WHERE id=?", (short_id,))
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    async def short_publications(self) -> list[dict]:
        async with self._lock:
            return await self._all('SELECT * FROM short_publications ORDER BY published_at')

    async def claim_story_poll(self, short_id: int, destination: str, video_message_id: int) -> bool:
        async with self._lock:
            cursor=await self.connection.execute(
                "INSERT INTO short_story_polls(short_id,destination,video_message_id,state) VALUES (?,?,?,'sending') "
                "ON CONFLICT(short_id,destination) DO UPDATE SET state='sending',error_class=NULL "
                "WHERE short_story_polls.state='failed' AND short_story_polls.video_message_id=excluded.video_message_id",
                (short_id,str(destination),video_message_id))
            return cursor.rowcount==1

    async def story_poll(self, short_id: int, destination: str) -> dict | None:
        async with self._lock:
            return await self._one('SELECT * FROM short_story_polls WHERE short_id=? AND destination=?',
                (short_id,str(destination)))

    async def finish_story_poll(self, short_id: int, destination: str, state: str,
                                message_id: int | None = None, error_class: str | None = None) -> None:
        if state not in ('sent','failed','uncertain'):raise ValueError('Invalid poll receipt state')
        async with self._lock:
            await self.connection.execute('UPDATE short_story_polls SET state=?,message_id=?,error_class=? '
                "WHERE short_id=? AND destination=? AND state='sending'",
                (state,message_id,error_class,short_id,str(destination)))

    async def claim_admin_alert(self, key, admin_id, text, *, retry_failed=False):
        async with self._lock:
            cursor = await self.connection.execute(
                "INSERT INTO admin_alerts(event_key,admin_id,message_text,state) VALUES (?,?,?,'sending') "
                "ON CONFLICT(event_key,admin_id) DO UPDATE SET state='sending' "
                "WHERE admin_alerts.state='failed' AND ?", (key, admin_id, text, retry_failed))
            return cursor.rowcount == 1

    async def finish_admin_alert(self, key, admin_id, state, message_id=None):
        if state not in ('sent','failed','uncertain'):
            raise ValueError('Invalid alert state')
        async with self._lock:
            await self.connection.execute('UPDATE admin_alerts SET state=?,message_id=? WHERE event_key=? AND admin_id=?',
                (state, message_id, key, admin_id))

    async def failed_admin_alerts(self):
        async with self._lock:
            return await self._all("SELECT DISTINCT event_key,message_text FROM admin_alerts WHERE state='failed' LIMIT 20")

    async def stats(self) -> dict[str, Any]:
        async with self._lock:
            return await self._one(
                "SELECT (SELECT COUNT(*) FROM users) AS total_users, "
                "(SELECT COALESCE(SUM(amount_stars), 0) FROM purchases "
                "WHERE date(created_at) = date('now')) AS today_revenue, "
                "(SELECT COUNT(*) FROM (SELECT DISTINCT tg_id, chapter_id FROM purchases WHERE chapter_id > 0)) AS unlocked_chapters"
            )

    async def seed_demo(self) -> tuple[int, list[int]]:
        from app import demo
        async with self._lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                await self.connection.execute(
                    "INSERT INTO series(title, slug, description, free_chapters_limit) VALUES (?, ?, ?, 2) "
                    "ON CONFLICT(slug) DO UPDATE SET title = excluded.title, description = excluded.description, "
                    "free_chapters_limit = excluded.free_chapters_limit",
                    (demo.TITLE, "demo-the-signal", demo.DESCRIPTION),
                )
                series = await self._one("SELECT id FROM series WHERE slug = 'demo-the-signal'")
                chapters = demo.CHAPTERS
                for number, (title, content) in enumerate(chapters, 1):
                    await self.connection.execute(
                        "INSERT INTO chapters(series_id, chapter_num, title, content_text, price_stars, is_published) "
                        "VALUES (?, ?, ?, ?, 5, 1) ON CONFLICT(series_id, chapter_num) DO UPDATE SET "
                        "title = excluded.title, content_text = excluded.content_text, price_stars = 5, is_published = 1",
                        (series["id"], number, title, content),
                    )
                rows = await self._all("SELECT id FROM chapters WHERE series_id = ? ORDER BY chapter_num", (series["id"],))
                await self.connection.commit()
                return series["id"], [row["id"] for row in rows]
            except BaseException:
                await self.connection.rollback()
                raise

    async def save_chapter(self, series_id: int, chapter_num: int, title: str, content: str, price_stars: int, published: bool) -> int:
        if chapter_num < 1 or price_stars < 1 or not title.strip() or not content.strip():
            raise ValueError("Chapter number and price must be positive; title and content must be nonempty")
        async with self._lock:
            async with self.connection.execute(
                "INSERT INTO chapters(series_id, chapter_num, title, content_text, price_stars, is_published) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (series_id, chapter_num, title.strip(), content.strip(), price_stars, int(published)),
            ) as cursor:
                chapter_id = cursor.lastrowid
            await self.connection.commit()
            return chapter_id
