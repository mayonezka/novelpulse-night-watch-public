"""PC lease publisher on a dedicated OS thread; never accesses asyncio/SQLite."""
import base64
import json
import logging
import threading
import time
from copy import deepcopy
from datetime import datetime,timezone
import httpx

PC_HEARTBEAT_SECONDS=20
PC_LEASE_SECONDS=45
RESUME_GAP_SECONDS=90
logger=logging.getLogger(__name__)


class ResumeDetector:
    def __init__(self,*,wall=time.time,monotonic=time.monotonic):
        self.wall,self.monotonic=wall,monotonic
        self.last_wall,self.last_mono=wall(),monotonic()

    def check(self):
        now,mono=self.wall(),self.monotonic()
        elapsed,steady=now-self.last_wall,mono-self.last_mono
        self.last_wall,self.last_mono=now,mono
        return elapsed>RESUME_GAP_SECONDS or steady>RESUME_GAP_SECONDS or abs(elapsed-steady)>RESUME_GAP_SECONDS


class PCHeartbeatThread:
    def __init__(self,repo,headers,epoch,runtime,on_stop,*,interval=PC_HEARTBEAT_SECONDS,tick_seconds=5,
                 initial_sha=None,initial_value=None):
        self.repo,self.headers,self.epoch=repo,headers,epoch
        self.runtime,self.on_stop,self.interval=runtime,on_stop,interval
        self.tick_seconds=tick_seconds
        self.closed=threading.Event()
        self.suspended=threading.Event()
        self.detector=ResumeDetector()
        self.thread=None
        self._cache_lock=threading.Lock()
        self.last_sha=initial_sha
        self.last_value=deepcopy(initial_value) if initial_value else None

    def cache_heartbeat(self,value,sha):
        with self._cache_lock:
            self.last_value=deepcopy(value)
            self.last_sha=sha

    def start(self):
        self.thread=threading.Thread(target=self._run,name='NovelPulse-PC-heartbeat',daemon=True)
        self.thread.start()

    def stop(self):
        self.closed.set()
        if self.thread and threading.current_thread() is not self.thread:
            self.thread.join(timeout=15)

    def publish(self,client):
        url='https://api.github.com/repos/'+self.repo+'/contents/state/pc_heartbeat.json'
        response=client.get(url,headers=self.headers)
        response.raise_for_status()
        item=response.json()
        value=json.loads(base64.b64decode(item['content']))
        stamp=datetime.fromisoformat(value['updated_at'].replace('Z','+00:00'))
        now=datetime.now(timezone.utc)
        if (value.get('epoch')!=self.epoch or value.get('lease_owner')!='pc'
                or value.get('status')!='online' or (now-stamp).total_seconds()>=PC_LEASE_SECONDS):
            self.suspended.set();self.on_stop('resume')
            return False
        value.update(status='online',updated_at=now.isoformat(),phase='active',pc_runtime=self.runtime,
                     heartbeat_interval_seconds=PC_HEARTBEAT_SECONDS,lease_ttl_seconds=PC_LEASE_SECONDS)
        encoded=base64.b64encode(json.dumps(value,sort_keys=True).encode()).decode()
        result=client.put(url,headers=self.headers,json={'message':'PC 20s heartbeat','sha':item['sha'],
                                                       'content':encoded,'branch':'main'})
        if result.status_code in (409,422):
            return False  # Another CAS writer won; re-read on the next short tick.
        result.raise_for_status()
        try:new_sha=result.json()['content']['sha']
        except (ValueError,KeyError,TypeError):new_sha=item['sha']
        self.cache_heartbeat(value,new_sha)
        return True

    def publish_offline_sync(self,reason,*,timeout=.8):
        """One-request lease release using the SHA cached by the lease publisher."""
        with self._cache_lock:
            sha=self.last_sha
            value=deepcopy(self.last_value)
        if not sha or value is None or value.get('epoch')!=self.epoch or value.get('lease_owner')!='pc':
            return False
        value.update(status='offline',lease_owner=None,updated_at=datetime.now(timezone.utc).isoformat(),
                     phase='stopped',reason=reason)
        encoded=base64.b64encode(json.dumps(value,sort_keys=True).encode()).decode()
        url='https://api.github.com/repos/'+self.repo+'/contents/state/pc_heartbeat.json'
        with httpx.Client(timeout=timeout,follow_redirects=False) as client:
            response=client.put(url,headers=self.headers,json={'message':'PC power event release',
                'sha':sha,'content':encoded,'branch':'main'})
            if response.status_code in (409,422):
                # A concurrent writer changed this file; a fresh read is the only safe retry.
                response=client.get(url,headers=self.headers)
                response.raise_for_status()
                item=response.json()
                current=json.loads(base64.b64decode(item['content']))
                if current.get('epoch')!=self.epoch or current.get('lease_owner')!='pc':return False
                current.update(status='offline',lease_owner=None,updated_at=datetime.now(timezone.utc).isoformat(),
                    phase='stopped',reason=reason)
                response=client.put(url,headers=self.headers,json={'message':'PC power event release retry',
                    'sha':item['sha'],'content':base64.b64encode(json.dumps(current,sort_keys=True).encode()).decode(),
                    'branch':'main'})
                value=current
            response.raise_for_status()
            try:new_sha=response.json()['content']['sha']
            except (ValueError,KeyError,TypeError):new_sha=sha
        self.cache_heartbeat(value,new_sha)
        return True

    def _run(self):
        next_publish=time.monotonic()+self.interval
        failures=0
        with httpx.Client(timeout=8,follow_redirects=False) as client:
            while not self.closed.wait(self.tick_seconds):
                if self.detector.check():
                    self.suspended.set();self.on_stop('resume')
                    return  # Never refresh an old pre-sleep epoch.
                if time.monotonic()<next_publish:
                    continue
                try:
                    ok=self.publish(client)
                    if self.suspended.is_set(): return
                    failures=0
                    next_publish=time.monotonic()+(self.interval if ok else 5)
                except Exception as error:
                    failures+=1
                    logger.warning('Dedicated heartbeat retry: %s',type(error).__name__)
                    next_publish=time.monotonic()+5
                    if failures>=3:
                        self.suspended.set();self.on_stop('heartbeat_unavailable')
                        return
