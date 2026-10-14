"""One small stdlib-only preflight before allocating a long-lived cashier job."""

import json
import os
import sys
from datetime import datetime,timezone
from urllib.request import Request,urlopen


def check():
    repo=os.environ['GITHUB_REPOSITORY']
    request=Request('https://api.github.com/repos/'+repo+'/contents/state/pc_heartbeat.json',
        headers={'Authorization':'Bearer '+os.environ['GITHUB_TOKEN'],'Accept':'application/vnd.github.raw+json',
                 'Cache-Control':'no-cache'})
    with urlopen(request,timeout=20) as response:
        value=json.load(response)
    now=datetime.now(timezone.utc)
    stamp=datetime.fromisoformat(value['updated_at'].replace('Z','+00:00'))
    age=(now-stamp).total_seconds()
    online=value.get('status')=='online' and age<1500
    cloud=value.get('lease_owner')=='cloud' and datetime.fromisoformat(
        value['cloud_lease_until'].replace('Z','+00:00'))>now
    enabled=not online and not cloud
    print('PC is online, cloud night-watch standing down' if online else
          'Cloud cashier already owns the lease' if cloud else 'PC offline: starting night-watch')
    with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as output:
        output.write('start='+str(enabled).lower()+'\n')


if __name__=='__main__':
    try:
        check()
    except Exception as error:
        print('Lease preflight failed safely: '+type(error).__name__,file=sys.stderr)
        raise SystemExit(1)
