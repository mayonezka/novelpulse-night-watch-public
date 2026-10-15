"""Private catalogue transfer; stable IDs, no reader/financial data, atomic merge."""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import Settings
from app.db import Database
from app.scheduling import parse_utc

MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class SeriesRecord(Record):
    id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=1000)
    slug: str = Field(min_length=1, max_length=1000)
    description: str = Field(max_length=1_000_000)
    free_chapters_limit: int = Field(ge=0)
    source_url: str | None
    translation_mode: str
    telegraph_url: str


class ChapterRecord(Record):
    id: int = Field(gt=0)
    series_id: int = Field(gt=0)
    chapter_num: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=1000)
    content_text: str = Field(min_length=1, max_length=5_000_000)
    price_stars: int = Field(ge=1)
    is_published: int = Field(ge=0, le=1)
    source_url: str | None
    translation_mode: str


class FactoryMetrics(Record):
    pending: int = Field(ge=0)
    uploaded: int = Field(ge=0)
    cooldown_until: str | None


class Snapshot(Record):
    version: int = Field(ge=1, le=1)
    generated_at: str
    last_youtube_upload_at: str | None
    factory_metrics: FactoryMetrics | None = None
    series: list[SeriesRecord] = Field(max_length=10000)
    chapters: list[ChapterRecord] = Field(max_length=100000)

    @field_validator('generated_at', 'last_youtube_upload_at')
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError('Snapshot timestamps must include UTC offset')
        return value


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(path)


async def export_catalog(db: Database, path: str | Path = 'data/catalog_snapshot.json') -> dict:
    async with db._lock:
        await db.connection.execute('BEGIN')
        try:
            series = await db._all('SELECT * FROM series ORDER BY id')
            chapters = await db._all('SELECT * FROM chapters WHERE is_published=1 ORDER BY series_id,chapter_num')
            last = await db._one("SELECT value FROM app_state WHERE key='last_youtube_upload_at'")
            counts = {r['status']: r['count'] for r in await db._all('SELECT status,COUNT(*) AS count FROM shorts_queue GROUP BY status')}
            cooldown = await db._one("SELECT value FROM app_state WHERE key='youtube_quota_cooldown_until'")
            await db.connection.commit()
        except BaseException:
            await db.connection.rollback()
            raise
    snapshot = Snapshot(version=1, generated_at=datetime.now(timezone.utc).isoformat(),
        last_youtube_upload_at=last['value'] if last else None,
        factory_metrics=FactoryMetrics(pending=counts.get('pending',0), uploaded=counts.get('uploaded',0),
                                       cooldown_until=cooldown['value'] if cooldown else None),
        series=[SeriesRecord(**s) for s in series], chapters=[ChapterRecord(**c) for c in chapters])
    encoded = snapshot.model_dump_json(indent=2).encode('utf-8')
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError('Catalogue exceeds the 50 MB snapshot limit')
    atomic_write(Path(path), encoded)
    return {'series': len(series), 'chapters': len(chapters), 'path': str(Path(path).resolve())}


async def import_catalog(db: Database, data: bytes | str) -> dict:
    if len(data.encode('utf-8') if isinstance(data, str) else data) > MAX_SNAPSHOT_BYTES:
        raise ValueError('Catalogue exceeds the 50 MB snapshot limit')
    snapshot = Snapshot.model_validate_json(data)
    ids = {s.id for s in snapshot.series}
    if len(ids) != len(snapshot.series) or len({s.slug for s in snapshot.series}) != len(ids):
        raise ValueError('Duplicate series identity')
    if len({c.id for c in snapshot.chapters}) != len(snapshot.chapters):
        raise ValueError('Duplicate chapter identity')
    if len({(c.series_id, c.chapter_num) for c in snapshot.chapters}) != len(snapshot.chapters):
        raise ValueError('Duplicate chapter number')
    if any(c.series_id not in ids for c in snapshot.chapters):
        raise ValueError('Chapter references a missing series')
    async with db._lock:
        await db.connection.execute('BEGIN IMMEDIATE')
        try:
            previous = await db._one("SELECT value FROM app_state WHERE key='catalog_snapshot_generated_at'")
            if previous and parse_utc(previous['value']) > parse_utc(snapshot.generated_at):
                raise ValueError('Refusing a snapshot older than the last imported catalogue')
            for record in snapshot.series:
                existing = await db._one('SELECT * FROM series WHERE id=?', (record.id,))
                if existing and (existing['slug'] != record.slug or
                        (existing['source_url'] and existing['source_url'] != record.source_url)):
                    raise ValueError(f'Series ID collision: {record.id}; use the same initial catalogue on both nodes')
                await _upsert(db, 'series', record.model_dump())
            for record in snapshot.chapters:
                existing = await db._one('SELECT * FROM chapters WHERE id=?', (record.id,))
                if existing and ((existing['series_id'], existing['chapter_num']) != (record.series_id, record.chapter_num)
                        or (existing['source_url'] and existing['source_url'] != record.source_url)):
                    raise ValueError(f'Chapter ID collision: {record.id}; purchases must retain their original identity')
                await _upsert(db, 'chapters', record.model_dump())
            for key, value in [('catalog_snapshot_generated_at', snapshot.generated_at),
                               ('factory_last_youtube_upload_at', snapshot.last_youtube_upload_at),
                               ('factory_metrics', snapshot.factory_metrics.model_dump_json() if snapshot.factory_metrics else None)]:
                if value is not None:
                    await db.connection.execute('INSERT INTO app_state(key,value) VALUES (?,?) '
                        'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
            await db.connection.commit()
        except BaseException:
            await db.connection.rollback()
            raise
    return {'series': len(snapshot.series), 'chapters': len(snapshot.chapters)}


async def _upsert(db, table, values):
    # Keys come exclusively from the strict, fixed record schemas above.
    columns = list(values)
    updates = ','.join(f'{c}=excluded.{c}' for c in columns if c != 'id')
    await db.connection.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
        f'ON CONFLICT(id) DO UPDATE SET {updates}', tuple(values.values()))


async def sync_catalog(db: Database, settings: Settings, *, optional=False) -> dict:
    path = settings.catalog_snapshot_path
    if settings.catalog_snapshot_url:
        headers = {}
        if settings.catalog_snapshot_token:
            headers['Authorization'] = 'Bearer ' + settings.catalog_snapshot_token.get_secret_value()
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            async with client.stream('GET', settings.catalog_snapshot_url, headers=headers) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_SNAPSHOT_BYTES:
                        raise ValueError('Catalogue exceeds the 50 MB snapshot limit')
        result = await import_catalog(db, bytes(body))
        atomic_write(path, bytes(body))
        return result
    if not path.is_file():
        if optional:
            return {'status': 'missing', 'series': 0, 'chapters': 0}
        raise FileNotFoundError('Нет локального снимка каталога. Скопируйте catalog_snapshot.json с ПК.')
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError('Catalogue exceeds the 50 MB snapshot limit')
    return await import_catalog(db, path.read_bytes())


async def run_cli(args):
    settings = Settings()
    db = Database(settings.db_path)
    await db.open()
    try:
        result = await export_catalog(db, args.path or settings.catalog_snapshot_path) if args.action == 'export' else await sync_catalog(
            db, settings.model_copy(update={'catalog_snapshot_path': Path(args.path)}) if args.path else settings)
        print(json.dumps(result, ensure_ascii=True))
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description='Transfer catalogue privately; never upload this file to public Pages')
    parser.add_argument('action', choices=['export', 'import'])
    parser.add_argument('--path')
    asyncio.run(run_cli(parser.parse_args()))


if __name__ == '__main__':
    main()
