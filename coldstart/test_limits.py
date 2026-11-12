import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import app,limits,model_api,sales

class EditableLimitsTests(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'qa');self.w=app.Workbench(self.temp.name)
 def tearDown(self):self.temp.cleanup()
 def test_every_business_limit_can_be_unlimited_then_restored(self):
  self.w.save_settings(limits.all_unlimited())
  for section,key,_,kind,_ in limits.FIELDS:self.assertEqual(self.w.config()[section][key],0 if kind=='interval' else -1)
  self.w.save_settings({'ai':{'calls_per_day':500},'radar':{'max_results':600},'publishing':{'min_gap_minutes':7,'max_posts_per_day':200}})
  self.assertEqual(self.w.config()['ai']['calls_per_day'],500);self.assertEqual(self.w.config()['publishing']['min_gap_minutes'],7)
 def test_unlimited_usage_continues_after_old_quota_and_finite_quota_still_stops(self):
  for _ in range(22):self.w.reserve_usage('llm',-1)
  self.assertEqual(self.w.state()['stats']['calls'],22)
  with self.assertRaises(ValueError):self.w.reserve_usage('llm',22)
  self.w.reserve_usage('llm',23)
 def test_invalid_limits_never_partially_save(self):
  original=self.w.config()
  for value in (-2,1.5,True,'2.5'):
   with self.assertRaises(ValueError):self.w.save_settings({'ai':{'calls_per_day':value}})
   self.assertEqual(self.w.config(),original)
  with self.assertRaises(ValueError):self.w.save_settings({'publishing':{'min_gap_minutes':-1}})
 def test_unlimited_output_omits_both_provider_token_fields(self):
  for base in ('https://api.openai.com/v1','https://gateway.example/v1'):
   body=model_api.request_body(base,'fixture','JSON','check',-1)
   self.assertNotIn('max_tokens',body);self.assertNotIn('max_completion_tokens',body)
 def test_zero_output_disables_ordinary_model_request_before_charging(self):
  self.w.save_settings({'ai':{'max_output_tokens':0}})
  with patch.object(app,'http_json') as http:
   with self.assertRaises(ValueError):self.w.llm('JSON','check')
   http.assert_not_called()
  self.assertEqual(self.w.state()['stats']['calls'],0)
 def test_unlimited_search_keeps_last_post_and_comments_beyond_twenty(self):
  self.w.save_settings(limits.all_unlimited());self.w.save_settings({'radar':{'xhs_enabled':True},'business':{'keywords':['Excel 求助']}})
  def remote(path,payload=None):
   if 'login' in path:return {'is_logged_in':True}
   if 'search?' in path:return {'feeds':[{'id':str(i),'xsecToken':'fixture','noteCard':{'displayTitle':'求助Excel'}} for i in range(11)]}
   self.assertTrue(payload['load_all_comments']);self.assertGreater(payload['comment_config']['max_comment_items'],20)
   return {'data':{'note':{'desc':'求助处理表格'},'comments':{'list':[{'id':'comment-'+payload['feed_id']+'-'+str(i),'content':'请问怎么做？'} for i in range(21)]}}}
  with patch.object(self.w,'xhs',side_effect=remote):self.w.scan()
  report=json.loads(self.w.meta('scan_report'));self.assertEqual(report['returned'],11);self.assertEqual(report['details_ok'],11);self.assertEqual(report['comments'],231)
 def test_unlimited_analysis_processes_all_existing_rows_in_chunks(self):
  self.w.save_settings({'ai':{'analysis_per_run':-1}})
  self.w.ingest([{'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/n'+str(i),'title':'求助Excel','excerpt':'每天合并表格'} for i in range(11)])
  def model(system,content,**kwargs):
   rows=json.loads(content)['candidates']
   return {'results':[{'id':r['id'],'intent':'buyer','score':70,'evidence':'每天合并表格','reason':'合成回归输入','reply':'表头一致吗？'} for r in rows]}
  with patch.object(self.w,'llm',side_effect=model) as llm:self.assertIn('完成 11 条',self.w.qualify());self.assertEqual(llm.call_count,2)
 def test_unlimited_display_returns_more_than_old_150_cap(self):
  records=[{'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/n'+str(i),'title':'求助Excel'} for i in range(155)]
  for offset in range(0,155,50):self.w.ingest(records[offset:offset+50])
  self.assertEqual(len(self.w.state()['leads']),150)
  self.w.save_settings({'display':{'leads':-1}});self.assertEqual(len(self.w.state()['leads']),155)
 def test_zero_publish_gap_and_unlimited_daily_count_do_not_defer(self):
  self.w.meta('xhs_bound_user','fixture-owner');self.w.generate(template=True);self.w.edit_draft({'id':1,'state':'approved'})
  self.w.save_settings({'publishing':{'enabled':True,'max_posts_per_day':-1,'min_gap_minutes':0}})
  with self.w.db() as db:db.execute("INSERT INTO attempts(draft_id,platform,started_at,state) VALUES(99,'xiaohongshu',?,'confirmed_published')",(app.now(),))
  self.w.preflight(self.w.draft(1),check_login=False)
 def test_remaining_and_combined_unlimited_reply_limits(self):
  self.assertEqual(limits.combine(-1,limits.remaining(-1,999)), -1)
  self.assertEqual(limits.combine(-1,limits.remaining(5,3)),2)
  self.assertEqual(limits.combine(2,limits.remaining(-1,100)),2)
 def test_csv_endpoint_obeys_unlimited_and_restored_finite_limits(self):
  from test_integration import RawHTTPClient
  client=RawHTTPClient(self.w)
  self.w.save_settings({'radar':{'import_per_request':-1}})
  csv='platform,url,title\n'+''.join(f'xiaohongshu,https://www.xiaohongshu.com/explore/csv{i},Excel求助\n' for i in range(121))
  self.assertEqual(client.post('/api/import',{'csv_text':csv})['inserted'],121)
  self.w.save_settings({'radar':{'import_per_request':2}})
  self.assertNotEqual(client.request('/api/import',{'csv_text':csv})[0],200)
  self.assertEqual(self.w.config()['radar']['import_per_request'],2)

if __name__=='__main__':unittest.main()
