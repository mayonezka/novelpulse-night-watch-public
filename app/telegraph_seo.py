"""Official zero-key Telegraph publishing with a persistent private account token."""

import argparse
import asyncio
import json
import hashlib
import re
from urllib.parse import urlparse, urljoin

import httpx

from app.db import Database
from app.privacy import assert_public_safe, BOT_URL, sanitize_payload

API_URL = "https://api.telegra.ph"
CONTENT_LIMIT = 64 * 1024
PORTAL_URL = 'https://mayonezka.github.io/novelpulse-library/'
BRIDGE_MARKER = '🌐 Библиотека NovelPulse · читать, искать и слушать'


def portal_bridge_nodes(base_url: str = PORTAL_URL) -> list[dict]:
    try:
        assert_public_safe(base_url)
    except ValueError:
        return [{'tag':'blockquote','children':[BRIDGE_MARKER, {'tag':'br'},
            {'tag':'a','attrs':{'href':BOT_URL},'children':['📚 Читать и слушать в NovelPulse']}]}]
    parsed = urlparse(base_url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Для SEO-моста нужен публичный HTTPS URL библиотеки')
    base_url = base_url.rstrip('/') + '/'
    children = [BRIDGE_MARKER]
    for label, path in (
        ('📚 Вся библиотека и поиск по сюжетам', ''),
        ('⚔️ ЛитРПГ и Система', 'seo-litrpg'),
        ('🌀 Исэкай и попаданцы', 'seo-isekai'),
        ('⚡ Магия и технологии', 'seo-lightning'),
        ('🎧 RSS аудиокниг', 'podcast.xml')):
        # Static, locally generated destinations; no user query leaves the bot/site.
        names = {'seo-litrpg':'seo_litrpg-system-2026.html', 'seo-isekai':'seo_popadancy-magic.html',
                 'seo-lightning':'seo_lightning-science.html'}
        children.extend([{'tag':'br'}, {'tag':'a','attrs':{'href':urljoin(base_url,names.get(path,path))},'children':[label]}])
    return [{'tag':'blockquote','children':children}]


def with_portal_bridge(nodes: list, base_url: str = PORTAL_URL) -> list:
    """Replace only our identifiable footer, preserving the article's existing DOM."""
    body = [n for n in nodes if not (isinstance(n,dict) and n.get('tag')=='blockquote'
            and n.get('children',[None])[:1]==[BRIDGE_MARKER])]
    return body + portal_bridge_nodes(base_url)


def canonical_dom(value):
    """Telegraph adds target=_blank to anchors; it is not part of the API Node schema.

    Ignore only this presentational enrichment, retaining all text, links and node order
    for strict lossless read-back validation and repeatable footer replacement.
    """
    if isinstance(value,list):
        return [canonical_dom(item) for item in value]
    if not isinstance(value,dict):
        return value
    result={key:canonical_dom(item) for key,item in value.items()}
    if result.get('tag')=='a' and result.get('attrs',{}).get('target')=='_blank':
        result['attrs']={key:item for key,item in result['attrs'].items() if key!='target'}
    return result


class TelegraphError(ValueError):
    pass


def bot_link(username: str, series_id: int) -> str:
    username = username.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) or series_id < 1:
        raise ValueError("Некорректное имя бота или ID новеллы")
    return f"https://t.me/{username}?start=series_{series_id}"


def paragraphs(text: str) -> list[dict]:
    nodes = []
    for paragraph in re.split(r"\n\s*\n", text):
        if not paragraph.strip():
            continue
        children = []
        for line in paragraph.strip().splitlines():
            if children:
                children.append({"tag": "br"})
            children.append(line)
        nodes.append({"tag": "blockquote" if paragraph.lstrip().startswith(("—", "«", "┌")) else "p", "children": children})
    return nodes


def build_article_nodes(series: dict, chapters: list[dict], bot_username: str, *, hub_url: str = "", channel_url: str = "", enforce_limit: bool = True) -> list[dict]:
    selected = {c["chapter_num"]: c for c in chapters if c.get("is_published", 1)}
    if not all(n in selected and selected[n]["content_text"].strip() for n in (1, 2, 3)):
        raise ValueError("Для SEO нужны опубликованные главы 1, 2 и 3")
    if series["free_chapters_limit"] < 2:
        raise ValueError("SEO-публикация двух полных глав требует бесплатного лимита не ниже 2")
    link = bot_link(bot_username, series["id"])
    nodes = [{"tag": "p", "children": ["📚 NovelPulse · ЛитРПГ и веб-новеллы на русском. Первые две главы бесплатно."]}]
    if series.get("source_url"):
        nodes.append({"tag": "p", "children": ["Оригинальная новелла: ",
                     {"tag": "a", "attrs": {"href": series["source_url"]}, "children": ["источник"]}]})
    for number in (1, 2, 3):
        chapter = selected[number]
        nodes.append({"tag": "hr"})
        nodes.append({"tag": "h3", "children": [f"Глава {number}. {chapter['title']}"]})
        text = chapter["content_text"] if number < 3 else chapter["content_text"][:350]
        nodes.extend(paragraphs(text))
    nodes.extend([{"tag": "hr"}, {"tag": "blockquote", "children": [
        "🔒 Глава 3 прервана на самом интересном месте! Читать продолжение в Telegram-боте NovelPulse 👉 ",
        {"tag": "a", "attrs": {"href": link}, "children": [link]}]},
        {"tag": "p", "children": ["Открывайте отдельные главы за Telegram Stars или читайте с VIP на 7 дней за 49 ⭐️."]}])
    if hub_url:
        nodes.append({"tag": "p", "children": ["🧭 Другие новеллы: ", {"tag": "a", "attrs": {"href": hub_url}, "children": ["каталог и подборка NovelPulse"]}]})
    if channel_url:
        nodes.append({"tag": "p", "children": ["📢 Трейлеры и новые главы: ", {"tag": "a", "attrs": {"href": channel_url}, "children": ["Telegram-канал NovelPulse"]}]})
    nodes = with_portal_bridge(nodes)
    encoded = json.dumps(nodes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if enforce_limit and len(encoded) > CONTENT_LIMIT:
        raise ValueError(f"Две полные главы занимают {len(encoded)} байт DOM: лимит Telegraph 64 KiB; публикация без обрезки невозможна")
    return nodes


def split_article_nodes(nodes: list[dict]) -> list[list[dict]]:
    if len(json.dumps(nodes, ensure_ascii=False).encode()) <= 60_000:
        return [nodes]
    groups, current = [], []
    for node in nodes:
        candidates = [node]
        if len(json.dumps(node, ensure_ascii=False).encode()) > 48_000:
            from app.text import split_text
            text = "\n".join(c for c in node.get("children", []) if isinstance(c, str))
            if not text or any(isinstance(c, dict) and c.get("tag") != "br" for c in node.get("children", [])):
                raise ValueError("Слишком большой несекционируемый DOM-узел")
            candidates = [{"tag": node["tag"], "children": [part]} for part in split_text(text, limit=16_000)]
        for candidate in candidates:
            if current and len(json.dumps(current + [candidate], ensure_ascii=False).encode()) > 48_000:
                groups.append(current)
                current = []
            current.append(candidate)
    if current:
        groups.append(current)
    return groups


async def _publish_article(db, series, chapters, username, context, client, token):
    groups = split_article_nodes(build_article_nodes(series, chapters, username, **context, enforce_limit=False))
    key = f"seo_article_parts:{series['id']}"
    parts = json.loads(await db.get_state(key) or "[]")
    if not parts and series["telegraph_url"]:
        parts = [{"url": series["telegraph_url"]}]
    for index, group in enumerate(groups):
        if index < len(parts):
            continue
        title = (series["title"] + (" — читать бесплатно на русском" if index == 0 else f" — бесплатные главы, часть {index + 1}"))[:256]
        page = await api_call(client, "createPage", {"access_token": token, "title": title,
            "author_name": "NovelPulse", "author_url": f"https://t.me/{username}",
            "content": json.dumps(group, ensure_ascii=False, separators=(",", ":")), "return_content": "true"})
        url = page.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "telegra.ph" or not parsed.path.strip("/") or parsed.query or parsed.fragment:
            raise TelegraphError("Telegraph вернул некорректный URL статьи")
        # Save every successful page so an interrupted multipart publication can resume.
        parts.append({"url": url, "digest": hashlib.sha256(json.dumps(group, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()})
        await db.set_state(key, json.dumps(parts))
    for index, group in enumerate(groups):
        content_nodes = list(group)
        if len(groups) > 1:
            links = ["📚 Бесплатные главы целиком · части: "]
            for number, part in enumerate(parts[:len(groups)], 1):
                links.extend([{"tag": "a", "attrs": {"href": part["url"]}, "children": [str(number)]}, " · "])
            content_nodes.append({"tag": "p", "children": links})
            if index < len(groups) - 1:
                for label, url in (("🧭 Подборка NovelPulse", context.get("hub_url")), ("📢 Telegram-канал", context.get("channel_url"))):
                    if url:
                        content_nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": url}, "children": [label]}]})
        content_nodes = with_portal_bridge(content_nodes)
        content = json.dumps(content_nodes, ensure_ascii=False, separators=(",", ":"))
        if len(content.encode()) > CONTENT_LIMIT:
            raise ValueError("Навигация превышает лимит страницы Telegraph")
        digest = hashlib.sha256(content.encode()).hexdigest()
        if parts[index].get("digest") != digest:
            await api_call(client, "editPage", {"access_token": token, "path": urlparse(parts[index]["url"]).path.lstrip("/"),
                "title": (series["title"] + (" — читать бесплатно на русском" if index == 0 else f" — бесплатные главы, часть {index + 1}"))[:256],
                "author_name": "NovelPulse", "author_url": f"https://t.me/{username}", "content": content, "return_content": "true"})
            parts[index]["digest"] = digest
            await db.set_state(key, json.dumps(parts))
    await db.set_telegraph_url(series["id"], parts[0]["url"])
    return parts[0]["url"]


def seo_teaser(series: dict, hook: str, bot_username: str) -> str:
    return (f"🔥 {series['title'][:180]}\n\n{hook}\n\n"
            "📚 Две главы бесплатно — откройте статью и нажмите Instant View в Telegram:\n"
            f"{series['telegraph_url']}\n\n"
            f"🔓 Продолжение и VIP в NovelPulse: {bot_link(bot_username, series['id'])}\n"
            "#NovelPulse #ЛитРПГ #ВебНовеллы #ЧтоЧитать")


async def api_call(client: httpx.AsyncClient, method: str, data: dict) -> dict:
    # POST keeps access tokens out of URLs and HTTP request logs. Do not retry ambiguous creations.
    if method in ('createPage', 'editPage'):
        data = dict(data)
        for field in ('title', 'author_name', 'author_url'):
            if field in data: data[field] = sanitize_payload(data[field])
        if 'content' in data:
            data['content'] = json.dumps(sanitize_payload(json.loads(data['content'])), ensure_ascii=False)
    for attempt in range(3):
        response = await client.post(f"{API_URL}/{method}", data=data, timeout=40)
        if response.status_code >= 400:
            raise TelegraphError(f"Telegraph {method}: HTTP {response.status_code}")
        try:
            payload = response.json()
            if payload.get("ok") is True and isinstance(payload.get("result"), dict):
                return payload["result"]
            error = str(payload.get("error", "INVALID_RESPONSE"))
        except (ValueError, AttributeError) as error:
            raise TelegraphError(f"Telegraph {method}: некорректный JSON") from error
        flood = re.fullmatch(r"FLOOD_WAIT_(\d+)", error)
        if flood and int(flood[1]) <= 60 and attempt < 2:
            await asyncio.sleep(max(1, int(flood[1])))
            continue
        # Never echo arbitrary server payloads that could contain an account token.
        safe_error = error if re.fullmatch(r"[A-Z0-9_]{1,100}", error) else "API_ERROR"
        raise TelegraphError(f"Telegraph {method}: {safe_error}")
    raise TelegraphError("Telegraph: исчерпаны повторы FLOOD_WAIT")


async def publish_series(db: Database, series_id: int, bot_username: str, *,
                          client: httpx.AsyncClient | None = None) -> str:
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await publish_series(db, series_id, bot_username, client=owned)
    async with db.seo_lock:
        series = await db.get_series(series_id)
        if not series:
            raise ValueError("Новелла не найдена")
        if series["telegraph_url"]:
            return series["telegraph_url"]
        chapters = [await db.get_chapter(c["id"]) for c in await db.list_chapters(series_id)]
        cluster = json.loads(await db.get_state("seo_cluster_links") or "{}")
        # Validate chapter availability before creating an account; large articles are paginated.
        build_article_nodes(series, chapters, bot_username, enforce_limit=False)
        token = await db.get_state("telegraph_access_token")
        if not token:
            account = await api_call(client, "createAccount", {"short_name": "NovelPulse",
                                     "author_name": "NovelPulse", "author_url": f"https://t.me/{bot_username}"})
            token = account.get("access_token")
            if not isinstance(token, str) or not token:
                raise TelegraphError("Telegraph createAccount: не вернул access_token")
            await db.set_state("telegraph_access_token", token)
        return await _publish_article(db, series, chapters, bot_username, cluster, client, token)


async def publish_all(db: Database, bot_username: str, *, client: httpx.AsyncClient | None = None) -> list[dict]:
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await publish_all(db, bot_username, client=owned)
    results = []
    for series in await db.list_all_series():
        try:
            url = await publish_series(db, series["id"], bot_username, client=client)
            results.append({"id": series["id"], "title": series["title"], "url": url})
        except (httpx.HTTPError, ValueError) as error:
            from app.pipeline import failure_reason
            results.append({"id": series["id"], "error": failure_reason(error)})
    return results


HUB_TITLES = (
    "Топ-5 лучших ЛитРПГ и Исэкай веб-новелл 2026 — читать онлайн бесплатно на русском",
    "Новые книги про Систему, Прокачку и Попаданцев: эксклюзивные переводы NovelPulse",
    "Каталог веб-новелл NovelPulse: все главы и переводы на русском языке",
)


def channel_link(channel_id: str | int) -> str:
    if not isinstance(channel_id, str) or not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", channel_id):
        raise ValueError("Для SEO-хаба нужен публичный @username Telegram-канала")
    return "https://t.me/" + channel_id[1:]


def build_hub_nodes(series: list[dict], username: str, channel_url: str, hubs: list[dict] | None = None, variant: int = 0) -> list[dict]:
    introductions = (
        "Выбираете ЛитРПГ или Исэкай для нового приключения? Редакционная подборка NovelPulse объединяет пять историй на русском: магия, развитие навыков и герои, которым предстоит освоить незнакомый мир. Прочитайте две бесплатные главы каждой новеллы и выберите свою историю.",
        "Новые книги про Систему и попаданцев начинаются с одного вопроса: что делать, когда привычные правила исчезают? В переводах NovelPulse герои ищут свой путь через прокачку, магию и новые способности. Здесь собраны описания историй и быстрые переходы к бесплатным главам.",
        "Полный каталог веб-новелл NovelPulse на русском языке. У каждой истории есть отдельная страница с бесплатными главами и ссылка на продолжение в Telegram-боте. Новые главы и видеотрейлеры публикуются в нашем канале; сохраните навигатор, чтобы легко вернуться к чтению.",
    )
    nodes = [{"tag": "p", "children": [introductions[variant]]},
             {"tag": "blockquote", "children": ["🔥 Первая платная глава новому читателю — всего 1 ⭐️! VIP на 7 дней — 49 ⭐️."]}]
    for item in series:
        nodes.extend([{"tag": "hr"}, {"tag": "h3", "children": [item["title"]]},
                      *paragraphs(item["description"]), {"tag": "p", "children": [
                          {"tag": "a", "attrs": {"href": item["telegraph_url"]}, "children": ["📚 Читать две главы бесплатно (Instant View)"]}, " · ",
                          {"tag": "a", "attrs": {"href": bot_link(username, item["id"])}, "children": ["📖 Читать продолжение в боте"]}]}])
    nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": channel_url}, "children": ["📢 Telegram-канал NovelPulse: трейлеры и навигатор библиотеки"]}]})
    for hub in hubs or []:
        nodes.append({"tag": "p", "children": [{"tag": "a", "attrs": {"href": hub["url"]}, "children": [hub["title"]]}]})
    nodes = with_portal_bridge(nodes)
    if len(json.dumps(nodes, ensure_ascii=False).encode()) > CONTENT_LIMIT:
        raise ValueError("SEO-хаб превышает лимит Telegraph 64 KiB")
    return nodes


async def sync_portal_backlinks(db: Database, *, base_url: str = PORTAL_URL,
                                client: httpx.AsyncClient | None = None) -> list[dict]:
    """Update every owned NovelPulse article, including multipart and old hub pages.

    Read current remote DOM before editing; preserve full free chapters and existing links.
    Successful updates are checkpointed individually and safe to repeat after interruption.
    """
    portal_bridge_nodes(base_url)
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await sync_portal_backlinks(db, base_url=base_url, client=owned)
    async with db.seo_lock:
        token = await db.get_state('telegraph_access_token')
        if not token:
            raise TelegraphError('Не найден аккаунт Telegraph')
        pages, offset = [], 0
        while True:
            batch = await api_call(client, 'getPageList', {'access_token':token,'offset':offset,'limit':200})
            rows = batch.get('pages')
            if not isinstance(rows, list):
                raise TelegraphError('Telegraph getPageList: INVALID_RESPONSE')
            pages.extend(rows)
            offset += len(rows)
            if offset >= batch.get('total_count', offset) or not rows:
                break
        report = []
        for item in pages:
            url = item.get('url', '')
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.netloc != 'telegra.ph' or not parsed.path.strip('/'):
                raise TelegraphError('Telegraph: некорректный URL собственной статьи')
            page = await api_call(client, 'getPage', {'path':parsed.path.lstrip('/'),
                'access_token':token,'return_content':'true'})
            # getPage omits can_edit in the live API; authenticated getPageList provides it.
            if item.get('can_edit') is not True or page.get('author_name') != 'NovelPulse':
                raise TelegraphError('Telegraph: статья не подтверждена как собственная NovelPulse')
            nodes = page.get('content')
            if not isinstance(nodes, list) or not nodes:
                raise TelegraphError('Telegraph: пустой DOM статьи')
            updated = with_portal_bridge(nodes, base_url)
            content = json.dumps(updated, ensure_ascii=False, separators=(',', ':'))
            if len(content.encode()) > CONTENT_LIMIT:
                raise TelegraphError('SEO-мост превышает лимит Telegraph: исходный текст сохранён')
            changed = canonical_dom(nodes) != canonical_dom(updated)
            if changed:
                result = await api_call(client, 'editPage', {'access_token':token,
                    'path':parsed.path.lstrip('/'),'title':page['title'],
                    'author_name':page.get('author_name','NovelPulse'),
                    'author_url':page.get('author_url',''),'content':content,'return_content':'true'})
                if canonical_dom(result.get('content')) != canonical_dom(updated):
                    raise TelegraphError('Telegraph: изменённый DOM не подтверждён')
            report.append({'url':url,'updated':changed,'verified':True,
                'content_bytes':len(content.encode()),'links':len(portal_bridge_nodes(base_url)[0]['children'])//2})
            await db.set_state('portal_backlink_sync', json.dumps(report, ensure_ascii=False))
        return report


async def publish_seo_cluster(db: Database, bot_username: str, channel_id: str | int, *,
                              client: httpx.AsyncClient | None = None) -> list[dict]:
    public_channel = channel_link(channel_id)
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await publish_seo_cluster(db, bot_username, channel_id, client=owned)
    results = await publish_all(db, bot_username, client=client)
    failures = [r for r in results if "error" in r]
    if failures:
        raise TelegraphError("Не готовы статьи новелл: " + "; ".join(r["error"] for r in failures))
    async with db.seo_lock:
        series = await db.list_series()
        if not series:
            raise ValueError("Каталог пуст")
        token = await db.get_state("telegraph_access_token")
        if not token:
            raise TelegraphError("Не найден аккаунт Telegraph")
        hubs = json.loads(await db.get_state("seo_hubs") or "[]")
        for index, title in enumerate(HUB_TITLES):
            if index < len(hubs):
                continue
            nodes = build_hub_nodes(series, bot_username, public_channel, variant=index)
            page = await api_call(client, "createPage", {"access_token": token, "title": title,
                "author_name": "NovelPulse", "author_url": f"https://t.me/{bot_username}",
                "content": json.dumps(nodes, ensure_ascii=False, separators=(",", ":")), "return_content": "true"})
            url = page.get("url", "")
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc != "telegra.ph" or not parsed.path.strip("/"):
                raise TelegraphError("Telegraph вернул некорректный URL хаба")
            hubs.append({"title": title, "url": url})
            await db.set_state("seo_hubs", json.dumps(hubs, ensure_ascii=False))
        context = {"hub_url": hubs[0]["url"], "channel_url": public_channel}
        await db.set_state("seo_cluster_links", json.dumps(context))
        for index, hub in enumerate(hubs):
            nodes = build_hub_nodes(series, bot_username, public_channel, hubs, variant=index)
            content = json.dumps(nodes, ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(content.encode()).hexdigest()
            if hub.get("digest") != digest:
                await api_call(client, "editPage", {"access_token": token, "path": urlparse(hub["url"]).path.lstrip("/"),
                    "title": hub["title"], "author_name": "NovelPulse", "author_url": f"https://t.me/{bot_username}",
                    "content": content, "return_content": "true"})
                hub["digest"] = digest
                await db.set_state("seo_hubs", json.dumps(hubs, ensure_ascii=False))
        for item in series:
            chapters = [await db.get_chapter(c["id"]) for c in await db.list_chapters(item["id"])]
            await _publish_article(db, item, chapters, bot_username, context, client, token)
        return hubs


async def _cli(cluster=False):
    from aiogram import Bot
    from app.config import Settings
    settings = Settings()
    db = Database(settings.db_path)
    await db.open()
    try:
        async with Bot(settings.bot_token.get_secret_value()) as bot:
            username = (await bot.get_me()).username
            if cluster:
                chat = await bot.get_chat(settings.channel_id) if settings.channel_id else None
                if not chat or not chat.username:
                    raise ValueError("CHANNEL_ID должен указывать на публичный канал")
                results = await publish_seo_cluster(db, username, "@" + chat.username)
            else:
                results = await publish_all(db, username)
        print(json.dumps(results, ensure_ascii=True, indent=2))
        if any("error" in r for r in results):
            raise SystemExit(1)
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Живая публикация новелл и SEO-хабов в Telegraph")
    parser.add_argument("--cluster", action="store_true")
    args = parser.parse_args()
    asyncio.run(_cli(args.cluster))
