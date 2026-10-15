"""Authenticated encrypted cashier checkpoints; one writer and replay-safe merging."""

import base64
import hashlib
import json
from datetime import datetime, timezone

TABLES = ('users', 'payment_orders', 'vip_orders', 'purchases', 'reading_progress',
          'reader_notifications', 'paywall_views', 'admin_alerts', 'processed_updates', 'xp_events', 'rank_awards')


def _fernet(key):
    from cryptography.fernet import Fernet
    # Stage 14 keys use standard base64; Fernet requires the URL-safe alphabet.
    raw = base64.b64decode(key.encode(), altchars=b'-_', validate=True)
    return Fernet(base64.urlsafe_b64encode(raw))


def seal_state(payload: dict, key: str, runtime: dict) -> dict:
    plain = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return {'version': 1, 'encoding': 'fernet', 'revision': payload['revision'],
            'runtime': runtime, 'ciphertext': _fernet(key).encrypt(plain).decode('ascii')}


def open_state(envelope: dict, key: str) -> dict:
    if envelope.get('version') != 1 or envelope.get('encoding') not in ('secretbox', 'fernet'):
        raise ValueError('Unknown encrypted checkpoint format')
    try:
        if envelope['encoding'] == 'fernet':
            value = json.loads(_fernet(key).decrypt(envelope['ciphertext'].encode('ascii')))
        else:
            # Read-only compatibility preserves existing financial state during migration.
            from nacl.secret import SecretBox
            value = json.loads(SecretBox(base64.b64decode(key, validate=True)).decrypt(
                base64.b64decode(envelope['ciphertext'], validate=True)))
    except Exception:
        raise ValueError('Cashier checkpoint authentication failed') from None
    if value.get('revision') != envelope['revision']:
        raise ValueError('Checkpoint revision mismatch')
    return value


def seal_catalog(catalog: dict, key: str) -> dict:
    """Full paid chapters must never appear in public source or commit history."""
    return {'version':1, 'encoding':'fernet', 'kind':'catalog',
            'ciphertext':_fernet(key).encrypt(json.dumps(catalog,ensure_ascii=False,
                                                       sort_keys=True).encode('utf-8')).decode('ascii')}


def open_catalog(envelope: dict, key: str) -> dict:
    if (envelope.get('version') != 1 or envelope.get('encoding') != 'fernet'
            or envelope.get('kind') != 'catalog'):
        raise ValueError('Unencrypted or unknown catalog format rejected')
    try:
        return json.loads(_fernet(key).decrypt(envelope['ciphertext'].encode('ascii')))
    except Exception:
        raise ValueError('Catalog authentication failed') from None


async def _columns(db, table):
    return [r['name'] for r in await db._all(f'PRAGMA table_info({table})')]


async def dump_cashier(db, bot_id: int) -> dict:
    async with db._lock:
        await db.connection.execute('BEGIN')
        try:
            tables = {}
            for table in TABLES:
                columns = await _columns(db, table)
                tables[table] = await db._all(f"SELECT {','.join(columns)} FROM {table}")
            revision = (await db._one('SELECT revision FROM cashier_clock WHERE id=1'))['revision']
            await db.connection.commit()
        except BaseException:
            await db.connection.rollback()
            raise
    return {'version': 1, 'bot_id': bot_id, 'revision': revision,
            'created_at': datetime.now(timezone.utc).isoformat(), 'tables': tables}


async def merge_cashier(db, payload: dict, bot_id: int, *, restore_durable_anchor=False) -> dict:
    """Absolute balances/VIP state are merged once under an exclusive polling lease."""
    if isinstance(payload.get('tables'),dict):
        missing=set(TABLES)-set(payload['tables'])
        if missing and missing <= {'xp_events','rank_awards'}:
            payload={**payload,'tables':{**payload['tables'],**{name:[] for name in missing}}}
    if (payload.get('version') != 1 or payload.get('bot_id') != bot_id or
            type(payload.get('revision')) is not int or payload['revision'] < 0 or
            set(payload.get('tables', {})) != set(TABLES)):
        raise ValueError('Checkpoint belongs to a different bot or schema')
    counts = {table: 0 for table in TABLES}
    async with db._lock:
        await db.connection.execute('BEGIN IMMEDIATE')
        try:
            local = (await db._one('SELECT revision FROM cashier_clock WHERE id=1'))['revision']
            if restore_durable_anchor:
                # Only a previously authenticated GitHub-committed local anchor may use
                # this path. Drop unacknowledged writes before importing the new owner.
                await db.connection.execute('PRAGMA defer_foreign_keys=ON')
                for table in reversed(TABLES):
                    await db.connection.execute(f'DELETE FROM {table}')
            if payload['revision'] < local and not restore_durable_anchor:
                raise ValueError('Refusing older cashier state; local data has not been synchronized')
            for table in TABLES:
                info = await db._all(f'PRAGMA table_info({table})')
                columns = [r['name'] for r in info]
                pk = [r['name'] for r in sorted(info, key=lambda r: r['pk']) if r['pk']]
                rows = payload['tables'][table]
                if not isinstance(rows, list) or len(rows) > 100000:
                    raise ValueError('Invalid cashier table size')
                identities = set()
                for row in rows:
                    # A warm Stage 21 runner may hydrate the last Stage 20 checkpoint.
                    # Only this known additive column has a backwards-compatible default.
                    legacy_peek = (table == 'paywall_views' and isinstance(row, dict)
                        and 'peek_used' in columns and set(row) == set(columns) - {'peek_used'})
                    if legacy_peek:
                        row = {**row, 'peek_used': 0}
                    missing_streak = set()
                    if table == 'users' and isinstance(row, dict):
                        defaults = {'streak_days': 0, 'last_checkin_date': '',
                                    'player_xp':0,'player_rank':'F','chapters_read_count':0}
                        missing_streak = set(columns) - set(row)
                        if not missing_streak <= defaults.keys():
                            raise ValueError('Invalid row schema: users')
                        row = {**row, **{k: defaults[k] for k in missing_streak}}
                    if (not isinstance(row, dict) or set(row) != set(columns) or
                            any(type(v) not in (int, float, str, type(None)) for v in row.values())):
                        raise ValueError(f'Invalid row schema: {table}')
                    identity = tuple(row[k] for k in pk)
                    if identity in identities:
                        raise ValueError(f'Duplicate identity: {table}')
                    identities.add(identity)
                    existing = await db._one(f"SELECT {','.join(columns)} FROM {table} WHERE " +
                        ' AND '.join(f'{k}=?' for k in pk), identity)
                    if legacy_peek and existing:
                        row['peek_used'] = existing['peek_used']
                    if missing_streak and existing:
                        for key in missing_streak:
                            row[key] = existing[key]
                    if existing == row:
                        continue
                    if table == 'purchases' and existing and any(existing[k] != row[k] for k in
                            ('tg_id','chapter_id','amount_stars','telegram_payment_charge_id','kind')):
                        raise ValueError('Purchase identity collision: refusing financial overwrite')
                    if table == 'users' and existing and existing['created_at'] != row['created_at']:
                        raise ValueError('Reader identity collision')
                    changes = [c for c in columns if c not in pk]
                    await db.connection.execute(f"INSERT INTO {table} ({','.join(columns)}) "
                        f"VALUES ({','.join('?' for _ in columns)}) ON CONFLICT({','.join(pk)}) DO UPDATE SET " +
                        ','.join(f'{c}=excluded.{c}' for c in changes), tuple(row[c] for c in columns))
                    counts[table] += 1
            await db.connection.execute('UPDATE cashier_clock SET revision=? WHERE id=1', (payload['revision'],))
            await db.connection.commit()
        except BaseException:
            await db.connection.rollback()
            raise
    return {'revision': payload['revision'], 'changed': sum(counts.values()), 'tables': counts}
