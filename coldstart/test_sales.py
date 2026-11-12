"""Business regressions: truthfully distinguish empty, duplicate, partial and failed runs."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import app,sales,autopilot

class SalesTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'qa');self.w=app.Workbench(self.temp.name)
  self.w.save_settings({'radar':{'xhs_enabled':True,'ark_search_enabled':False,'max_queries':1,'max_results':5,'max_details':2},'business':{'keywords':['Excel 求助']}})
 def tearDown(self):self.temp.cleanup()
 def remote(self,path,payload=None):
  if 'login/status' in path:return {'is_logged_in':True,'user_id':'fixture-owner'}
  if 'feeds/search' in path:return {'feeds':[{'id':f'n{i}','xsecToken':'fixture-token','noteCard':{'displayTitle':'求助Excel匹配','user':{'nickname':'合成作者'}}} for i in (1,2)]}
  return {'data':{'note':{'desc':'每天整理十张表，有没有办法自动合并？'},'comments':{'list':[]}}}
 def report(self):return json.loads(self.w.meta('scan_report'))
 def test_expired_login_is_actionable_and_consumes_no_search_or_model(self):
  self.w.save_settings({'radar':{'ark_search_enabled':True}})
  with patch.object(self.w,'xhs',return_value={'is_logged_in':False}),patch.object(autopilot,'ark_search') as ark:
   with self.assertRaises(autopilot.SetupRequired):self.w.scan()
   ark.assert_not_called()
  self.assertEqual(self.report()['status'],'needs_login');self.assertIn('重新登录',self.report()['next_action'])
  self.assertEqual(self.w.state()['stats']['searches'],0)
 def test_duplicates_are_counted_as_existing_instead_of_empty(self):
  with patch.object(self.w,'xhs',side_effect=self.remote):self.w.scan();self.w.scan()
  r=self.report();self.assertEqual(r['new'],0);self.assertEqual(r['returned'],2);self.assertEqual(r['duplicate'],2);self.assertEqual(r['status'],'done')
 def test_authoritative_detail_replaces_stale_or_cleared_card_title(self):
  for title in ('已经解决，不再需要',''):
   def remote(path,payload=None):
    value=self.remote(path,payload)
    if payload and payload.get('feed_id'):value['data']['note'].update(title=title,desc='问题已解决。')
    return value
   with self.subTest(title=title),patch.object(self.w,'xhs',side_effect=remote):self.w.scan()
   self.assertTrue(all(r['title']==title for r in self.w.state()['leads']))
 def test_one_bad_detail_retains_its_title_and_other_readable_posts(self):
  def remote(path,payload=None):
   if payload and payload.get('feed_id')=='n1':raise RuntimeError('原文读取超时')
   return self.remote(path,payload)
  with patch.object(self.w,'xhs',side_effect=remote):self.w.scan()
  r=self.report();self.assertEqual(r['status'],'partial');self.assertEqual(r['new'],2);self.assertEqual(r['details_failed'],1);self.assertEqual(r['details_ok'],1)
  self.assertEqual(len(self.w.state()['leads']),2)
 def test_partial_search_job_is_needs_review(self):
  def remote(path,payload=None):
   if payload and payload.get('feed_id'):raise RuntimeError('原文读取超时')
   return self.remote(path,payload)
  self.w.enqueue('scan')
  with patch.object(self.w,'xhs',side_effect=remote):self.w.run_one_job()
  self.assertEqual(self.w.state()['jobs'][0]['state'],'needs_review')
 def test_empty_search_explains_keywords_not_market_absence(self):
  with patch.object(self.w,'xhs',side_effect=lambda path,payload=None: {'is_logged_in':True} if 'login' in path else {'feeds':[]}):self.w.scan()
  self.assertEqual(self.report()['status'],'empty');self.assertIn('短词',self.report()['next_action'])
 def test_ark_without_citations_does_not_claim_empty_xhs_search(self):
  self.w.save_settings({'radar':{'xhs_enabled':False,'ark_search_enabled':True}})
  self.w.enqueue('scan')
  with patch.object(autopilot,'ark_search',return_value=0),patch.object(self.w,'xhs') as remote:self.w.run_one_job()
  remote.assert_not_called();self.assertEqual(self.report()['source'],'ark_citations');self.assertEqual(self.report()['status'],'no_verified_sources');self.assertIn('未执行小红书站内搜索',self.w.state()['jobs'][0]['message']);self.assertEqual(self.w.state()['jobs'][0]['state'],'needs_review')
 def test_search_check_never_uses_disabled_xhs_source(self):
  self.w.save_settings({'radar':{'xhs_enabled':False,'ark_search_enabled':True}})
  with patch.object(self.w,'xhs') as remote:
   with self.assertRaisesRegex(autopilot.SetupRequired,'站内搜索未启用'):sales.check(self.w,'search_check')
  remote.assert_not_called();self.assertEqual(self.w.state()['stats']['searches'],0)
 def test_search_check_stamp_tracks_actual_source_and_queries(self):
  stamp=sales.check_stamp(self.w,'search_check');self.w.save_settings({'radar':{'xhs_enabled':False}})
  self.assertNotEqual(stamp,sales.check_stamp(self.w,'search_check'))
 def test_focus_adds_plain_language_queries_without_overwriting_owner_words(self):
  before=self.w.config()['business']['keywords'];self.w.save_settings({'business':{'search_focus':'office'}})
  self.assertIn('Excel 求助',sales.query_pool(self.w.config()));self.assertEqual(self.w.config()['business']['keywords'],before)
 def test_pending_analysis_is_queued_even_when_scan_only_finds_duplicates(self):
  with patch.object(self.w,'xhs',side_effect=self.remote):self.w.scan()
  self.w.save_settings({'ai':{'api_key':'fixture-key'}})
  with patch.object(self.w,'xhs',side_effect=self.remote):self.w.scan()
  self.assertTrue(any(j['kind']=='qualify' for j in self.w.state()['jobs']))
 def test_recent_search_uses_real_upstream_filters(self):
  with patch.object(self.w,'xhs',side_effect=self.remote) as remote:self.w.scan()
  call=next(c for c in remote.call_args_list if '/feeds/search?' in c.args[0])
  self.assertEqual(call.args[1]['filters'],{'sort_by':'最新','publish_time':'一周内'})
 def test_priority_excludes_old_title_only_recruiting_and_contacted_records(self):
  row={'intent':'buyer','score':85,'state':'candidate','excerpt':'每天十张表，想自动合并','title':'求助Excel合并','published_at':app.now()}
  self.assertTrue(sales.priority(row))
  for patch_values in ({'published_at':'2025-01-01T00:00:00+08:00'},{'excerpt':''},{'state':'contacted'},{'excerpt':'招聘兼职打字员，负责抄录数据'}):
   with self.subTest(values=patch_values):self.assertFalse(sales.priority({**row,**patch_values}))
 def test_recent_explicit_help_can_be_reviewed_without_fabricating_ai_intent(self):
  row={'intent':'unconfirmed','score':30,'state':'candidate','excerpt':'有没有人懂进销库存台账，我需要做汇总表。','title':'求助各位Excel大佬','published_at':app.now()}
  self.assertTrue(sales.priority(row));self.assertEqual(row['intent'],'unconfirmed');self.assertEqual(row['score'],30)
 def test_comment_priority_never_inherits_parent_demand_or_recruitment(self):
  row={'intent':'unconfirmed','score':5,'state':'candidate','excerpt':'谢谢分享','title':'求助 有偿预算 怎么实现','published_at':app.now(),'record_type':'comment'}
  self.assertFalse(sales.priority(row))
  row.update(title='招聘兼职打字员',excerpt='求助，有没有人能做自动合并表格？',intent='buyer',score=90)
  self.assertTrue(sales.priority(row))
 def test_pause_resume_api_is_validated(self):
  from test_integration import RawHTTPClient
  client=RawHTTPClient(self.w)
  client.post('/api/control',{'action':'pause'});self.assertTrue(self.w.state()['control']['paused'])
  client.post('/api/control',{'action':'resume'});self.assertFalse(self.w.state()['control']['paused'])
  self.assertNotEqual(client.request('/api/control',{'action':'shutdown'})[0],200)
 def test_model_result_becomes_stale_after_model_changes(self):
  with patch.object(self.w,'llm',return_value={'ok':True}):sales.check(self.w,'model_check')
  self.w.save_settings({'ai':{'model':'another-model'}})
  self.assertEqual(sales.state(self.w)['checks'][0]['status'],'stale')
 def test_qualify_does_not_drain_entire_daily_budget(self):
  self.w.enqueue('qualify')
  with patch.object(self.w,'qualify',return_value='完成 8 条候选分析；购买意向仍需实际核实'):self.w.run_one_job()
  self.assertFalse(any(j['kind']=='qualify' and j['state']=='pending' for j in self.w.state()['jobs']))
 def test_isolated_model_check_records_failure_and_fix_without_social_requests(self):
  with patch.object(self.w,'llm',side_effect=RuntimeError('HTTP 401')),patch.object(self.w,'xhs') as xhs:
   with self.assertRaises(autopilot.SetupRequired):sales.check(self.w,'model_check')
   xhs.assert_not_called()
  r=json.loads(self.w.meta('sales_checks'))['model_check'];self.assertEqual(r['status'],'failed');self.assertIn('Key',r['next_action'])
 def test_writing_check_returns_unsent_real_sample_not_just_ping(self):
  with patch.object(self.w,'llm',return_value={'reply':'表头一样的话，可以先试试Power Query。每张表的列名一致吗？'}),patch.object(self.w,'xhs') as remote:
   self.assertIn('尚未发送',sales.check(self.w,'writing_check'));remote.assert_not_called()
 def test_link_check_missing_token_does_not_visit_arbitrary_url(self):
  self.w.ingest([{'url':'https://www.xiaohongshu.com/explore/n1','title':'Excel求助','platform':'xiaohongshu'}])
  with patch.object(self.w,'xhs') as remote:
   with self.assertRaises(autopilot.SetupRequired):sales.check(self.w,'link_check',1)
   remote.assert_not_called()
 def test_reply_draft_needs_body_and_never_sends(self):
  self.w.ingest([{'url':'https://www.xiaohongshu.com/explore/n1','title':'Excel求助','platform':'xiaohongshu','excerpt':'十张表怎么合并？'}])
  with patch.object(self.w,'llm',return_value={'reply':'表头一样可以用Power Query合并。每张表的列名一致吗？'}),patch.object(self.w,'xhs') as remote:
   self.assertIn('尚未发送',sales.draft_reply(self.w,1));remote.assert_not_called()
  self.assertIn('Power Query',self.w.state()['leads'][0]['reply'])
 def test_reply_draft_rejects_invented_contact_channel(self):
  self.w.ingest([{'url':'https://www.xiaohongshu.com/explore/n1','title':'Excel求助','platform':'xiaohongshu','excerpt':'十张表怎么合并？'}])
  with patch.object(self.w,'llm',return_value={'reply':'加我微信123456789'}):
   with self.assertRaises(ValueError):sales.draft_reply(self.w,1)
 def test_rewrite_does_not_touch_approved_content(self):
  self.w.generate(template=True);self.w.meta('xhs_bound_user','fixture-owner');self.w.edit_draft({'id':1,'state':'approved'})
  before=self.w.draft(1)
  with patch.object(self.w,'llm') as model:
   with self.assertRaises(ValueError):sales.rewrite(self.w,1)
   model.assert_not_called()
  self.assertEqual(self.w.draft(1),before)
 def test_rewrite_validates_cards_before_changing_original(self):
  self.w.generate(template=True);before=self.w.draft(1)
  with patch.object(self.w,'llm',return_value={'title':'换个说法','body':'新正文','tags':[],'cards':[]}):
   with self.assertRaises(ValueError):sales.rewrite(self.w,1)
  self.assertEqual(self.w.draft(1),before)

if __name__=='__main__':unittest.main()
