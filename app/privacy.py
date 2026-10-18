"""Fail-closed public copy validation and anonymous legacy URL replacement."""
import html
import re
from urllib.parse import unquote, urlparse

PRIVATE_MARKERS = ('mayonezka',)
BOT_URL = 'https://t.me/NovelPulseBot'
URL = re.compile(r'https?://[^\s<>"\x27]+', re.I)


def assert_public_safe(value):
    """Reject personal identifiers, including encoded URLs and button targets."""
    if hasattr(value, 'model_dump'):
        value = value.model_dump(exclude_none=True)
    if isinstance(value, dict):
        for key, item in value.items():
            assert_public_safe(key)
            assert_public_safe(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_public_safe(item)
    elif isinstance(value, str):
        decoded = value
        for _ in range(4):
            decoded = unquote(html.unescape(decoded))
        if any(marker in decoded.casefold() for marker in PRIVATE_MARKERS):
            raise ValueError('Privacy guard: personal identifier in public copy')
    return value


def anonymous_series_url(series):
    url = series.get('telegraph_url', '')
    if url and urlparse(url).hostname == 'telegra.ph':
        return assert_public_safe(url)
    return BOT_URL + '?start=series_' + str(series['id'])


def sanitize_public_text(text, series_urls=None):
    """Replace only legacy identifying URLs; fail on remaining personal text."""
    def replace(match):
        url = match.group()
        if not any(marker in unquote(html.unescape(url)).casefold() for marker in PRIVATE_MARKERS):
            return url
        sid = re.search(r'series_(\d+)', url)
        return (series_urls or {}).get(int(sid[1]), BOT_URL + '?start=series_' + sid[1]) if sid else BOT_URL
    return assert_public_safe(URL.sub(replace, text))


def sanitize_payload(value, series_urls=None):
    if isinstance(value, str):
        return sanitize_public_text(value, series_urls)
    if hasattr(value, 'model_dump'):
        return type(value).model_validate(sanitize_payload(value.model_dump(exclude_none=True), series_urls))
    if isinstance(value, dict):
        return {key: sanitize_payload(item, series_urls) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item, series_urls) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_payload(item, series_urls) for item in value)
    return value


class PublicBot:
    """Guard public send/edit boundaries, including scripts outside main.py."""
    def __init__(self, bot):
        self._bot = bot

    def __getattr__(self, name):
        target = getattr(self._bot, name)
        if not name.startswith(('send_', 'edit_message_', 'set_chat_')):
            return target
        async def guarded(*args, **kwargs):
            args, kwargs = sanitize_payload(args), sanitize_payload(kwargs)
            assert_public_safe((args, kwargs))
            return await target(*args, **kwargs)
        return guarded


def public_bot(bot):
    return bot if isinstance(bot, PublicBot) else PublicBot(bot)


class PublicPrivacyMiddleware:
    """Protect direct channel API calls made by bot handlers as well."""
    def __init__(self, channel_id):
        self.channel_id = channel_id

    async def __call__(self, make_request, bot, method):
        if (getattr(method, 'chat_id', None) == self.channel_id
                and type(method).__name__.startswith(('Send', 'EditMessage', 'SetChat'))):
            for name in ('text', 'caption', 'question', 'description', 'title', 'reply_markup', 'options', 'media',
                         'entities', 'caption_entities', 'link_preview_options', 'explanation'):
                if getattr(method, name, None) is not None:
                    setattr(method, name, sanitize_payload(getattr(method, name)))
            assert_public_safe(method)
        return await make_request(bot, method)
