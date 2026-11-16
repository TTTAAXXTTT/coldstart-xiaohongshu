"""One scheduler plan and one business-facing view of its actual work.

Reading this module's snapshot never probes an account, calls a model, or queues work.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta

LABELS = {
    'scan': '搜索相关需求', 'public_scan': '搜索相关需求',
    'public_monitor': '检查公开评论与新回复', 'qualify': '分析找到的需求',
    'autopilot': '安排内容创作', 'generate': '创作一篇内容', 'publish': '发布已批准的内容',
    'cards': '生成配图', 'check': '综合检查', 'sync_xhs': '回查本人作品',
    'process_comments': '检查本人作品的评论', 'import_own': '接入本人作品',
    'reply_draft': '拟一条评论', 'rewrite': '改写内容', 'login': '登录小红书',
    'model_check': '检查模型连接', 'writing_check': '检查写作效果',
    'source_check': '检查小红书账号', 'search_check': '检查搜索接口',
    'link_check': '检查原帖链接', 'publish_check': '检查发稿准备', 'cards_check': '检查图片',
    'recovery_check': '检测异常是否恢复',
}
MODES = {'full': '全自动评论与发帖', 'comments': '只自动评论与回复',
         'observe': '只找需求和分析，不发送', 'manual': '手动操作', 'custom': '原有自定义流程'}
AUTOMATIC = ('scheduler', 'legacy')


class Suspended(RuntimeError):
    """A resumable read/analysis pipeline stopped at a safe step boundary."""


def manual_waiting(w):
    with w.db() as db:
        return db.execute("SELECT 1 FROM jobs WHERE state='pending' AND origin='manual' LIMIT 1").fetchone() is not None


def clock():
    from app import now
    return datetime.fromisoformat(now())


def parse(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else result.replace(tzinfo=clock().tzinfo)
    except (TypeError, ValueError):
        return None


def iso(value):
    return value.isoformat(timespec='seconds') if value else None


def mode(config):
    if config['public']['enabled']:
        if config['public']['send_enabled']:
            return ('full' if config['autonomy']['enabled'] and config['publishing']['auto_approve'] else 'custom') if config['publishing']['enabled'] else 'comments'
        return 'custom' if config['publishing']['enabled'] else 'observe'
    if config['autonomy']['enabled'] or config['schedule']['enabled'] or config['publishing']['enabled']:
        return 'custom'
    return 'manual'


def enabled(kind, config):
    if kind in ('public_scan', 'public_monitor'):
        return config['public']['enabled']
    if kind == 'autopilot':
        return config['autonomy']['enabled'] and (not config['public']['enabled'] or config['publishing']['enabled'])
    if kind in ('scan', 'generate'):
        return config['schedule']['enabled'] and not config['autonomy']['enabled'] and not config['public']['enabled']
    if kind == 'publish':
        return config['publishing']['enabled']
    return True


def initialize(w):
    """Migrate only the queue metadata; preserve all business/account records."""
    with w.db() as db:
        if 'origin' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
            if not getattr(w, '_legacy_schema_backup', False):
                folder = w.data / 'backups'
                folder.mkdir(exist_ok=True)
                target = sqlite3.connect(folder / ('before-0.5-' + clock().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3'))
                try:
                    db.backup(target)
                finally:
                    target.close()
            db.execute("ALTER TABLE jobs ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
        migrated = db.execute("SELECT 1 FROM meta WHERE key='workflow_queue_v1'").fetchone()
        if not migrated:
            # Old periodic jobs had no origin. Only these three machine-only entries
            # can be identified reliably; ordinary user requests remain untouched.
            db.execute("UPDATE jobs SET origin='legacy' WHERE kind IN ('public_scan','public_monitor','autopilot') AND state='pending'")
            db.execute("INSERT INTO meta VALUES('workflow_queue_v1','1')")
    if not w.meta('workflow_first_due'):
        w.meta('workflow_first_due', iso(clock()))
    prune(w)


def prune(w):
    config = w.config()
    with w.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if config['public']['enabled']:
            db.execute("UPDATE jobs SET kind='public_scan' WHERE kind='scan' AND state='pending'")
        rows = db.execute("SELECT id,kind,payload,origin,state FROM jobs WHERE state IN ('pending','running') ORDER BY CASE WHEN state='running' THEN 0 ELSE 1 END,id").fetchall()
        seen = {}
        for row in rows:
            key = (row['kind'], row['payload'])
            reason = '当前模式已关闭这项自动工作' if row['state']=='pending' and row['origin'] in AUTOMATIC and not enabled(row['kind'], config) else '已合并到相同任务 #' + str(seen[key]['id']) if key in seen and row['state']=='pending' else ''
            if reason:
                if key in seen and row['origin']=='manual' and seen[key]['origin']!='manual':
                    db.execute("UPDATE jobs SET origin='manual' WHERE id=?", (seen[key]['id'],))
                    seen[key] = {**seen[key], 'origin':'manual'}
                db.execute("UPDATE jobs SET state='cancelled',message=?,updated_at=? WHERE id=?", (reason, iso(clock()), row['id']))
            else:
                seen[key] = dict(row)


def due(w, kind, minutes, legacy=''):
    stamp = parse(w.meta('workflow_finished:' + kind)) or parse(w.meta(legacy) if legacy else '')
    failure = json.loads(w.meta('workflow_failure:' + kind) or '{}')
    if failure.get('count') and parse(failure.get('time')):
        return parse(failure['time']) + timedelta(minutes=min(30, 2 ** min(5, failure['count'] - 1)))
    return stamp + timedelta(minutes=minutes) if stamp else parse(w.meta('workflow_first_due')) or clock()


def monitor_due(w, config):
    p = config['public']
    fallback = due(w, 'public_monitor', p['watch_minutes'], 'public_watch_tick')
    if w.meta('workflow_failure:public_monitor'):
        failure = json.loads(w.meta('workflow_failure:public_monitor'))
        if failure.get('count'):
            return fallback
    times = []
    if p['threads_per_round'] != 0:
        with w.db() as db:
            row = db.execute("SELECT MIN(CASE WHEN next_at='' THEN created_at ELSE next_at END) FROM public_threads WHERE account=? AND state='active'" + (" AND owner<>?" if not p['own_comments_enabled'] else ''),
                             [w.meta('xhs_bound_user')] * (2 if not p['own_comments_enabled'] else 1)).fetchone()
        if row[0] is not None:
            times.append(parse(row[0]) or clock())
    if p['own_comments_enabled'] and p['own_posts_per_round'] != 0:
        last = parse(w.meta('public_owned_tick'))
        times.append(last + timedelta(minutes=p['own_watch_minutes']) if last else parse(w.meta('workflow_first_due')) or clock())
    return min(times) if times else fallback


def plan(w, config=None):
    """The UI and the worker both use these same eligibility times."""
    c = config or w.config()
    steps = []
    def add(key, kind, at, detail, payload=None):
        steps.append({'key': key, 'kind': kind, 'label': LABELS[kind], 'at': iso(at), 'detail': detail, 'payload': payload or {}})
    if c['public']['enabled']:
        add('search', 'public_scan', due(w, 'public_scan', c['public']['search_minutes'], 'public_scan_at'), '搜索新问题，更新原帖，再接入公开讨论')
        search = steps[-1]
        with w.db() as db:
            used = db.execute("SELECT COUNT(*) FROM usage WHERE kind='search' AND substr(time,1,10)=?", (clock().date().isoformat(),)).fetchone()[0]
        if c['radar']['searches_per_day'] == 0 or c['radar']['max_queries'] == 0:
            search['hold_reason'] = '搜索次数或每轮关键词数设为0，自动搜索不执行；可在业务设置调整'
        elif c['radar']['searches_per_day'] > 0 and used >= c['radar']['searches_per_day']:
            search['at'] = iso(clock().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
            search['detail'] = '今日搜索用量已用完，明日恢复；已接入的讨论继续检查'
        add('comments', 'public_monitor', monitor_due(w, c), '逐段检查新回复；只有适合参与的问题才拟稿或发送')
    if enabled('autopilot', c) and c['autonomy']['enabled']:
        add('writing', 'autopilot', due(w, 'autopilot', c['autonomy']['cycle_minutes'], 'last_autonomy_tick'),
            '检查创作额度、选题和待发稿；到上限时等待，不会每轮强行写一篇')
    elif c['schedule']['enabled'] and not c['public']['enabled']:
        if c['radar']['xhs_enabled'] or c['radar']['ark_search_enabled']:
            add('search', 'scan', due(w, 'scan', c['schedule']['scan_minutes'], 'last_scheduled_scan'), '原有定时搜索')
        today = clock().replace(hour=int(c['schedule']['generate_time'][:2]), minute=int(c['schedule']['generate_time'][3:]), second=0, microsecond=0)
        if w.meta('last_scheduled_generate') == today.date().isoformat():
            today += timedelta(days=1)
        failure = json.loads(w.meta('workflow_failure:generate') or '{}')
        if failure.get('count'):
            today = max(today, due(w, 'generate', 0))
        add('writing', 'generate', today, '原有每日创作安排')
    if c['publishing']['enabled']:
        with w.db() as db:
            rows = db.execute("SELECT id,title,scheduled_at FROM drafts WHERE platform='xiaohongshu' AND state='approved' ORDER BY scheduled_at,id LIMIT 6").fetchall()
        for row in rows:
            add('publish:' + str(row['id']), 'publish', parse(row['scheduled_at']) or clock(), '待发：' + row['title'], {'draft_id': row['id'], 'scheduled_at': row['scheduled_at']})
    return sorted(steps, key=lambda s: (s['at'], s['key']))


def tick(w):
    if w.stop.is_set() or w.meta('pause_requested') == '1' or w.meta('shutdown_requested') == '1':
        return
    prune(w)
    moment = clock()
    for step in plan(w):
        if not step.get('hold_reason') and parse(step['at']) <= moment:
            w.enqueue(step['kind'], step['payload'], origin='scheduler')


def finished(w, job, state):
    if state == 'pending' or job['kind']=='recovery_check':
        return
    kind = job['kind']
    w.meta('workflow_finished:' + kind, iso(clock()))
    if kind == 'generate' and job['origin'] in AUTOMATIC and state not in ('failed','needs_setup'):
        w.meta('last_scheduled_generate', clock().date().isoformat())
    if state in ('failed', 'needs_setup'):
        old = json.loads(w.meta('workflow_failure:' + kind) or '{}')
        w.meta('workflow_failure:' + kind, json.dumps({'count': old.get('count', 0) + 1, 'time': iso(clock())}))
    else:
        w.meta('workflow_failure:' + kind, '{}')


def activity(w, title, detail=''):
    current = getattr(w, '_active_job', None)
    if current:
        w.meta('workflow_activity', json.dumps({'job_id': current['id'], 'title': title, 'detail': detail,
                                                'updated_at': iso(clock())}, ensure_ascii=False))


def job_control(w, action, job_id=None):
    with w.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if action == 'cancel':
            row = db.execute("SELECT kind,payload FROM jobs WHERE id=? AND state='pending'", (int(job_id),)).fetchone()
            changed = db.execute("UPDATE jobs SET state='cancelled',message='已手动取消，未开始执行',updated_at=? WHERE id=? AND state='pending'", (iso(clock()), int(job_id)))
            if not changed.rowcount:
                raise ValueError('这项任务已开始或已结束，请刷新查看；执行中的提交会先核对结果')
            if row['kind'] == 'publish':
                payload = json.loads(row['payload'])
                db.execute("UPDATE drafts SET state='cancelled',receipt='已手动取消本稿排期，原稿仍保留',approved_digest='',approved_at='',target_binding='',updated_at=? WHERE id=? AND state='approved'" + (' AND scheduled_at=?' if 'scheduled_at' in payload else ''),
                           [iso(clock()), payload['draft_id']] + ([payload['scheduled_at']] if 'scheduled_at' in payload else []))
        elif action == 'clear_pending':
            db.execute("INSERT OR REPLACE INTO meta VALUES('pause_requested','1')")
            changed = db.execute("UPDATE jobs SET state='cancelled',message='已取消等待任务并暂停调度',updated_at=? WHERE state='pending'", (iso(clock()),))
        else:
            raise ValueError('未知队列操作')
    return {'cancelled': changed.rowcount}


def snapshot(w):
    c = w.config()
    moment = clock()
    paused = w.meta('pause_requested') == '1'
    stopping = w.stop.is_set() or w.meta('shutdown_requested') == '1'
    thread = getattr(w, '_worker_thread', None)
    alive = bool(thread and thread.is_alive())
    with w.db() as db:
        counts = dict(db.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state'))
        running = [dict(r) for r in db.execute("SELECT id,kind,state,origin,payload,updated_at FROM jobs WHERE state='running' ORDER BY id")]
        pending = [dict(r) for r in db.execute("SELECT id,kind,state,origin,payload,created_at FROM jobs WHERE state='pending' ORDER BY CASE WHEN origin='manual' THEN 0 ELSE 1 END,id LIMIT 20")]
        latest = [dict(r) for r in db.execute("SELECT id,kind,state,message,updated_at FROM jobs WHERE state NOT IN ('pending','running') ORDER BY id DESC LIMIT 1")]
        day_usage = dict(db.execute("SELECT kind,COUNT(*) FROM usage WHERE substr(time,1,10)=? GROUP BY kind", (moment.date().isoformat(),)))
        drafts = dict(db.execute("SELECT state,COUNT(*) FROM drafts WHERE platform='xiaohongshu' GROUP BY state"))
        thread_count = db.execute("SELECT COUNT(*) FROM public_threads WHERE account=? AND state='active'", (w.meta('xhs_bound_user'),)).fetchone()[0]
    status, title = ('stopping', '正在结束当前任务') if stopping else ('pausing', '正在收尾，随后暂停') if paused and running else ('paused', '已暂停') if paused else ('running', '正在工作') if running else ('offline', '后台调度未启动') if not alive else ('queued', '准备执行下一项') if pending else ('waiting', '等待下次安排') if plan(w, c) else ('manual', '手动模式，等待操作')
    if paused and running and running[0]['kind']=='recovery_check':status,title='checking','正在检测异常（自动任务仍暂停）'
    activity_data = json.loads(w.meta('workflow_activity') or '{}')
    active = running[0] if running else None
    if active:
        active['label'] = LABELS.get(active['kind'], active['kind'])
        active['stage'] = activity_data if activity_data.get('job_id') == active['id'] else {}
        if active['kind'] == 'publish':
            verification = w.publishing_progress()
            if verification:
                active['stage'] = {'title':'需要账号本人验证' if verification.get('stage')=='verification_required' else '核对身份验证后的提交结果',
                                   'detail':verification.get('detail',''), 'updated_at':verification.get('checked_at',iso(moment))}
        active['elapsed_seconds'] = max(0, int((moment - (parse(active['updated_at']) or moment)).total_seconds()))
    for index, row in enumerate(pending, 1):
        row.update(label=LABELS.get(row['kind'], row['kind']), position=index,
                   wait_reason='暂停期间仅执行这项只读检测' if paused and row['kind']=='recovery_check' and row['origin']=='manual' else '已暂停，继续运行后才执行' if paused else '程序正在退出，重启后才执行' if stopping else '后台调度未启动，启动程序后才执行' if not alive and not active else '等待当前任务结束，再按顺序执行' if active else '等待前面的任务执行' if index > 1 else '即将开始')
    steps = plan(w, c)
    for step in steps:
        matches = [r for r in running + pending if r['kind'] == step['kind'] and (step['kind'] != 'publish' or json.loads(r['payload']).get('draft_id') == step['payload']['draft_id'])]
        is_running = any(r['state'] == 'running' for r in matches)
        step['state'] = 'finishing' if is_running and (paused or stopping) else 'running' if is_running else 'disabled' if step.get('hold_reason') else 'paused' if paused or stopping or not alive else 'queued' if matches else 'due' if parse(step['at']) <= moment else 'scheduled'
        step['seconds_until'] = max(0, int((parse(step['at']) - moment).total_seconds()))
        if step['state'] in ('paused','disabled','running','finishing'):
            step['next_at'] = None
        else:
            step['next_at'] = step['at']
        step.pop('payload', None)
    for row in running + pending:
        row.pop('payload', None)
    import recovery
    incidents=recovery.snapshot(w)
    attention=incidents['active']
    if latest:
        latest[0]['label'] = LABELS.get(latest[0]['kind'], latest[0]['kind'])
        latest[0]['recovered']=any(i['id']=='job:'+str(latest[0]['id']) for i in incidents['recovered'])
    return {'status': status, 'title': title, 'mode': mode(c), 'mode_label': MODES[mode(c)],
            'worker_alive': alive, 'paused': paused, 'active': active, 'queue': pending,
            'counts': counts, 'pending_count': counts.get('pending', 0), 'plan': steps,
            'last_result': latest[0] if latest else None, 'attention': attention, 'recovery': incidents,
            'usage': day_usage, 'draft_counts': drafts, 'active_threads': thread_count,
            'server_time': iso(moment), 'business': {'name': c['business']['name'], 'offer': c['business']['offer'],
            'audience': c['business']['audience'], 'keywords': c['business']['keywords'], 'model': c['ai']['model']},
            'rules': {'search_minutes': c['public']['search_minutes'] if c['public']['enabled'] else c['autonomy']['cycle_minutes'] if c['autonomy']['enabled'] else c['schedule']['scan_minutes'],
                      'watch_minutes': c['public']['watch_minutes'], 'own_watch_minutes': c['public']['own_watch_minutes'],
                      'own_comments': c['public']['own_comments_enabled'], 'send_comments': c['public']['enabled'] and c['public']['send_enabled'],
                      'write_posts': enabled('autopilot', c) and c['autonomy']['enabled'] or c['schedule']['enabled'] and not c['public']['enabled'], 'publish': c['publishing']['enabled'],
                      'slots': c['publishing']['slots'], 'generations_per_day': c['autonomy']['generations_per_day'],
                      'comments_per_day': c['public']['comments_per_day'], 'replies_per_day': c['public']['replies_per_day'],
                      'posts_per_day': c['publishing']['max_posts_per_day']}}
