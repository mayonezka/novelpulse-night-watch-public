"""Dependency-free always() safety relay, also works when pip installation failed."""
import json
import os
import time
from urllib.request import Request,urlopen
from urllib.error import HTTPError


def relay():
    base='https://api.github.com/repos/'+os.environ['GITHUB_REPOSITORY']+'/'
    headers={'Authorization':'Bearer '+os.environ['GITHUB_TOKEN'],
        'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','Cache-Control':'no-cache'}
    run_id=os.environ['GITHUB_RUN_ID']
    active={'queued','in_progress','waiting','pending','requested'}
    request=Request(base+'actions/workflows/night_watch.yml/runs?per_page=100',headers=headers)
    with urlopen(request,timeout=15) as response:
        runs=json.load(response)['workflow_runs']
    other=[r for r in runs if str(r['id'])!=run_id and r['status'] in active]
    if other:
        return {'status':'already_queued','run_id':other[0]['id']}
    request=Request(base+'actions/workflows/night_watch.yml/dispatches',method='POST',
        headers={**headers,'Content-Type':'application/json'},data=json.dumps({'ref':'main'}).encode())
    with urlopen(request,timeout=15) as response:
        if response.status!=204: raise RuntimeError('Relay dispatch not accepted')
    return {'status':'dispatched'}


if __name__=='__main__':
    for attempt in range(3):
        try:
            print(json.dumps(relay()));break
        except Exception as error:
            print('Relay retry: '+type(error).__name__+((' HTTP '+str(error.code)) if isinstance(error,HTTPError) else ''))
            if attempt==2: raise SystemExit(1)
            time.sleep(2**attempt)
