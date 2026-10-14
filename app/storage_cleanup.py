"""Checked, idempotent removal of disposable media; never follow external links."""

import argparse
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / 'data'


@asynccontextmanager
async def media_slot():
    """One generation/delivery job across bot, CLI and all OS processes."""
    DATA_DIR.mkdir(exist_ok=True)
    stream = (DATA_DIR / '.media.lock').open('a+b')
    acquired = False
    try:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            if stream.read(1) == b'':
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    except OSError as error:
        if not acquired:
            raise RuntimeError('Другой процесс уже генерирует или отправляет видео') from error
        raise
    finally:
        if acquired:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def checked_path(path, root=DATA_DIR):
    root = Path(root).resolve()
    candidate = Path(path).absolute()
    if not candidate.resolve().is_relative_to(root) or candidate.resolve() == root:
        raise ValueError('Refusing to remove a path outside the media directory')
    for parent in (candidate, *candidate.parents):
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('Refusing to traverse a linked media path')
    return candidate


def delete_video(path, *, root=DATA_DIR):
    """Call only after a confirmed delivery, or when explicitly evicting a cache."""
    path = checked_path(path, root)
    if path.suffix.lower() != '.mp4':
        raise ValueError('Expected an MP4 path')
    removed = []
    for asset in (path, path.with_suffix('.preview.png')):
        asset = checked_path(asset, root)
        if asset.is_file():
            size = asset.stat().st_size
            os.remove(asset)
            removed.append({'path': str(asset), 'bytes': size, 'mp4': asset.suffix.lower() == '.mp4'})
    return removed


def disk_status(root=DATA_DIR):
    root = Path(root).resolve()
    files = [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()
             and p.resolve().is_relative_to(root)] if root.exists() else []
    videos = [p for p in files if p.suffix.lower() == '.mp4']
    return {'mp4_count': len(videos), 'mp4_bytes': sum(p.stat().st_size for p in videos),
            'data_bytes': sum(p.stat().st_size for p in files)}


def enforce_zero_mp4_clutter(root=DATA_DIR):
    """Caller owns media_slot. Remote draft IDs survive; local media is disposable."""
    root = Path(root).resolve()
    removed = []
    for path in sorted(root.rglob('*.mp4')) if root.exists() else []:
        removed.extend(delete_video(path, root=root))
    return {'mp4_deleted': sum(p['mp4'] for p in removed), 'bytes_deleted': sum(p['bytes'] for p in removed),
            'disk': disk_status(root)}


async def cleanup_storage(db, *, root=DATA_DIR, evict_pending=False):
    root = Path(root).resolve()
    removed, errors, candidates = [], [], set()
    for name in ('archive', '.archive_stage9'):
        archive = checked_path(root / 'shorts_queue' / name, root)
        if archive.exists():
            for path in archive.rglob('*'):
                if path.is_file():
                    candidates.add(path)
    for pattern in ('*.mp4', '*.png'):
        candidates.update((root / 'traffic_drop').glob(pattern))
    candidates.add(root / 'promo_latest.mp4')
    candidates.add(root / 'promo_latest.preview.png')
    for row in await db.list_shorts():
        if row['status'] in ('uploaded', 'sent') or (evict_pending and row['status'] == 'pending'):
            path = checked_path(row['video_path'], root)
            candidates.update((path, path.with_suffix('.preview.png')))
    for candidate in sorted(candidates):
        try:
            path = checked_path(candidate, root)
            if path.is_file():
                size = path.stat().st_size
                os.remove(path)
                removed.append({'path': str(path), 'bytes': size, 'mp4': path.suffix.lower() == '.mp4'})
        except (OSError, ValueError) as error:
            errors.append({'path': str(candidate), 'reason': str(error)})
    for name in ('archive', '.archive_stage9'):
        archive = checked_path(root / 'shorts_queue' / name, root)
        if archive.exists():
            for directory in sorted((p for p in archive.rglob('*') if p.is_dir()), reverse=True):
                checked_path(directory, root).rmdir()
            archive.rmdir()
    return {'removed': removed, 'mp4_deleted': sum(item['mp4'] for item in removed),
            'bytes_freed': sum(item['bytes'] for item in removed), 'errors': errors,
            'disk': disk_status(root)}


async def _cli(args):
    from app.config import Settings
    from app.db import Database
    db = Database(Settings().db_path)
    await db.open()
    try:
        removed_by_mark = []
        for short_id in args.mark_uploaded:
            row = await db.get_short(short_id)
            assets = [checked_path(row['video_path'])] if row else []
            if assets:
                assets.append(assets[0].with_suffix('.preview.png'))
            before = {p: p.stat().st_size for p in assets if p.is_file()}
            await db.mark_short_uploaded(short_id)
            removed_by_mark.extend({'path': str(p), 'bytes': size, 'mp4': p.suffix.lower() == '.mp4'}
                                   for p, size in before.items() if not p.exists())
        report = await cleanup_storage(db, evict_pending=args.evict_pending)
        report['removed'].extend(removed_by_mark)
        report['bytes_freed'] += sum(p['bytes'] for p in removed_by_mark)
        report['mp4_deleted'] += sum(p['mp4'] for p in removed_by_mark)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in report.items() if k != 'removed'}, ensure_ascii=True))
    finally:
        await db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Remove only disposable media inside data/')
    parser.add_argument('--mark-uploaded', nargs='*', type=int, default=[])
    parser.add_argument('--evict-pending', action='store_true')
    parser.add_argument('--report', default='data/stage11_cleanup_report.json')
    asyncio.run(_cli(parser.parse_args()))
