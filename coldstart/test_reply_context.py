"""Synthetic regression from a real discovered contradiction; never sends comments."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import app,sales

class ReplyContextTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'qa');self.w=app.Workbench(self.temp.name)
  self.w.ingest([{'url':'https://www.xiaohongshu.com/explore/fixture-context','title':'合成公式问题','excerpt':'格式是常规，双击完还是不行。回车后仍然显示公式。'}]);self.ident=self.w.state()['leads'][0]['id']
 def tearDown(self):self.temp.cleanup()
 def test_failed_steps_are_reviewed_before_saving_reply(self):
  with patch.object(self.w,'llm',side_effect=[{'reply':'先检查文本格式，改为常规。'},{'reply':'是整张表都显示公式，还是只有这一个单元格？'}]) as model:sales.draft_reply(self.w,self.ident)
  self.assertEqual(model.call_count,2);self.assertEqual(self.w.state()['leads'][0]['reply'],'是整张表都显示公式，还是只有这一个单元格？')
 def test_remaining_known_fact_conflict_never_replaces_saved_reply(self):
  with self.w.db() as db:db.execute('UPDATE leads SET reply=? WHERE id=?',('已人工核对的旧草稿',self.ident))
  with patch.object(self.w,'llm',return_value={'reply':'先检查文本格式，改为常规。'}):
   with self.assertRaisesRegex(ValueError,'格式冲突'):sales.draft_reply(self.w,self.ident)
  self.assertEqual(self.w.state()['leads'][0]['reply'],'已人工核对的旧草稿')
 def test_reviewed_reply_cannot_repeat_failed_double_click(self):
  with patch.object(self.w,'llm',return_value={'reply':'可以再双击单元格后按回车试试。'}):
   with self.assertRaisesRegex(ValueError,'重复了对方已尝试无效'):sales.draft_reply(self.w,self.ident)
  self.assertEqual(self.w.state()['leads'][0]['reply'],'')

if __name__=='__main__':unittest.main()
