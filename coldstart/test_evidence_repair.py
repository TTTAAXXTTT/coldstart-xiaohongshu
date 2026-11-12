"""Synthetic source evidence repairs; not live customer acquisition."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import app,sales

class EvidenceRepairTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'qa');self.w=app.Workbench(self.temp.name)
  self.w.ingest([{'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/fixture-buyer','title':'合成需求','excerpt':'找个人开发者，帮我把货盘导入飞书。预算还没确定。'}])
  self.row=self.w.state()['leads'][0]
  self.initial={'results':[{'id':self.row['id'],'intent':'buyer','score':90,'evidence':'用户需要开发','reason':'合成需求判断','reply':'先挑几份不同格式的货盘试一下。'}]}
 def tearDown(self):self.temp.cleanup()
 def test_exact_second_quote_retains_model_decision_with_real_evidence(self):
  fixed={'results':[{'id':self.row['id'],'evidence':'找个人开发者，帮我把货盘导入飞书。'}]}
  with patch.object(self.w,'llm',side_effect=[self.initial,fixed]) as model:self.w._qualify_batch()
  row=self.w.state()['leads'][0];self.assertEqual(row['intent'],'buyer');self.assertEqual(row['score'],90);self.assertIn(row['evidence'],self.row['excerpt']);self.assertEqual(model.call_count,2)
 def test_repair_cannot_invent_or_combine_evidence_or_change_intent(self):
  for value in ('虚构了十万元预算','找个人开发者，预算还没确定。'):
   with self.subTest(value=value),patch.object(self.w,'llm',return_value={'results':[{'id':self.row['id'],'evidence':value,'intent':'seller','score':100}]}):
    result=sales.repair_evidence(self.w,self.initial['results'],[self.row]);self.assertEqual(result[0]['evidence'],'');self.assertEqual(result[0]['score'],90);self.assertEqual(result[0]['intent'],'buyer')
 def test_valid_original_quote_needs_no_additional_request(self):
  self.initial['results'][0]['evidence']='预算还没确定。'
  with patch.object(self.w,'llm') as model:result=sales.repair_evidence(self.w,self.initial['results'],[self.row])
  model.assert_not_called();self.assertEqual(result,self.initial['results'])
 def test_failed_repair_stays_unconfirmed_and_explains_missing_evidence(self):
  with patch.object(self.w,'llm',side_effect=[self.initial,RuntimeError('合成网络异常')]):self.w._qualify_batch()
  row=self.w.state()['leads'][0];self.assertEqual(row['intent'],'unconfirmed');self.assertEqual(row['score'],30);self.assertIn('未给出可核对的原句',row['reason'])
 def test_comment_cannot_use_post_title_as_its_evidence(self):
  row={**self.row,'record_type':'comment','title':'求购自动化服务','excerpt':'谢谢分享'}
  with patch.object(self.w,'llm',return_value={'results':[{'id':row['id'],'evidence':row['title']}]}) as model:result=sales.repair_evidence(self.w,self.initial['results'],[row])
  self.assertNotIn(row['title'],model.call_args.args[1]);self.assertEqual(result[0]['evidence'],'')

if __name__=='__main__':unittest.main()
