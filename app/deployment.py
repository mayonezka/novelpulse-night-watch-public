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


def matches_public_text(remote: bytes, local: Path) -> bool:
    # Contents API publishes LF text; Windows exports may use CRLF on disk.
    return remote.decode('utf-8').replace('\r\n', '\n') == local.read_text(encoding='utf-8')


def message_contains_url(message, url: str) -> bool:
    return url in (message.text or '') or any(getattr(entity, 'url', None) == url for entity in (message.entities or []))


def public_static_files(directory: Path, series_ids: list[int], *, secrets: list[str] = (), chapter_pages=()) -> dict[str, str]:
    root = directory.resolve()
    for name in chapter_pages:
        match=re.fullmatch(r'series_([1-9][0-9]*)_ch_([1-9][0-9]*)\.html',name)
        if not match or int(match[1]) not in series_ids:raise ValueError('Invalid public chapter filename')
    names = ['index.html', *[f'series_{i}.html' for i in series_ids], *chapter_pages, 'sitemap.xml', 'robots.txt']
    files = {}
    for name in names:
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError('Public deployment requires regular files within docs/')
        content = path.read_text(encoding='utf-8')
        if (any(secret and secret in content for secret in secrets)
                or re.search(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|[0-9]{8,12}:[A-Za-z0-9_-]{30,})', content)
                or '-----BEGIN PRIVATE KEY-----' in content):
            raise ValueError('Secret-like content detected in public static export')
        files[name] = content
    return files


async def github_request(client, method, path, *, expected=(200,), **kwargs):
    response = await client.request(method, 'https://api.github.com' + path, **kwargs)
    if response.status_code not in expected:
        raise ValueError(f'GitHub {method} {path}: HTTP {response.status_code}')
    return response.json() if response.content else {}


async def deploy_github_pages(db: Database, settings: Settings, username: str, channel: str,
                               token: str, *, client=None, directory='docs') -> dict:
    if client is None:
        async with httpx.AsyncClient(timeout=30, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'NovelPulse-PublicLibrary'}) as owned:
            return await deploy_github_pages(db, settings, username, channel, token, client=owned, directory=directory)
    ids = [s['id'] for s in await db.list_series()]
    chapter_pages=[f"series_{sid}_ch_{c['chapter_num']}.html" for sid in ids for c in await db.list_chapters(sid)]
    allowed_names={'index.html','sitemap.xml','robots.txt',*[f'series_{sid}.html' for sid in ids],*chapter_pages}
    secret_values = [token, settings.bot_token.get_secret_value(), await db.get_state('telegraph_access_token') or '']
    if settings.llm_api_key:
        secret_values.append(settings.llm_api_key.get_secret_value())
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
    if state.get('provider') == 'github_pages':
        full_name = state['repository']
        if not full_name.startswith(login + '/'):
            raise ValueError('Stored repository belongs to another account')
        repo = await github_request(client, 'GET', '/repos/' + full_name)
        tree = await github_request(client, 'GET', f'/repos/{full_name}/git/trees/{repo["default_branch"]}?recursive=1')
        if tree.get('truncated') or any(n['path'] not in allowed_names for n in tree['tree']):
            raise ValueError('Repository contains unexpected files; deployment stopped')
    else:
        repo = await github_request(client, 'POST', '/user/repos', expected=(201,), json={
            'name': 'novelpulse-library', 'description': 'Публичная библиотека русских веб-новелл NovelPulse',
            'private': False, 'auto_init': False})
        full_name = repo['full_name']
        state = {'provider': 'github_pages', 'repository': full_name, 'repository_url': repo['html_url'], 'status': 'created'}
        await db.set_state('public_web_deployment', json.dumps(state))
    if repo.get('private') or not repo.get('permissions', {}).get('admin'):
        raise ValueError('Dedicated public repository with admin access is required')
    base_url = f'https://{login.lower()}.github.io/{repo["name"]}/'
    await generate_web_portal(db, username, output_dir=directory, base_url=base_url, channel_id=channel)
    files = public_static_files(Path(directory), ids, secrets=secret_values,chapter_pages=chapter_pages)
    branch = repo['default_branch']
    if len(files)>20:
        await publish_static_commit(client,full_name,branch,files)
    for name, content in files.items():
        if len(files)>20:break
        current = await client.get(f'https://api.github.com/repos/{full_name}/contents/{name}', params={'ref': branch})
        payload = {'message': f'Publish public library: {name}', 'content': base64.b64encode(content.encode()).decode(), 'branch': branch}
        if current.status_code == 200:
            existing = current.json()
            if existing.get('type') != 'file':
                raise ValueError('Unexpected remote content type')
            if base64.b64decode(existing.get('content', '')).decode() == content:
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
                 digest=hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest())
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
        data=content.encode('utf-8')
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
