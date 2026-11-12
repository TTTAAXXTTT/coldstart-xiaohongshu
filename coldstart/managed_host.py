"""持有本次外部服务的Windows Job句柄；宿主退出时回收自己的服务和浏览器。"""
from __future__ import annotations
import argparse
import os
import subprocess
import sqlite3
import json
import time
from contextlib import ExitStack, closing
from pathlib import Path
import process_runner


def option(command,name):
    try:return command[command.index(name)+1]
    except (ValueError,IndexError):return None


def shutdown_requested(command):
    directory=option(command,'--data-dir')
    if not directory:return False
    path=Path(directory).resolve()/'coldstart.sqlite3'
    if not path.is_file():return False
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            row=db.execute("SELECT value FROM meta WHERE key='shutdown_requested'").fetchone()
        return bool(row and row[0]=='1')
    except sqlite3.Error:return False


def stop_requested(args,command):
    return bool(args.stop_file and Path(args.stop_file).is_file()) or shutdown_requested(command)


def request_data_shutdown(command):
    directory=option(command,'--data-dir')
    if not directory:return False
    path=Path(directory).resolve()/'coldstart.sqlite3'
    if not path.is_file():return False
    try:
        with closing(sqlite3.connect(path,timeout=1)) as db,db:db.execute("INSERT INTO meta(key,value) VALUES('shutdown_requested','1') ON CONFLICT(key) DO UPDATE SET value='1'")
        return True
    except sqlite3.Error:return False


def related_environment(command):
    directory=option(command,'--data-dir')
    if not directory:raise ValueError('Owned authenticated service requires a data directory')
    try:
        path=Path(directory).resolve()/'coldstart.sqlite3'
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            row=db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
        token=json.loads(row[0])['radar']['xhs_token']
        if not isinstance(token,str) or not token.strip():raise ValueError()
    except (OSError,sqlite3.Error,TypeError,KeyError,ValueError):raise ValueError('Local service authentication is not prepared; run the installer/start entrypoint') from None
    return {**os.environ,'AUTH_TOKEN':token}


def pid_alive(pid):
    if not isinstance(pid,int) or pid<=4:return True
    if os.name!='nt':
        try:os.kill(pid,0);return True
        except ProcessLookupError:return False
        except PermissionError:return True
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)];kernel.GetExitCodeProcess.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:return ctypes.get_last_error()!=87
    try:
        code=wintypes.DWORD()
        return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
    finally:kernel.CloseHandle(handle)


def clear_owned_ready(args,command,process):
    value=option(command,'--ready-file');directory=option(command,'--data-dir')
    if not value or not directory or process is None:return
    path=Path(value).resolve();scope=Path(args.cwd).resolve().parent
    if not path.is_relative_to(scope):return
    try:
        record=json.loads(path.read_text(encoding='utf-8'))
        if Path(record.get('data_dir','')).resolve()!=Path(directory).resolve():return
        # Windows venv redirectors can report a descendant PID. Never delete
        # another live process's readiness record; wait briefly for our group.
        deadline=time.monotonic()+2
        while record.get('pid')!=process.pid and pid_alive(record.get('pid')) and time.monotonic()<deadline:time.sleep(.05)
        if record.get('pid')==process.pid or not pid_alive(record.get('pid')):path.unlink()
    except (OSError,ValueError,TypeError):pass


def run_once(args,command):
    log=Path(args.log);log.parent.mkdir(parents=True,exist_ok=True)
    job=process_runner.WindowsJob() if os.name=='nt' else None
    children=[]
    process=None
    with ExitStack() as stack:
        output=stack.enter_context(log.open('ab',buffering=0))
        def launch(command,cwd,stream,environment=None):
            options={'creationflags':subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP|0x00000004} if job else {'start_new_session':True}
            child=subprocess.Popen(command,cwd=cwd,stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,env=environment,**options)
            children.append(child)
            if job:job.attach_and_resume(child)
            return child
        try:
            if args.related_exe:
                if not args.related_cwd or not args.related_log:raise ValueError('Related service requires cwd and log')
                stream=stack.enter_context(Path(args.related_log).open('ab',buffering=0))
                environment=related_environment(command) if args.related_auth_from_data else None
                related=launch([args.related_exe,*args.related_arg],args.related_cwd,stream,environment)
                time.sleep(.8)
                if related.poll() is not None:raise RuntimeError('Related service exited before backend startup')
            process=launch(command,args.cwd,output)
            stop_seen=None;stop_written=False
            while process.poll() is None:
                if args.stop_file and Path(args.stop_file).is_file() and not stop_written:
                    stop_seen=stop_seen or time.monotonic()
                    stop_written=request_data_shutdown(command)
                    if not stop_written and time.monotonic()-stop_seen>10:return 0
                if args.related_exe and related.poll() is not None:raise RuntimeError('Related service exited; stopping the owned service group')
                time.sleep(.1)
            return process.returncode
        finally:
            if job:job.close()
            for child in children:
                if child.poll() is None:
                    if os.name=='nt':child.kill();child.wait(timeout=5)
                    else:process_runner.stop_owned_process(child)
            clear_owned_ready(args,command,process)


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--cwd',required=True)
    parser.add_argument('--log',required=True)
    parser.add_argument('--related-exe')
    parser.add_argument('--related-cwd')
    parser.add_argument('--related-log')
    parser.add_argument('--related-arg',action='append',default=[])
    parser.add_argument('--related-auth-from-data',action='store_true')
    parser.add_argument('--restart',action='store_true')
    parser.add_argument('--restart-delay',type=float,default=5)
    parser.add_argument('--stop-file')
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command:parser.error('Missing service command')
    if not .1<=args.restart_delay<=60:parser.error('Restart delay must be 0.1 to 60 seconds')
    attempts=0
    while True:
        if stop_requested(args,command):return 0
        try:code=run_once(args,command)
        except Exception as error:
            with Path(args.log).open('a',encoding='utf-8') as output:output.write('Owned service failure: '+str(error)+'\n')
            code=1
        if stop_requested(args,command):return 0
        if code==0 or not args.restart:return code
        attempts+=1;delay=min(60,args.restart_delay*2**min(attempts-1,5))
        with Path(args.log).open('a',encoding='utf-8') as output:output.write(f'Restarting owned services after failure; attempt {attempts}, wait {delay:g}s\n')
        deadline=time.monotonic()+delay
        while time.monotonic()<deadline:
            if stop_requested(args,command):return 0
            time.sleep(min(.25,max(0,deadline-time.monotonic())))


if __name__=='__main__':raise SystemExit(main())
