"""GitHub CAS polling lease and encrypted, durable PC/cloud cashier handover."""

import atexit
import asyncio
import base64
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError, TelegramRetryAfter

from app.cloud_state import dump_cashier, merge_cashier, open_state, seal_state, seal_catalog, open_catalog
from app.node_sync import export_catalog, import_catalog
from app.scheduling import parse_utc

logger = logging.getLogger(__name__)
HEARTBEAT = 'state/pc_heartbeat.json'
DELTA = 'state/cloud_delta.json'
CATALOG = 'state/catalog_snapshot.json'
ACTIVE = {'queued', 'in_progress', 'waiting', 'requested', 'pending'}


def utc_now():
    return datetime.now(timezone.utc)


def pc_online(value, now=None):
    try:
        age = ((now or utc_now()) - parse_utc(value['updated_at'])).total_seconds()
        # A PC clock ahead of UTC must not authorize a competing cloud poller.
        return value.get('status') == 'online' and age < 25 * 60
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('Invalid PC heartbeat; refusing unsafe takeover') from None


def cloud_online(value, now=None):
    try:
        return value.get('lease_owner') == 'cloud' and parse_utc(value['cloud_lease_until']) > (now or utc_now())
    except (ValueError, KeyError, TypeError):
        return False


class LeaseLost(RuntimeError):
    pass


class CASConflict(RuntimeError):
    pass


async def local_github_token():
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        return token
    env = {**os.environ, 'GIT_TERMINAL_PROMPT':'0', 'GCM_INTERACTIVE':'never'}
    result = await asyncio.to_thread(subprocess.run,
        ['git','-c','credential.interactive=false','credential','fill'],
        input='protocol=https\nhost=github.com\n\n', env=env, capture_output=True, text=True, timeout=15,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    values = dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    if result.returncode != 0 or not values.get('password'):
        raise RuntimeError('GitHub credentials unavailable')
    return values['password']


class GitHubState:
    def __init__(self, repo, token, *, client=None):
        self.repo, self.token = repo, token
        self.client = client or httpx.AsyncClient(timeout=25, follow_redirects=False)
        self.headers = {'Authorization':'Bearer '+token, 'Accept':'application/vnd.github+json',
                        'X-GitHub-Api-Version':'2022-11-28', 'Cache-Control':'no-cache'}

    async def request(self, method, path, **kwargs):
        response = await self.client.request(method, 'https://api.github.com/repos/'+self.repo+('/'+path if path else ''),
            headers=self.headers, **kwargs)
        if response.status_code in (409,422):
            raise CASConflict('GitHub compare-and-swap failed')
        if response.status_code >= 400:
            raise RuntimeError(f'GitHub {method} {path.split("?")[0]}: HTTP {response.status_code}')
        return response.json() if response.content else {}

    async def read(self, path):
        item = await self.request('GET','contents/'+path)
        content = item.get('content')
        if item.get('encoding') != 'base64':
            item_blob = await self.request('GET','git/blobs/'+item['sha'])
            content = item_blob['content']
        return json.loads(base64.b64decode(content)), item['sha']

    async def write(self, path, value, sha=None):
        encoded = json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2).encode()
        data = {'message':'Synchronize '+path, 'content':base64.b64encode(encoded).decode(), 'branch':'main'}
        if sha:
            data['sha'] = sha
        result = await self.request('PUT','contents/'+path,json=data)
        return result['content']['sha']

    async def replace(self, path, value):
        for attempt in range(5):
            old, sha = await self.read(path)
            if old == value:
                return sha
            try:
                return await self.write(path,value,sha)
            except CASConflict:
                if attempt == 4:
                    raise
                await asyncio.sleep(0.3 * (attempt+1))

    async def runs(self):
        runs = []
        for page in range(1,11):
            values = await self.request('GET',f'actions/workflows/night_watch.yml/runs?per_page=100&page={page}')
            items = values['workflow_runs']
            runs.extend(r for r in items if r['status'] in ACTIVE)
            if len(items) < 100:
                break
        return runs

    async def close(self):
        await self.client.aclose()


class CloudFailover:
    def __init__(self, db, settings, github):
        self.db, self.settings, self.github = db, settings, github
        if not settings.cloud_state_key:
            raise RuntimeError('CLOUD_STATE_KEY is required for encrypted failover')
        self.key = settings.cloud_state_key.get_secret_value()
        self.bot_id = int(settings.bot_token.get_secret_value().split(':')[0])
        self.session = uuid.uuid4().hex
        self.run_id = os.environ.get('GITHUB_RUN_ID','local-test')
        self.role = settings.cloud_role
        self.stop = asyncio.Event()
        self.flush_lock = asyncio.Lock()
        self.last_revision = None
        self.last_runtime = None
        self.last_catalog = None
        self.merged = 0
        self.owned = False
        self.ready = False
        self.background = None
        self.shutdown_path = Path('data/shutdown.request')
        self._atexit = None
        self.public_runner = False

    @classmethod
    async def create(cls, db, settings):
        token = settings.cloud_github_token.get_secret_value() if settings.cloud_github_token else await local_github_token()
        return cls(db,settings,GitHubState(settings.cloud_repo,token))

    async def pull_cashier(self):
        envelope, _ = await self.github.read(DELTA)
        result = await merge_cashier(self.db,open_state(envelope,self.key),self.bot_id)
        self.merged += result['changed']
        return result

    async def claim(self):
        for attempt in range(5):
            value, sha = await self.github.read(HEARTBEAT)
            if self.role == 'cloud':
                if pc_online(value) or cloud_online(value):
                    return False
                value.update(lease_owner='cloud', epoch=self.session, cloud_run_id=self.run_id,
                    cloud_lease_until=(utc_now()+timedelta(seconds=90)).isoformat())
            else:
                previous = value.get('epoch') if value.get('lease_owner') == 'cloud' else None
                value.update(status='online',updated_at=utc_now().isoformat(),host='pc',
                    lease_owner='pc',epoch=self.session,phase='taking_over',previous_cloud_epoch=previous)
            try:
                await self.github.write(HEARTBEAT,value,sha)
                self.owned = True
                return True
            except CASConflict:
                if attempt == 4:
                    raise
        return False

    async def assert_owner(self):
        value, _ = await self.github.read(HEARTBEAT)
        expected = value.get('epoch') == self.session and value.get('lease_owner') == self.role
        valid = pc_online(value) if self.role == 'pc' else cloud_online(value)
        if not expected or not valid:
            self.stop.set()
            raise LeaseLost('Polling lease transferred; standing down')
        return value

    async def flush(self, *, state='active', force=False):
        async with self.flush_lock:
            if self.role == 'pc':
                await self.sync_catalog()
            payload = await dump_cashier(self.db,self.bot_id)
            runtime = {'node':self.role,'state':state,'session':self.session,'run_id':self.run_id if self.role=='cloud' else None}
            if not force and payload['revision'] == self.last_revision and runtime == self.last_runtime:
                return
            for attempt in range(5):
                lease,_ = await self.github.read(HEARTBEAT)
                draining = (self.role == 'cloud' and lease.get('lease_owner') == 'pc'
                    and lease.get('phase') == 'taking_over' and lease.get('previous_cloud_epoch') == self.session)
                if self.owned and lease.get('epoch') != self.session and not draining:
                    raise LeaseLost('Checkpoint writer lease changed')
                remote, sha = await self.github.read(DELTA)
                if remote['revision'] > payload['revision']:
                    raise LeaseLost('A newer cashier checkpoint already exists')
                try:
                    await self.github.write(DELTA,seal_state(payload,self.key,runtime),sha)
                    self.last_revision, self.last_runtime = payload['revision'], runtime
                    await self.save_status('synchronized')
                    return
                except CASConflict:
                    if attempt == 4:
                        raise
                    await asyncio.sleep(0.3)

    async def save_status(self, sync):
        value, _ = await self.github.read(HEARTBEAT)
        await self.db.set_state('cloud_status',json.dumps({'active_node':'PC Primary' if self.role=='pc' else 'Cloud Night-Watch',
            'heartbeat_at':value['updated_at'],'repo':self.settings.cloud_repo,'sync':sync,
            'merged_items':self.merged,'revision':self.last_revision,'checked_at':utc_now().isoformat(),
            'encryption':'Fernet','public_runner':self.public_runner,
            'pc_runtime':value.get('pc_runtime',{})}))

    async def prepare(self):
        repository = await self.github.request('GET','')
        self.public_runner = repository.get('private') is False
        if self.role == 'cloud':
            if not await self.claim():
                return False
            catalog, _ = await self.github.read(CATALOG)
            await import_catalog(self.db,json.dumps(open_catalog(catalog,self.key)))
            await self.pull_cashier()
        else:
            # Read first; merge only after the old writer has stopped and flushed.
            await self.github.read(DELTA)
            await self.claim()
            await self.wait_cloud_stopped()
            await self.pull_cashier()
            await self.sync_catalog()
            await self.renew()
            self.shutdown_path.unlink(missing_ok=True)
            self._atexit = self.exit_fallback
            atexit.register(self._atexit)
        await self.flush(force=True)
        self.ready = True
        self.db.durability_hook = self.flush
        self.background = asyncio.create_task(self.lease_loop())
        return True

    async def sync_catalog(self):
        revision=(await self.db._one('SELECT revision FROM catalog_clock WHERE id=1'))['revision']
        stamp=await self.db.get_state('last_youtube_upload_at')
        if (revision,stamp)==self.last_catalog:
            return
        path=Path(self.db.path).parent/'catalog_snapshot.json'
        await export_catalog(self.db,path)
        catalog = json.loads(path.read_text(encoding='utf-8'))
        # Compare plaintext locally; Fernet's randomized tokens otherwise create needless commits.
        remote, _ = await self.github.read(CATALOG)
        existing = open_catalog(remote,self.key)
        if {k:v for k,v in existing.items() if k != 'generated_at'} != {k:v for k,v in catalog.items() if k != 'generated_at'}:
            await self.github.replace(CATALOG,seal_catalog(catalog,self.key))
        self.last_catalog=(revision,stamp)

    async def wait_cloud_stopped(self):
        loop = asyncio.get_running_loop()
        deadline = loop.time()+65
        while True:
            runs = await self.github.runs()
            if not runs:
                return
            envelope,_ = await self.github.read(DELTA)
            drained = envelope.get('runtime',{}).get('state') == 'stopped'
            if drained or loop.time() >= deadline:
                for run in runs:
                    await self.github.request('POST',f"actions/runs/{run['id']}/cancel")
                break
            await asyncio.sleep(2)
        # A cancel request is not a proof that getUpdates has stopped.
        deadline = loop.time()+180
        while await self.github.runs():
            if loop.time() > deadline:
                raise RuntimeError('Cloud cancellation not confirmed; PC polling remains disabled')
            await asyncio.sleep(2)
        # Expire any server-side long poll left behind by a forcibly cancelled runner.
        await asyncio.sleep(21)

    async def renew(self):
        for attempt in range(5):
            value, sha = await self.github.read(HEARTBEAT)
            if value.get('epoch') != self.session or value.get('lease_owner') != self.role:
                raise LeaseLost('Polling lease changed')
            if self.role == 'pc':
                from app.startup import pc_runtime
                value.update(status='online',updated_at=utc_now().isoformat(),phase='active',pc_runtime=pc_runtime())
            else:
                value['cloud_lease_until'] = (utc_now()+timedelta(seconds=90)).isoformat()
            try:
                await self.github.write(HEARTBEAT,value,sha)
                await self.save_status('synchronized')
                return
            except CASConflict:
                if attempt == 4:
                    raise

    async def lease_loop(self):
        elapsed = 0
        while not self.stop.is_set():
            try:
                if self.role == 'pc' and self.shutdown_path.exists():
                    self.stop.set()
                    return
                await self.assert_owner()
                if self.role=='cloud' or elapsed >= 900:
                    await self.renew()
                    elapsed = 0
                await self.flush()
            except Exception as error:
                logger.warning('Lease monitor stopped: %s',type(error).__name__)
                self.stop.set()
                return
            await asyncio.sleep(30)
            elapsed += 30

    async def shutdown(self, *, dispatch=True):
        self.db.durability_hook = None
        self.stop.set()
        if self.background:
            self.background.cancel()
            try:
                await self.background
            except asyncio.CancelledError:
                pass
        if self.owned and self.ready:
            # Persist in-flight changes before making the lease available to a successor.
            await self.flush(state='stopped',force=True)
            value,sha = await self.github.read(HEARTBEAT)
            if value.get('epoch') == self.session:
                value['lease_owner'] = None
                if self.role=='pc':
                    value.update(status='offline',updated_at=utc_now().isoformat(),phase='stopped')
                else:
                    value['cloud_lease_until'] = utc_now().isoformat()
                await self.github.write(HEARTBEAT,value,sha)
                if dispatch:
                    await self.github.request('POST','actions/workflows/night_watch.yml/dispatches',json={'ref':'main'})
        elif self.owned:
            # Failed hydration/takeover must never publish an incomplete financial database.
            value,sha=await self.github.read(HEARTBEAT)
            if value.get('epoch')==self.session:
                value.update(status='offline',lease_owner=None,phase='prepare_failed',updated_at=utc_now().isoformat())
                await self.github.write(HEARTBEAT,value,sha)
                if dispatch:
                    await self.github.request('POST','actions/workflows/night_watch.yml/dispatches',json={'ref':'main'})
        if self._atexit:
            atexit.unregister(self._atexit)
        await self.github.close()

    def exit_fallback(self):
        """Best effort only: power loss/TerminateProcess cannot execute atexit."""
        try:
            with httpx.Client(timeout=3) as client:
                base='https://api.github.com/repos/'+self.settings.cloud_repo+'/'
                response=client.get(base+'contents/'+HEARTBEAT,headers=self.github.headers)
                item=response.json()
                value=json.loads(base64.b64decode(item['content']))
                if value.get('epoch') != self.session:
                    return
                value.update(status='offline',lease_owner=None,updated_at=utc_now().isoformat(),phase='stopped')
                response=client.put(base+'contents/'+HEARTBEAT,headers=self.github.headers,json={'message':'PC exit',
                    'sha':item['sha'],'content':base64.b64encode(json.dumps(value).encode()).decode(),'branch':'main'})
                if response.is_success:
                    client.post(base+'actions/workflows/night_watch.yml/dispatches',headers=self.github.headers,json={'ref':'main'})
        except Exception:
            pass


async def poll_with_lease(dispatcher,bot,db,manager,*,max_seconds=None):
    """Sequential update -> durable checkpoint -> next getUpdates acknowledgement."""
    start=asyncio.get_running_loop().time()
    row=await db._one('SELECT MAX(update_id) AS last FROM processed_updates')
    offset=row['last']+1 if row['last'] is not None else None
    while not manager.stop.is_set():
        if max_seconds is not None and asyncio.get_running_loop().time()-start >= max_seconds:
            return
        try:
            await manager.assert_owner()
            updates=await bot.get_updates(offset=offset,timeout=20,
                allowed_updates=dispatcher.resolve_used_update_types())
            for update in updates:
                # Finish already fetched work while PC waits for our final drain.
                if not await db._one('SELECT update_id FROM processed_updates WHERE update_id=?',(update.update_id,)):
                    await dispatcher.feed_update(bot,update)
                    async with db._lock:
                        await db.connection.execute('INSERT INTO processed_updates(update_id,processed_at) VALUES (?,?)',
                            (update.update_id,utc_now().isoformat()))
                    await manager.flush()
                offset=update.update_id+1
        except (LeaseLost,TelegramConflictError):
            manager.stop.set()
            return
        except TelegramRetryAfter as error:
            await asyncio.sleep(min(error.retry_after,30))
        except TelegramNetworkError:
            await asyncio.sleep(2)
        except Exception as error:
            # No further Telegram acknowledgement after a failed durable write.
            logger.error('Cashier paused safely: %s',type(error).__name__)
            manager.stop.set()
            raise RuntimeError('Durable cashier synchronization failed; polling stopped') from None


async def cloud_summary(db,settings):
    if not settings.cloud_failover_enabled:
        return '☁️ Cloud failover: не настроен'
    info=json.loads(await db.get_state('cloud_status') or '{}')
    runtime=info.get('pc_runtime',{})
    watchdog = 'Active (every 5 min)' if runtime.get('watchdog_active') else 'не подтверждён'
    pid = runtime.get('pid','—') if runtime.get('executable')=='pythonw.exe' else 'не подтверждён'
    runner = 'Public standard runners: ∞ минут/месяц' if info.get('public_runner') else 'public runner не подтверждён'
    return (f"☁️ Активный узел: {info.get('active_node','передача аренды')}\n"
        f"PC Heartbeat UTC: {info.get('heartbeat_at','нет')}\n"
        f"GitHub: https://github.com/{settings.cloud_repo} · {info.get('sync','нет синхронизации')}\n"
        f"Ночных изменений объединено: {info.get('merged_items',0)} · revision: {info.get('revision','—')}\n"
        f"🛡 NovelPulse_Watchdog: {watchdog}\n"
        f"Detached Background PID (pythonw.exe): {pid}\n"
        f"🔐 Cloud State: {info.get('encryption','не подтверждено')} / Encrypted\n"
        f"{runner} · лимит job 6 ч, без гарантии расписания")
