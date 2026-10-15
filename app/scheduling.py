"""UTC upload clock independent of browser, rendering and Telegram polling."""

from datetime import datetime, timezone, timedelta

MAX_YOUTUBE_UPLOADS_PER_24H = 4


def parse_utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


async def upload_schedule(db, settings, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('The scheduler clock must have a timezone')
    raw = await db.get_state('last_youtube_upload_at')
    enabled = settings.autopilot_interval_hours > 0
    due_at = parse_utc(raw) + timedelta(hours=settings.autopilot_interval_hours) if raw else now
    blocked = await youtube_blocked_until(db, now=now)
    if blocked:
        due_at = max(due_at, parse_utc(blocked))
    remaining = max(0.0, (due_at - now).total_seconds())
    return {'last_youtube_upload_at': raw, 'enabled': enabled,
            'due': enabled and remaining == 0, 'seconds_remaining': remaining if enabled else None,
            'next_upload_at': due_at.isoformat() if enabled else None, 'cooldown_until': blocked}


async def youtube_blocked_until(db, *, now: datetime | None = None):
    """Shared pacer for scheduler and uploader; no browser or media dependencies."""
    now = now or datetime.now(timezone.utc)
    active = []
    for key in ('youtube_quota_cooldown_until', 'youtube_upload_blocked_until'):
        raw = await db.get_state(key)
        if raw:
            try:
                until = parse_utc(raw)
                if until > now:
                    active.append(until)
            except ValueError:
                continue
    uploads = await db._all("SELECT published_at FROM short_publications WHERE provider='youtube' "
        "AND julianday(published_at)>julianday(?)-1 AND julianday(published_at)<=julianday(?) "
        "ORDER BY julianday(published_at)", (now.isoformat(), now.isoformat()))
    if len(uploads) >= MAX_YOUTUBE_UPLOADS_PER_24H:
        until = parse_utc(uploads[-MAX_YOUTUBE_UPLOADS_PER_24H]['published_at']) + timedelta(hours=24)
        if until > now:
            active.append(until)
    if not active:
        return None
    effective = max(active).isoformat()
    if await db.get_state('youtube_quota_cooldown_until') != effective:
        await db.set_state('youtube_quota_cooldown_until', effective)
    return effective
