"""Deterministic static link audit and bounded, credential-free live verification."""
import asyncio
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlsplit, unquote
import statistics

from bs4 import BeautifulSoup
import httpx
from app.crawler_indexing import sitemap_html_urls
from app.site_verification import google_verification_files


def html_sizes(directory):
    auxiliary = google_verification_files(directory)
    sizes = {p.name:p.stat().st_size for p in Path(directory).glob('*.html') if p.name not in auxiliary}
    return {'html_count':len(sizes),'average_bytes':round(statistics.mean(sizes.values()),2) if sizes else 0,
            'max_bytes':max(sizes.values(),default=0),'max_file':max(sizes,key=sizes.get) if sizes else '',
            'total_bytes':sum(sizes.values())}


def audit_portal(directory, base):
    root=Path(directory).resolve()
    urls=sitemap_html_urls(root,base)
    errors=[];titles=[];descriptions=[];pages={}
    auxiliary = google_verification_files(root)
    for path in root.glob('*.html'):
        if path.name in auxiliary:continue
        pages[path.name]=BeautifulSoup(path.read_text(encoding='utf-8'),'html.parser')
    expected={urljoin(base,name) for name in pages}
    if len(urls)!=len(set(urls)) or set(urls)!=expected:
        errors.append('Sitemap does not exactly enumerate the HTML pages')
    for name,soup in pages.items():
        canonical=soup.find('link',rel='canonical')
        if not canonical or canonical.get('href')!=urljoin(base,name):errors.append(name+': canonical mismatch')
        title=soup.title.get_text(strip=True) if soup.title else ''
        desc=soup.find('meta',attrs={'name':'description'})
        desc=desc.get('content','').strip() if desc else ''
        if not title or not desc:errors.append(name+': missing title/description')
        titles.append(title);descriptions.append(desc)
        if 'data:audio' in str(soup):errors.append(name+': inline audio remains')
        for tag in soup.find_all(True):
            for attr in ('href','src'):
                value=tag.get(attr)
                if not value:continue
                resolved=urlsplit(urljoin(urljoin(base,name),value))
                site=urlsplit(base)
                if (resolved.scheme,resolved.netloc)!=(site.scheme,site.netloc):continue
                decoded=unquote(resolved.path)
                if not decoded.startswith(site.path):continue  # Another project on the same host.
                relative=decoded[len(site.path):] or 'index.html'
                target=(root/relative).resolve()
                if not target.is_relative_to(root) or not target.is_file():
                    errors.append(f'{name}: broken {attr}: {value}')
                elif resolved.fragment and target.suffix=='.html':
                    other=pages.get(target.name)
                    if other and not other.find(id=unquote(resolved.fragment)):
                        errors.append(f'{name}: missing fragment: {value}')
    duplicate_titles=[v for v,n in Counter(titles).items() if n>1]
    duplicate_descriptions=[v for v,n in Counter(descriptions).items() if n>1]
    if duplicate_titles:errors.append('Duplicate titles')
    if duplicate_descriptions:errors.append('Duplicate descriptions')
    return {'html_pages':len(pages),'sitemap_urls':len(urls),'duplicate_titles':len(duplicate_titles),
            'duplicate_descriptions':len(duplicate_descriptions),'errors':errors,'ok':not errors,
            'sizes':html_sizes(root)}


async def audit_live_portal(directory,base,*,client=None):
    if client is None:
        async with httpx.AsyncClient(timeout=30,follow_redirects=True) as owned:
            return await audit_live_portal(directory,base,client=owned)
    semaphore=asyncio.Semaphore(8)
    async def check(url):
        async with semaphore:
            try:
                response=await client.get(url,headers={'Cache-Control':'no-cache'})
                soup=BeautifulSoup(response.text,'html.parser')
                canonical=soup.find('link',rel='canonical')
                return {'url':url,'status':response.status_code,
                        'canonical_matches':bool(canonical and canonical.get('href')==url)}
            except httpx.HTTPError as error:
                return {'url':url,'status':None,'canonical_matches':False,'error':type(error).__name__}
    results=await asyncio.gather(*(check(u) for u in sitemap_html_urls(directory,base)))
    return {'urls':results,'all_200':all(r['status']==200 for r in results),
            'all_canonicals_match':all(r['canonical_matches'] for r in results)}
