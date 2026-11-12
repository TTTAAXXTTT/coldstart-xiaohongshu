"""Offline regression checks for review findings; no real accounts or providers."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import app
from test_integration import RawHTTPClient


class ReviewAppTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w=app.Workbench(self.temp.name)
        self.w.meta('xhs_bound_user','fixture-account-A')
        self.w.save_settings({'publishing':{'enabled':True,'max_posts_per_day':-1,'min_gap_minutes':0}})

    def approved_draft(self,scheduled_at=None):
        asset=self.w.data/'fixture.png'
        Image.new('RGB',(8,8),'white').save(asset)
        schedule=scheduled_at or (datetime.now(app.CST)-timedelta(minutes=1)).isoformat(timespec='seconds')
        with self.w.db() as db:
            draft_id=db.execute("INSERT INTO drafts(platform,title,body,tags,cards,assets,generator,scheduled_at,created_at,updated_at) VALUES('xiaohongshu','合成测试标题','合成测试正文','[]','[]',?,'template',?,?,?)",(json.dumps([str(asset)]),schedule,app.now(),app.now())).lastrowid
        self.w.edit_draft({'id':draft_id,'state':'approved'})
        return self.w.draft(draft_id)

    def login_patch(self):
        return patch.object(self.w,'xhs',return_value={'is_logged_in':True,'user_id':'fixture-account-A'})

    def test_scheduled_job_is_cancelled_after_rescheduling(self):
        row=self.approved_draft()
        self.w.tick()
        tomorrow=(datetime.now(app.CST)+timedelta(days=1)).isoformat(timespec='seconds')
        self.w.edit_draft({'id':row['id'],'state':'approved','scheduled_at':tomorrow})
        with patch.object(self.w,'creator_submit') as submit,self.login_patch():
            self.w.run_one_job()
        submit.assert_not_called()
        self.assertEqual(self.w.draft(row['id'])['scheduled_at'],tomorrow)
        self.assertEqual(self.w.draft(row['id'])['state'],'approved')
        with self.w.db() as db:
            self.assertEqual(db.execute('SELECT state FROM jobs').fetchone()[0],'cancelled')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],0)

    def test_rescheduling_during_creator_check_prevents_submission(self):
        row=self.approved_draft()
        self.w.tick()
        tomorrow=(datetime.now(app.CST)+timedelta(days=1)).isoformat(timespec='seconds')
        def reschedule():
            self.w.edit_draft({'id':row['id'],'state':'approved','scheduled_at':tomorrow})
        with self.login_patch(),patch.object(self.w,'creator_check',side_effect=reschedule),patch.object(self.w,'creator_submit') as submit:
            self.w.run_one_job()
        submit.assert_not_called()
        self.assertEqual(self.w.draft(row['id'])['state'],'approved')
        self.assertEqual(self.w.draft(row['id'])['scheduled_at'],tomorrow)

    def test_explicit_immediate_publish_still_sends_future_draft(self):
        row=self.approved_draft((datetime.now(app.CST)+timedelta(days=1)).isoformat(timespec='seconds'))
        self.w.enqueue('publish',{'draft_id':row['id']})
        with self.login_patch(),patch.object(self.w,'creator_check'),patch.object(self.w,'creator_submit',return_value={'status':'submitted'}) as submit:
            self.w.run_one_job()
        submit.assert_called_once()
        self.assertEqual(self.w.draft(row['id'])['state'],'submitted_unverified')

    def test_changed_binding_during_snapshot_is_rejected_before_bridge(self):
        row=self.approved_draft()
        original_open=Path.open
        def change_account(path,*args,**kwargs):
            stream=original_open(path,*args,**kwargs)
            if path.name=='request.json' and kwargs.get('mode',args[0] if args else 'r')=='w':
                self.w.meta('xhs_bound_user','fixture-account-B')
            return stream
        with self.login_patch(),patch.object(self.w,'creator_check'),patch.object(Path,'open',new=change_account),patch.object(app.xhs_session,'invoke') as bridge:
            self.w.publish(row['id'])
        bridge.assert_not_called()
        self.assertEqual(self.w.draft(row['id'])['state'],'blocked')

    def test_bridge_uses_snapshot_account_if_binding_changes_after_validation(self):
        row=self.approved_draft()
        request=self.w.data/'request.json'
        request.write_text(json.dumps({'target_binding':row['target_binding']}),encoding='utf-8')
        def invoke(expected_user,**kwargs):
            self.w.meta('xhs_bound_user','fixture-account-B')
            self.assertEqual(expected_user,'fixture-account-A')
            return {'status':'submitted'}
        with patch.object(app.xhs_session,'invoke',side_effect=invoke) as bridge:
            self.w.creator_submit(request)
        bridge.assert_called_once()

    def test_rebind_api_rejects_inflight_publish_then_allows_idle_change(self):
        row=self.approved_draft()
        with self.w.db() as db:db.execute("UPDATE drafts SET state='publishing' WHERE id=?",(row['id'],))
        client=RawHTTPClient(self.w)
        with patch.object(self.w,'xhs',return_value={'is_logged_in':True,'user_id':'fixture-account-B'}) as remote:
            self.assertEqual(client.request('/api/rebind-xhs',{'confirm':True})[0],400)
            remote.assert_not_called()
            self.assertEqual(self.w.meta('xhs_bound_user'),'fixture-account-A')
            with self.w.db() as db:db.execute("UPDATE drafts SET state='draft' WHERE id=?",(row['id'],))
            self.assertEqual(client.request('/api/rebind-xhs',{'confirm':True})[0],200)
        self.assertEqual(self.w.meta('xhs_bound_user'),'fixture-account-B')

    def test_recovery_does_not_hold_operation_lock_while_waiting_for_settings_lock(self):
        # A publisher takes lock then operation_lock. A recovery waiting on lock
        # must leave operation_lock free so the publisher can finish.
        for method in ('recover_pre_submit','recover_creator_absent'):
            with self.subTest(method=method):
                acquired=threading.Event()
                base_lock=threading.RLock()
                class NotifyingLock:
                    def __enter__(self):acquired.set();base_lock.acquire();return self
                    def __exit__(self,*args):base_lock.release()
                self.w.lock=NotifyingLock()
                errors=[]
                def recover():
                    try:getattr(self.w,method)(9999)
                    except ValueError as error:errors.append(error)
                base_lock.acquire()
                worker=threading.Thread(target=recover,daemon=True)
                worker.start()
                try:
                    self.assertTrue(acquired.wait(2))
                    available=self.w.operation_lock.acquire(blocking=False)
                    if available:self.w.operation_lock.release()
                    self.assertTrue(available)
                finally:
                    base_lock.release()
                    worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors),1)

    def test_radar_token_is_retained_only_for_same_origin_or_explicit_replacement(self):
        self.w.save_settings({'radar':{'xhs_url':'https://adapter-a.invalid/v1','xhs_token':'fixture-token-A'}})
        self.w.save_settings({'radar':{'xhs_url':'https://adapter-a.invalid/v2','xhs_token':''}})
        self.assertEqual(self.w.config()['radar']['xhs_token'],'fixture-token-A')
        self.w.save_settings({'radar':{'xhs_url':'https://adapter-b.invalid/v1','xhs_token':''}})
        self.assertEqual(self.w.config()['radar']['xhs_token'],'')
        self.w.save_settings({'radar':{'xhs_url':'https://adapter-a.invalid/v1','xhs_token':'fixture-token-C'}})
        self.assertEqual(self.w.config()['radar']['xhs_token'],'fixture-token-C')

    def test_qualification_discards_results_if_source_changes_during_model_call(self):
        record={'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/review-fixture','title':'找人做自动化','excerpt':'需要处理表格'}
        self.w.ingest([record])
        def model(*args,**kwargs):
            self.w.ingest([{**record,'excerpt':'问题已经全部解决，目前不再需要任何自动化服务'}])
            return {'results':[{'id':1,'score':95,'intent':'buyer','evidence':'需要处理表格','reason':'旧正文','reply':'旧建议'}]}
        with patch.object(self.w,'llm',side_effect=model),patch.object(app.sales,'repair_evidence',side_effect=lambda w,items,rows:items):
            self.assertIn('完成 0 条',self.w.qualify())
        with self.w.db() as db:row=dict(db.execute('SELECT * FROM leads WHERE id=1').fetchone())
        self.assertEqual(row['analyzed_at'],'')
        self.assertEqual(row['reply'],'')
        self.assertNotEqual(row['evidence'],'需要处理表格')
        fresh={'results':[{'id':1,'score':10,'intent':'unconfirmed','evidence':'问题已经全部解决','reason':'新正文','reply':'新建议'}]}
        with patch.object(self.w,'llm',return_value=fresh):
            self.assertIn('完成 1 条',self.w.qualify())

    def seeded_long_lead(self):
        record={'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/refresh-fixture','title':'表格自动化','excerpt':'目前每天都在重复合并许多张表格，想找人做自动化，需要处理多个来源的数据'}
        self.w.ingest([record])
        with self.w.db() as db:
            db.execute("UPDATE leads SET score=95,intent='buyer',evidence='想找人做自动化',reply='旧建议',analyzed_at=?,state='contacted' WHERE id=1",(app.now(),))
            original=dict(db.execute('SELECT * FROM leads WHERE id=1').fetchone())
        return record,original

    def test_authoritative_shorter_refresh_discards_obsolete_buyer_analysis(self):
        record,original=self.seeded_long_lead()
        self.w.ingest([{**record,'excerpt':'已解决，不再需要'}],authoritative=True)
        with self.w.db() as db:row=dict(db.execute('SELECT * FROM leads WHERE id=1').fetchone())
        self.assertEqual(row['excerpt'],'已解决，不再需要')
        self.assertNotEqual(row['source_hash'],original['source_hash'])
        self.assertEqual(row['analyzed_at'],'')
        self.assertEqual(row['reply'],'')
        self.assertEqual(row['evidence'],'')
        self.assertEqual(row['intent'],'unconfirmed')
        self.assertEqual(row['state'],'contacted')

    def test_partial_refresh_keeps_longer_body_and_existing_analysis(self):
        record,original=self.seeded_long_lead()
        self.w.ingest([{**record,'excerpt':''}])
        with self.w.db() as db:row=dict(db.execute('SELECT * FROM leads WHERE id=1').fetchone())
        for key in ('excerpt','source_hash','analyzed_at','reply','evidence','intent','score','state'):
            self.assertEqual(row[key],original[key],key)


if __name__=='__main__':unittest.main()
