"""Public catalog search only: no reader data or paid chapter bodies are indexed."""
import re
from html import escape

TROPES = (
    ('dungeons', '💀 Подземелья и Боссы', ('подземел', 'босс', 'разлом'), ('litrpg',)),
    ('technology', '⚡ Магия и Технологии', ('молни', 'рельсотрон', 'физик', 'технолог'), ('science_magic',)),
    ('pets', '🐾 Питомцы и Эволюция', ('питом', 'укрот', 'прируч', 'эволюц'), ('taming',)),
    ('hidden', '👑 Скрытая Имба / Ранг 0', ('нулев', 'ранг', 'трансценд', 'скрыт'), ()),
    ('cultivation', '🌱 Культивация и Травы', ('садов', 'трав', 'культивац'), ('cultivation',)),
    ('crafting', '🍺 Крафт и Профессии', ('крафт', 'професс', 'пивовар', 'ремес', 'садов', 'рельсотрон'), ()),
)
TROPE_IDS = frozenset(t[0] for t in TROPES)


def normalize(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text).casefold().replace('ё', 'е')).strip()


def query_tokens(query: str) -> list[str]:
    # A small transparent suffix rule matches молния/молний/молнии without a heavy NLP runtime.
    return [re.sub(r'(?:иями|ами|ями|ого|ему|ому|ия|ий|ии|ие|ая|ую|ые|ой|ый|ей|ов|ам|ям|ом|ем|а|я|ы|и|ь|ю|у|е)$', '', t)
            if len(t) > 4 else t for t in re.findall(r'[\w]+', normalize(query[:120]))]


def catalog_text(series: dict) -> str:
    return normalize(' '.join(str(series.get(k) or '') for k in
        ('title', 'description', 'genre_title', 'genre_slug', 'chapter_titles')))


def trope_ids(series: dict) -> list[str]:
    text = catalog_text(series)
    genres = str(series.get('genre_slug', '')).split()
    return [slug for slug, _, terms, shelves in TROPES
            if any(term in text for term in terms) or any(g in shelves for g in genres)]


def search_catalog(series: list[dict], query: str = '', trope: str | None = None) -> list[dict]:
    if trope is not None and trope not in TROPE_IDS:
        raise ValueError('Неизвестный сюжетный фильтр')
    tokens = query_tokens(query)
    return [s for s in series if all(t in catalog_text(s) for t in tokens)
            and (trope is None or trope in trope_ids(s))]


def search_attributes(series: dict, kind: str) -> str:
    return (f'data-filter-item="{escape(kind)}" data-search="{escape(catalog_text(series), quote=True)}" '
            f'data-tropes="{" ".join(trope_ids(series))}"')


def web_search_widget() -> str:
    pills = ''.join(f'<button type="button" data-trope="{slug}" aria-pressed="false">{escape(title)}</button>'
                    for slug, title, *_ in TROPES)
    return ('<section class="catalog-search" aria-label="Поиск по сюжетам"><label for="catalog-query">'
            '🔍 Найдите свою историю</label><input id="catalog-query" type="search" maxlength="120" '
            'placeholder="Например: молния, питомцы, скрытый ранг" autocomplete="off">'
            f'<div class="trope-pills">{pills}<button id="search-reset" type="button">Сбросить</button></div>'
            '<p id="search-status" role="status" aria-live="polite"></p><noscript>'
            'Поиск требует JavaScript. Все книги и подборки доступны ниже.</noscript></section>')


WEB_SEARCH_SCRIPT = r'''<script>
(() => {
 const input=document.getElementById('catalog-query');
 const items=[...document.querySelectorAll('[data-filter-item]')];
 const pills=[...document.querySelectorAll('[data-trope]')];
 let trope='';
 function tokens(value){return (value.toLowerCase().replace(/ё/g,'е').slice(0,120).match(/[\p{L}\p{N}_]+/gu)||[])
  .map(t=>t.length>4?t.replace(/(?:иями|ами|ями|ого|ему|ому|ия|ий|ии|ие|ая|ую|ые|ой|ый|ей|ов|ам|ям|ом|ем|а|я|ы|и|ь|ю|у|е)$/u,''):t);}
 function apply(){let books=0,hubs=0;const words=tokens(input.value);
  items.forEach(item=>{item.hidden=!words.every(t=>item.dataset.search.includes(t)) ||
   !!(trope&&!item.dataset.tropes.split(' ').includes(trope));
   if(!item.hidden){if(item.dataset.filterItem==='book')books++;else hubs++;}});
  pills.forEach(p=>p.setAttribute('aria-pressed',String(p.dataset.trope===trope)));
  document.getElementById('search-status').textContent=books||hubs?
   `Найдено: ${books} новелл · ${hubs} подборок`:'Ничего не найдено. Попробуйте другой запрос или сбросьте фильтры.';
 }
 input.addEventListener('input',apply);
 pills.forEach(p=>p.addEventListener('click',()=>{trope=trope===p.dataset.trope?'':p.dataset.trope;apply();}));
 document.getElementById('search-reset').addEventListener('click',()=>{input.value='';trope='';apply();input.focus();});
 apply();
})();</script>'''
