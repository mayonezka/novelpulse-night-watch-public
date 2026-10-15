"""PC lease publisher on a dedicated OS thread; never accesses asyncio/SQLite."""
import base64
import json
import logging
import threading
import time
from datetime import datetime,timezone
import httpx

PC_HEARTBEAT_SECONDS=45
PC_LEASE_SECONDS=120
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
    def __init__(self,repo,headers,epoch,runtime,on_stop,*,interval=PC_HEARTBEAT_SECONDS,tick_seconds=5):
        self.repo,self.headers,self.epoch=repo,headers,epoch
        self.runtime,self.on_stop,self.interval=runtime,on_stop,interval
        self.tick_seconds=tick_seconds
        self.closed=threading.Event()
        self.suspended=threading.Event()
        self.detector=ResumeDetector()
        self.thread=None

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
        result=client.put(url,headers=self.headers,json={'message':'PC 45s heartbeat','sha':item['sha'],
                                                       'content':encoded,'branch':'main'})
        if result.status_code in (409,422):
            return False  # Another CAS writer won; re-read on the next short tick.
        result.raise_for_status()
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
