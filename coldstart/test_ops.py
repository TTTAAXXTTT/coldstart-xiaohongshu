"""总包运行控制、备份与宿主进程的真实本地测试，不连接平台。"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import app
import ops
import validation

ROOT=Path(__file__).resolve().parent
QA=ROOT.parent/'qa';QA.mkdir(exist_ok=True)


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=QA)
        self.data=Path(self.temp.name)/'data'
        self.w=app.Workbench(self.data)
    def tearDown(self):self.temp.cleanup()

    def test_configuration_sets_paths_without_publishing_or_echoing_key(self):
        result=ops.configure(self.data,['xiaohongshu'],key='fixture-credential')
        self.assertTrue(result['model_configured']);self.assertFalse(result['publishing_enabled']);self.assertFalse(result['autonomy_enabled'])
        self.assertNotIn('fixture-credential',json.dumps(result))
        self.assertEqual(self.w.config()['publishing']['channels'],['xiaohongshu'])

    def test_local_auth_is_generated_once_and_never_returned(self):
        first=ops.prepare_local_auth(self.data);secret=self.w.config()['radar']['xhs_token']
        self.assertTrue(first['generated']);self.assertGreaterEqual(len(secret),40)
        self.assertNotIn(secret,json.dumps(first));self.assertNotIn(secret,json.dumps(ops.status(self.data)))
        self.assertFalse(ops.prepare_local_auth(self.data)['generated']);self.assertEqual(self.w.config()['radar']['xhs_token'],secret)

    def test_local_auth_does_not_change_remote_credentials_or_running_data(self):
        self.w.save_settings({'radar':{'xhs_url':'https://fixture.invalid','xhs_token':'fixture-remote-secret'}})
        self.assertFalse(ops.prepare_local_auth(self.data)['managed']);self.assertEqual(self.w.config()['radar']['xhs_token'],'fixture-remote-secret')
        with validation.InstanceLock(self.data):
            with self.assertRaises(ValueError):ops.prepare_local_auth(self.data)

    def test_owned_service_receives_private_auth_only_in_environment(self):
        import managed_host
        with self.assertRaises(ValueError):managed_host.related_environment(['backend','--data-dir',str(self.data)])
        ops.prepare_local_auth(self.data)
        environment=managed_host.related_environment(['backend','--data-dir',str(self.data)])
        self.assertEqual(environment['AUTH_TOKEN'],self.w.config()['radar']['xhs_token'])

    def test_configuration_rejects_unsupported_channels_without_changes(self):
        before=self.w.config()
        with self.assertRaises(ValueError):ops.configure(self.data,['unsupported'],key='fixture-key')
        self.assertEqual(self.w.config(),before)

    def test_reconfiguration_preserves_existing_credentials(self):
        ops.configure(self.data,['xiaohongshu'],key='fixture-original')
        ops.configure(self.data,['xiaohongshu'],key='')
        self.assertEqual(self.w.config()['ai']['api_key'],'fixture-original')

    def test_launcher_manages_only_default_local_xhs_endpoint(self):
        self.assertTrue(ops.status(self.data)['manage_local_xhs'])
        for url in ('http://127.0.0.1:19001','https://fixture.invalid:18060','http://127.0.0.1:18060/proxy'):
            with self.subTest(url=url):
                self.w.save_settings({'radar':{'xhs_url':url}})
                result=ops.status(self.data);self.assertFalse(result['manage_local_xhs']);self.assertNotIn(url,json.dumps(result))

    def test_configuration_cannot_reinitialize_running_instance(self):
        with self.w.db() as db:db.execute("INSERT INTO jobs(kind,payload,state,created_at,updated_at) VALUES('scan','{}','running',?,?)",(app.now(),app.now()))
        with validation.InstanceLock(self.data):
            with self.assertRaises(ValueError):ops.configure(self.data,key='fixture-key')
        self.assertEqual(self.w.state()['jobs'][0]['state'],'running')

    def test_enabling_requires_credentials_and_then_sets_authorized_flags(self):
        with self.assertRaises(ValueError):ops.enable(self.data)
        ops.configure(self.data,['xiaohongshu'],key='fixture-key')
        ops.enable(self.data)
        cfg=self.w.config();self.assertTrue(cfg['autonomy']['enabled']);self.assertFalse(cfg['publishing']['auto_approve']);self.assertFalse(cfg['publishing']['enabled']);self.assertFalse(cfg['schedule']['enabled'])

    def test_diagnostic_excludes_secrets_customer_text_and_event_messages(self):
        ops.configure(self.data,['xiaohongshu'],key='fixture-private-key')
        self.w.ingest([{'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/fixture','title':'private-customer-text'}])
        self.w.log('info','private-event-text')
        result=json.dumps(ops.status(self.data))
        for value in ['fixture-private-key','private-customer-text','private-event-text']:self.assertNotIn(value,result)
        self.assertIn('NOT_TESTED',result)

    def test_control_does_not_recover_or_mutate_running_job_state(self):
        with self.w.db() as db:db.execute("INSERT INTO jobs(kind,payload,state,created_at,updated_at) VALUES('scan','{}','running',?,?)",(app.now(),app.now()))
        ops.control(self.data,'pause');self.assertTrue(ops.status(self.data)['paused'])
        ops.control(self.data,'resume');self.assertFalse(ops.status(self.data)['paused'])
        self.assertEqual(self.w.state()['jobs'][0]['state'],'running')

    def test_reset_cannot_change_controls_of_running_instance(self):
        ops.control(self.data,'pause')
        with validation.InstanceLock(self.data):
            with self.assertRaises(ValueError):ops.control(self.data,'reset')
        self.assertTrue(ops.status(self.data)['paused'])

    def test_pause_prevents_new_work_until_resumed(self):
        ops.control(self.data,'pause');called=threading.Event()
        with patch.object(self.w,'tick',side_effect=called.set),patch.object(self.w,'run_one_job',return_value=False):
            thread=threading.Thread(target=self.w.worker);thread.start()
            try:
                self.assertFalse(called.wait(.15))
                ops.control(self.data,'resume');self.assertTrue(called.wait(2))
            finally:self.w.stop.set();thread.join(timeout=3)

    def test_shutdown_waits_for_inflight_job_and_does_not_start_next(self):
        started=threading.Event();release=threading.Event();calls=[]
        def active():started.set();release.wait(3);calls.append('finished');return True
        with patch.object(self.w,'tick'),patch.object(self.w,'run_one_job',side_effect=active):
            thread=threading.Thread(target=self.w.worker);thread.start()
            try:
                self.assertTrue(started.wait(1));ops.control(self.data,'shutdown');self.assertTrue(thread.is_alive())
                release.set();thread.join(timeout=3);self.assertFalse(thread.is_alive());self.assertEqual(calls,['finished'])
            finally:release.set();self.w.stop.set();thread.join(timeout=3)

    def test_model_check_queue_is_idempotent_without_initializing_workbench(self):
        self.assertEqual(ops.enqueue_check(self.data)['job_id'],ops.enqueue_check(self.data)['job_id'])
        self.assertEqual(len(self.w.state()['jobs']),1)

    def test_backup_is_readable_consistent_and_contains_private_configuration(self):
        ops.configure(self.data,['xiaohongshu'],key='fixture-backup-secret')
        asset=self.data/'assets/fixture.txt';asset.write_text('fixture asset',encoding='utf-8')
        result=ops.backup(self.data,Path(self.temp.name)/'backups')
        with zipfile.ZipFile(result['backup']) as z:
            self.assertIsNone(z.testzip());self.assertEqual(z.read('data/assets/fixture.txt'),b'fixture asset')
            dbfile=Path(self.temp.name)/'restored.sqlite3';dbfile.write_bytes(z.read('data/coldstart.sqlite3'))
            self.assertNotIn('data/run.lock',z.namelist())
        with sqlite3.connect(dbfile) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            cfg=json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]);self.assertEqual(cfg['ai']['api_key'],'fixture-backup-secret')
        db.close()
        self.assertTrue(result['private'])

    def test_backup_refuses_active_instance_and_recursive_target(self):
        with validation.InstanceLock(self.data):
            with self.assertRaises(ValueError):ops.backup(self.data,Path(self.temp.name)/'backups')
        with self.assertRaises(ValueError):ops.backup(self.data,self.data/'nested')

    def test_real_server_exits_on_local_shutdown_and_releases_its_data_lock(self):
        with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        process=subprocess.Popen([sys.executable,'-u',str(ROOT/'app.py'),'--no-browser','--port',str(port),'--data-dir',str(self.data)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')
        try:
            deadline=time.monotonic()+8
            # 初始化完成后取得文件锁且进程存活即可请求退出；不做绕过限制的网络连接。
            while time.monotonic()<deadline:
                try:
                    with validation.InstanceLock(self.data):pass
                except ValueError:break
                if process.poll() is not None:self.fail(process.stdout.read())
                time.sleep(.05)
            else:self.fail('Server did not acquire instance lock')
            ops.control(self.data,'shutdown');self.assertEqual(process.wait(timeout=8),0)
            with validation.InstanceLock(self.data):pass
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=3)
            process.stdout.close()

    @unittest.skipUnless(os.name=='nt','Windows Job lifetime test')
    def test_managed_host_termination_stops_owned_descendants(self):
        folder=Path(self.temp.name);marker=folder/'should-not-exist';ready=folder/'ready'
        script=folder/'spawn.py'
        script.write_text('import subprocess,sys,time\nfrom pathlib import Path\nsubprocess.Popen([sys.executable,"-c",'+repr('import time;from pathlib import Path;time.sleep(1);Path('+repr(str(marker))+').write_text("survived")')+'])\nPath('+repr(str(ready))+').write_text("ready")\ntime.sleep(20)\n',encoding='utf-8')
        process=subprocess.Popen([sys.executable,str(ROOT/'managed_host.py'),'--cwd',str(folder),'--log',str(folder/'host.log'),'--',sys.executable,str(script)])
        try:
            deadline=time.monotonic()+5
            while not ready.exists() and time.monotonic()<deadline:time.sleep(.05)
            self.assertTrue(ready.exists());process.terminate();process.wait(timeout=5);time.sleep(1.2)
            self.assertFalse(marker.exists(),'Owned descendant survived host termination')
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=3)


if __name__=='__main__':unittest.main()
