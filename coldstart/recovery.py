"""Current incidents and read-only recovery checks; job history is immutable.

GET state only reads local evidence. Network probes run as explicit worker jobs.
"""
import hashlib,json,re
from datetime import datetime,timedelta

CHECKS={'model':'model_check','writing':'writing_check','account':'source_check',
        'creator':'publish_check','search':'search_check','cards':'cards_check'}
KINDS={'model_check':'model','writing_check':'writing','source_check':'account',
       'publish_check':'creator','search_check':'search','cards_check':'cards'}


def scope(w, capability):
    c=w.config()
    if capability in ('model','writing'):
        value=[c['ai']]
        if capability=='writing':value.append(c['business'])
    else:
        value=[c['radar']['xhs_url'],c['radar']['xhs_token'],w.meta('xhs_bound_user')]
        if capability in ('search','scan'):value.extend([c['business'],c['radar']['search_mode']])
        if capability=='cards':
            with w.db() as db:value.append([tuple(r) for r in db.execute('SELECT id,assets,updated_at FROM drafts ORDER BY id')])
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def capability(row):
    kind=row['kind'];message=row.get('message','')
    if kind=='login':
        return 'account' if re.search('主站|绑定|当前账号|真实登录',message) else 'creator'
    if kind in ('scan','public_scan'):return 'scan'
    if kind=='link_check':return 'link'
    if kind in ('public_monitor','sync_xhs') and re.search('登录|绑定身份|当前账号',message):return 'account'
    if kind in ('generate','qualify','rewrite','reply_draft','autopilot') and re.search(r'欠费|AccountOverdueError|HTTP (?:401|402|403|429|5\d\d)',message):return 'model'
    return KINDS.get(kind,'')


def failures(w):
    with w.db() as db:
        rows=[dict(r) for r in db.execute("SELECT * FROM jobs WHERE id IN (SELECT MAX(id) FROM jobs WHERE state IN ('done','failed','needs_setup','needs_review') AND kind<>'recovery_check' GROUP BY kind) AND state IN ('failed','needs_setup','needs_review') ORDER BY id DESC")]
        # An ordinary model test can finish using the fallback. Keep the old
        # primary incident until primary evidence exists, even after that done job.
        check=json.loads(w.meta('sales_checks') or '{}').get('model_check',{})
        if check.get('status')=='passed' and '本次实际使用备用模型' in check.get('detail','') and not any(r['kind']=='model_check' for r in rows):
            old=db.execute("SELECT * FROM jobs WHERE kind='model_check' AND state IN ('failed','needs_setup') ORDER BY id DESC LIMIT 1").fetchone()
            if old:rows.append(dict(old))
        return rows


def record_success(w, cap, initial_scope, detail='检查通过'):
    if initial_scope!=scope(w,cap):return
    from app import now,dump
    ids=[r['id'] for r in failures(w) if capability(r)==cap]
    w.meta('recovery_success:'+cap,dump({'scope':initial_scope,'time':now(),'jobs':ids,'detail':detail}))


def check_proof(w, kind, failed_at):
    """A later successful compatible probe must match the CURRENT settings."""
    import sales
    caps={'account':('source_check','publish_check'), 'creator':('publish_check',),
          'model':('model_check','writing_check'), 'writing':('writing_check',),
          'search':('search_check',),'cards':('cards_check',)}
    rows=json.loads(w.meta('sales_checks') or '{}').values()
    for row in sorted(rows,key=lambda r:r.get('time',''),reverse=True):
        if row.get('kind') not in caps.get(kind,()):continue
        if row.get('status')!='passed' or row.get('time','')<=failed_at:continue
        if row.get('config_stamp')!=sales.check_stamp(w,row['kind']):continue
        # A working fallback is not evidence that a failed primary recovered.
        if kind=='model' and '本次实际使用备用模型' in row.get('detail',''):continue
        return {'time':row['time'],'detail':'后续'+({'source_check':'账号','publish_check':'发稿准备','model_check':'模型','writing_check':'写作'}.get(row['kind'],'专项'))+'检查已通过'}
    return None


def recovered_check(w,item):
    cap=capability({**item,'message':item.get('detail','')})
    proof=check_proof(w,cap,item['time']) if cap else None
    if proof:return proof
    if cap and cap!='link':
        evidence=json.loads(w.meta('recovery_success:'+cap) or '{}')
        if evidence.get('time','')>item['time'] and evidence.get('scope')==scope(w,cap):return evidence
    return None


def resolved_job(w,row):
    cap=capability(row)
    if cap:
        evidence=json.loads(w.meta('recovery_success:'+cap) or '{}')
        if row['id'] in evidence.get('jobs',[]) and evidence.get('scope')==scope(w,cap):return evidence
        proof=check_proof(w,cap,row['updated_at'])
        if proof:return proof
    payload=json.loads(row['payload'])
    if cap=='link':
        evidence=json.loads(w.meta('recovery_link:'+str(row['id'])) or '{}')
        if evidence.get('scope')==scope(w,'link'):return evidence
        import sales
        item=json.loads(w.meta('sales_checks') or '{}').get('link_check:'+str(payload.get('lead_id')), {})
        if item.get('status')=='passed' and item.get('time','')>row['updated_at'] and item.get('config_stamp')==sales.check_stamp(w,'link_check'):return item
    if row['kind'] in ('publish','cards','rewrite') and payload.get('draft_id'):
        with w.db() as db:draft=db.execute('SELECT state,updated_at,assets FROM drafts WHERE id=?',(payload['draft_id'],)).fetchone()
        if draft and draft['state'] in ('published','cancelled'):
            return {'time':draft['updated_at'],'detail':'作品已回查确认' if draft['state']=='published' else '对应稿件已取消，旧排期不再执行'}
        if draft and draft['state']=='approved' and draft['updated_at']>row['updated_at']:
            return {'time':draft['updated_at'],'detail':'稿件已重新核对并批准'}
    if row['kind'] in ('scan','public_scan'):
        scan=json.loads(w.meta('scan_report') or '{}')
        if scan.get('time','')>row['updated_at'] and scan.get('status') in ('done','empty') and scan.get('config_scope')==scope(w,'scan'):
            return {'time':scan['time'],'detail':'后续搜索流程已正常完成'}
    return None


def snapshot(w):
    from app import now
    import workflow
    active=[];recovered=[];c=w.config();bound=w.meta('xhs_bound_user')
    def add(key,kind,title,detail,action,**extra):
        active.append({'id':key,'kind':kind,'title':title,'detail':detail,'action':action,**extra})
    if workflow.mode(c)!='manual' and not all(c['ai'][k] for k in ('base_url','model','api_key')):
        add('config:model','settings','模型配置还不完整','填写模型地址、模型名称和Key；保存后重新检测。','model')
    if workflow.mode(c)!='manual' and not bound:
        add('config:account','account','还没有确认小红书账号','登录后检查当前账号，检测不会自动提交内容。','account')
    with w.db() as db:
        unknown=db.execute("SELECT COUNT(*) FROM public_turns p JOIN public_threads t ON t.id=p.thread WHERE t.account=? AND p.state='unknown'",(bound,)).fetchone()[0]
        unknown_drafts=db.execute("SELECT COUNT(*) FROM drafts WHERE platform='xiaohongshu' AND state IN ('unknown','submitted_unverified')").fetchone()[0]
        failed_threads=db.execute("SELECT COUNT(*) FROM public_threads WHERE account=? AND state='active' AND errors>0",(bound,)).fetchone()[0]
        usage=dict(db.execute("SELECT kind,COUNT(*) FROM usage WHERE substr(time,1,10)=? GROUP BY kind",(now()[:10],)))
        jobs=[dict(r) for r in db.execute("SELECT id,payload,state,message,updated_at,origin FROM jobs WHERE kind='recovery_check' ORDER BY id DESC LIMIT 20")]
    if unknown:add('results:comments','comments',f'{unknown} 条评论发送结果待核对','只隔离对应作品；检测只回读原帖，未确认前保留提示并防止重发。','verify_comments')
    if unknown_drafts:add('results:drafts','content',f'{unknown_drafts} 篇内容发布结果待核对','只核对已有提交；未找到唯一证据时保持待核对，不自动重发。','verify_drafts')
    if failed_threads:add('source:threads','comments',f'{failed_threads} 段讨论原文暂未读到','重新检测只读取这些讨论，不拟稿、不发表评论。','verify_threads')
    for row in failures(w):
        if row['origin'] in workflow.AUTOMATIC and not workflow.enabled(row['kind'],c):continue
        proof=resolved_job(w,row)
        if proof:
            recovered.append({'id':'job:'+str(row['id']),'title':workflow.LABELS.get(row['kind'],row['kind'])+'异常已恢复','time':proof['time'],'detail':proof.get('detail','后续检查通过')})
            continue
        if row['kind']=='publish':
            payload=json.loads(row['payload'])
            with w.db() as db:draft=db.execute('SELECT state FROM drafts WHERE id=?',(payload.get('draft_id'),)).fetchone()
            if draft and draft['state'] in ('unknown','submitted_unverified'):continue
        if row['kind']=='public_monitor' and row['state']=='needs_review':
            # Current durable records describe the held/failed discussions. Old
            # aggregate job warnings do not create another permanent alert.
            continue
        cap=capability(row)
        action=cap if cap in CHECKS or cap in ('scan','link') else 'inspect'
        add('job:'+str(row['id']),'check',workflow.LABELS.get(row['kind'],row['kind'])+'上次未完成',w.redact(row['message']),action,job_id=row['id'])
    for section,key,usage_kind,label in (('ai','calls_per_day','llm','模型'),('public','comments_per_day','public_comment','主动评论'),('public','replies_per_day','public_reply','公开续答')):
        ceiling=c[section][key]
        if ceiling>=0 and usage.get(usage_kind,0)>=ceiling and workflow.mode(c)!='manual':
            add('quota:'+usage_kind,'settings',label+('次数设为0，当前不执行' if ceiling==0 else '今日用量已用完'),'调整上限或等待下一日；恢复后提示自动关闭。','local')
    attempts=json.loads(w.meta('recovery_attempts') or '{}')
    for issue in active:
        issue['checking']=False
        attempt=attempts.get(issue['id'],{})
        if attempt.get('scope')==scope(w,issue['action']) and issue['action'] not in ('local','inspect') and not issue['id'].startswith('config:'):
            issue['next_check_at']=attempt.get('next_at')
        for job in jobs:
            selection=json.loads(job['payload']).get('issue_id','all')
            if selection not in ('all',issue['id']):continue
            issue['checking']=job['state'] in ('pending','running')
            issue['check_state']=job['state'];issue['last_checked_at']=job['updated_at'];issue['check_detail']=w.redact(job['message'])
            break
    return {'active':active,'recovered':recovered[:8],'last_check':json.loads(w.meta('recovery_report') or '{}')}


def request(w,issue_id='all',origin='manual'):
    if not isinstance(issue_id,str):raise ValueError('异常编号必须为文本')
    state=snapshot(w)
    if issue_id!='all' and not any(i['id']==issue_id for i in state['active']):
        raise ValueError('该异常已恢复或提示已更新，请刷新查看')
    # Promote an automatic probe when the user explicitly asks while paused.
    with w.db() as db:
        job=db.execute("SELECT id,origin FROM jobs WHERE kind='recovery_check' AND state IN ('pending','running') AND (json_extract(payload,'$.issue_id')=? OR json_extract(payload,'$.issue_id')='all') ORDER BY id LIMIT 1",(issue_id,)).fetchone()
        if job:
            if origin=='manual':db.execute("UPDATE jobs SET origin='manual' WHERE id=? AND state='pending'",(job['id'],))
            return job['id']
    return w.enqueue('recovery_check',{'issue_id':issue_id},origin=origin)


def run(w,issue_id='all'):
    """No send/publish/approve/login endpoint may be called here."""
    import sales,autopilot,public_engagement as public,workflow
    from app import dump,now
    before=snapshot(w)['active']
    targets=[i for i in before if issue_id=='all' or i['id']==issue_id]
    actions=list(dict.fromkeys(i['action'] for i in targets));results=[];initial_scopes={};read_threads=set()
    for action in actions:
        if w.stop.is_set() or w.meta('shutdown_requested')=='1':break
        workflow.activity(w,'正在重新检测异常','只读核对，不会重发文章或评论；自动任务的暂停状态保持不变')
        initial=scope(w,action)
        initial_scopes[action]=initial
        try:
            if action in CHECKS:
                detail=sales.check(w,CHECKS[action])
                runtime=json.loads(w.meta('last_model_runtime') or '{}')
                if action not in ('model','writing') or not runtime.get('fallback'):
                    record_success(w,action,initial,'专项恢复检查通过')
            elif action=='scan':
                detail=sales.scan(w,analyze=False,recovery_probe=True)
                if json.loads(w.meta('scan_report') or '{}').get('status') not in ('done','empty'):
                    raise ValueError('搜索已检查，仍有原文或来源需要处理')
                record_success(w,'scan',initial,'重新搜索及原文读取通过')
            elif action=='verify_drafts':detail=autopilot.sync_xhs(w,verify_only=True)
            elif action in ('verify_comments','verify_threads'):
                account=autopilot.current_xhs_account(w)
                with w.db() as db:
                    sql="SELECT DISTINCT t.* FROM public_threads t"
                    sql+=(" JOIN public_turns p ON p.thread=t.id WHERE t.account=? AND p.state='unknown'" if action=='verify_comments' else " WHERE t.account=? AND t.state='active' AND t.errors>0")
                    rows=[dict(r) for r in db.execute(sql+' ORDER BY t.checked_at,t.id LIMIT 3',(account,))]
                confirmed=0;errors=0
                for thread in rows:
                    if w.stop.is_set() or w.meta('shutdown_requested')=='1':break
                    if thread['id'] in read_threads:continue
                    read_threads.add(thread['id'])
                    try:
                        data=public.read(w,thread)
                        if scope(w,action)!=initial:raise ValueError('检测期间账号或服务配置发生变化，请重新检测')
                        if action=='verify_comments':
                            with w.db() as db:turns=[dict(r) for r in db.execute("SELECT * FROM public_turns WHERE thread=? AND state='unknown'",(thread['id'],))]
                            confirmed+=sum(bool(public.confirm(w,thread,turn,data)) for turn in turns)
                        with w.db() as db:
                            db.execute("UPDATE public_threads SET checked_at=? WHERE id=?",(now(),thread['id']))
                            db.execute("UPDATE public_threads SET errors=0,error='' WHERE id=?",(thread['id'],))
                    except Exception as error:
                        errors+=1
                        with w.db() as db:db.execute("UPDATE public_threads SET checked_at=?,error=? WHERE id=?",(now(),w.redact(error),thread['id']))
                detail=f'只读检查{len(rows)}段讨论，确认{confirmed}条既有发送，{errors}段仍未读到；未执行新发送'
            elif action=='link':
                issue=next(i for i in targets if i['action']=='link')
                with w.db() as db:payload=json.loads(db.execute('SELECT payload FROM jobs WHERE id=?',(issue['job_id'],)).fetchone()[0])
                detail=sales.check(w,'link_check',payload['lead_id'])
                # A link check only resolves that exact lead's failed check.
                w.meta('recovery_link:'+str(issue['job_id']),dump({'time':now(),'scope':scope(w,'link'),'detail':'原帖链接重新读取通过'}))
            else:detail='本机状态已重新读取；尚无恢复证据的提示继续保留，请查看对应内容或记录'
            results.append({'action':action,'status':'checked','detail':str(detail)})
        except Exception as error:results.append({'action':action,'status':'failed','detail':w.redact(error)})
    remaining={i['id'] for i in snapshot(w)['active']}
    resolved=sum(i['id'] not in remaining for i in targets)
    report={'time':now(),'checked':len(targets),'resolved':resolved,'remaining':len(targets)-resolved,'status':'passed' if not any(i['id'] in remaining for i in targets) else 'needs_review','results':results}
    w.meta('recovery_report',dump(report))
    attempts=json.loads(w.meta('recovery_attempts') or '{}')
    for item in targets:
        count=0 if item['id'] not in remaining else attempts.get(item['id'],{}).get('count',0)+1
        delay=(1,2,5,15,30)[min(max(count-1,0),4)]
        attempts[item['id']]={'count':count,'scope':initial_scopes.get(item['action'],''),'next_at':(datetime.fromisoformat(now())+timedelta(minutes=delay)).isoformat(timespec='seconds')}
    w.meta('recovery_attempts',dump(attempts))
    return f'异常检测完成：已恢复{resolved}项，仍需处理{len(targets)-resolved}项；没有重发文章或评论'


def tick(w):
    from app import now
    if w.meta('pause_requested')=='1' or w.meta('shutdown_requested')=='1' or w.stop.is_set():return
    with w.db() as db:
        if db.execute("SELECT 1 FROM jobs WHERE kind='recovery_check' AND state IN ('pending','running')").fetchone():return
        used=dict(db.execute("SELECT kind,COUNT(*) FROM usage WHERE substr(time,1,10)=? GROUP BY kind",(now()[:10],)))
    c=w.config()
    attempts=json.loads(w.meta('recovery_attempts') or '{}')
    for issue in snapshot(w)['active']:
        if issue['id'].startswith('config:') or issue['action'] in ('inspect','local') or issue.get('checking'):continue
        if issue['action'] in ('model','writing') and (not all(c['ai'][k] for k in ('base_url','model','api_key')) or 0<=c['ai']['calls_per_day']<=used.get('llm',0)):continue
        if issue['action'] in ('scan','search') and 0<=c['radar']['searches_per_day']<=used.get('search',0):continue
        attempt=attempts.get(issue['id'],{})
        if attempt.get('scope')==scope(w,issue['action']) and attempt.get('next_at','')>now():continue
        request(w,issue['id'],origin='scheduler');break
