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


def acquisition_source(payload: str | None) -> str:
    if not payload:
        return 'direct'
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
        lines.extend(f'• {source}: {count}' for source,count in summary['sources'])
        if not summary['sources']:lines.append('• Пока нет событий')
        lines.append('Начало наблюдений: '+(summary['first_observed_at'] or 'ожидается первый /start')+
            '. Старые действия не выданы за новые события.')
    return '\n'.join(lines)
