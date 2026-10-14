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


async def serve():
    settings=Settings(_env_file=None,node_mode='bot_24x7',cloud_role='cloud',cloud_failover_enabled=True,
        db_path=Path('data/cloud.sqlite3'),catalog_snapshot_path=Path('data/catalog_snapshot.json'),
        autopilot_interval_hours=4)
    task=asyncio.create_task(run(settings,cashier_max_seconds=int(os.environ.get('CLOUD_MAX_SECONDS','19800'))))
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
