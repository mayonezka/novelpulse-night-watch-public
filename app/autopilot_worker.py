"""Desktop factory child only: never owns getUpdates or the cashier lease."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from aiogram import Bot
from app.config import Settings
from app.db import Database


async def work(args):
    settings=Settings(_env_file=None,node_mode='youtube_factory',autopilot_worker=True)
    db=Database(settings.db_path);await db.open()
    github=None
    try:
        async with Bot(settings.bot_token.get_secret_value()) as bot:
            epoch=os.environ.get('WORKER_PC_EPOCH')
            if epoch:
                from app.cloud_failover import GitHubState,local_github_token,HEARTBEAT,pc_online
                github=GitHubState(settings.cloud_repo,await local_github_token())
                @bot.session.middleware()
                async def fence(make_request,bot,method):
                    if method.__api_method__.startswith(('send','edit','pin')):
                        lease,_=await github.read(HEARTBEAT)
                        if lease.get('epoch')!=epoch or lease.get('lease_owner')!='pc' or not pc_online(lease):
                            raise RuntimeError('Autopilot PC lease changed; public send deferred')
                    return await make_request(bot,method)
            from app.autopilot import _run_autopilot_cycle,publish_channel_showcase
            report=await _run_autopilot_cycle(db,bot,settings=settings,output_dir=args.output_dir)
            if settings.channel_id:
                from app.telegraph_seo import publish_seo_cluster
                chat=await bot.get_chat(settings.channel_id)
                if chat.username:
                    await publish_seo_cluster(db,(await bot.get_me()).username,'@'+chat.username)
                report['showcase']=await publish_channel_showcase(db,bot,settings.channel_id)
            target=Path(args.result)
            temporary=target.with_suffix('.tmp')
            temporary.write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8');temporary.replace(target)
    finally:
        if github: await github.close()
        await db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--result',required=True)
    parser.add_argument('--output-dir',required=True)
    directory=Path(os.environ.get('DB_PATH','data/farm-unit-1.sqlite3')).resolve().parent
    directory.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        handlers=[logging.FileHandler(directory/'autopilot_worker.log',encoding='utf-8')])
    logging.getLogger('httpx').setLevel(logging.WARNING)
    try: asyncio.run(work(parser.parse_args()))
    except Exception as error:
        logging.error('Autopilot child failed: %s',type(error).__name__)
        raise SystemExit(1)
