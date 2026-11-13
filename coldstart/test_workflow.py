"""Queue, upgrade, scheduling and business-facing runtime acceptance tests."""
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import app
import autopilot
import public_engagement
import workflow
from test_integration import RawHTTPClient


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = app.Workbench(self.tmp.name)

    def full(self):
        self.w.save_settings({'public': {'enabled': True, 'send_enabled': True, 'own_comments_enabled': True},
                              'autonomy': {'enabled': True, 'cycle_minutes': 30},
                              'publishing': {'enabled': True, 'auto_approve': True}, 'radar': {'xhs_enabled': True},
                              'schedule': {'enabled': True}})
        self.w.meta('xhs_bound_user', 'synthetic-owner')

    def rows(self):
        with self.w.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY id')]

    def live_thread(self):
        self.w._worker_thread = threading.current_thread()

    def test_one_plan_owns_search_and_comments_not_legacy_schedule(self):
        self.full()
        for _ in range(10):
            self.w.tick()
        kinds = [r['kind'] for r in self.rows()]
        self.assertCountEqual(kinds, ['public_scan', 'public_monitor', 'autopilot'])
        self.assertTrue(all(r['origin'] == 'scheduler' for r in self.rows()))

    def test_comments_mode_never_schedules_writing_or_old_daily_generation(self):
        self.full()
        self.w.save_settings({'publishing': {'enabled': False}})
        self.w.tick()
        self.assertCountEqual([r['kind'] for r in self.rows()], ['public_scan', 'public_monitor'])

    def test_switching_mode_removes_only_obsolete_automatic_work(self):
        self.full()
        self.w.tick()
        manual = self.w.enqueue('generate', {'template': True})
        self.w.save_settings({'publishing': {'enabled': False}})
        workflow.prune(self.w)
        jobs = {r['id']: r for r in self.rows()}
        self.assertEqual(jobs[manual]['state'], 'pending')
        self.assertEqual(next(r for r in jobs.values() if r['kind'] == 'autopilot')['state'], 'cancelled')

    def test_manual_search_reuses_existing_automatic_search(self):
        self.full()
        first = self.w.enqueue('public_scan', origin='scheduler')
        self.assertEqual(self.w.enqueue('scan'), first)
        self.assertEqual(len(self.rows()), 1)

    def test_manual_search_completion_does_not_trigger_immediate_duplicate_auto_search(self):
        self.full()
        self.w.enqueue('scan')
        self.assertEqual(self.rows()[0]['kind'],'public_scan')
        with patch.object(self.w,'scan',return_value='合成搜索完成'),patch.object(public_engagement,'discover'):
            self.w.run_one_job()
        self.w.tick()
        self.assertEqual(len([r for r in self.rows() if r['kind']=='public_scan']),1)

    def test_old_duplicate_searches_merge_without_losing_manual_intent(self):
        self.full()
        first=self.w.enqueue('public_scan',origin='scheduler')
        with self.w.db() as db:
            duplicate=db.execute("INSERT INTO jobs(kind,payload,origin,created_at,updated_at) VALUES('scan','{}','manual',?,?)",(app.now(),app.now())).lastrowid
        workflow.prune(self.w)
        jobs={r['id']:r for r in self.rows()}
        self.assertEqual(jobs[first]['origin'],'manual')
        self.assertEqual(jobs[duplicate]['state'],'cancelled')
        self.w.save_settings({'public':{'enabled':False},'autonomy':{'enabled':False},'publishing':{'enabled':False},'schedule':{'enabled':False}})
        workflow.prune(self.w)
        self.assertEqual(next(r for r in self.rows() if r['id']==first)['state'],'pending')

    def test_manual_comment_check_reuses_pending_monitor(self):
        self.full()
        first = self.w.enqueue('public_monitor', origin='scheduler')
        self.assertEqual(self.w.enqueue('process_comments'), first)

    def test_paused_queue_has_no_countdown_and_cannot_be_executed(self):
        self.full()
        self.w.tick()
        self.w.meta('pause_requested', '1')
        with patch.object(self.w, 'scan') as scan:
            self.assertFalse(self.w.run_one_job())
        scan.assert_not_called()
        view = workflow.snapshot(self.w)
        self.assertEqual(view['status'], 'paused')
        self.assertTrue(all(s['next_at'] is None for s in view['plan']))
        self.assertTrue(all('已暂停' in j['wait_reason'] for j in view['queue']))

    def test_tick_does_not_create_more_work_during_pause_or_shutdown(self):
        self.full()
        for key in ('pause_requested', 'shutdown_requested'):
            self.w.meta(key, '1')
            self.w.tick()
            self.assertEqual(self.rows(), [])
            self.w.meta(key, '0')

    def test_queue_counts_do_not_depend_on_display_limit(self):
        self.w.save_settings({'display': {'jobs': 0}})
        self.w.enqueue('model_check')
        state = self.w.state()
        self.assertEqual(state['jobs'], [])
        self.assertEqual(state['workflow']['pending_count'], 1)
        self.assertEqual(state['workflow']['queue'][0]['label'], '检查模型连接')

    def test_cancelling_waiting_job_never_changes_running_job(self):
        one = self.w.enqueue('model_check')
        two = self.w.enqueue('writing_check')
        with self.w.db() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (one,))
        with self.assertRaises(ValueError):
            workflow.job_control(self.w, 'cancel', one)
        workflow.job_control(self.w, 'cancel', two)
        self.assertEqual([r['state'] for r in self.rows()], ['running', 'cancelled'])

    def test_clear_queue_pauses_to_prevent_immediate_refill(self):
        self.full()
        self.w.tick()
        count = len(self.rows())
        self.assertEqual(workflow.job_control(self.w, 'clear_pending')['cancelled'], count)
        self.w.tick()
        self.assertEqual(len(self.rows()), count)
        self.assertTrue(all(r['state'] == 'cancelled' for r in self.rows()))
        self.assertEqual(self.w.meta('pause_requested'), '1')

    def test_queue_api_reports_completed_task_cannot_be_cancelled(self):
        job = self.w.enqueue('model_check')
        client = RawHTTPClient(self.w)
        code, body = client.request('/api/queue-control', {'action': 'cancel', 'job_id': job})
        self.assertEqual(code, 200)
        self.assertEqual(body['cancelled'], 1)
        code, body = client.request('/api/queue-control', {'action': 'cancel', 'job_id': job})
        self.assertEqual(code, 400)
        self.assertIn('已结束', body['error'])

    def test_successful_search_is_scheduled_from_completion(self):
        self.full()
        self.w.enqueue('public_scan')
        with patch.object(self.w, 'scan', return_value='合成搜索结果'), patch.object(public_engagement, 'discover'):
            self.w.run_one_job()
        step = next(s for s in workflow.plan(self.w) if s['kind'] == 'public_scan')
        seconds = (workflow.parse(step['at']) - workflow.clock()).total_seconds()
        self.assertGreater(seconds, 29 * 60)
        self.assertLessEqual(seconds, 30 * 60)
        self.w.tick()
        self.assertEqual(len([r for r in self.rows() if r['kind'] == 'public_scan']), 1)

    def test_failure_backoff_does_not_block_unrelated_jobs(self):
        self.full()
        self.w.enqueue('public_scan')
        with patch.object(self.w, 'scan', side_effect=RuntimeError('合成超时')):
            self.w.run_one_job()
        when = workflow.due(self.w, 'public_scan', 30)
        self.assertGreater((when - workflow.clock()).total_seconds(), 55)
        self.assertLessEqual((when - workflow.clock()).total_seconds(), 60)
        manual = self.w.enqueue('model_check')
        with patch.object(app.sales, 'check', return_value='合成模型检查成功'):
            self.w.run_one_job()
        self.assertEqual(next(r for r in self.rows() if r['id'] == manual)['state'], 'done')

    def test_changing_interval_immediately_changes_next_plan(self):
        self.full()
        self.w.meta('workflow_finished:public_scan', app.now())
        self.w.save_settings({'public': {'search_minutes': 7}})
        when = next(s for s in workflow.plan(self.w) if s['kind'] == 'public_scan')['at']
        self.assertAlmostEqual((workflow.parse(when) - workflow.clock()).total_seconds(), 420, delta=2)

    def test_public_writer_never_repeats_search_or_owned_reply_scan(self):
        self.full()
        self.w.save_settings({'ai': {'base_url': 'https://provider.invalid/v1', 'model': 'synthetic', 'api_key': 'fixture-secret'},
                              'autonomy': {'generations_per_day': 0}, 'publishing': {'auto_approve': False}})
        with patch.object(self.w, 'scan') as scan, patch.object(autopilot, 'sync_xhs') as sync:
            autopilot.run_cycle(self.w)
        scan.assert_not_called()
        sync.assert_not_called()
        report = json.loads(self.w.meta('autonomy_report'))
        self.assertEqual(report['status'], 'done')
        self.assertIn('今日自动创作次数', json.dumps(report, ensure_ascii=False))

    def test_pausing_active_request_is_not_reported_as_already_stopped(self):
        self.w.enqueue('model_check')
        entered = threading.Event()
        release = threading.Event()
        def check(*args):
            workflow.activity(self.w, '正在等待模型响应', '合成测试')
            entered.set()
            release.wait(3)
            return '检查完成'
        with patch.object(app.sales, 'check', side_effect=check):
            runner = threading.Thread(target=self.w.run_one_job)
            runner.start()
            try:
                self.assertTrue(entered.wait(2))
                self.w.meta('pause_requested', '1')
                view = workflow.snapshot(self.w)
                self.assertEqual(view['status'], 'pausing')
                self.assertEqual(view['active']['stage']['title'], '正在等待模型响应')
            finally:
                release.set()
                runner.join(3)
        self.assertEqual(workflow.snapshot(self.w)['status'], 'paused')

    def test_worker_waiting_and_paused_are_actual_thread_states(self):
        self.w.meta('pause_requested', '1')
        thread = threading.Thread(target=self.w.worker)
        thread.start()
        try:
            for _ in range(50):
                if workflow.snapshot(self.w)['worker_alive']:
                    break
                time.sleep(.01)
            self.assertTrue(workflow.snapshot(self.w)['worker_alive'])
            self.assertEqual(workflow.snapshot(self.w)['status'], 'paused')
        finally:
            self.w.stop.set()
            thread.join(2)
        self.assertFalse(workflow.snapshot(self.w)['worker_alive'])

    def test_reading_status_has_no_model_platform_or_queue_side_effects(self):
        self.full()
        self.w.save_settings({'ai': {'api_key': 'synthetic-secret-never-show'}})
        before = self.rows()
        with patch.object(self.w, 'xhs') as xhs, patch.object(self.w, 'llm') as llm:
            state = self.w.state()
        xhs.assert_not_called()
        llm.assert_not_called()
        self.assertEqual(self.rows(), before)
        self.assertNotIn('synthetic-secret-never-show', json.dumps(state))

    def test_daily_search_limit_waits_until_tomorrow_not_failure_loop(self):
        self.full()
        self.w.save_settings({'radar': {'searches_per_day': 1}})
        self.w.reserve_usage('search', 1)
        self.w.tick()
        self.assertNotIn('public_scan', [r['kind'] for r in self.rows()])
        search = next(s for s in workflow.plan(self.w) if s['kind'] == 'public_scan')
        self.assertEqual(workflow.parse(search['at']).date(), workflow.clock().date() + timedelta(days=1))

    def test_search_limit_zero_has_explicit_disabled_reason(self):
        self.full()
        self.live_thread()
        self.w.save_settings({'radar': {'searches_per_day': 0}})
        self.w.tick()
        search = next(s for s in workflow.snapshot(self.w)['plan'] if s['kind'] == 'public_scan')
        self.assertEqual(search['state'], 'disabled')
        self.assertIsNone(search['next_at'])
        self.assertIn('设为0', search['hold_reason'])

    def test_offline_thread_does_not_pretend_it_is_running(self):
        self.full()
        self.assertEqual(workflow.snapshot(self.w)['status'], 'offline')

    def test_mode_summary_matches_all_public_modes(self):
        self.full()
        self.assertEqual(workflow.snapshot(self.w)['mode'], 'full')
        self.w.save_settings({'publishing': {'enabled': False}})
        self.assertEqual(workflow.snapshot(self.w)['mode'], 'comments')
        self.w.save_settings({'public': {'send_enabled': False}})
        self.assertEqual(workflow.snapshot(self.w)['mode'], 'observe')

    def test_old_queue_upgrade_keeps_manual_checks_and_business_settings(self):
        self.full()
        old_settings = self.w.config()
        self.w.enqueue('model_check')
        self.w.enqueue('public_scan')
        with self.w.db() as db:
            db.execute("DELETE FROM meta WHERE key='workflow_queue_v1'")
        reopened = app.Workbench(self.tmp.name)
        self.assertEqual(reopened.config(), old_settings)
        self.assertEqual([r['origin'] for r in self.rows()], ['manual', 'legacy'])
        self.assertEqual([r['state'] for r in self.rows()], ['pending', 'pending'])

    def test_runtime_business_summary_has_actual_model_and_rules(self):
        self.full()
        self.w.save_settings({'business': {'name': '合成业务', 'offer': '合成问题解答', 'keywords': ['合成问题']},
                              'ai': {'model': 'synthetic-model'}, 'public': {'watch_minutes': 9}})
        view = workflow.snapshot(self.w)
        self.assertEqual(view['business']['model'], 'synthetic-model')
        self.assertEqual(view['rules']['watch_minutes'], 9)
        self.assertEqual(view['business']['keywords'], ['合成问题'])

    def test_active_monitor_uses_earliest_thread_due_time_not_fixed_minute(self):
        self.full()
        self.w.save_settings({'public': {'own_comments_enabled': False}})
        later = workflow.clock() + timedelta(minutes=9)
        with self.w.db() as db:
            db.execute("INSERT INTO public_threads(account,feed,token,target,title,context,next_at,created_at) VALUES('synthetic-owner','synthetic-feed','','','合成讨论','合成正文',?,?)", (workflow.iso(later), app.now()))
        self.assertEqual(workflow.monitor_due(self.w, self.w.config()), workflow.parse(workflow.iso(later)))

    def draft(self, title='合成排期稿'):
        with self.w.db() as db:
            return db.execute("INSERT INTO drafts(platform,title,body,tags,cards,state,generator,scheduled_at,created_at,updated_at) VALUES('xiaohongshu',?,'合成正文','[]','[]','approved','ai',?,?,?)", (title, app.now(), app.now(), app.now())).lastrowid

    def test_cancelling_publish_removes_schedule_instead_of_immediate_requeue(self):
        self.w.save_settings({'publishing': {'enabled': True}})
        draft = self.draft()
        job = self.w.enqueue('publish', {'draft_id': draft}, origin='scheduler')
        workflow.job_control(self.w, 'cancel', job)
        self.w.tick()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.w.draft(draft)['state'], 'cancelled')
        self.assertIn('原稿仍保留', self.w.draft(draft)['receipt'])

    def test_cancelling_old_publish_job_does_not_cancel_new_draft_schedule(self):
        self.w.save_settings({'publishing': {'enabled': True}})
        draft = self.draft()
        job = self.w.enqueue('publish', {'draft_id': draft, 'scheduled_at': app.now()}, origin='scheduler')
        later = workflow.iso(workflow.clock() + timedelta(days=1))
        with self.w.db() as db:
            db.execute('UPDATE drafts SET scheduled_at=? WHERE id=?', (later, draft))
        workflow.job_control(self.w, 'cancel', job)
        self.assertEqual(self.w.draft(draft)['state'], 'approved')
        self.assertEqual(self.w.draft(draft)['scheduled_at'], later)

    def test_plan_distinguishes_different_publish_jobs(self):
        self.full()
        self.live_thread()
        one, two = self.draft('合成稿一'), self.draft('合成稿二')
        job = self.w.enqueue('publish', {'draft_id': one})
        with self.w.db() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
        plan = {s['key']: s for s in workflow.snapshot(self.w)['plan']}
        self.assertEqual(plan['publish:' + str(one)]['state'], 'running')
        self.assertEqual(plan['publish:' + str(two)]['state'], 'due')
        self.w.meta('pause_requested', '1')
        plan = {s['key']: s for s in workflow.snapshot(self.w)['plan']}
        self.assertEqual(plan['publish:' + str(one)]['state'], 'finishing')
        self.assertIsNone(plan['publish:' + str(one)]['next_at'])

    def test_publish_identity_verification_is_visible_in_current_activity(self):
        draft = self.draft()
        job = self.w.enqueue('publish', {'draft_id': draft})
        with self.w.db() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (job,))
        with patch.object(self.w, 'publishing_progress', return_value={'stage': 'verification_required', 'detail': '合成验证提示', 'checked_at': app.now()}):
            active = workflow.snapshot(self.w)['active']
        self.assertEqual(active['stage']['title'], '需要账号本人验证')
        self.assertEqual(active['stage']['detail'], '合成验证提示')

    def test_actual_old_job_schema_gets_one_consistent_upgrade_backup(self):
        self.w.enqueue('model_check')
        with self.w.db() as db:
            db.execute('ALTER TABLE jobs DROP COLUMN origin')
            db.execute("DELETE FROM meta WHERE key='workflow_queue_v1'")
        old = self.w.config()
        reopened = app.Workbench(self.tmp.name)
        backups = list((reopened.data / 'backups').glob('before-0.5-*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertNotIn('origin', [r[1] for r in db.execute('PRAGMA table_info(jobs)')])
            self.assertEqual(db.execute('SELECT kind FROM jobs').fetchone()[0], 'model_check')
        self.assertEqual(reopened.config(), old)
        app.Workbench(self.tmp.name)
        self.assertEqual(len(list((reopened.data / 'backups').glob('before-0.5-*.sqlite3'))), 1)

    def test_legacy_partial_switch_combination_is_honestly_named_custom(self):
        self.full()
        self.w.save_settings({'publishing': {'auto_approve': False}})
        self.assertEqual(workflow.snapshot(self.w)['mode'], 'custom')

    def test_cancel_pending_is_responsive_during_external_operation_lock(self):
        job = self.w.enqueue('model_check')
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        def hold():
            with self.w.lock:
                entered.set()
                release.wait(3)
        def cancel():
            workflow.job_control(self.w, 'cancel', job)
            done.set()
        holder = threading.Thread(target=hold)
        holder.start()
        canceller = threading.Thread(target=cancel)
        try:
            self.assertTrue(entered.wait(1))
            canceller.start()
            self.assertTrue(done.wait(1), 'Cancelling a pending task must not wait for the external send lock')
        finally:
            release.set()
            holder.join(2)
            if canceller.ident:
                canceller.join(2)

    def test_paused_analysis_resumes_same_job_without_reanalysing_finished_rows(self):
        self.w.save_settings({'ai': {'analysis_per_run': -1}})
        self.w.ingest([{'platform':'xiaohongshu','url':f'https://www.xiaohongshu.com/explore/synthetic-{i}', 'title':'合成问题','excerpt':'合成正文'} for i in range(12)])
        job = self.w.enqueue('qualify')
        processed = []
        def batch(size):
            with self.w.db() as db:
                rows = [r[0] for r in db.execute("SELECT id FROM leads WHERE analyzed_at='' ORDER BY id LIMIT ?", (size,))]
                for lead in rows:
                    db.execute('UPDATE leads SET analyzed_at=? WHERE id=?', (app.now(), lead))
            processed.extend(rows)
            if len(processed) == 8:
                self.w.meta('pause_requested','1')
            return f'完成 {len(rows)} 条候选分析'
        with patch.object(self.w,'_qualify_batch',side_effect=batch):
            self.w.run_one_job()
            self.assertEqual(self.rows()[0]['state'],'pending')
            self.assertEqual(len(processed),8)
            self.w.meta('pause_requested','0')
            self.w.run_one_job()
        self.assertEqual(len(set(processed)),12)
        self.assertEqual(self.rows()[0]['id'],job)
        self.assertEqual(self.rows()[0]['state'],'done')

    def test_pause_between_content_steps_keeps_resumable_job_without_failure(self):
        self.w.save_settings({'autonomy': {'enabled':True,'generations_per_day':1}, 'ai': {'base_url':'https://provider.invalid/v1','model':'synthetic','api_key':'fixture-secret'}})
        self.w.enqueue('autopilot')
        def scan():
            self.w.meta('pause_requested','1')
            return '合成搜索已保留'
        with patch.object(autopilot,'sync_xhs',return_value='合成只读回查'),patch.object(self.w,'scan',side_effect=scan),patch.object(self.w,'llm') as llm:
            self.w.run_one_job()
        llm.assert_not_called()
        self.assertEqual(self.rows()[0]['state'],'pending')
        self.assertIn('等待继续',self.rows()[0]['message'])
        self.assertEqual(json.loads(self.w.meta('autonomy_report'))['status'],'paused')
        self.assertEqual(self.w.meta('workflow_failure:autopilot'),'')

    def test_legacy_daily_generation_failure_retries_after_backoff(self):
        self.w.save_settings({'schedule':{'enabled':True,'generate_time':'00:00'}})
        self.w.enqueue('generate',origin='scheduler')
        with patch.object(self.w,'generate',side_effect=RuntimeError('合成超时')):
            self.w.run_one_job()
        self.assertNotEqual(self.w.meta('last_scheduled_generate'),workflow.clock().date().isoformat())
        due = next(s for s in workflow.plan(self.w) if s['kind']=='generate')
        self.assertGreater((workflow.parse(due['at'])-workflow.clock()).total_seconds(),55)
        self.assertLessEqual((workflow.parse(due['at'])-workflow.clock()).total_seconds(),60)

    def test_writing_check_uses_current_business_instead_of_fixed_spreadsheet(self):
        self.w.save_settings({'business':{'name':'合成AI问题答疑','offer':'帮助个人处理合成AI工具的问题','keywords':['合成AI问题']}})
        with patch.object(self.w,'llm',return_value={'reply':'先确认你用的是哪个工具和版本，再看具体的报错位置。'}) as llm:
            app.sales.check(self.w,'writing_check')
        request=json.loads(llm.call_args.args[1])
        self.assertEqual(request['business']['name'],'合成AI问题答疑')
        self.assertIn('synthetic_question',request)
        self.assertNotIn('Excel',llm.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
