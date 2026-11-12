"""输入校验、内容完整性和本机单实例锁。"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

PUBLISH_PLATFORMS = {'xiaohongshu'}
CONTENT_LIMITS = {'xiaohongshu':(20,1000)}


def text(value, label, maximum, allow_empty=False):
    if not isinstance(value,str):
        raise ValueError(label+'必须是文本')
    value=value.strip()
    if len(value)>maximum or (not value and not allow_empty) or '\x00' in value:
        raise ValueError(f'{label}不能为空、不能含空字符且最多{maximum}字' if not allow_empty else f'{label}最多{maximum}字，不能含空字符')
    return value


def integer(value,label,low,high):
    if isinstance(value,bool) or not isinstance(value,(int,str)):
        raise ValueError(label+'需要整数')
    try: result=int(value)
    except (ValueError,TypeError): raise ValueError(label+'需要整数') from None
    if not low<=result<=high:
        raise ValueError(f'{label}应在{low}至{high}之间')
    return result


def content(platform,title,body,tags,cards=None):
    if platform not in PUBLISH_PLATFORMS:
        raise ValueError('该平台尚未实现内容生成与发布')
    title_limit,body_limit=CONTENT_LIMITS[platform]
    title=text(title,'标题',title_limit)
    body=text(body,'正文',body_limit)
    if not isinstance(tags,list) or len(tags)>5:
        raise ValueError('标签需要最多5项的列表')
    tags=list(dict.fromkeys(text(t,'标签',30).lstrip('#') for t in tags))
    if any(not tag for tag in tags):
        raise ValueError('标签不能为空')
    result={'platform':platform,'title':title,'body':body,'tags':tags}
    if cards is not None:
        if not isinstance(cards,list) or not 1<=len(cards)<=9:
            raise ValueError('需要1至9张卡片；不能静默丢弃或裁切卡片')
        validated=[]
        for card in cards:
            if not isinstance(card,dict): raise ValueError('卡片结构不正确')
            validated.append({'title':text(card.get('title'),'卡片标题',20),'body':text(card.get('body'),'卡片正文',130)})
        result['cards']=validated
    return result


def image_manifest(paths):
    from PIL import Image, UnidentifiedImageError
    if not isinstance(paths,list) or len(paths)>12:
        raise ValueError('素材需要最多12项的文件列表')
    manifest=[]
    for value in paths:
        if not isinstance(value,str) or not value or '\x00' in value:
            raise ValueError('素材路径不正确')
        path=Path(value).expanduser().resolve()
        if not path.is_file() or not 0<path.stat().st_size<=20*1024*1024:
            raise ValueError('素材不存在、为空或超过20MB：'+str(path))
        try:
            with Image.open(path) as image:
                if image.format not in ('PNG','JPEG','WEBP') or image.width*image.height>40_000_000:
                    raise ValueError('图片格式或尺寸不支持')
                image_format=image.format
                image.verify()
        except (OSError,UnidentifiedImageError,Image.DecompressionBombError,Image.DecompressionBombWarning,ValueError):
            raise ValueError('素材必须是有效的PNG/JPEG/WEBP图片：'+str(path)) from None
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.append({'path':str(path),'sha256':digest,'bytes':path.stat().st_size,'format':image_format})
    return manifest


def approval_digest(row,manifest,target):
    fields={k:row[k] for k in ('platform','title','body','tags')}
    fields['manifest']=manifest
    fields['target']=target
    return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()


def publish_url(value,platform):
    value=text(value,'作品链接',4000)
    parsed=urlsplit(value)
    domains={'xiaohongshu':('xiaohongshu.com','xhslink.com')}
    host=(parsed.hostname or '').lower()
    if parsed.scheme!='https' or parsed.username or parsed.password or not any(host==d or host.endswith('.'+d) for d in domains.get(platform,())):
        raise ValueError('请填写该平台的https作品链接')
    if not parsed.path.strip('/'):
        raise ValueError('需要具体作品链接，不能只填写平台首页')
    return value


def source_time(value):
    value=str(value or '').strip()
    if value.isdigit() and 9<=len(value)<=14:
        try:
            stamp=int(value)/(1000 if len(value)>=12 else 1)
            return datetime.fromtimestamp(stamp,timezone(timedelta(hours=8))).isoformat(timespec='seconds')
        except (OSError,ValueError,OverflowError): return value[:100]
    return value[:100]


class InstanceLock:
    """仅锁本程序所选数据目录；进程退出后由操作系统释放。"""
    def __init__(self,directory):
        directory=Path(directory).resolve()
        directory.mkdir(parents=True,exist_ok=True)
        self.file=open(directory/'run.lock','a+b')
        self.file.seek(0,2)
        if self.file.tell()==0:
            self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (OSError,IOError):
            self.file.close()
            raise ValueError('这个数据目录已由另一个工作台实例使用，请先关闭原实例') from None
    def close(self):
        if self.file.closed:return
        self.file.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
        self.file.close()
    def __enter__(self):return self
    def __exit__(self,*_args):self.close()
