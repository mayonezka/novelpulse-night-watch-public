"""Idempotent per-user Windows login launcher registration (no administrator rights)."""

import argparse
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MARKER = "' NovelPulse managed startup v1"
WATCHDOG = 'NovelPulse_Watchdog'
TASK_NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'


def watchdog_xml(user_sid, root=PROJECT_ROOT):
    """One least-privilege task with indefinite five-minute and user-logon triggers."""
    ET.register_namespace('', TASK_NS)
    task = ET.Element('{' + TASK_NS + '}Task', version='1.2')
    def child(parent, name, text=None, **attributes):
        node = ET.SubElement(parent, '{' + TASK_NS + '}' + name, attributes)
        node.text = text
        return node
    triggers = child(task, 'Triggers')
    timer = child(triggers, 'TimeTrigger')
    repeat = child(timer, 'Repetition')
    child(repeat, 'Interval', 'PT5M')
    child(repeat, 'StopAtDurationEnd', 'false')
    child(timer, 'StartBoundary', (datetime.now()+timedelta(seconds=20)).isoformat(timespec='seconds'))
    child(timer, 'Enabled', 'true')
    logon = child(triggers, 'LogonTrigger')
    child(logon, 'Enabled', 'true')
    child(logon, 'UserId', user_sid)
    principals = child(task, 'Principals')
    principal = child(principals, 'Principal', id='Owner')
    child(principal, 'UserId', user_sid)
    child(principal, 'LogonType', 'InteractiveToken')
    child(principal, 'RunLevel', 'LeastPrivilege')
    settings = child(task, 'Settings')
    for name, value in {'MultipleInstancesPolicy':'IgnoreNew','DisallowStartIfOnBatteries':'false',
            'StopIfGoingOnBatteries':'false','StartWhenAvailable':'true','Enabled':'true',
            'Hidden':'true','ExecutionTimeLimit':'PT1M'}.items():
        child(settings, name, value)
    actions = child(task, 'Actions', Context='Owner')
    action = child(actions, 'Exec')
    child(action, 'Command', str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'System32/wscript.exe'))
    child(action, 'Arguments', '"' + str(Path(root).resolve() / 'scripts/start_silent.vbs') + '"')
    child(action, 'WorkingDirectory', str(Path(root).resolve()))
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(task, encoding='unicode')


def _task_command(arguments):
    return subprocess.run(['schtasks.exe', *arguments], capture_output=True, timeout=20,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def watchdog_status(root=PROJECT_ROOT):
    missing = {'active':False, 'name':WATCHDOG, 'interval_minutes':None}
    if os.name != 'nt':
        return missing
    result = _task_command(['/Query','/TN',WATCHDOG,'/XML'])
    if result.returncode:
        return missing
    try:
        # schtasks emits UTF-16 on some Windows builds, UTF-8 on others.
        raw = result.stdout
        if raw.startswith(b'\xff\xfe') or b'\x00' in raw[:60]:
            xml = raw.decode('utf-16')
        else:
            xml = raw.decode('utf-8-sig')
        task = ET.fromstring(xml)
        ns = {'t':TASK_NS}
        interval = task.findtext('t:Triggers/t:TimeTrigger/t:Repetition/t:Interval', namespaces=ns)
        enabled = task.findtext('t:Settings/t:Enabled', namespaces=ns) != 'false'
        logon = (task.find('t:Triggers/t:LogonTrigger', ns) is not None and
                 task.findtext('t:Triggers/t:LogonTrigger/t:Enabled', namespaces=ns) != 'false')
        timer = task.findtext('t:Triggers/t:TimeTrigger/t:Enabled', namespaces=ns) != 'false'
        args = task.findtext('t:Actions/t:Exec/t:Arguments', namespaces=ns)
        command = task.findtext('t:Actions/t:Exec/t:Command', namespaces=ns) or ''
        expected = '"' + str(Path(root).resolve() / 'scripts/start_silent.vbs') + '"'
        return {'active':enabled and logon and timer and interval == 'PT5M' and args == expected
                        and Path(command).name.lower() == 'wscript.exe',
                'name':WATCHDOG,'interval_minutes':5 if interval == 'PT5M' else None}
    except (ET.ParseError, UnicodeError):
        return missing


def install_watchdog(root=PROJECT_ROOT):
    if os.name != 'nt':
        raise RuntimeError('Windows Task Scheduler is required')
    import csv
    identity = subprocess.run(['whoami.exe','/user','/fo','csv','/nh'], capture_output=True,
                              text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if identity.returncode:
        raise RuntimeError('Cannot identify the current Windows user')
    sid = next(csv.reader(identity.stdout.splitlines()))[1]
    if not sid.startswith('S-1-'):
        raise RuntimeError('Invalid Windows user SID')
    path = Path(root) / 'data/watchdog_task.xml'
    path.parent.mkdir(exist_ok=True)
    path.write_text(watchdog_xml(sid, root), encoding='utf-16')
    result = _task_command(['/Create','/F','/TN',WATCHDOG,'/XML',str(path.resolve())])
    if result.returncode:
        raise RuntimeError('Windows refused watchdog registration (exit ' + str(result.returncode) + ')')
    status = watchdog_status(root)
    if not status['active']:
        raise RuntimeError('Watchdog registration did not pass trigger verification')
    return status


def pc_runtime():
    """Only safe operational metadata may be committed to the public heartbeat."""
    return {'watchdog_active':watchdog_status()['active'], 'pid':os.getpid(),
            'executable':Path(__import__('sys').executable).name.lower()}


def startup_path(appdata: str | Path | None = None) -> Path:
    directory = appdata or os.environ.get('APPDATA')
    if not directory:
        raise RuntimeError('Windows APPDATA is unavailable on this node')
    return Path(directory) / 'Microsoft/Windows/Start Menu/Programs/Startup/NovelPulse_Autopilot.vbs'


def launcher_text(root: str | Path = PROJECT_ROOT) -> str:
    root = Path(root).resolve()
    launcher = root / 'scripts/start_silent.vbs'
    if not launcher.is_file():
        raise FileNotFoundError('scripts/start_silent.vbs is missing')
    # VBScript strings escape quotes by doubling them.
    command = 'wscript.exe "' + str(launcher) + '"'
    return (MARKER + '\nOption Explicit\nDim shell\n'
            'Set shell = CreateObject("WScript.Shell")\n'
            'shell.CurrentDirectory = "' + str(root).replace('"', '""') + '"\n'
            'shell.Run "' + command.replace('"', '""') + '", 0, False\n')


def install_startup(*, appdata=None, root=PROJECT_ROOT) -> dict:
    if os.name != 'nt' and appdata is None:
        raise RuntimeError('Startup registration is available on Windows only')
    target = startup_path(appdata)
    text = launcher_text(root)
    if target.exists():
        try:
            managed = target.read_text(encoding='utf-16').startswith(MARKER)
        except UnicodeError:
            managed = False
        if not managed:
            raise RuntimeError('Startup file is not managed by NovelPulse; refusing to overwrite it')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.vbs.tmp')
    # Windows Script Host supports UTF-16 BOM, including non-ASCII project paths.
    temporary.write_text(text, encoding='utf-16')
    temporary.replace(target)
    return startup_status(appdata=appdata, root=root)


def startup_status(*, appdata=None, root=PROJECT_ROOT) -> dict:
    try:
        target = startup_path(appdata)
        installed = target.is_file() and target.read_text(encoding='utf-16') == launcher_text(root)
        return {'installed': installed, 'path': str(target), 'trigger': 'Windows user login'}
    except (OSError, UnicodeError, RuntimeError):
        return {'installed': False, 'path': None, 'trigger': 'unavailable on this node'}


def main():
    parser = argparse.ArgumentParser(description='Register NovelPulse at Windows login')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--install-watchdog', action='store_true')
    args = parser.parse_args()
    print(json.dumps(install_watchdog() if args.install_watchdog else
                     install_startup() if args.install else startup_status(), ensure_ascii=True))


if __name__ == '__main__':
    main()
