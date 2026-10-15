"""One bounded publishing subprocess; polling and heartbeat never render frames."""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def cycle_slot(directory):
    path=Path(directory)/'.autopilot.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    stream=path.open('a+b')
    try:
        if os.name=='nt':
            import msvcrt
            if not stream.read(1): stream.write(b'0');stream.flush()
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError('Другой процесс уже выполняет цикл автопилота') from None
    try: yield
    finally: stream.close()


async def terminate_tree(process):
    if process.returncode is not None: return
    if os.name=='nt':
        # The PID is from OUR new subprocess, never a name-based kill of owner applications.
        killer=await asyncio.create_subprocess_exec('taskkill.exe','/PID',str(process.pid),'/T','/F',
            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        await killer.wait()
    else:
        import signal
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
    await process.wait()


def worker_environment(settings):
    env=os.environ.copy()
    for field in type(settings).model_fields:
        value=getattr(settings,field)
        name=field.upper()
        if value is None:
            env.pop(name,None)
        elif hasattr(value,'get_secret_value'):
            env[name]=value.get_secret_value()
        elif isinstance(value,list):
            env[name]=','.join(map(str,value))
        elif isinstance(value,bool): env[name]=str(value).lower()
        else: env[name]=str(value)
    env.update(AUTOPILOT_WORKER='true',NODE_MODE='youtube_factory')
    return env


async def run_isolated_cycle(db,bot,settings,output_dir):
    from app.storage_cleanup import media_slot,enforce_zero_mp4_clutter
    root=Path(__file__).resolve().parents[1]
    data=Path(db.path).resolve().parent
    result_path=data/f'autopilot-result-{uuid.uuid4().hex}.json'
    env=worker_environment(settings)
    info=json.loads(await db.get_state('cloud_status') or '{}')
    env['WORKER_PC_EPOCH']=info.get('epoch','')
    with cycle_slot(data):
        log_path=data/'autopilot_worker.log'
        # Bounded startup log; secrets never appear in arguments or stdout.
        if log_path.exists() and log_path.stat().st_size>2_000_000:
            log_path.replace(log_path.with_suffix('.previous.log'))
        with log_path.open('ab') as log:
            options=({'creationflags':subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.BELOW_NORMAL_PRIORITY_CLASS}
                     if os.name=='nt' else {'start_new_session':True})
            process=await asyncio.create_subprocess_exec(sys.executable,'-m','app.autopilot_worker',
                '--result',str(result_path),'--output-dir',str(Path(output_dir).resolve()),cwd=root,env=env,
                stdout=log,stderr=log,**options)
            await db.set_state('autopilot_worker',json.dumps({'pid':process.pid,'state':'running'}))
            try:
                await asyncio.wait_for(process.wait(),settings.autopilot_timeout_seconds)
                if process.returncode or not result_path.is_file():
                    raise RuntimeError('Изолированный автопилот остановлен; детали в приватном autopilot_worker.log')
                report=json.loads(result_path.read_text(encoding='utf-8'))
                payload=report.get('short',{}).get('owner_alert_payload')
                if payload:
                    from app.notifications import notify_admins
                    report['short']['admin_alert']=await notify_admins(bot,payload['text'],settings=settings,
                        db=None if settings.node_mode=='youtube_factory' else db,
                        event_key=payload['event_key'])
                    if db.durability_hook: await db.durability_hook()
                return report
            finally:
                await terminate_tree(process)
                result_path.unlink(missing_ok=True)
                await db.set_state('autopilot_worker',json.dumps({'pid':process.pid,'state':'stopped'}))
                try:
                    async with media_slot():
                        cleanup_root=Path('data') if Path(output_dir).resolve()==(root/'data/traffic_drop').resolve() else Path(output_dir)
                        enforce_zero_mp4_clutter(cleanup_root)
                except RuntimeError:
                    pass
