"""Observed distinct milestones; no synthetic historical backfill or public user IDs."""
import re

EVENTS = ('bot_start','open_chapter_1','open_chapter_2','view_paywall',
          'use_free_key','click_pay_button','invoice_sent','payment_success')
STAGES = (('Запуск бота',('bot_start',)),('Глава 1',('open_chapter_1',)),
          ('Глава 2',('open_chapter_2',)),('Paywall',('view_paywall',)),
          ('Бесплатный ключ / WUF',('use_free_key',)),
          ('Кнопка оплаты / счёт',('click_pay_button','invoice_sent')),
          ('Оплата Stars',('payment_success',)))
PARENTS=(None,0,1,2,3,3,5)

# Stable, human-readable acquisition tags used by the Stage 45 Telegram seed posts.
# Keep these values identical to the /start payloads so /funnel reports each placement.
TELEGRAM_CAMPAIGN_SOURCES = frozenset({
    'tg_dark', 'tg_rail', 'tg_humor', 'tg_cultivation', 'tg_isekai', 'tg_tamer',
})
_TELEGRAM_CAMPAIGN_PATTERN = re.compile(r'tg_[a-z][a-z0-9_]{0,31}')
EXTERNAL_CAMPAIGN_LABELS = {
    'pikabu_s1': 'Пикабу · Нулевой ранг',
    'dtf_s1': 'DTF · Нулевой ранг',
    'habr_s1': 'Хабр · Нулевой ранг',
    'forum_s1': 'Форумы · Нулевой ранг',
}
EXTERNAL_CAMPAIGN_SOURCES = frozenset(EXTERNAL_CAMPAIGN_LABELS)


def is_telegram_campaign_source(source: str) -> bool:
    return isinstance(source, str) and _TELEGRAM_CAMPAIGN_PATTERN.fullmatch(source) is not None


def is_campaign_source(source: str) -> bool:
    return isinstance(source, str) and (source in EXTERNAL_CAMPAIGN_SOURCES or is_telegram_campaign_source(source))


def source_title(source: str) -> str:
    label = EXTERNAL_CAMPAIGN_LABELS.get(source)
    return f'{label} [{source}]' if label else source

_CANONICAL_SOURCE_PATTERNS = (
    re.compile(r'(?:series|ch|ref|raid)_[1-9][0-9]{0,17}'),
    re.compile(r'web_portal:(?:[1-9][0-9]{0,17}|ch_[1-9][0-9]{0,17})'),
    re.compile(r'rss_podcast:[1-9][0-9]{0,17}'),
    re.compile(r'yt_long:[1-9][0-9]{0,17}'),
)


def acquisition_source(payload: str | None) -> str:
    if not payload:
        return 'direct'
    if is_campaign_source(payload):
        return payload
    match = re.fullmatch(r'yt_long_s_([1-9][0-9]{0,17})', payload)
    if match:
        return 'yt_long:' + match[1]
    match = re.fullmatch(r'(web_s|web_ch|rss_s)_([1-9][0-9]{0,17})', payload)
    if match:
        kind, number = match.groups()
        return ('rss_podcast:' if kind == 'rss_s' else 'web_portal:ch_' if kind == 'web_ch' else 'web_portal:') + number
    if re.fullmatch(r'(?:series|ch|ref|raid)_[1-9][0-9]{0,17}|quiz',payload):
        return payload
    return 'other'


def normalize_start_source(source: str) -> str:
    """Validate the canonical value persisted for a bot_start funnel event."""
    if source in {'direct', 'other', 'quiz'} or is_campaign_source(source):
        return source
    if any(pattern.fullmatch(source) for pattern in _CANONICAL_SOURCE_PATTERNS):
        return source
    return 'other'


def source_label(source: str) -> str:
    return 'ref_*' if source.startswith('ref_') else 'raid_*' if source.startswith('raid_') else source


def summarize(rows: list[dict]) -> dict:
    cohorts={event:set() for event in EVENTS}
    first={}
    for row in rows:
        cohorts[row['event_name']].add(row['tg_id'])
        if row['event_name']=='bot_start' and row['tg_id'] not in first:
            first[row['tg_id']]=source_label(row['source'])
    stages=[];sets=[]
    for index,(label,events) in enumerate(STAGES):
        current=set().union(*(cohorts[e] for e in events))
        parent=PARENTS[index]
        previous=sets[parent] if parent is not None else None
        conversion=round(100*len(current & previous)/len(previous),1) if previous else None
        stages.append({'label':label,'users':len(current),'previous_step_conversion':conversion,
            'parent_step':parent+1 if parent is not None else None})
        sets.append(current)
    sources={}
    for source in first.values():sources[source]=sources.get(source,0)+1
    return {'stages':stages,'events':{k:len(v) for k,v in cohorts.items()},
        'sources':sorted(sources.items(),key=lambda item:(-item[1],item[0]))[:10],
        'external_sources':[(tag,sources.get(tag,0)) for tag in EXTERNAL_CAMPAIGN_LABELS],
        'first_observed_at':rows[0]['created_at'] if rows else None}


def funnel_text(summary: dict, *, compact=False) -> str:
    lines=['📊 ВОРОНКА NOVELPULSE · уникальные читатели']
    for index,stage in enumerate(summary['stages'],1):
        value=stage['previous_step_conversion']
        conversion='—' if value is None else f'{value:.1f}%'
        parent=f" от шага {stage['parent_step']}" if stage['parent_step'] is not None else ''
        lines.append(f"{index}. {stage['label']}: {stage['users']} · {conversion}{parent}")
    if not compact:
        lines.extend([f"Счёт подтверждён отправкой: {summary['events']['invoice_sent']} чит.",
            '', 'Процент = доля читателей указанного родительского шага, замеченных и на текущем.',
            'Ключи и Stars — разные ветки от Paywall: шаги 5 и 6 считаются от шага 4, оплата от шага 6.',
            'Источники первого /start:'])
        ordinary = [(source,count) for source,count in summary['sources'] if source not in EXTERNAL_CAMPAIGN_SOURCES]
        lines.extend(f'• {source_title(source)}: {count}' for source,count in ordinary)
        if not ordinary:lines.append('• Других источников пока нет')
        lines.append('Внешний посев · первый /start:')
        external = dict(summary.get('external_sources', summary['sources']))
        lines.extend(f'• {source_title(source)}: {external.get(source,0)}' for source in EXTERNAL_CAMPAIGN_LABELS)
        lines.append('Начало наблюдений: '+(summary['first_observed_at'] or 'ожидается первый /start')+
            '. Старые действия не выданы за новые события.')
    return '\n'.join(lines)
