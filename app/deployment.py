"""Publish a whitelist of public library files to an isolated GitHub Pages repository."""

import base64
import hashlib
import json
import re
from pathlib import Path

import httpx
from dotenv import set_key

from app.config import PipelineSettings, Settings
from app.db import Database
from app.web_portal import generate_web_portal
from app.crawler_indexing import indexnow_key, write_indexnow_key, submit_portal_indexnow
from app.seo_hubs import SEO_FILENAMES
from app.portal_sections import READER_PAGES
from app.site_verification import GOOGLE_FILENAME, google_verification_files
from app.podcast_feed import catalog_podcast_assets,valid_preview_bytes
from app.privacy import assert_public_safe


def matches_public_text(remote: bytes, local: Path) -> bool:
    if local.suffix.lower()=='.mp3':return remote==local.read_bytes()
    # Contents API publishes LF text; Windows exports may use CRLF on disk.
    return remote.decode('utf-8').replace('\r\n', '\n') == local.read_text(encoding='utf-8')


def message_contains_url(message, url: str) -> bool:
    return url in (message.text or '') or any(getattr(entity, 'url', None) == url for entity in (message.entities or []))


def public_static_files(directory: Path, series_ids: list[int], *, secrets: list[str] = (), chapter_pages=(), verification_key='',
                        seo_pages=(),podcast_feed=False,podcast_audio=None) -> dict[str, str | bytes]:
    root = directory.resolve()
    if any(name not in (*SEO_FILENAMES,*READER_PAGES) for name in seo_pages):raise ValueError('Invalid SEO filename')
    podcast_audio=podcast_audio or {}
    for name,data in podcast_audio.items():
        match=re.fullmatch(r'podcast_([1-9][0-9]*)\.mp3',name)
        if not match or int(match[1]) not in series_ids or not valid_preview_bytes(data):
            raise ValueError('Invalid public podcast preview')
    for name in chapter_pages:
        match=re.fullmatch(r'series_([1-9][0-9]*)_ch_([1-9][0-9]*)\.html',name)
        if not match or int(match[1]) not in series_ids:raise ValueError('Invalid public chapter filename')
    names = ['index.html', *[f'series_{i}.html' for i in series_ids], *chapter_pages, 'sitemap.xml', 'robots.txt']
    names.extend(seo_pages)
    names.extend(google_verification_files(root))
    names.extend(n for n in ('styles.css','catalog.js') if (root/n).exists())
    if podcast_feed:names.append('podcast.xml')
    names.extend(podcast_audio)
    if verification_key:
        if not re.fullmatch('[0-9a-f]{32}', verification_key):
            raise ValueError('Invalid IndexNow key')
        names.append(verification_key + '.txt')
    files = {}
    for name in names:
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError('Public deployment requires regular files within docs/')
        if name in podcast_audio:
            content=path.read_bytes()
            if content!=podcast_audio[name]:raise ValueError('Podcast bytes differ from the authorized catalog preview')
            if any(secret and any(secret.encode(codec) in content for codec in ('utf-8','utf-16-le','utf-16-be')) for secret in secrets):
                raise ValueError('Secret-like content detected in public podcast export')
            scanned=content.decode('latin-1')
        else:
            content = path.read_text(encoding='utf-8')
            scanned=content
        if name == verification_key + '.txt' and content != verification_key:
            raise ValueError('IndexNow verification file content mismatch')
        if (any(secret and secret in scanned for secret in secrets)
                or re.search(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|[0-9]{8,12}:[A-Za-z0-9_-]{30,})', scanned)
                or '-----BEGIN PRIVATE KEY-----' in scanned):
            raise ValueError('Secret-like content detected in public static export')
        assert_public_safe(scanned)
        files[name] = content
    return files


async def github_request(client, method, path, *, expected=(200,), **kwargs):
    response = await client.request(method, 'https://api.github.com' + path, **kwargs)
    if response.status_code not in expected:
        raise ValueError(f'GitHub {method} {path}: HTTP {response.status_code}')
    return response.json() if response.content else {}


def pages_repository(login: str, state: dict, configured_base: str | None) -> str:
    """Transferred organization repositories must never revert to the token owner."""
    from urllib.parse import urlparse
    if state.get('provider') == 'github_pages':
        repository = state.get('repository', '')
    elif configured_base:
        parsed = urlparse(configured_base)
        suffix = '.github.io'
        if (not parsed.hostname or not parsed.hostname.endswith(suffix)
                or parsed.path.rstrip('/') != '/novelpulse-library'):
            raise ValueError('Configured GitHub Pages URL must identify the dedicated library')
        repository = parsed.hostname[:-len(suffix)] + '/novelpulse-library'
    else:
        repository = login + '/novelpulse-library'
    if not re.fullmatch(r'[A-Za-z0-9-]+/novelpulse-library', repository):
        raise ValueError('Invalid dedicated public Pages repository')
    return repository


async def deploy_github_pages(db: Database, settings: Settings, username: str, channel: str,
                               token: str, *, client=None, directory='docs', indexing_client=None,
                               indexnow_wait_seconds=180) -> dict:
    if client is None:
        async with httpx.AsyncClient(timeout=30, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'NovelPulse-PublicLibrary'}) as owned:
            return await deploy_github_pages(db, settings, username, channel, token, client=owned,
                directory=directory, indexing_client=indexing_client, indexnow_wait_seconds=indexnow_wait_seconds)
    ids = [s['id'] for s in await db.list_series()]
    seo_pages = (*SEO_FILENAMES, *(READER_PAGES if await db.get_state('portal_reader_sections') == '1' else ()))
    chapter_pages=[f"series_{sid}_ch_{c['chapter_num']}.html" for sid in ids for c in await db.list_chapters(sid)]
    podcast_audio=await catalog_podcast_assets(db)
    allowed_names={'index.html','sitemap.xml','robots.txt','podcast.xml','styles.css','catalog.js',*seo_pages,*podcast_audio,
        *[f'series_{sid}.html' for sid in ids],*chapter_pages,GOOGLE_FILENAME}
    secret_values = [token, settings.bot_token.get_secret_value(), await db.get_state('telegraph_access_token') or '']
    if settings.llm_api_key:
        secret_values.append(settings.llm_api_key.get_secret_value())
    for field in ('cloud_state_key','cloud_github_token'):
        secret=getattr(settings,field,None)
        if secret:secret_values.append(secret.get_secret_value())
    ignore = Path('.gitignore').read_text(encoding='utf-8')
    if '.env' not in ignore.splitlines() or '*.db' not in ignore.splitlines():
        raise ValueError('.gitignore must exclude .env and *.db before public deployment')
    # Validate the actual exported content before creating any public repository.
    public_static_files(Path(directory), ids, secrets=secret_values)
    user = await github_request(client, 'GET', '/user')
    login = user['login']
    if not re.fullmatch(r'[A-Za-z0-9-]+', login):
        raise ValueError('Invalid GitHub owner')
    state = json.loads(await db.get_state('public_web_deployment') or '{}')
    full_name = pages_repository(login, state, settings.web_base_url)
    owner = full_name.split('/')[0]
    base_url = f'https://{owner.lower()}.github.io/novelpulse-library/'
    verification_key = indexnow_key(base_url)
    allowed_names.add(verification_key + '.txt')
    if state.get('provider') == 'github_pages':
        repo = await github_request(client, 'GET', '/repos/' + full_name)
        if repo.get('full_name', full_name).casefold() != full_name.casefold():
            raise ValueError('Repository redirected; update deployment state after transfer')
        tree = await github_request(client, 'GET', f'/repos/{full_name}/git/trees/{repo["default_branch"]}?recursive=1')
        if tree.get('truncated') or any(n['path'] not in allowed_names for n in tree['tree']):
            raise ValueError('Repository contains unexpected files; deployment stopped')
    else:
        creation_path = '/user/repos' if owner.casefold() == login.casefold() else '/orgs/' + owner + '/repos'
        repo = await github_request(client, 'POST', creation_path, expected=(201,), json={
            'name': 'novelpulse-library', 'description': 'Публичная библиотека русских веб-новелл NovelPulse',
            'private': False, 'auto_init': False})
        full_name = repo['full_name']
        state = {'provider': 'github_pages', 'repository': full_name, 'repository_url': repo['html_url'], 'status': 'created'}
        await db.set_state('public_web_deployment', json.dumps(state))
    if repo.get('private') or not repo.get('permissions', {}).get('admin'):
        raise ValueError('Dedicated public repository with admin access is required')
    if repo['name'] != 'novelpulse-library':
        raise ValueError('Expected the dedicated novelpulse-library repository')
    await generate_web_portal(db, username, output_dir=directory, base_url=base_url, channel_id=channel,
                              google_site_verification=settings.google_site_verification)
    write_indexnow_key(directory, base_url)
    files = public_static_files(Path(directory), ids, secrets=secret_values,chapter_pages=chapter_pages,
                               verification_key=verification_key,seo_pages=seo_pages,
                               podcast_feed=True,podcast_audio=podcast_audio)
    branch = repo['default_branch']
    if state.get('status')=='created':
        # An empty Git repository has no ref/tree for a bulk Git Data commit yet.
        initial=await client.get(f'https://api.github.com/repos/{full_name}/contents/index.html',params={'ref':branch})
        if initial.status_code==404:
            await github_request(client,'PUT',f'/repos/{full_name}/contents/index.html',expected=(200,201),
                json={'message':'Initialize verified public library','content':base64.b64encode(files['index.html'].encode()).decode(),'branch':branch})
        elif initial.status_code!=200:raise ValueError('Cannot initialize the public Pages branch')
    if len(files)>20:
        await publish_static_commit(client,full_name,branch,files)
    for name, content in files.items():
        if len(files)>20:break
        current = await client.get(f'https://api.github.com/repos/{full_name}/contents/{name}', params={'ref': branch})
        data=content if isinstance(content,bytes) else content.encode()
        payload = {'message': f'Publish public library: {name}', 'content': base64.b64encode(data).decode(), 'branch': branch}
        if current.status_code == 200:
            existing = current.json()
            if existing.get('type') != 'file':
                raise ValueError('Unexpected remote content type')
            if base64.b64decode(existing.get('content', '')) == data:
                continue
            payload['sha'] = existing['sha']
        elif current.status_code != 404:
            raise ValueError(f'GitHub contents {name}: HTTP {current.status_code}')
        await github_request(client, 'PUT', f'/repos/{full_name}/contents/{name}', expected=(200, 201), json=payload)
    pages = await client.get(f'https://api.github.com/repos/{full_name}/pages')
    if pages.status_code == 404:
        await github_request(client, 'POST', f'/repos/{full_name}/pages', expected=(201,),
                             json={'build_type': 'legacy', 'source': {'branch': branch, 'path': '/'}})
    elif pages.status_code != 200:
        raise ValueError(f'GitHub Pages: HTTP {pages.status_code}')
    tree = await github_request(client, 'GET', f'/repos/{full_name}/git/trees/{branch}?recursive=1')
    remote_files = sorted(n['path'] for n in tree['tree'])
    if tree.get('truncated') or remote_files != sorted(files):
        raise ValueError('Public repository file audit failed')
    state.update(base_url=base_url, branch=branch, status='building', files=remote_files,
                 digest=hashlib.sha256(json.dumps({n:hashlib.sha256(c if isinstance(c,bytes) else c.encode()).hexdigest()
                     for n,c in files.items()},sort_keys=True).encode()).hexdigest())
    await db.set_state('public_web_deployment', json.dumps(state))
    # This public, unauthenticated client is separate from the GitHub credentialed transport.
    try:
        state['indexnow'] = await submit_portal_indexnow(db, directory, base_url,
            client=indexing_client, wait_seconds=indexnow_wait_seconds)
    except (httpx.HTTPError, OSError, ValueError) as error:
        state['indexnow'] = {'status': 'failed', 'error': type(error).__name__}
    await db.set_state('public_web_deployment', json.dumps(state))
    return state


async def publish_static_commit(client, full_name, branch, files):
    """One branch update/build for the entire verified 50+ page static export."""
    base='/repos/'+full_name
    ref=await github_request(client,'GET',base+'/git/ref/heads/'+branch)
    parent=await github_request(client,'GET',base+'/git/commits/'+ref['object']['sha'])
    tree=await github_request(client,'GET',base+'/git/trees/'+parent['tree']['sha']+'?recursive=1')
    old={r['path']:r['sha'] for r in tree['tree'] if r['type']=='blob'}
    changes=[]
    for name,content in files.items():
        data=content if isinstance(content,bytes) else content.encode('utf-8')
        digest=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        if old.get(name)==digest:continue
        blob=await github_request(client,'POST',base+'/git/blobs',expected=(201,),json={
            'encoding':'base64','content':base64.b64encode(data).decode()})
        changes.append({'path':name,'mode':'100644','type':'blob','sha':blob['sha']})
    if not changes:return
    newtree=await github_request(client,'POST',base+'/git/trees',expected=(201,),json={'base_tree':parent['tree']['sha'],'tree':changes})
    commit=await github_request(client,'POST',base+'/git/commits',expected=(201,),json={
        'message':'Publish chapter SEO library with audio previews','tree':newtree['sha'],'parents':[ref['object']['sha']]})
    await github_request(client,'PATCH',base+'/git/refs/heads/'+branch,json={'sha':commit['sha'],'force':False})


def configure_public_base(url: str, *, env_path='.env') -> str:
    config = PipelineSettings(_env_file=None, web_base_url=url)
    base = config.web_base_url
    if not base.startswith('https://'):
        raise ValueError('Live public portal must use HTTPS')
    set_key(str(env_path), 'WEB_BASE_URL', base, quote_mode='never')
    return base
