"""Recovery evidence, paused-worker isolation, and strictly read-only probes."""
import json,tempfile,unittest
from unittest.mock import patch
from datetime import datetime,timedelta
import app,autopilot,recovery,sales,public_engagement as public,workflow
from test_integration import RawHTTPClient


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.w=app.Workbench(self.temp.name)
        self.w.meta('xhs_bound_user','fixture-owner')
        self.w.save_settings({'ai':{'api_key':'fixture-key','calls_per_day':-1},'radar':{'xhs_enabled':True}})
        self.past=(datetime.fromisoformat(app.now())-timedelta(minutes=10)).isoformat(timespec='seconds')

    def fail(self,kind='source_check',message='登录已失效',payload=None):
        ident=self.w.enqueue(kind,payload)
        with self.w.db() as db:db.execute("UPDATE jobs SET state='failed',message=?,updated_at=? WHERE id=?",(message,self.past,ident))
        return ident

    def ids(self):return {i['id'] for i in recovery.snapshot(self.w)['active']}

    def transport(self,path,payload=None,**kw):
        self.assertNotIn('/comment',path);self.assertNotIn('/publish',path)
        if path.endswith('/login/status'):return {'is_logged_in':True,'user_id':'fixture-owner'}
        if path.endswith('/user/me'):return {'data':{'feeds':None}}
        if '/feeds/search?' in path:return {'feeds':[]}
        raise AssertionError('Unexpected endpoint '+path)

    def thread(self,state='unknown',errors=0):
        with self.w.db() as db:
            t=db.execute("INSERT INTO public_threads(account,feed,token,target,title,context,errors,error,created_at) VALUES('fixture-owner',?,'fixture-token','','合成讨论','合成原文',?,'原文超时',?)",('feed'+str(errors),errors,app.now())).lastrowid
            db.execute("INSERT INTO public_turns(thread,message_id,body,author,reply,state,created_at,updated_at) VALUES(?,'initial','合成提问','visitor','合成回复',?,?,?)",(t,state,app.now(),app.now()))
        return t

    def test_later_account_check_closes_old_login_without_changing_history(self):
        ident=self.fail('login','无法核对主站账号，未保存会话')
        with patch.object(self.w,'xhs',side_effect=self.transport):sales.check(self.w,'source_check')
        self.assertNotIn('job:'+str(ident),self.ids())
        with self.w.db() as db:self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=?',(ident,)).fetchone()[0],'failed')

    def test_existing_compatible_check_closes_login_without_network(self):
        ident=self.fail('login','无法核对主站账号')
        self.w.meta('sales_checks',app.dump({'source_check':{'kind':'source_check','status':'passed','time':app.now(),'config_stamp':sales.check_stamp(self.w,'source_check')}}))
        with patch.object(self.w,'xhs',side_effect=AssertionError('GET must not probe')):
            self.assertNotIn('job:'+str(ident),self.ids())
            self.w.state()

    def test_config_change_and_new_failure_invalidate_old_success(self):
        first=self.fail()
        initial=recovery.scope(self.w,'account');recovery.record_success(self.w,'account',initial)
        self.assertNotIn('job:'+str(first),self.ids())
        second=self.fail()
        self.assertIn('job:'+str(second),self.ids())
        self.w.save_settings({'radar':{'xhs_url':'http://127.0.0.1:18061'}})
        self.assertIn('job:'+str(second),self.ids())

    def test_scope_change_during_probe_cannot_certify_recovery(self):
        ident=self.fail();initial=recovery.scope(self.w,'account')
        self.w.meta('xhs_bound_user','other-fixture')
        recovery.record_success(self.w,'account',initial)
        self.assertIn('job:'+str(ident),self.ids())

    def test_config_change_during_real_check_does_not_write_passed_proof(self):
        ident=self.fail()
        def transport(*args,**kwargs):
            self.w.save_settings({'radar':{'xhs_url':'http://127.0.0.1:18061'}})
            return {'is_logged_in':True,'user_id':'fixture-owner'}
        with patch.object(self.w,'xhs',side_effect=transport):recovery.run(self.w,'job:'+str(ident))
        self.assertIn('job:'+str(ident),self.ids())
        self.assertEqual(json.loads(self.w.meta('sales_checks'))['source_check']['status'],'failed')

    def test_only_valid_primary_json_recovers_model(self):
        ident=self.fail('model_check','模型欠费')
        response=lambda text:{'choices':[{'message':{'content':text},'finish_reason':'stop'}]}
        with patch('app.http_json',return_value=response('普通文字')):
            with self.assertRaises(ValueError):self.w.llm('JSON','fixture')
        self.assertIn('job:'+str(ident),self.ids())
        with patch('app.http_json',return_value=response('{"ok":true}')):self.w.llm('JSON','fixture')
        self.assertNotIn('job:'+str(ident),self.ids())

    def test_fallback_success_does_not_claim_primary_recovered(self):
        ident=self.fail('model_check','模型欠费')
        self.w.save_settings({'ai':{'fallback_base_url':'https://fallback.invalid/v1','fallback_model':'fixture-fallback','fallback_api_key':'fixture-other'}})
        response={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]}
        with patch('app.http_json',side_effect=[RuntimeError('HTTP 402'),response]):recovery.run(self.w,'job:'+str(ident))
        self.assertIn('job:'+str(ident),self.ids())

    def test_invalid_probe_schema_does_not_close_previous_incident(self):
        ident=self.fail('model_check','模型返回错误格式')
        response={'choices':[{'message':{'content':'{"wrong":true}'},'finish_reason':'stop'}]}
        with patch('app.http_json',return_value=response):recovery.run(self.w,'job:'+str(ident))
        self.assertIn('job:'+str(ident),self.ids())

    def test_ordinary_fallback_check_finishing_does_not_hide_primary_failure(self):
        ident=self.fail('model_check','主模型欠费')
        self.w.save_settings({'ai':{'fallback_base_url':'https://fallback.invalid/v1','fallback_model':'fixture-fallback','fallback_api_key':'fixture-other'}})
        self.w.enqueue('model_check')
        response={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]}
        with patch('app.http_json',side_effect=[RuntimeError('HTTP 402'),response]):self.w.run_one_job()
        self.assertIn('job:'+str(ident),self.ids())

    def test_paused_worker_does_not_run_automatic_probe(self):
        ident=self.fail();recovery.request(self.w,'job:'+str(ident),origin='scheduler')
        self.w.meta('pause_requested','1');self.assertFalse(self.w.run_one_job(recovery_only=True))

    def test_paused_worker_runs_only_explicit_manual_recovery(self):
        self.w.meta('pause_requested','1');ordinary=self.w.enqueue('generate',{'template':True})
        ident=self.fail();check=recovery.request(self.w,'job:'+str(ident))
        self.assertFalse(self.w.run_one_job())
        with patch.object(self.w,'xhs',side_effect=self.transport):self.assertTrue(self.w.run_one_job(recovery_only=True))
        self.assertEqual(self.w.meta('pause_requested'),'1')
        with self.w.db() as db:
            self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=?',(ordinary,)).fetchone()[0],'pending')
            self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=?',(check,)).fetchone()[0],'done')
        self.assertFalse(self.w.run_one_job(recovery_only=True))

    def test_manual_request_promotes_pending_auto_probe_and_deduplicates(self):
        ident=self.fail();key='job:'+str(ident)
        auto=recovery.request(self.w,key,origin='scheduler')
        self.assertEqual(recovery.request(self.w,key),auto)
        with self.w.db() as db:self.assertEqual(db.execute('SELECT origin FROM jobs WHERE id=?',(auto,)).fetchone()[0],'manual')

    def test_shutdown_blocks_even_manual_recovery(self):
        self.fail();recovery.request(self.w);self.w.meta('shutdown_requested','1')
        self.assertFalse(self.w.run_one_job(recovery_only=True))

    def test_auto_probe_has_one_global_queue_and_backoff(self):
        self.fail();self.fail('search_check','搜索失败')
        recovery.tick(self.w);recovery.tick(self.w)
        with self.w.db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE kind='recovery_check'").fetchone()[0],1)
        with patch.object(self.w,'xhs',side_effect=RuntimeError('仍未登录')):self.w.run_one_job()
        attempts=json.loads(self.w.meta('recovery_attempts'))
        self.assertTrue(all(v['next_at']>app.now() for v in attempts.values()))
        self.assertFalse(any(i['id'].startswith('job:') and 'recovery_check' in i['title'] for i in recovery.snapshot(self.w)['active']))

    def test_model_quota_stops_automatic_paid_probes(self):
        self.fail('model_check','模型失败');self.w.save_settings({'ai':{'calls_per_day':0}})
        recovery.tick(self.w)
        with self.w.db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE kind='recovery_check'").fetchone()[0],0)

    def test_quota_prompt_clears_on_adjustment_and_new_day(self):
        self.w.save_settings({'public':{'enabled':True},'ai':{'calls_per_day':1}})
        self.w.reserve_usage('llm',1);self.assertIn('quota:llm',self.ids())
        self.w.save_settings({'ai':{'calls_per_day':-1}});self.assertNotIn('quota:llm',self.ids())
        self.w.save_settings({'ai':{'calls_per_day':1}})
        with patch('app.now',return_value=(datetime.fromisoformat(app.now())+timedelta(days=1)).isoformat()):self.assertNotIn('quota:llm',self.ids())

    def test_unknown_comment_stays_until_exact_readback_no_send(self):
        self.thread();self.assertIn('results:comments',self.ids())
        with patch.object(self.w,'xhs',side_effect=self.transport),patch.object(public,'read',return_value={'comments':[]}):recovery.run(self.w,'results:comments')
        self.assertIn('results:comments',self.ids())
        comment={'id':'fixture-reply','user_id':'fixture-owner','content':'合成回复','root_id':'fixture-reply','parent_id':''}
        with patch.object(self.w,'xhs',side_effect=self.transport),patch.object(public,'read',return_value={'comments':[comment]}),patch.object(public,'submit',side_effect=AssertionError('must not submit')):
            recovery.run(self.w,'results:comments')
        self.assertNotIn('results:comments',self.ids())

    def test_failed_thread_read_recovers_independently_of_unknown_send(self):
        self.thread(errors=1)
        with patch.object(self.w,'xhs',side_effect=self.transport),patch.object(public,'read',return_value={'comments':[]}):recovery.run(self.w,'source:threads')
        self.assertNotIn('source:threads',self.ids());self.assertIn('results:comments',self.ids())

    def test_check_all_does_not_read_same_unknown_and_failed_thread_twice(self):
        self.thread(errors=1)
        with patch.object(self.w,'xhs',side_effect=self.transport),patch.object(public,'read',side_effect=RuntimeError('fixture timeout')) as read:
            recovery.run(self.w)
        self.assertEqual(read.call_count,1)
        self.assertIn('source:threads',self.ids());self.assertIn('results:comments',self.ids())

    def test_published_draft_closes_old_publish_warning(self):
        self.w.generate(template=True)
        with self.w.db() as db:ident=db.execute('SELECT MAX(id) FROM drafts').fetchone()[0]
        job=self.fail('publish','提交结果待核对',{'draft_id':ident})
        with self.w.db() as db:db.execute("UPDATE drafts SET state='published' WHERE id=?",(ident,))
        self.assertNotIn('job:'+str(job),self.ids())

    def test_readonly_sync_never_enters_reply_engine(self):
        self.w.save_settings({'autonomy':{'enabled':True,'reply_to_own_comments':True}})
        with patch.object(self.w,'xhs',side_effect=self.transport),patch.object(autopilot,'reply_to_own_comments',side_effect=AssertionError('must not reply')):
            autopilot.sync_xhs(self.w,verify_only=True)

    def test_recovery_search_does_not_enqueue_ai_and_runs_while_paused(self):
        ident=self.fail('scan','搜索失败');self.w.meta('pause_requested','1')
        with patch.object(self.w,'xhs',side_effect=self.transport):recovery.run(self.w,'job:'+str(ident))
        self.assertNotIn('job:'+str(ident),self.ids())
        with self.w.db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE kind='qualify'").fetchone()[0],0)
        self.assertEqual(self.w.meta('pause_requested'),'1')

    def test_api_validates_incident_and_preserves_origin_guard(self):
        client=RawHTTPClient(self.w);self.fail()
        self.assertEqual(client.request('/api/recovery-check',{'issue_id':'job:99999'})[0],400)
        self.assertEqual(client.request('/api/recovery-check',{'issue_id':{}})[0],400)
        self.assertEqual(client.request('/api/recovery-check',{'issue_id':'all'},headers={'Origin':'https://example.invalid'})[0],403)
        one=client.post('/api/recovery-check',{'issue_id':'all'})
        self.assertEqual(one['job_id'],client.post('/api/recovery-check',{'issue_id':'all'})['job_id'])

    def test_state_reads_have_no_network_queue_or_metadata_writes(self):
        self.fail()
        with self.w.db() as db:before=[tuple(r) for r in db.execute('SELECT * FROM meta ORDER BY key')]
        with patch.object(self.w,'xhs',side_effect=AssertionError('GET must not probe')),patch.object(self.w,'enqueue',side_effect=AssertionError('GET must not queue')):
            for _ in range(2):self.w.state()
        with self.w.db() as db:self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM meta ORDER BY key')])
