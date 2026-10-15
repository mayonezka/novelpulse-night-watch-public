"""Bounded GitHub runner cashier session, stopped by lease transfer or signals."""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from main import run
from app.cloud_failover import GitHubState
from app.night_watch import HotStandby,RUN_BUDGET_SECONDS


async def serve():
    settings=Settings(_env_file=None,node_mode='bot_24x7',cloud_role='cloud',cloud_failover_enabled=True,
        db_path=Path('data/cloud.sqlite3'),catalog_snapshot_path=Path('data/catalog_snapshot.json'),
        autopilot_interval_hours=4)
    async def cashier(ready,remaining):
        return await run(settings,cashier_max_seconds=remaining,on_cashier_ready=ready)
    github=GitHubState(os.environ['CLOUD_REPO'],os.environ['GITHUB_TOKEN'])
    standby=HotStandby(github,os.environ['GITHUB_RUN_ID'],run_cashier=cashier,
        budget=int(os.environ.get('CLOUD_MAX_SECONDS',str(RUN_BUDGET_SECONDS))))
    task=asyncio.create_task(standby.serve())
    loop=asyncio.get_running_loop()
    for signum in (signal.SIGINT,signal.SIGTERM):
        loop.add_signal_handler(signum,task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(name)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('aiogram.dispatcher').setLevel(logging.CRITICAL)
    try:
        asyncio.run(serve())
    except Exception as error:
        print('Cloud cashier stopped safely: '+type(error).__name__,file=sys.stderr)
        raise SystemExit(1)
