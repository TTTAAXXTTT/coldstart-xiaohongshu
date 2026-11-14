import json,tempfile,unittest,io,urllib.error
from unittest.mock import patch
import app,public_engagement as pub,autopilot,sales

class MonitorFairnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.w=app.Workbench(self.tmp.name)
        self.w.save_settings({'public':{'enabled':True,'own_comments_enabled':False},'business':{'offer':'合成答疑'}})
        self.w.meta('xhs_bound_user','synthetic-owner')
        with self.w.db() as db:
            for feed in ('one','two'):
                db.execute("INSERT INTO public_threads(account,feed,token,target,title,context,created_at) VALUES('synthetic-owner',?,'fixture','','合成帖子','合成原文',?)",(feed,app.now()))

    def test_regular_reads_are_short_but_submitted_readback_keeps_full_budget(self):
        report={'comment_readback':{'feed_id':'one','owner_id':'visitor','comments':[]}}
        source={'account':'synthetic-owner','feed':'one','token':'fixture'}
        with patch.object(pub.xhs_session,'invoke',return_value=report) as invoke:
            pub.read(self.w,source)
            self.assertEqual(invoke.call_args.kwargs['read_timeout'],45)
            pub.read(self.w,{**source,'_submitted_readback':True})
            self.assertEqual(invoke.call_args.kwargs['read_timeout'],180)

    def test_manual_check_runs_before_older_scheduled_monitor(self):
        self.w.enqueue('public_monitor',origin='scheduler')
        manual=self.w.enqueue('model_check')
        self.assertEqual(self.w.state()['workflow']['queue'][0]['id'],manual)
        with patch.object(app.sales,'check',return_value='合成检查完成'),patch.object(pub,'cycle') as monitor:
            self.w.run_one_job()
        monitor.assert_not_called()
        with self.w.db() as db:self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=?',(manual,)).fetchone()[0],'done')

    def test_pending_manual_request_yields_before_any_platform_read(self):
        self.w.enqueue('model_check')
        with patch.object(self.w,'xhs') as xhs,patch.object(pub,'process_thread') as process:
            self.assertIn('让出',pub.cycle(self.w))
        xhs.assert_not_called();process.assert_not_called()

    def test_manual_request_arriving_during_failed_read_yields_before_second_post(self):
        def broken(*args):
            self.w.enqueue('model_check')
            raise TimeoutError('合成原帖不可读取')
        with patch.object(autopilot,'current_xhs_account',return_value='synthetic-owner'),patch.object(pub,'process_thread',side_effect=broken) as process,patch.object(pub,'discover'):
            pub.cycle(self.w)
        self.assertEqual(process.call_count,1)
        self.assertEqual(json.loads(self.w.meta('public_report'))['checked'],1)

    def test_round_time_budget_yields_without_marking_unread_posts_checked(self):
        with patch.object(autopilot,'current_xhs_account',return_value='synthetic-owner'),patch.object(pub.time,'monotonic',side_effect=[0,1,61]),patch.object(pub,'process_thread') as process,patch.object(pub,'discover'):
            pub.cycle(self.w)
        self.assertEqual(process.call_count,1)
        with self.w.db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM public_threads WHERE checked_at='' ").fetchone()[0],1)

    def test_broken_owned_post_does_not_starve_remaining_owned_posts(self):
        self.w.save_settings({'public':{'own_comments_enabled':True}})
        with patch.object(self.w,'xhs',return_value={'data':{'feeds':[{'id':'one','xsecToken':'fixture'},{'id':'two','xsecToken':'fixture'}]}}),patch.object(pub,'read',side_effect=[TimeoutError('合成错误'),{'comments':[]}]):
            self.assertEqual(pub.discover_owned(self.w,'synthetic-owner'),2)
        coverage=json.loads(self.w.meta('public_owned_coverage'))
        self.assertEqual(len(coverage['errors']),1)
        self.assertEqual(self.w.meta('public_owned_cursor'),'2')

    def test_provider_overdue_error_is_specific_and_does_not_echo_body(self):
        error=urllib.error.HTTPError('https://provider.invalid/v1',403,'Forbidden',{},io.BytesIO(b'{"error":{"code":"AccountOverdueError","message":"fixture-secret-never-echo"}}'))
        with patch.object(app.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(RuntimeError) as caught:app.http_json('https://provider.invalid/v1',{'model':'synthetic'})
        self.assertIn('欠费',str(caught.exception));self.assertNotIn('fixture-secret-never-echo',str(caught.exception))
        self.assertIn('处理欠费',sales.advice(str(caught.exception)))

    def test_read_budget_cannot_change_publish_timeout(self):
        import xhs_session
        # Use the existing pinned bundle, but validation must reject before launching.
        with patch.object(xhs_session.process_runner,'run') as run:
            with self.assertRaises(ValueError):xhs_session.invoke(mode='publish',read_timeout=45)
        run.assert_not_called()
