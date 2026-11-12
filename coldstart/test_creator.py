"""Creator-auth and recovery regressions. All external actions use explicit fixtures."""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import app
import ops
import xhs_session
QA=Path(__file__).resolve().parent.parent/'qa'
QA.mkdir(exist_ok=True)


class CreatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=QA)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.w=app.Workbench(self.root/'data')

    def draft(self):
        self.w.save_settings({'publishing':{'channels':['xiaohongshu'],'enabled':True}})
        self.w.meta('xhs_bound_user','fixture-account')
        self.w.generate(template=True)
        row=self.w.state()['drafts'][0]
        self.w.edit_draft({'id':row['id'],'state':'approved'})
        return row

    def test_creator_preflight_failure_never_starts_attempt(self):
        row=self.draft()
        with patch.object(self.w,'xhs',return_value={'is_logged_in':True,'user_id':'fixture-account'}),patch.object(self.w,'creator_check',side_effect=xhs_session.BeforeSubmission('needs creator login')),patch.object(self.w,'creator_submit') as submit:
            with self.assertRaises(xhs_session.BeforeSubmission):self.w.publish(row['id'])
            submit.assert_not_called()
        with self.w.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],0)

    def test_explicit_pre_submit_failure_is_blocked_with_evidence(self):
        row=self.draft()
        with patch.object(self.w,'xhs',return_value={'is_logged_in':True,'user_id':'fixture-account'}),patch.object(self.w,'creator_check'),patch.object(self.w,'creator_submit',side_effect=xhs_session.BeforeSubmission('form unavailable')):
            self.w.publish(row['id'])
        self.assertEqual(self.w.draft(row['id'])['state'],'blocked')
        with self.w.db() as db:
            self.assertEqual(db.execute('SELECT state FROM attempts').fetchone()[0],'pre_submit_failed')
            self.assertEqual(db.execute('SELECT phase FROM attempt_evidence').fetchone()[0],'pre_submit')

    def test_upload_timeout_stays_unknown_and_blocks_other_drafts(self):
        row=self.draft()
        with patch.object(self.w,'xhs',return_value={'is_logged_in':True,'user_id':'fixture-account'}),patch.object(self.w,'creator_check'),patch.object(self.w,'creator_submit',side_effect=RuntimeError('timeout after upload began')):
            self.w.publish(row['id'])
        self.assertEqual(self.w.draft(row['id'])['state'],'unknown')
        other=self.draft()
        self.w.edit_draft({'id':other['id'],'title':row['title'],'body':row['body'],'cards':[{'title':'合成验证卡','body':'这是一段本地测试卡片文字。'}],'state':'draft'})
        self.w.render_cards(other['id']);self.w.edit_draft({'id':other['id'],'state':'approved'})
        with self.assertRaisesRegex(ValueError,'结果不确定'):self.w.preflight(self.w.draft(other['id']),check_login=False)

    def test_unknown_publication_does_not_block_different_content(self):
        row=self.draft()
        with self.w.db() as db:db.execute("UPDATE drafts SET state='unknown' WHERE id=?",(row['id'],))
        other=self.draft()
        self.w.edit_draft({'id':other['id'],'title':'另一篇实用方法','cards':[{'title':'合成验证卡','body':'这是一段本地测试卡片文字。'}],'state':'draft'})
        self.w.render_cards(other['id']);self.w.edit_draft({'id':other['id'],'state':'approved'})
        self.w.preflight(self.w.draft(other['id']),check_login=False)

    def test_before_click_failure_task_is_needs_setup_not_done(self):
        row=self.draft()
        with patch.object(self.w,'xhs',return_value={'is_logged_in':True,'user_id':'fixture-account'}),patch.object(self.w,'creator_check'),patch.object(self.w,'creator_submit',side_effect=xhs_session.BeforeSubmission('editing failed','editing')):
            self.w.enqueue('publish',{'draft_id':row['id']});self.w.run_one_job()
        self.assertEqual(self.w.state()['jobs'][0]['state'],'needs_setup')

    def fixture_bridge(self,**changes):
        binary=self.root/'xhs-session-windows-amd64.exe';binary.write_bytes(b'fixture-executable-not-run')
        adapter=self.root/'adapters/xiaohongshu-mcp';adapter.mkdir(parents=True)
        cookie=adapter/'cookies.json';cookie.write_text('{"fixture":true}')
        report={'version':'0.2.2','mode':'check','main_ok':True,'creator_ready':True,'user_id':'fixture-account','creator_user_id':'fixture-account','stage':'pre_submit','cookie_sha256':hashlib.sha256(cookie.read_bytes()).hexdigest()}
        report.update(changes)
        return binary,report

    def test_bridge_binds_real_cookie_digest_and_account_to_process_result(self):
        binary,report=self.fixture_bridge()
        with patch.object(xhs_session,'BRIDGE_SHA256',hashlib.sha256(binary.read_bytes()).hexdigest()),patch.object(xhs_session.process_runner,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(report))) as run:
            self.assertTrue(xhs_session.invoke('fixture-account',root=self.root)['creator_ready'])
            self.assertIn('fixture-account',run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['env']['COOKIES_PATH'],str(self.root/'adapters/xiaohongshu-mcp/cookies.json'))

    def test_bridge_rejects_account_change(self):
        binary,report=self.fixture_bridge(user_id='different-account')
        with patch.object(xhs_session,'BRIDGE_SHA256',hashlib.sha256(binary.read_bytes()).hexdigest()),patch.object(xhs_session.process_runner,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(report))):
            with self.assertRaises(RuntimeError):xhs_session.invoke('fixture-account',root=self.root)

    def test_read_only_comments_mode_checks_account_cookie_without_creator_permission(self):
        binary,report=self.fixture_bridge(mode='read-comments',creator_ready=False,stage='comments_read',comment_readback={'feed_id':'fixture-note','comments':[]})
        with patch.object(xhs_session,'BRIDGE_SHA256',hashlib.sha256(binary.read_bytes()).hexdigest()),patch.object(xhs_session.process_runner,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(report))) as run:
            result=xhs_session.invoke('fixture-account',mode='read-comments',request_file=self.root/'request.json',root=self.root)
        self.assertEqual(result['stage'],'comments_read');self.assertEqual(run.call_args.kwargs['timeout'],195)

    def test_bridge_rejects_different_creator_identity(self):
        binary,report=self.fixture_bridge(creator_user_id='different-creator')
        with patch.object(xhs_session,'BRIDGE_SHA256',hashlib.sha256(binary.read_bytes()).hexdigest()),patch.object(xhs_session.process_runner,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(report))):
            with self.assertRaises(RuntimeError):xhs_session.invoke('fixture-account',root=self.root)

    def test_bridge_rejects_changed_cookie_after_check(self):
        binary,report=self.fixture_bridge(cookie_sha256='not-the-same')
        with patch.object(xhs_session,'BRIDGE_SHA256',hashlib.sha256(binary.read_bytes()).hexdigest()),patch.object(xhs_session.process_runner,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(report))):
            with self.assertRaisesRegex(ValueError,'会话文件发生变化'):xhs_session.invoke('fixture-account',root=self.root)

    def proof_fixture(self,message=None):
        binary=self.root/'adapter.exe';binary.write_bytes(b'pinned-fixture')
        logs=self.root/'logs';logs.mkdir(exist_ok=True)
        timestamp=app.now()
        message=message or '发布内容失败: title=测试标题 创作者中心登录失效，请重新扫码登录'
        log='time="'+timestamp+'" level=info msg="xiaohongshu-mcp version: v2.7.3"\n'
        log+='time="'+timestamp+'" level=error msg="'+message+'"\n'
        log+='time="'+timestamp+'" level=error msg="POST /api/v1/publish 500"\n'
        (logs/'xhs-service-fixture.log').write_text(log,encoding='utf-8')
        return binary,logs,timestamp

    def test_recovery_requires_exact_constructor_error_and_pinned_binary(self):
        binary,logs,timestamp=self.proof_fixture()
        with patch.object(xhs_session,'ADAPTER_SHA256',hashlib.sha256(binary.read_bytes()).hexdigest()):
            proof=xhs_session.historical_proof({'title':'测试标题'},{'started_at':timestamp},timestamp,logs,binary)
            self.assertEqual(proof['type'],'pinned_creator_constructor_failed_before_upload')

    def test_generic_timeout_is_never_recovery_evidence(self):
        binary,logs,timestamp=self.proof_fixture('发布内容失败: title=测试标题 请求超时')
        with patch.object(xhs_session,'ADAPTER_SHA256',hashlib.sha256(binary.read_bytes()).hexdigest()):
            with self.assertRaises(ValueError):xhs_session.historical_proof({'title':'测试标题'},{'started_at':timestamp},timestamp,logs,binary)

    def test_wrong_title_or_time_cannot_restore_unknown(self):
        binary,logs,timestamp=self.proof_fixture()
        with patch.object(xhs_session,'ADAPTER_SHA256',hashlib.sha256(binary.read_bytes()).hexdigest()):
            with self.assertRaises(ValueError):xhs_session.historical_proof({'title':'其他标题'},{'started_at':timestamp},timestamp,logs,binary)
            later=(datetime.fromisoformat(timestamp)+timedelta(minutes=1)).isoformat()
            with self.assertRaises(ValueError):xhs_session.historical_proof({'title':'测试标题'},{'started_at':later},later,logs,binary)

    def test_empty_search_item_does_not_discard_valid_items(self):
        self.w.save_settings({'radar':{'xhs_enabled':True,'max_details':0,'max_queries':1}})
        def transport(path,*args,**kwargs):
            if path.endswith('/status'):return {'is_logged_in':True}
            return {'feeds':[{'id':'empty','noteCard':{'user':{'nickname':'fixture'}}},{'id':'valid','noteCard':{'displayTitle':'求助自动化'}}]}
        with patch.object(self.w,'xhs',side_effect=transport):self.w.scan()
        self.assertEqual(len(self.w.state()['leads']),1)
        self.assertIn('valid',self.w.state()['leads'][0]['url'])

    def test_not_tested_search_does_not_mark_passed_connections_as_setup_failure(self):
        self.w.save_settings({'ai':{'api_key':'fixture'},'publishing':{'channels':['xiaohongshu']},'radar':{'ark_search_enabled':True}})
        with patch.object(self.w,'llm',return_value={'ok':True}),patch.object(self.w,'xhs',return_value={'is_logged_in':True,'user_id':'fixture-account'}),patch.object(self.w,'creator_check'):
            self.w.enqueue('check');self.w.run_one_job()
        self.assertEqual(self.w.state()['jobs'][0]['state'],'done')
        self.assertEqual(json.loads(self.w.meta('diagnostics'))['items'][-1]['status'],'not_tested')

    def test_restart_preserves_pause_and_clears_only_shutdown(self):
        ops.control(self.w.data,'pause');ops.control(self.w.data,'shutdown');ops.control(self.w.data,'prepare_start')
        result=ops.status(self.w.data)
        self.assertTrue(result['paused']);self.assertFalse(result['shutdown_requested'])

    def test_diagnostic_uses_timestamped_evidence(self):
        self.w.meta('diagnostics',app.dump({'time':app.now(),'items':[{'component':'模型','status':'passed'},{'component':'小红书账号','status':'passed'},{'component':'小红书创作者中心','status':'passed'}]}))
        status=ops.status(self.w.data)
        self.assertEqual(status['live_network'],'PASSED_LAST_CHECK');self.assertEqual(status['live_accounts'],'PASSED_LAST_CHECK')
        self.assertIn('last_check_time',status)

    def unknown_attempt(self):
        row=self.draft()
        moment=(datetime.now(app.CST)-timedelta(minutes=5)).isoformat(timespec='seconds')
        with self.w.db() as db:
            db.execute("UPDATE drafts SET state='unknown' WHERE id=?",(row['id'],))
            db.execute("INSERT INTO attempts(draft_id,platform,started_at,state) VALUES(?,'xiaohongshu',?,'unknown')",(row['id'],moment))
        return row

    def test_creator_all_notes_empty_can_restore_with_machine_evidence(self):
        row=self.unknown_attempt()
        proof={'checked_at':app.now(),'inventory_loaded':True,'inventory_empty':True,'inventory_view':'all'}
        with patch.object(xhs_session,'invoke',return_value=proof):self.w.recover_creator_absent(row['id'])
        self.assertEqual(self.w.draft(row['id'])['state'],'draft')
        self.assertNotIn('用户已',self.w.draft(row['id'])['receipt'])
        with self.w.db() as db:self.assertEqual(db.execute('SELECT phase FROM attempt_evidence').fetchone()[0],'creator_all_notes_verified_empty')

    def test_creator_nonempty_or_incomplete_view_cannot_restore_unknown(self):
        row=self.unknown_attempt()
        for proof in [{'inventory_loaded':True,'inventory_empty':False,'inventory_view':'all'},{'inventory_loaded':False,'inventory_empty':True,'inventory_view':'all'},{'inventory_loaded':True,'inventory_empty':True,'inventory_view':'published_only'}]:
            with patch.object(xhs_session,'invoke',return_value=proof):
                with self.assertRaises(ValueError):self.w.recover_creator_absent(row['id'])
            self.assertEqual(self.w.draft(row['id'])['state'],'unknown')

    def test_different_account_empty_inventory_cannot_restore_old_submission(self):
        row=self.unknown_attempt();self.w.meta('xhs_bound_user','different-account')
        with patch.object(xhs_session,'invoke') as remote:
            with self.assertRaisesRegex(ValueError,'原发布目标或账号'):self.w.recover_creator_absent(row['id'])
            remote.assert_not_called()
        self.assertEqual(self.w.draft(row['id'])['state'],'unknown')

    def test_account_change_during_inventory_check_preserves_unknown(self):
        row=self.unknown_attempt()
        def inventory(*args,**kwargs):
            self.w.meta('xhs_bound_user','different-account')
            return {'checked_at':app.now(),'inventory_loaded':True,'inventory_empty':True,'inventory_view':'all'}
        with patch.object(xhs_session,'invoke',side_effect=inventory):
            with self.assertRaisesRegex(ValueError,'核对期间发布目标或账号'):self.w.recover_creator_absent(row['id'])
        self.assertEqual(self.w.draft(row['id'])['state'],'unknown')

    def test_failure_after_final_click_is_never_classified_as_safe_to_resubmit(self):
        binary,report=self.fixture_bridge(mode='publish',stage='final_click_started',detail='unknown after click')
        with patch.object(xhs_session,'BRIDGE_SHA256',hashlib.sha256(binary.read_bytes()).hexdigest()),patch.object(xhs_session.process_runner,'run',return_value=SimpleNamespace(returncode=1,stdout=json.dumps(report))):
            with self.assertRaises(RuntimeError) as error:xhs_session.invoke('fixture-account',mode='publish',request_file=self.root/'request.json',root=self.root)
        self.assertNotIsInstance(error.exception,xhs_session.BeforeSubmission)

    def test_proven_before_final_click_failure_preserves_record_without_consuming_submission_quota(self):
        row=self.draft();self.w.save_settings({'publishing':{'max_posts_per_day':1,'min_gap_minutes':30}})
        with self.w.db() as db:
            attempt=db.execute("INSERT INTO attempts(draft_id,platform,started_at,state) VALUES(?,'xiaohongshu',?,'pre_submit_failed')",(row['id'],app.now())).lastrowid
            db.execute('INSERT INTO attempt_evidence VALUES(?,?,?,?)',(attempt,'editing','{}',app.now()))
        self.w.preflight(self.w.draft(row['id']),check_login=False)
        with self.w.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0],1)

    def test_unproven_failure_still_counts_for_quota(self):
        row=self.draft();self.w.save_settings({'publishing':{'max_posts_per_day':1}})
        with self.w.db() as db:db.execute("INSERT INTO attempts(draft_id,platform,started_at,state) VALUES(?,'xiaohongshu',?,'pre_submit_failed')",(row['id'],app.now()))
        with self.assertRaises(app.PublishDeferred):self.w.preflight(self.w.draft(row['id']),check_login=False)

    def test_identity_verification_timeout_remains_unknown(self):
        binary,report=self.fixture_bridge(mode='publish',stage='verification_required',detail='identity confirmation still required')
        with patch.object(xhs_session,'BRIDGE_SHA256',hashlib.sha256(binary.read_bytes()).hexdigest()),patch.object(xhs_session.process_runner,'run',return_value=SimpleNamespace(returncode=1,stdout=json.dumps(report))):
            with self.assertRaises(RuntimeError) as error:xhs_session.invoke('fixture-account',mode='publish',request_file=self.root/'request.json',root=self.root)
        self.assertNotIsInstance(error.exception,xhs_session.BeforeSubmission)

    def progress_fixture(self):
        row=self.draft()
        with self.w.db() as db:
            db.execute("UPDATE drafts SET state='publishing' WHERE id=?",(row['id'],))
            db.execute("INSERT INTO attempts(draft_id,platform,started_at,state) VALUES(?,'xiaohongshu',?,'started')",(row['id'],app.now()))
        directory=self.root/'logs/creator-fixture';directory.mkdir(parents=True)
        self.w.meta('creator_progress_dir',str(directory))
        value={'mode':'publish','stage':'verification_required','user_id':'fixture-account','creator_user_id':'fixture-account','checked_at':app.now(),'cookie_sha256':'private-cookie-reference','detail':'private-arbitrary-detail'}
        (directory/'progress.json').write_text(json.dumps(value),encoding='utf-8')
        return directory,value

    def test_identity_progress_exposes_only_safe_current_instructions(self):
        directory,value=self.progress_fixture()
        with patch.object(app,'ROOT',self.root/'coldstart'):
            progress=self.w.publishing_progress()
        self.assertEqual(progress['stage'],'verification_required')
        self.assertNotIn('private-',json.dumps(progress))
        self.assertNotIn('cookie_sha256',progress)

    def test_identity_progress_rejects_wrong_account_stale_time_and_outside_path(self):
        directory,value=self.progress_fixture()
        for changes in [{'creator_user_id':'other-account'},{'checked_at':(datetime.now(app.CST)-timedelta(minutes=3)).isoformat()}]:
            (directory/'progress.json').write_text(json.dumps({**value,**changes}),encoding='utf-8')
            with patch.object(app,'ROOT',self.root/'coldstart'):self.assertEqual(self.w.publishing_progress(),{})
        self.w.meta('creator_progress_dir',str(self.root/'outside-logs'))
        with patch.object(app,'ROOT',self.root/'coldstart'):self.assertEqual(self.w.publishing_progress(),{})

    def test_sms_progress_uses_safe_instructions_without_phone_or_code(self):
        directory,value=self.progress_fixture()
        value.update(verification_type='sms',detail='验证码已发送至 +86 private-phone; private-code')
        (directory/'progress.json').write_text(json.dumps(value),encoding='utf-8')
        with patch.object(app,'ROOT',self.root/'coldstart'):progress=self.w.publishing_progress()
        self.assertIn('短信验证码',progress['detail'])
        self.assertNotIn('扫码',progress['detail'])
        self.assertNotIn('private-',json.dumps(progress))

    def test_verified_identity_progress_does_not_repeat_scan_instruction(self):
        directory,value=self.progress_fixture()
        (directory/'progress.json').write_text(json.dumps({**value,'stage':'verification_passed'}),encoding='utf-8')
        with patch.object(app,'ROOT',self.root/'coldstart'):progress=self.w.publishing_progress()
        self.assertEqual(progress['stage'],'verification_passed')
        self.assertNotIn('扫码',progress['detail'])


if __name__=='__main__':unittest.main()
