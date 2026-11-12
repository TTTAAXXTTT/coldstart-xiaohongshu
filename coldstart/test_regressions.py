"""审计中发现的问题：先在原版复现，再作为修复后的回归验收。"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app

QA = Path(__file__).resolve().parent.parent / 'qa'
QA.mkdir(exist_ok=True)


class AuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=QA)
        self.w = app.Workbench(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def lead(self, **kwargs):
        record = {'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/fixture1?xsec_token=old','title':'求推荐自动化工具','excerpt':''}
        record.update(kwargs)
        return record

    def draft(self):
        self.w.save_settings({'publishing':{'channels':['xiaohongshu']}})
        self.w.generate(template=True)
        return self.w.state()['drafts'][0]

    def test_later_scan_enriches_original_record_and_refreshes_url(self):
        self.w.ingest([self.lead()])
        with self.w.db() as db:
            db.execute("UPDATE leads SET state='contacted',note='真实跟进记录' WHERE id=1")
        self.w.ingest([self.lead(url='https://www.xiaohongshu.com/explore/fixture1?xsec_token=fresh',excerpt='补充完整正文，想找人协助实现。')])
        record = self.w.state()['leads'][0]
        self.assertIn('完整正文',record['excerpt'])
        self.assertIn('fresh',record['url'])
        self.assertEqual(record['state'],'contacted')
        self.assertEqual(record['note'],'真实跟进记录')

    def test_invalid_import_is_atomic(self):
        with self.assertRaises(ValueError):
            self.w.ingest([self.lead(),self.lead(url='javascript:invalid')])
        self.assertEqual(len(self.w.state()['leads']),0)

    def test_comment_intent_comes_from_comment_not_parent_title(self):
        self.w.ingest([self.lead(external_id='comment-1',record_type='comment',excerpt='收藏了，谢谢分享')])
        record=self.w.state()['leads'][0]
        self.assertNotEqual(record['intent'],'buyer')
        self.assertEqual(record['evidence'],'')

    def test_editing_text_invalidates_old_generated_cards(self):
        draft=self.draft()
        self.w.edit_draft({'id':draft['id'],'body':'这是完全修改后的新正文。','state':'draft'})
        updated=self.w.draft(draft['id'])
        self.assertEqual(json.loads(updated['assets']),[])
        with self.assertRaises(ValueError):
            self.w.edit_draft({'id':draft['id'],'state':'approved'})

    def test_approval_rejects_non_image_assets(self):
        draft=self.draft()
        path=Path(self.temp.name)/'bad.png'
        path.write_text('not an image',encoding='utf-8')
        with self.assertRaises(ValueError):
            self.w.edit_draft({'id':draft['id'],'assets':[str(path)],'state':'approved'})

    def test_approved_asset_cannot_change_before_publish(self):
        draft=self.draft()
        self.w.edit_draft({'id':draft['id'],'state':'approved'})
        path=Path(json.loads(draft['assets'])[0])
        from PIL import Image
        Image.new('RGB',(100,100),'red').save(path)
        self.w.save_settings({'publishing':{'enabled':True}})
        with patch.object(self.w,'xhs',return_value={'is_logged_in':True}):
            with self.assertRaises(ValueError):
                self.w.preflight(self.w.draft(draft['id']))

    def test_generation_rejects_overlong_content_instead_of_truncation(self):
        self.w.save_settings({'publishing':{'channels':['xiaohongshu']}})
        value={'variants':[{'platform':'xiaohongshu','title':'标题超过二十个汉字'*4,'body':'原文完整内容','tags':[],'cards':[]}]}
        with patch.object(self.w,'llm',return_value=value):
            with self.assertRaises(ValueError):
                self.w.generate('明确选题')
        self.assertEqual(len(self.w.state()['drafts']),0)

    def test_base_url_is_normalized_before_storing(self):
        self.w.save_settings({'ai':{'base_url':'  https://model.example/v1/  '}})
        self.assertEqual(self.w.config()['ai']['base_url'],'https://model.example/v1')

    def test_clear_secret_is_supported(self):
        self.w.save_settings({'ai':{'api_key':'fixture-secret'}})
        self.w.save_settings({'ai':{'api_key':None}})
        self.assertEqual(self.w.config()['ai']['api_key'],'')


if __name__=='__main__':
    unittest.main(verbosity=2)
