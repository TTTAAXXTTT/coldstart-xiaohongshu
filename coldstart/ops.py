"""Windows 总包入口使用的本地配置、运行控制、诊断与一致性备份。"""
from __future__ import annotations

import argparse
import getpass
import importlib.metadata
import json
import os
import sqlite3
import sys
import tempfile
import secrets
import zipfile
from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import validation
import model_api

ROOT=Path(__file__).resolve().parent
CST=timezone(timedelta(hours=8))
CHANNELS={'xiaohongshu'}


def timestamp():return datetime.now(CST).isoformat(timespec='seconds')


@contextmanager
def connection(directory,readonly=False):
    path=Path(directory).resolve()/'coldstart.sqlite3'
    if not path.is_file():raise ValueError('数据尚未初始化，请先运行安装与配置入口')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=10) if readonly else sqlite3.connect(path,timeout=10)
    db.row_factory=sqlite3.Row
    try:
        with db:yield db
    finally:db.close()


def read_config(directory):
    with connection(directory,True) as db:
        row=db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
        if not row:raise ValueError('数据设置不完整')
        return json.loads(row[0])


def prepare_local_auth(directory):
    """Create a private per-install service token before the owned service starts."""
    from urllib.parse import urlsplit
    with validation.InstanceLock(directory):
        config=read_config(directory);source=urlsplit(config['radar']['xhs_url'])
        managed=source.scheme=='http' and source.hostname in ('127.0.0.1','localhost') and source.port==18060 and source.path.rstrip('/')=='' and not source.query
        if not managed:return {'ok':True,'managed':False,'generated':False}
        generated=not config['radar'].get('xhs_token')
        if generated:
            config['radar']['xhs_token']=secrets.token_urlsafe(32)
            with connection(directory) as db:db.execute("UPDATE meta SET value=? WHERE key='settings'",(json.dumps(config,ensure_ascii=False),))
    return {'ok':True,'managed':True,'local_auth_ready':True,'generated':generated}


def status(directory):
    directory=Path(directory).resolve()
    result={'data_exists':(directory/'coldstart.sqlite3').is_file(),'python':sys.version.split()[0],'model_configured':False,'live_network':'NOT_TESTED','live_accounts':'NOT_TESTED','publishing_evidence':'NOT_VERIFIED'}
    try:result['pillow']=importlib.metadata.version('Pillow')
    except importlib.metadata.PackageNotFoundError:result['pillow']='NOT_INSTALLED'
    if not result['data_exists']:return result
    config=read_config(directory)
    from urllib.parse import urlsplit
    source=urlsplit(config['radar']['xhs_url'])
    managed=source.scheme=='http' and source.hostname in ('127.0.0.1','localhost') and source.port==18060 and source.path.rstrip('/')=='' and not source.query
    result.update(model=config['ai']['model'],model_configured=all(config['ai'][k] for k in ('base_url','model','api_key')),channels=config['publishing']['channels'],publishing_enabled=config['publishing']['enabled'],autonomy_enabled=config.get('autonomy',{}).get('enabled',False),manage_local_xhs=managed)
    with connection(directory,True) as db:
        meta=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('pause_requested','shutdown_requested')"))
        result.update(paused=meta.get('pause_requested')=='1',shutdown_requested=meta.get('shutdown_requested')=='1',jobs=dict(Counter(row[0] for row in db.execute('SELECT state FROM jobs'))),drafts=dict(Counter(row[0] for row in db.execute('SELECT state FROM drafts'))))
        saved=db.execute("SELECT value FROM meta WHERE key='diagnostics'").fetchone()
        if saved:
            evidence=json.loads(saved[0])
            items=evidence.get('items',[])
            result['last_check_time']=evidence.get('time','')
            result['last_checks']=[{'component':i['component'],'status':i['status']} for i in items]
            model=next((i['status'] for i in items if i['component']=='模型'),None)
            if model:result['live_network']=model.upper()+'_LAST_CHECK'
            accounts=[i['status'] for i in items if i['component'] in ('小红书账号','小红书创作者中心')]
            if accounts:result['live_accounts']=('PASSED' if len(accounts)==2 and all(x=='passed' for x in accounts) else 'INCOMPLETE')+'_LAST_CHECK'
        verified=db.execute("SELECT COUNT(*) FROM drafts WHERE state='published' AND public_url<>''").fetchone()[0]
        result['verified_published_notes']=verified
        if verified:result['publishing_evidence']='VERIFIED_LINKS_RECORDED'
        result['evidence_note']='以上为带时间的已记录检查与作品回执；启动不会自动重新验证或保证获客'
    # 故意不导出凭证、用户原文、客户、评论正文或可能带原始响应的日志。
    return result


def configure(directory,channels=None,key=None,interactive=False,replace_key=False,provider=None,base_url=None,model=None):
    from app import Workbench
    if channels is not None and (not channels or any(p not in CHANNELS for p in channels)):
        raise ValueError('仅支持小红书渠道xiaohongshu')
    if provider is not None and provider not in model_api.PRESETS:
        raise ValueError('未知模型服务商')
    with validation.InstanceLock(directory):
        w=Workbench(directory)
        config=w.config()
        chosen=channels if channels is not None else config['publishing']['channels']
        current=config['ai']
        selection=provider
        if interactive:
            if not sys.stdin.isatty():raise ValueError('模型配置需要真实交互控制台；未读取或显示凭证')
            choices=list(model_api.PRESETS)
            print('Model provider / 模型服务商：')
            for index,name in enumerate(choices,1):print(str(index)+'. '+model_api.PRESETS[name]['name'])
            answer=input('选择编号（回车保留当前接口）: ').strip()
            if answer:
                if answer not in [str(i) for i in range(1,len(choices)+1)]:raise ValueError('服务商编号无效，配置未保存')
                selection=choices[int(answer)-1]
        defaults=model_api.PRESETS[selection] if selection else current
        url=base_url if base_url is not None else os.environ.get('COLDSTART_BASE_URL') or defaults['base_url']
        selected_model=model if model is not None else os.environ.get('COLDSTART_MODEL') or defaults['model']
        if interactive:
            url=input('Base URL ['+url+']: ').strip() or url
            selected_model=input('模型名称 ['+selected_model+']: ').strip() or selected_model
        changed_origin=model_api.origin(url)!=model_api.origin(current['base_url'])
        if key is None:
            key=''
            if changed_origin or not current['api_key'] or replace_key:
                env_name=model_api.PRESETS[model_api.provider_id(url)]['key_env']
                key=os.environ.get('COLDSTART_API_KEY','') or (os.environ.get(env_name,'') if env_name else '')
        if interactive and not key:
            prompt='API Key（输入隐藏；回车稍后在工作台填写）: ' if changed_origin or not current['api_key'] else 'API Key（输入隐藏；回车保留已有）: '
            key=getpass.getpass(prompt).strip()
        patch={'ai':{'base_url':url,'model':selected_model},'radar':{'xhs_enabled':'xiaohongshu' in chosen},'publishing':{'channels':list(dict.fromkeys(chosen))}}
        if key:patch['ai']['api_key']=key
        w.save_settings(patch)
    return status(directory)


def control(directory,action):
    values={'pause':{'pause_requested':'1'},'resume':{'pause_requested':'0'},'shutdown':{'shutdown_requested':'1'},'reset':{'pause_requested':'0','shutdown_requested':'0'},'prepare_start':{'shutdown_requested':'0'}}
    if action not in values:raise ValueError('控制动作无效')
    if action in ('reset','prepare_start'):
        # 只允许在数据目录无人使用时重置；不能误恢复另一个正在运行的手动实例。
        with validation.InstanceLock(directory):return _write_control(directory,values[action])
    return _write_control(directory,values[action])


def _write_control(directory,values):
    with connection(directory) as db:
        db.execute('BEGIN IMMEDIATE')
        for key,value in values.items():db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))
    return {'ok':True,'control':values,'note':'暂停/停止在当前任务结束后生效；不会撤回已提交内容'}


def enable(directory):
    from app import Workbench
    with validation.InstanceLock(directory):
        w=Workbench(directory)
        if not w.config()['ai']['api_key']:raise ValueError('尚未配置Key，请先运行安装与配置入口')
        w.save_settings({'autonomy':{'enabled':True},'schedule':{'enabled':False}})
    return status(directory)


def enable_public(directory):
    from app import Workbench
    import public_engagement
    with validation.InstanceLock(directory):
        w=Workbench(directory)
        public_engagement.control(w,'start')
    return status(directory)


def enqueue_check(directory):
    moment=timestamp()
    with connection(directory) as db:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute("SELECT id FROM jobs WHERE kind='check' AND state IN ('pending','running') ORDER BY id LIMIT 1").fetchone()
        job_id=existing[0] if existing else db.execute("INSERT INTO jobs(kind,payload,state,created_at,updated_at) VALUES('check','{}','pending',?,?)",(moment,moment)).lastrowid
    return {'job_id':job_id,'note':'已排队；运行中的工作台会检查模型及账号。模型短请求会计入调用名额'}


def backup(directory,destination):
    directory=Path(directory).resolve();destination=Path(destination).resolve()
    if destination==directory or directory in destination.parents:raise ValueError('备份目标必须在data目录之外，避免递归打包')
    destination.mkdir(parents=True,exist_ok=True)
    with validation.InstanceLock(directory):
        with connection(directory) as source,tempfile.TemporaryDirectory(prefix='.work-',dir=destination) as temp:
            stable=Path(temp)/'coldstart.sqlite3'
            target=sqlite3.connect(stable)
            try:source.backup(target)
            finally:target.close()
            paths=[]
            for path in directory.rglob('*'):
                if not path.is_file():continue
                if not path.resolve().is_relative_to(directory):raise ValueError('数据目录有指向外部的文件，停止备份')
                relative=path.relative_to(directory)
                if relative.parts[0]=='backups' or path.name in ('run.lock','coldstart.sqlite3','coldstart.sqlite3-wal','coldstart.sqlite3-shm'):continue
                paths.append((path,relative.as_posix()))
            name='coldstart-data-'+datetime.now(CST).strftime('%Y%m%d-%H%M%S-%f')+'.zip'
            output=destination/name
            with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as z:
                z.write(stable,'data/coldstart.sqlite3')
                for path,relative in paths:z.write(path,'data/'+relative)
            with zipfile.ZipFile(output) as z:
                if z.testzip() is not None:raise ValueError('备份压缩包校验失败')
    return {'backup':str(output),'files':len(paths)+1,'private':True,'note':'备份包含本机凭证，仅供自己恢复；没有自动上传'}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Coldstart local operations')
    parser.add_argument('--data-dir',default=str(ROOT/'data'))
    sub=parser.add_subparsers(dest='command',required=True)
    cfg=sub.add_parser('configure');cfg.add_argument('--channels');cfg.add_argument('--interactive',action='store_true');cfg.add_argument('--replace-key',action='store_true');cfg.add_argument('--provider',choices=list(model_api.PRESETS));cfg.add_argument('--base-url');cfg.add_argument('--model')
    ctl=sub.add_parser('control');ctl.add_argument('action',choices=['pause','resume','shutdown','reset','prepare_start'])
    diag=sub.add_parser('status');diag.add_argument('--output')
    sub.add_parser('enable');sub.add_parser('enable-public');sub.add_parser('check');sub.add_parser('prepare-local-auth')
    back=sub.add_parser('backup');back.add_argument('--destination',default=str(ROOT.parent/'backups'))
    args=parser.parse_args(argv)
    try:
        if args.command=='configure':result=configure(args.data_dir,args.channels.split(',') if args.channels else None,interactive=args.interactive,replace_key=args.replace_key,provider=args.provider,base_url=args.base_url,model=args.model)
        elif args.command=='control':result=control(args.data_dir,args.action)
        elif args.command=='enable':result=enable(args.data_dir)
        elif args.command=='enable-public':result=enable_public(args.data_dir)
        elif args.command=='backup':result=backup(args.data_dir,args.destination)
        elif args.command=='check':result=enqueue_check(args.data_dir)
        elif args.command=='prepare-local-auth':result=prepare_local_auth(args.data_dir)
        else:
            result=status(args.data_dir)
            if args.output:Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,sqlite3.Error) as error:
        print(json.dumps({'error':str(error)},ensure_ascii=False),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
