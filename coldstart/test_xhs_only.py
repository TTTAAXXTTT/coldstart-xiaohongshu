"""Single-platform migration and boundary regressions; all inputs are fixtures."""
import json
from pathlib import Path
import tempfile
import unittest
import app
import ops
import validation

class XiaohongshuOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'qa')
        self.w=app.Workbench(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_only_retained_platform_is_accepted_for_settings_and_content(self):
        self.assertEqual(set(app.PLATFORMS),{'xiaohongshu'})
        self.assertEqual(validation.PUBLISH_PLATFORMS,{'xiaohongshu'})
        before=self.w.config()
        for platform in ('douyin','kuaishou','zhihu','bilibili','tencent','web'):
            with self.subTest(platform=platform):
                with self.assertRaises(ValueError):self.w.save_settings({'publishing':{'channels':[platform]}})
                with self.assertRaises(ValueError):validation.content(platform,'标题','正文',[])
                with self.assertRaises(ValueError):ops.configure(self.temp.name,[platform])
        self.assertEqual(self.w.config(),before)

    def test_legacy_settings_migrate_without_changing_credentials_binding_or_approval(self):
        self.w.save_settings({'ai':{'api_key':'fixture-preserved-key'},'publishing':{'min_gap_minutes':30}})
        self.w.meta('xhs_bound_user','fixture-owner')
        self.w.generate('完整保留的原稿',template=True)
        self.w.edit_draft({'id':1,'state':'approved'})
        original=self.w.draft(1)
        raw=self.w.config()
        raw['radar'].update(tavily_enabled=True,tavily_key='fixture-retired-search',platforms=['xiaohongshu','zhihu'])
        raw['publishing'].update(sau_root='fixture-retired-path',sau_python='fixture-retired-python',accounts={'douyin':'creator'},channels=['xiaohongshu','douyin'])
        with self.w.db() as db:db.execute("UPDATE meta SET value=? WHERE key='settings'",(app.dump(raw),))
        upgraded=app.Workbench(self.temp.name)
        config=upgraded.config()
        self.assertEqual(config['ai']['api_key'],'fixture-preserved-key')
        self.assertEqual(upgraded.meta('xhs_bound_user'),'fixture-owner')
        self.assertEqual(config['radar']['platforms'],['xiaohongshu'])
        self.assertEqual(config['publishing']['channels'],['xiaohongshu'])
        for key in ('sau_root','sau_python','accounts'):self.assertNotIn(key,config['publishing'])
        for key in ('tavily_enabled','tavily_key'):self.assertNotIn(key,config['radar'])
        after=upgraded.draft(1)
        for key in ('state','title','body','assets','approved_digest','target_binding'):self.assertEqual(after[key],original[key])
        self.assertTrue(all(Path(p).is_file() for p in json.loads(after['assets'])))
        self.assertTrue(upgraded.meta('xhs_only_migrated_at'))

    def test_retired_settings_cannot_be_saved_again(self):
        for section,key in (('publishing','sau_root'),('publishing','accounts'),('radar','tavily_enabled')):
            with self.subTest(key=key),self.assertRaises(ValueError):self.w.save_settings({section:{key:'retired'}})

    def test_import_rejects_other_sources_without_partial_rows(self):
        valid={'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/fixture','title':'真实来源夹具'}
        for invalid in ({**valid,'platform':'douyin'},{**valid,'url':'https://example.invalid/a'}):
            with self.assertRaises(ValueError):self.w.ingest([valid,invalid])
            self.assertEqual(self.w.state()['leads'],[])
        self.assertEqual(self.w.ingest([valid]),1)

    def test_scheduled_scan_uses_remaining_sources(self):
        self.w.save_settings({'schedule':{'enabled':True},'radar':{'xhs_enabled':True}})
        self.w.tick()
        self.assertTrue(any(j['kind']=='scan' for j in self.w.state()['jobs']))

    def test_legacy_other_platform_records_do_not_enter_views_stats_or_schedule(self):
        with self.w.db() as db:
            db.execute("INSERT INTO leads(fingerprint,platform,url,title,excerpt,captured_at,score,intent,evidence,reason,state) VALUES('retired','retired','https://example.invalid/a','旧记录','',?,0,'unconfirmed','','','won')",(app.now(),))
            db.execute("INSERT INTO drafts(platform,title,body,tags,cards,state,generator,scheduled_at,created_at,updated_at) VALUES('retired','旧稿','旧正文','[]','[]','approved','ai',?,?,?)",(app.now(),app.now(),app.now()))
        self.w.save_settings({'publishing':{'enabled':True}})
        self.w.tick()
        state=self.w.state()
        self.assertEqual(state['leads'],[])
        self.assertEqual(state['drafts'],[])
        self.assertEqual(state['stats']['won'],0)
        self.assertEqual(state['stats']['pending'],0)
        self.assertFalse(any(j['kind']=='publish' for j in state['jobs']))

if __name__=='__main__':unittest.main()
