"""Local creator-session bridge. Credentials stay inside the adapter directory."""
from __future__ import annotations
import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
import process_runner

ROOT = Path(__file__).resolve().parent
ADAPTER_SHA256 = 'e8963ebc3f04b3d54e02a4c51f61e1f47eea426e55b337dc30100f64e752426f'
BRIDGE_SHA256 = 'c757f9e6359f1ee4e5248b142cc9fd4069f5bcfe6e89727cd5b28c0b8c394e45'


class BeforeSubmission(RuntimeError):
    """Positive evidence that the final publish action was never entered."""
    def __init__(self,message,phase='pre_submit'):
        super().__init__(message)
        self.phase=phase


def invoke(bound_user='', mode='check', request_file=None, cancel=None, root=None, progress_callback=None):
    root = Path(root or ROOT)
    adapter = root/'adapters/xiaohongshu-mcp'
    binary = root/'xhs-session-windows-amd64.exe'
    if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != BRIDGE_SHA256:
        raise ValueError('创作者中心检查工具缺失或版本不匹配，请完整覆盖更新包')
    if not adapter.is_dir():
        raise ValueError('小红书适配器尚未安装')
    timeout = 1800 if mode == 'publish' else 900 if mode == 'login' else 420 if mode == 'prepare' else 180 if mode=='read-comments' else 75
    args = [str(binary), '--mode', mode, '--timeout', str(timeout), '--expected-user', str(bound_user)]
    if request_file is not None:
        args += ['--request-file', str(Path(request_file).resolve())]
    debug=root.parent/'logs'/('creator-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    debug.mkdir(parents=True,exist_ok=True)
    if progress_callback is not None:progress_callback(debug)
    args += ['--result-file',str(debug/'result.json')]
    result = process_runner.run(args, cwd=adapter, timeout=timeout+15, cancel=cancel,
                                env={'COOKIES_PATH':str(adapter/'cookies.json'),'COLDSTART_DEBUG_DIR':str(debug)})
    (debug/'process.log').write_text(result.stderr if hasattr(result,'stderr') else '',encoding='utf-8')
    try:
        value = json.loads(result.stdout.strip())
        if not isinstance(value,dict) or value.get('version')!='0.2.2' or value.get('mode')!=mode:
            raise ValueError()
        if bound_user and value.get('user_id') != bound_user and value.get('main_ok') is True:
            raise ValueError()
        if value.get('creator_ready') is True and value.get('creator_user_id') != value.get('user_id'):
            raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeError('创作者中心工具未返回有效回执；发布时请先核对结果') from None
    if result.returncode or value.get('main_ok') is not True or (mode!='read-comments' and value.get('creator_ready') is not True):
        detail = str(value.get('detail') or '创作者中心授权检查未通过')[:1000]
        if value.get('stage') in ('pre_submit','uploading','editing'):
            raise BeforeSubmission(detail+'；尚未调用最终发布动作',value['stage'])
        raise RuntimeError(detail+'；请核对平台结果后再操作')
    if mode != 'publish':
        cookie = adapter/'cookies.json'
        if not cookie.is_file() or value.get('cookie_sha256') != hashlib.sha256(cookie.read_bytes()).hexdigest():
            raise ValueError('检查期间会话文件发生变化，请重新检查')
    return value


def historical_proof(row, attempt, completed_at, logs, adapter_binary):
    """Accept only the pinned upstream's exact constructor error, with time matching."""
    binary = Path(adapter_binary)
    if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest()!=ADAPTER_SHA256:
        raise ValueError('无法核对原小红书适配器版本，不自动恢复')
    start = datetime.fromisoformat(attempt['started_at'])
    end = datetime.fromisoformat(completed_at)+timedelta(seconds=2)
    expected = '发布内容失败: title='+row['title']+' 创作者中心登录失效，请重新扫码登录'
    matches = []
    for path in Path(logs).glob('xhs-service-*.log'):
        raw = path.read_bytes()
        if len(raw)>20*1024*1024:
            continue
        text = raw.decode('utf-8','replace')
        if 'xiaohongshu-mcp version: v2.7.3' not in text:
            continue
        lines = text.splitlines()
        for i,line in enumerate(lines):
            match = re.fullmatch(r'time="([^"]+)" level=error msg="(.*)"',line)
            if not match or match[2]!=expected:
                continue
            timestamp = datetime.fromisoformat(match[1])
            if not start<=timestamp<=end or not 0<=(timestamp-start).total_seconds()<=180:
                continue
            if not any('msg="POST /api/v1/publish 500"' in item for item in lines[i+1:i+3]):
                continue
            matches.append({'type':'pinned_creator_constructor_failed_before_upload',
                            'file':path.name,'line':i+1,'sha256':hashlib.sha256(raw).hexdigest(),
                            'occurred_at':match[1],'adapter_sha256':ADAPTER_SHA256})
    if len(matches)!=1:
        raise ValueError('未找到唯一、可核对的上传前失败证据，保持原提交状态')
    return matches[0]
