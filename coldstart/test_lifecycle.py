"""迁移、读写失败、长内容、重启和安装前检查的回归验收。"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from test_integration import RawHTTPClient
import app
import process_runner
import validation

QA=Path(__file__).resolve().parent.parent/'qa'
QA.mkdir(exist_ok=True)


class LifecycleTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory(dir=QA)
    def tearDown(self):self.temp.cleanup()

    def test_v01_upgrade_keeps_data_and_creates_consistent_backup(self):
        path=Path(self.temp.name)/'coldstart.sqlite3'
        connection=sqlite3.connect(path)
        connection.executescript((Path(__file__).parent/'tests/fixtures/legacy-schema.sql').read_text(encoding='utf-8'))
        connection.execute("INSERT INTO meta(key,value) VALUES('settings',?)",(json.dumps(app.DEFAULT,ensure_ascii=False),))
        connection.execute("INSERT INTO leads(fingerprint,platform,url,title,excerpt,author,published_at,captured_at,score,intent,evidence,reason,state,note) VALUES('old','xiaohongshu','https://www.xiaohongshu.com/explore/original','原始需求','原始正文','','',?,80,'buyer','原始正文','原始分析','won','保留真实跟进记录')",(app.now(),))
        connection.execute("INSERT INTO drafts(platform,title,body,tags,cards,state,generator,scheduled_at,created_at,updated_at) VALUES('xiaohongshu','旧稿标题','原始正文','[]','[]','approved','template',?,?,?)",(app.now(),app.now(),app.now()))
        connection.commit();connection.close()
        w=app.Workbench(self.temp.name)
        record=w.state()['leads'][0]
        self.assertEqual(record['state'],'won');self.assertEqual(record['note'],'保留真实跟进记录')
        self.assertEqual(w.state()['stats']['won'],1)
        self.assertEqual(w.draft(1)['state'],'draft')
        backups=list((Path(self.temp.name)/'backups').glob('*.sqlite3'))
        self.assertEqual(len(backups),1)
        backup=sqlite3.connect(backups[0])
        try:self.assertEqual(backup.execute('SELECT note FROM leads').fetchone()[0],'保留真实跟进记录')
        finally:backup.close()
        app.Workbench(self.temp.name)
        self.assertEqual(len(list((Path(self.temp.name)/'backups').glob('*.sqlite3'))),1)

    def test_http_get_failure_is_json_instead_of_connection_drop(self):
        w=app.Workbench(self.temp.name);client=RawHTTPClient(w)
        with patch.object(w,'state',side_effect=sqlite3.OperationalError('database is locked')):
            status,result=client.request('/api/state')
        self.assertEqual(status,503);self.assertIn('error',result)

    def test_http_rejects_cross_origin_and_wrong_content_type(self):
        w=app.Workbench(self.temp.name);client=RawHTTPClient(w)
        for headers in ({'Origin':'https://external.invalid'},{'Content-Type':'text/plain'},{'X-Workbench':'wrong'},{'Host':'external.invalid'}):
            status,_=client.request('/api/jobs',{'kind':'scan'},headers=headers)
            self.assertEqual(status,403)
        self.assertEqual(w.state()['jobs'],[])

    def test_bad_configuration_does_not_partially_replace_working_settings(self):
        w=app.Workbench(self.temp.name)
        before=w.config()
        with self.assertRaises(ValueError):w.save_settings({'business':{'name':'这个修改不能残留'},'ai':{'base_url':'http://remote.invalid/api'}})
        self.assertEqual(w.config(),before)

    def test_source_timestamps_are_not_capture_timestamps(self):
        w=app.Workbench(self.temp.name)
        w.ingest([{'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/a','title':'原帖','published_at':1609459200000}])
        record=w.state()['leads'][0]
        self.assertEqual(record['published_at'],'2021-01-01T08:00:00+08:00')
        self.assertNotEqual(record['published_at'],record['captured_at'])

    def test_empty_or_invalid_images_cannot_be_approved(self):
        w=app.Workbench(self.temp.name);w.save_settings({'publishing':{'channels':['xiaohongshu']}});w.generate(template=True)
        row=w.state()['drafts'][0]
        for assets in ([],[str(Path(self.temp.name)/'missing.png')]):
            with self.assertRaises(ValueError):w.edit_draft({'id':row['id'],'assets':assets,'state':'approved'})
        self.assertEqual(w.draft(row['id'])['state'],'draft')

    def test_http_export_does_not_contain_credentials(self):
        w=app.Workbench(self.temp.name);w.save_settings({'ai':{'api_key':'fixture-sensitive-key'}})
        status,value=RawHTTPClient(w).request('/api/export')
        self.assertEqual(status,200);self.assertNotIn(b'fixture-sensitive-key',value)

    def test_oversized_import_does_not_save_first_hundred_rows(self):
        w=app.Workbench(self.temp.name)
        records=[{'url':f'https://example.invalid/{i}','platform':'web','title':'公开内容'} for i in range(101)]
        with self.assertRaises(ValueError):w.ingest(records)
        self.assertEqual(w.state()['leads'],[])

    def test_process_timeout_stops_its_child_tree(self):
        marker=Path(self.temp.name)/'child-survived.txt'
        child=f"import time;from pathlib import Path;time.sleep(2);Path({str(marker)!r}).write_text('unexpected')"
        parent=f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(20)"
        with self.assertRaises(RuntimeError):process_runner.run([sys.executable,'-c',parent],timeout=.3)
        time.sleep(2.4)
        self.assertFalse(marker.exists(),'本次派生的子进程未被正确回收')


if __name__=='__main__':unittest.main(verbosity=2)
