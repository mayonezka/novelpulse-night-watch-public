"""Warm runner state machine and idempotent relay, independent of slow cron delivery."""
import asyncio
import logging
import os
import time
from app.cloud_failover import GitHubState,HEARTBEAT,RUNNER,pc_online,cloud_online,utc_now

logger=logging.getLogger(__name__)
STANDBY_POLL_SECONDS=20
RUN_BUDGET_SECONDS=5*3600+20*60


async def ensure_successor(github,run_id):
    other=[r for r in await github.runs() if str(r['id'])!=str(run_id)]
    if other:
        return {'status':'already_queued','run_id':other[0]['id']}
    await github.request('POST','actions/workflows/night_watch.yml/dispatches',json={'ref':'main'})
    return {'status':'dispatched'}


class HotStandby:
    def __init__(self,github,run_id,*,run_cashier,budget=RUN_BUDGET_SECONDS,clock=time.monotonic,
                 sleep=asyncio.sleep,poll_seconds=STANDBY_POLL_SECONDS):
        self.github,self.run_id,self.run_cashier=github,str(run_id),run_cashier
        self.budget,self.clock,self.sleep,self.poll_seconds=budget,clock,sleep,poll_seconds
        self.mode=None
        self.last_publish=-1e9
        self.drained_epoch=None
        self.transitions=[]

    async def publish(self,mode,*,epoch=None,force=False):
        if mode==self.mode and self.clock()-self.last_publish<60 and not force:
            return
        value={'protocol':17,'mode':mode,'run_id':self.run_id,'updated_at':utc_now().isoformat(),
            'drained_epoch':self.drained_epoch,'active_epoch':epoch,
            'poll_seconds':self.poll_seconds,'pc_lease_seconds':120}
        try:
            _,sha=await self.github.read(RUNNER)
        except RuntimeError as error:
            if 'HTTP 404' not in str(error): raise
            sha=None
        except KeyError:
            sha=None
        await self.github.write(RUNNER,value,sha)
        if mode!=self.mode:
            logger.info('Night-Watch transition: %s -> %s',self.mode,mode)
            self.transitions.append(mode)
        self.mode,self.last_publish=mode,self.clock()

    async def serve(self):
        deadline=self.clock()+self.budget
        try:
            while self.clock()<deadline-45:
                try:
                    heartbeat,_=await self.github.read(HEARTBEAT)
                    if pc_online(heartbeat) or cloud_online(heartbeat):
                        await self.publish('HOT_STANDBY')
                        await self.sleep(self.poll_seconds)
                        continue
                    await self.publish('ACQUIRING')
                    async def ready(epoch):
                        await self.publish('ACTIVE_CASHIER',epoch=epoch,force=True)
                    remaining=max(1,deadline-self.clock()-30)
                    try:
                        result=await asyncio.wait_for(self.run_cashier(ready,remaining),timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    if result:
                        self.drained_epoch=result.get('epoch')
                    await self.publish('HOT_STANDBY',force=True)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # No polling after a failed lease/state read; the warm process keeps retrying.
                    logger.warning('Warm standby retry: %s',type(error).__name__)
                await self.sleep(min(self.poll_seconds,max(0,deadline-self.clock()-30)))
        finally:
            try:
                await self.publish('RELAY',force=True)
                await ensure_successor(self.github,self.run_id)
            finally:
                await self.github.close()
