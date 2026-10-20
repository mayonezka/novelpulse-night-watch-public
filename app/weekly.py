"""Rolling UTC weekly comparison built only from observed reader/receipt records."""
import json
from datetime import datetime, timedelta, timezone
from app.analytics import source_title
from app.scheduling import parse_utc


async def weekly_summary(db, *, now=None):
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:raise ValueError('Weekly clock must be timezone aware')
    async def window(start,end):
        args=(start.isoformat(),end.isoformat())
        async def scalar(sql):
            row=await db._one(sql,args)
            return next(iter(row.values())) or 0
        result={
            'readers':await scalar('SELECT COUNT(*) FROM users WHERE julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)'),
            'chapter1':await scalar("SELECT COUNT(DISTINCT tg_id) FROM funnel_events WHERE event_name='open_chapter_1' AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)"),
            'paywall':await scalar("SELECT COUNT(DISTINCT tg_id) FROM funnel_events WHERE event_name='view_paywall' AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)"),
            'purchases':await scalar('SELECT COUNT(*) FROM purchases WHERE amount_stars>0 AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)'),
            'stars':await scalar('SELECT SUM(amount_stars) FROM purchases WHERE amount_stars>0 AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)'),
            'youtube':await scalar("SELECT COUNT(DISTINCT short_id) FROM short_publications WHERE provider='youtube' AND julianday(published_at)>=julianday(?) AND julianday(published_at)<julianday(?)"),
            'recovered':await scalar("SELECT COUNT(DISTINCT p.short_id) FROM short_publications p WHERE p.provider='youtube' AND julianday(p.published_at)>=julianday(?) AND julianday(p.published_at)<julianday(?) AND EXISTS (SELECT 1 FROM youtube_upload_events e WHERE e.short_id=p.short_id AND e.phase='published' AND e.recovered=1)"),
            'errors':await scalar("SELECT COUNT(*) FROM youtube_upload_events WHERE phase='error' AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)"),
        }
        sources=await db._all("SELECT COALESCE((SELECT NULLIF(f.source,'') FROM funnel_events f WHERE f.tg_id=u.tg_id AND f.event_name='bot_start' AND julianday(f.created_at)<julianday(?) ORDER BY julianday(f.created_at),f.id LIMIT 1),'unknown') source,COUNT(*) users FROM users u WHERE julianday(u.created_at)>=julianday(?) AND julianday(u.created_at)<julianday(?) GROUP BY source",
                             (end.isoformat(),*args))
        result['sources']={r['source']:r['users'] for r in sources}
        for row in await db._all("SELECT value FROM app_state WHERE key LIKE 'youtube_longform:%'"):
            try:
                receipt=json.loads(row['value'])
                if receipt.get('status')=='uploaded' and start<=parse_utc(receipt['published_at'])<end:
                    result['youtube']+=1
            except (ValueError,KeyError,TypeError):pass
        return result
    current=await window(now-timedelta(days=7),now)
    previous=await window(now-timedelta(days=14),now-timedelta(days=7))
    return {'as_of':now.isoformat(),'current':current,'previous':previous,
            'note':'Восстановления/ошибки: журнал Stage 63 и подтверждённые записи восстановления; ранняя история неполна.'}


def weekly_text(report):
    labels=(('Новые читатели','readers'),('Открыли главу 1','chapter1'),('Дошли до paywall','paywall'),
            ('Покупки Stars','purchases'),('Выручка, Stars','stars'),('YouTube успешно','youtube'),
            ('Из черновиков*','recovered'),('Ошибки попыток*','errors'))
    current,previous=report['current'],report['previous']
    lines=['📊 /weekly · последние 7×24ч против предыдущих 7×24ч (UTC)',
           f'{"Метрика":<20} {"7 дней":>7} {"Пред.7":>7} {"Δ":>6}']
    for label,key in labels:
        lines.append(f'{label:<20} {current[key]:>7} {previous[key]:>7} {current[key]-previous[key]:>+6}')
    lines.append('\nИсточники новых читателей: сейчас / ранее')
    for source in sorted(set(current['sources'])|set(previous['sources'])):
        lines.append(f'{source_title(source)}: {current["sources"].get(source,0)} / {previous["sources"].get(source,0)}')
    lines.extend(['','* '+report['note']])
    return '\n'.join(lines)
