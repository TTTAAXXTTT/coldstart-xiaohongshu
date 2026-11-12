"""HTTP、队列、真实数据库/PNG、上游参数解析与故障注入；不连接真实账号。"""
from __future__ import annotations
import argparse
import ast
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error
from PIL import Image
import app
import process_runner
import validation

QA=Path(__file__).resolve().parent.parent/'qa'
QA.mkdir(exist_ok=True)


class RawHTTPClient:
    def __init__(self,w):self.w=w
    def request(self,path,body=None,method=None,headers=None):
        data=b'' if body is None else json.dumps(body,ensure_ascii=False).encode()
        method=method or ('POST' if body is not None else 'GET')
        h={'Host':'127.0.0.1:18796','Content-Type':'application/json','X-Workbench':'local','Content-Length':str(len(data)),**(headers or {})}
        raw=(f'{method} {path} HTTP/1.0\r\n'+''.join(k+': '+v+'\r\n' for k,v in h.items())+'\r\n').encode()+data
        class Connection:
            def __init__(self):self.input=io.BytesIO(raw);self.output=bytearray()
            def makefile(self,*a,**k):return self.input
            def sendall(self,value):self.output.extend(value)
        connection=Connection()
        app.make_handler(self.w)(connection,('127.0.0.1',1),SimpleNamespace(server_port=18796))
        head,content=bytes(connection.output).split(b'\r\n\r\n',1)
        status=int(head.split(b'\r\n')[0].split()[1])
        kind=next((line for line in head.split(b'\r\n') if line.lower().startswith(b'content-type:')),b'')
        return status,json.loads(content) if b'application/json' in kind else content
    def post(self,path,body):
        status,result=self.request(path,body)
        if status!=200:raise AssertionError(f'{path}: {status}: {result}')
        return result


class IntegratedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=QA)
        self.w=app.Workbench(self.temp.name)
        creator = patch.object(self.w,'creator_check',return_value={'creator_ready':True,'fixture':True}); creator.start(); self.addCleanup(creator.stop)
        submit = patch.object(self.w,'creator_submit',side_effect=lambda p:self.w.xhs('/api/v1/publish',json.loads(Path(p).read_text(encoding='utf-8')))); submit.start(); self.addCleanup(submit.stop)
        self.http=RawHTTPClient(self.w)
    def tearDown(self):self.temp.cleanup()
    def make_draft(self,platform='xiaohongshu'):
        self.w.meta('xhs_bound_user','fixture-user')
        self.w.save_settings({'publishing':{'channels':[platform]}})
        self.w.generate(template=True)
        draft=self.w.state()['drafts'][0]
        self.w.edit_draft({'id':draft['id'],'state':'approved'})
        return self.w.draft(draft['id'])

    def test_http_to_queue_to_scan_ai_cards_submit_and_reconcile(self):
        self.http.post('/api/settings',{'ai':{'base_url':'https://model.invalid/api/v3','model':'fixture-model','api_key':'fixture-model-key'},'radar':{'xhs_enabled':True,'max_queries':1,'max_results':1,'max_details':1},'publishing':{'channels':['xiaohongshu']}})
        calls=[]
        def transport(url,payload=None,token='',timeout=120):
            calls.append((url,payload))
            if 'model.invalid' in url:
                self.assertEqual(url,'https://model.invalid/api/v3/chat/completions')
                system=payload['messages'][0]['content']
                if '筛选员' in system:
                    value={'results':[{'id':1,'intent':'buyer','score':80,'evidence':'想找人做自动化','reason':'明确求助','reply':'先了解输入和输出。'},{'id':2,'intent':'buyer','score':90,'evidence':'求助自动化','reason':'原帖标题','reply':'不发送'}]}
                elif '中文内容编辑' in system:
                    value={'variants':[{'platform':'xiaohongshu','title':'自动化需求怎么说清楚','body':'先列出输入资料，再列出预期输出。把必须人工确认的步骤单独标注。','tags':['自动化'],'cards':[{'title':'先说清楚输入','body':'需要处理哪些资料？来自哪里？'},{'title':'再说清楚输出','body':'最终需要什么格式和结果？'}]}]}
                else:value={'ok':True}
                return {'choices':[{'message':{'content':json.dumps(value,ensure_ascii=False)},'finish_reason':'stop'}],'usage':{'total_tokens':30}}
            if url.endswith('/login/status'):return {'success':True,'data':{'is_logged_in':True,'user_id':'fixture-user'}}
            if '/feeds/search?' in url:return {'success':True,'data':{'feeds':[{'id':'fixture-note','xsecToken':'fixture-token','noteCard':{'displayTitle':'求助自动化','user':{'nickname':'合成用户，非真实客户'}}}]}}
            if url.endswith('/feeds/detail'):return {'success':True,'data':{'feed_id':'fixture-note','data':{'note':{'desc':'想找人做自动化','time':1794283200000},'comments':{'list':[{'id':'fixture-comment','content':'谢谢分享','userInfo':{'nickname':'合成评论'}}]}}}}
            if url.endswith('/api/v1/publish'):
                self.assertTrue(all('attempt-assets' in p for p in payload['images']))
                for p in payload['images']:
                    with Image.open(p) as image:self.assertEqual(image.size,(1080,1440))
                return {'success':True,'data':{'status':'发布完成','title':payload['title']}}
            raise AssertionError('Unexpected endpoint '+url)
        with patch.object(app,'http_json',side_effect=transport):
            self.http.post('/api/jobs',{'kind':'check'});self.assertTrue(self.w.run_one_job())
            self.assertEqual(self.w.meta('xhs_bound_user'),'fixture-user')
            self.http.post('/api/jobs',{'kind':'scan'});self.assertTrue(self.w.run_one_job());self.assertTrue(self.w.run_one_job())
            leads=self.w.state()['leads']
            self.assertEqual(len(leads),2)
            comment=next(l for l in leads if l['record_type']=='comment')
            self.assertEqual(comment['intent'],'unconfirmed')
            self.assertEqual(comment['evidence'],'')
            self.http.post('/api/jobs',{'kind':'generate'});self.assertTrue(self.w.run_one_job())
            draft=self.w.state()['drafts'][0]
            self.http.post('/api/draft',{'id':draft['id'],'scheduled_at':(datetime.now(app.CST)-timedelta(minutes=2)).isoformat(),'state':'approved'})
            self.http.post('/api/settings',{'publishing':{'enabled':True}})
            self.w.tick();self.assertTrue(self.w.run_one_job())
            self.assertEqual(self.w.draft(draft['id'])['state'],'submitted_unverified')
            self.assertEqual(self.w.state()['jobs'][0]['state'],'needs_review')
            self.http.post('/api/reconcile',{'id':draft['id'],'public_url':'https://www.xiaohongshu.com/explore/fixture-confirmed'})
            self.assertEqual(self.w.draft(draft['id'])['state'],'published')
            self.assertEqual(self.w.state()['stats']['won'],0)
            self.assertEqual(len([url for url,_ in calls if url.endswith('/api/v1/publish')]),1)

    def test_daily_quota_defers_without_blocking_or_retrying_now(self):
        draft=self.make_draft()
        self.w.save_settings({'publishing':{'enabled':True,'max_posts_per_day':1}})
        with self.w.db() as db:db.execute("INSERT INTO attempts(draft_id,platform,started_at,state) VALUES(?,?,?,'submitted_unverified')",(99,'xiaohongshu',app.now()))
        self.w.enqueue('publish',{'draft_id':draft['id']})
        with patch.object(self.w,'xhs') as remote:
            self.w.run_one_job();remote.assert_not_called()
        row=self.w.draft(draft['id'])
        self.assertEqual(row['state'],'approved')
        self.assertGreater(datetime.fromisoformat(row['scheduled_at']).date(),datetime.now(app.CST).date())
        self.assertEqual(self.w.state()['jobs'][0]['state'],'deferred')
        before=len(self.w.state()['jobs']);self.w.tick();self.assertEqual(len(self.w.state()['jobs']),before)

    def test_interval_is_enforced_across_midnight(self):
        draft=self.make_draft()
        self.w.save_settings({'publishing':{'enabled':True}})
        fixed=datetime(2026,11,11,0,5,tzinfo=app.CST)
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
        with self.w.db() as db:db.execute("INSERT INTO attempts(draft_id,platform,started_at,state) VALUES(?,?,?,'submitted_unverified')",(99,'xiaohongshu','2026-11-10T23:55:00+08:00'))
        with patch.object(app,'datetime',Frozen),patch.object(self.w,'xhs') as remote:
            with self.assertRaises(app.PublishDeferred) as caught:self.w.preflight(draft)
            self.assertEqual(caught.exception.resume_at,'2026-11-11T01:55:00+08:00')
            remote.assert_not_called()

    def test_disabling_publish_preserves_queued_draft(self):
        draft=self.make_draft();self.w.enqueue('publish',{'draft_id':draft['id']})
        with patch.object(self.w,'xhs') as remote:self.w.run_one_job();remote.assert_not_called()
        self.assertEqual(self.w.draft(draft['id'])['state'],'approved')
        self.assertEqual(self.w.state()['jobs'][0]['state'],'deferred')

    def test_account_change_invalidates_previous_approval(self):
        draft=self.make_draft();self.w.save_settings({'publishing':{'enabled':True}})
        self.w.meta('xhs_bound_user','different-user')
        with patch.object(self.w,'xhs') as remote:
            with self.assertRaises(ValueError):self.w.publish(draft['id'])
            remote.assert_not_called()

    def test_unidentified_logged_in_account_cannot_publish(self):
        draft=self.make_draft();self.w.save_settings({'publishing':{'enabled':True}})
        with patch.object(self.w,'xhs',return_value={'is_logged_in':True}):
            with self.assertRaises(ValueError):self.w.publish(draft['id'])
        self.assertEqual(self.w.draft(draft['id'])['state'],'approved')

    def test_staged_images_are_stable_when_original_is_modified(self):
        draft=self.make_draft();self.w.save_settings({'publishing':{'enabled':True}})
        original=Path(json.loads(draft['assets'])[0]);old=hashlib.sha256(original.read_bytes()).hexdigest()
        def xhs(path,payload=None,**kwargs):
            if path.endswith('/status'):return {'is_logged_in':True,'user_id':'fixture-user'}
            staged=Path(payload['images'][0]);self.assertNotEqual(staged,original)
            Image.new('RGB',(50,50),'red').save(original)
            self.assertEqual(hashlib.sha256(staged.read_bytes()).hexdigest(),old)
            return {'status':'发布完成'}
        with patch.object(self.w,'xhs',side_effect=xhs):self.w.publish(draft['id'])
        self.assertEqual(self.w.draft(draft['id'])['state'],'submitted_unverified')

    def test_other_drafts_remain_editable_during_external_upload(self):
        first=self.make_draft()
        self.w.generate('另一条选题',template=True)
        other=self.w.state()['drafts'][0]
        self.w.save_settings({'publishing':{'enabled':True}})
        entered=threading.Event();release=threading.Event();errors=[]
        def xhs(path,payload=None,**kwargs):
            if path.endswith('/status'):return {'is_logged_in':True,'user_id':'fixture-user'}
            entered.set();release.wait(5);return {'status':'发布完成'}
        def publish():
            try:self.w.publish(first['id'])
            except Exception as e:errors.append(str(e))
        with patch.object(self.w,'xhs',side_effect=xhs):
            thread=threading.Thread(target=publish);thread.start();self.assertTrue(entered.wait(3))
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future=executor.submit(self.w.edit_draft,{'id':other['id'],'state':'cancelled'})
                    future.result(timeout=1)
            finally:release.set();thread.join(timeout=3)
        self.assertFalse(errors)
        self.assertEqual(self.w.draft(other['id'])['state'],'cancelled')

    def test_unknown_submission_requires_reconciliation_before_followup(self):
        draft=self.make_draft();self.w.save_settings({'publishing':{'enabled':True}})
        with self.w.db() as db:db.execute("UPDATE drafts SET state='unknown' WHERE id=?",(draft['id'],))
        self.w.generate('下一条选题',template=True);other=self.w.state()['drafts'][0]
        self.w.edit_draft({'id':other['id'],'state':'approved'})
        self.w.preflight(self.w.draft(other['id']),check_login=False)
        self.http.post('/api/reconcile',{'id':draft['id'],'confirmed_not_published':True})
        self.assertEqual(self.w.draft(draft['id'])['state'],'draft')
        self.assertEqual(self.w.draft(draft['id'])['approved_digest'],'')

    def test_reconciliation_rejects_wrong_platform_and_homepage(self):
        draft=self.make_draft()
        with self.w.db() as db:db.execute("UPDATE drafts SET state='unknown' WHERE id=?",(draft['id'],))
        for url in ('https://attacker.invalid/note','https://www.xiaohongshu.com/'):
            status,_=self.http.request('/api/reconcile',{'id':draft['id'],'public_url':url});self.assertEqual(status,400)
        self.assertEqual(self.w.draft(draft['id'])['state'],'unknown')

    def test_missing_configuration_is_not_reported_as_pass(self):
        self.w.enqueue('check')
        with patch.object(self.w,'xhs',side_effect=ValueError('服务尚未配置')):self.w.run_one_job()
        self.assertEqual(self.w.state()['jobs'][0]['state'],'needs_setup')
        self.assertTrue(any(r['status']=='needs_setup' for r in self.w.state()['diagnostics']['items']))

    def test_invalid_model_batch_does_not_save_partial_analysis(self):
        self.w.ingest([{'url':'https://www.xiaohongshu.com/explore/a','platform':'xiaohongshu','title':'求助','excerpt':'想找人做自动化'},{'url':'https://www.xiaohongshu.com/explore/b','platform':'xiaohongshu','title':'另一条','excerpt':'预算需要核实'}])
        result={'results':[{'id':1,'score':80,'intent':'buyer','evidence':'想找人做自动化'},{'id':2,'score':'not-an-integer','intent':'buyer','evidence':'预算'}]}
        with patch.object(self.w,'llm',return_value=result):
            with self.assertRaises(ValueError):self.w.qualify()
        self.assertTrue(all(not r['analyzed_at'] for r in self.w.state()['leads']))

    def test_missing_platform_output_saves_no_partial_drafts(self):
        self.w.save_settings({'publishing':{'channels':['xiaohongshu']}})
        value={'variants':[]}
        with patch.object(self.w,'llm',return_value=value):
            with self.assertRaises(ValueError):self.w.generate('测试选题')
        self.assertEqual(self.w.state()['drafts'],[])

    def test_material_generation_failure_blocks_auto_approval(self):
        self.w.save_settings({'publishing':{'channels':['xiaohongshu'],'auto_approve':True}})
        self.w.enqueue('generate',{'template':True})
        with patch.object(self.w,'render_cards',side_effect=OSError('磁盘写入失败')):self.w.run_one_job()
        self.assertEqual(self.w.state()['drafts'][0]['state'],'blocked')
        self.assertEqual(self.w.state()['jobs'][0]['state'],'needs_review')

    def test_model_truncation_and_schema_errors_are_not_saved(self):
        self.w.save_settings({'ai':{'base_url':'https://model.invalid/v1','model':'fixture','api_key':'fixture-key'}})
        for response in ({'choices':[{'finish_reason':'length','message':{'content':'{"variants":[]}'}}]}, {'choices':[{'message':{'content':'[]'}}]}, {'choices':[{'message':{'content':'not json'}}]}):
            with patch.object(app,'http_json',return_value=response):
                with self.assertRaises(ValueError):self.w.generate('测试选题')
        self.assertEqual(self.w.state()['drafts'],[])

    def test_model_errors_are_not_retried_and_quota_persists(self):
        self.w.save_settings({'ai':{'base_url':'https://model.invalid/v1','model':'fixture','api_key':'fixture-key','calls_per_day':1}})
        with patch.object(app,'http_json',side_effect=RuntimeError('HTTP 429')) as call:
            with self.assertRaises(RuntimeError):self.w.llm('system','test')
            with self.assertRaises(ValueError):self.w.llm('system','test')
            self.assertEqual(call.call_count,1)

    def test_concurrent_quota_reservations_cannot_exceed_limit(self):
        def reserve(_):
            try:self.w.reserve_usage('llm',2);return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=8) as executor:results=list(executor.map(reserve,range(20)))
        self.assertEqual(sum(results),2)

    def test_malformed_http_input_does_not_create_records(self):
        for path,payload in [('/api/import',{'csv_text':'wrong,headers\na,b'}),('/api/jobs',{'kind':'generate','payload':{'template':'false'}}),('/api/draft',{'id':999,'state':'approved'}),('/api/settings',{'ai':{'calls_per_day':False}}),('/api/settings',{'publishing':{'channels':['bilibili']}})]:
            status,result=self.http.request(path,payload);self.assertEqual(status,400,(path,result))
        self.assertEqual(self.w.state()['drafts'],[])
        self.assertEqual(self.w.state()['leads'],[])

    def test_import_bom_and_export_formula_escaping(self):
        self.http.post('/api/import',{'csv_text':'\ufeffplatform,url,title,excerpt,author,published_at\nxiaohongshu,https://www.xiaohongshu.com/explore/a,=1+1,原文,,\n'})
        status,content=self.http.request('/api/export');self.assertEqual(status,200)
        self.assertIn("'=1+1",content.decode('utf-8-sig'))

    def test_no_duplicate_scheduled_jobs_after_repeated_ticks(self):
        draft=self.make_draft();self.w.edit_draft({'id':draft['id'],'scheduled_at':(datetime.now(app.CST)-timedelta(minutes=5)).isoformat(),'state':'approved'})
        self.w.save_settings({'publishing':{'enabled':True}})
        for _ in range(10):self.w.tick()
        self.assertEqual(len([j for j in self.w.state()['jobs'] if j['kind']=='publish']),1)

    def test_restart_preserves_pending_approved_drafts_and_usage(self):
        draft=self.make_draft();self.w.reserve_usage('llm',5);job_id=self.w.enqueue('scan')
        restarted=app.Workbench(self.temp.name)
        self.assertEqual(restarted.draft(draft['id'])['state'],'approved')
        self.assertEqual(restarted.state()['jobs'][0]['id'],job_id)
        with self.assertRaises(ValueError):restarted.reserve_usage('llm',1)

    def test_single_instance_lock_does_not_reset_running_work(self):
        with validation.InstanceLock(self.temp.name):
            with self.assertRaises(ValueError):validation.InstanceLock(self.temp.name)
        with validation.InstanceLock(self.temp.name):pass

    def test_running_job_can_be_cancelled_without_starting_another(self):
        self.w.enqueue('scan');self.w.stop.set()
        self.assertFalse(self.w.run_one_job())
        self.assertEqual(self.w.state()['jobs'][0]['state'],'pending')



class ProcessRunnerTests(unittest.TestCase):
    def test_real_subprocess_unicode_and_bounded_output(self):
        result=process_runner.run([sys.executable,'-c',"import sys;sys.stdout.write('x'*300000+'\\n中文输出\\n');sys.stderr.write('e'*300000+'\\n')"],timeout=5)
        self.assertEqual(result.returncode,0)
        self.assertIn('中文输出',result.stdout)
        self.assertLessEqual(len(result.stdout.encode()),140000)
        self.assertLessEqual(len(result.stderr.encode()),140000)

    def test_real_subprocess_timeout(self):
        started=time.monotonic()
        with self.assertRaises(RuntimeError):process_runner.run([sys.executable,'-c','import time;time.sleep(20)'],timeout=.2)
        self.assertLess(time.monotonic()-started,8)


if __name__=='__main__':unittest.main(verbosity=2)
