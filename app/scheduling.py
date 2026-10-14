"""UTC upload clock independent of browser, rendering and Telegram polling."""

from datetime import datetime, timezone, timedelta


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
    remaining = max(0.0, (due_at - now).total_seconds())
    return {'last_youtube_upload_at': raw, 'enabled': enabled,
            'due': enabled and remaining == 0, 'seconds_remaining': remaining if enabled else None,
            'next_upload_at': due_at.isoformat() if enabled else None}
