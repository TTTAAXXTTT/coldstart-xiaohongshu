"""自动编排、真实接口结构及异常保护。外部响应使用明确的合成夹具。"""
import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch

import app
import autopilot
from test_integration import RawHTTPClient

QA=Path(__file__).resolve().parent.parent/'qa'


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=QA)
        self.w=app.Workbench(self.temp.name)
        creator = patch.object(self.w,'creator_check',return_value={'creator_ready':True,'fixture':True}); creator.start(); self.addCleanup(creator.stop)
        submit = patch.object(self.w,'creator_submit',side_effect=lambda p:self.w.xhs('/api/v1/publish',json.loads(Path(p).read_text(encoding='utf-8')))); submit.start(); self.addCleanup(submit.stop)
    def tearDown(self):self.temp.cleanup()
    def configure(self):
        self.w.save_settings({'ai':{'base_url':'https://ark.cn-beijing.volces.com/api/v3','model':'doubao-seed-2-1-lite-260915','api_key':'fixture-key'},'radar':{'ark_search_enabled':True,'max_queries':1},'publishing':{'channels':['xiaohongshu'],'enabled':True,'auto_approve':True},'autonomy':{'enabled':True}})
    def model(self,value,annotations=()):
        return {'choices':[{'message':{'content':json.dumps(value,ensure_ascii=False),'annotations':list(annotations)},'finish_reason':'stop'}],'usage':{'total_tokens':20}}
    def variant(self):
        return {'variants':[{'platform':'xiaohongshu','title':'报表自动化的三个输入','body':'先列出数据来源，再确定输出和人工确认步骤。','tags':['自动化'],'cards':[{'title':'数据来源','body':'说明格式与整理频率。'}]}]}
    def selected(self):return {'topic':'报表自动化的三个输入','keywords':['报表自动化 求助','想找人做报表'],'source_ids':[],'reason':'合成规划夹具'}
    def submitted(self):
        self.w.meta('xhs_bound_user','fixture-account')
        self.w.save_settings({'publishing':{'channels':['xiaohongshu']}})
        self.w.generate(template=True)
        row=self.w.state()['drafts'][0]
        with self.w.db() as db:
            db.execute("UPDATE drafts SET state='submitted_unverified',target_binding=? WHERE id=?",(self.w.publish_target('xiaohongshu'),row['id']))
            db.execute("INSERT INTO attempts(draft_id,platform,started_at,state) VALUES(?,'xiaohongshu',?,'submitted_unverified')",(row['id'],app.now()))
        return self.w.draft(row['id'])
    def xhs_fixture(self,row=None,duplicate=False,foreign=False,old=False):
        def remote(path,payload=None,runtime_config=None):
            if path=='/api/v1/login/status':return {'is_logged_in':True,'user_id':'fixture-account'}
            if path=='/api/v1/user/me':
                feeds=[] if row is None else [{'id':x,'xsecToken':'fixture-token','noteCard':{'displayTitle':row['title']}} for x in (['fixture-note','fixture-duplicate'] if duplicate else ['fixture-note'])]
                return {'data':{'feeds':feeds}}
            if path=='/api/v1/feeds/detail':
                stamp=datetime.now(app.CST)-timedelta(days=1) if old else datetime.now(app.CST)
                return {'data':{'note':{'noteId':payload['feed_id'],'user':{'userId':'foreign' if foreign else 'fixture-account'},'title':row['title'],'desc':row['body']+' #自动化','time':int(stamp.timestamp()*1000)}}}
            raise AssertionError('Unexpected endpoint '+path)
        return remote

    def test_seed_json_tasks_disable_default_thinking(self):
        self.configure()
        with patch.object(app,'http_json',return_value=self.model({'ok':True})) as transport:
            self.assertTrue(self.w.llm('JSON','test',max_tokens=64)['ok'])
        payload=transport.call_args.args[1]
        self.assertEqual(payload['thinking'],{'type':'disabled'})
        self.assertEqual(payload['response_format'],{'type':'json_object'})
        self.assertNotIn('web_search_options',payload)

    def test_other_provider_does_not_receive_ark_only_fields(self):
        self.w.save_settings({'ai':{'base_url':'https://fixture.invalid/v1','model':'fixture-model','api_key':'fixture-key'}})
        with patch.object(app,'http_json',return_value=self.model({'ok':True})) as transport:self.w.llm('JSON','test')
        self.assertNotIn('thinking',transport.call_args.args[1])

    def test_ark_search_accepts_only_annotated_platform_urls(self):
        self.configure()
        annotations=[{'type':'url_citation','url_citation':{'url':'https://www.xiaohongshu.com/explore/fixture','title':'求助：报表怎么做'}},{'type':'url_citation','url_citation':{'url':'https://www.xiaohongshu.com/','title':'首页'}},{'type':'url_citation','url_citation':{'url':'https://evil.invalid/fixture','title':'伪造'}}]
        response=self.model({'results':[{'url':'https://www.xiaohongshu.com/explore/fixture','excerpt':'编造的原文不会被使用'},{'url':'https://www.xiaohongshu.com/explore/not-cited'},{'url':'https://www.xiaohongshu.com/'},{'url':'https://evil.invalid/fixture'}]},annotations)
        with patch.object(app,'http_json',return_value=response) as transport:self.assertEqual(autopilot.ark_search(self.w,'fixture'),1)
        self.assertIn('web_search_options',transport.call_args.args[1])
        lead=self.w.state()['leads'][0]
        self.assertEqual(lead['excerpt'],'');self.assertEqual(lead['record_type'],'web_citation');self.assertEqual(lead['intent'],'unconfirmed')

    def test_search_reference_is_never_a_verified_buyer(self):
        self.w.ingest([{'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/fixture','title':'想找人 求助 预算','record_type':'web_citation'}])
        with patch.object(self.w,'llm',return_value={'results':[{'id':1,'intent':'buyer','score':100,'evidence':'预算','reason':'model claim'}]}):self.w.qualify()
        lead=self.w.state()['leads'][0]
        self.assertEqual(lead['intent'],'unconfirmed');self.assertLessEqual(lead['score'],30);self.assertEqual(lead['evidence'],'')

    def test_search_and_model_quota_are_reserved_atomically(self):
        self.configure();self.w.save_settings({'radar':{'searches_per_day':1}});self.w.reserve_usage('search',1)
        with patch.object(app,'http_json') as remote:
            with self.assertRaises(ValueError):self.w.llm('JSON','test',web_search=True)
            remote.assert_not_called()
        with self.w.db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM usage WHERE kind='llm'").fetchone()[0],0)

    def test_bad_plan_cannot_change_working_configuration(self):
        self.configure();old=self.w.config()
        bad=self.selected();bad['source_ids']=[999]
        with patch.object(self.w,'llm',return_value=bad):
            with self.assertRaises(ValueError):autopilot.plan(self.w)
        self.assertEqual(self.w.config(),old)

    def test_plan_cannot_expand_publishing_permissions(self):
        self.configure();old=self.w.config()['publishing'];result={**self.selected(),'publishing':{'channels':['unknown'],'max_posts_per_day':999}}
        with patch.object(self.w,'llm',return_value=result):autopilot.plan(self.w)
        self.assertEqual(self.w.config()['publishing'],old)
        self.assertEqual(self.w.config()['business']['keywords'],app.DEFAULT['business']['keywords'])
        self.assertEqual(json.loads(self.w.meta('autonomy_plan'))['keywords'],result['keywords'])

    def test_repeated_ticks_enqueue_one_autonomous_cycle(self):
        self.configure();self.w.tick();self.w.tick()
        self.assertEqual([j['kind'] for j in self.w.state()['jobs']],['autopilot'])

    def test_default_mode_never_enqueues_autonomous_work(self):
        self.w.tick();self.assertEqual(self.w.state()['jobs'],[])
        with self.assertRaises(autopilot.SetupRequired):autopilot.run_cycle(self.w)

    def test_autonomous_due_job_keeps_schedule_revision_after_reschedule(self):
        self.configure();self.w.save_settings({'publishing':{'auto_approve':False},'autonomy':{'max_pending_drafts':0}})
        self.w.generate(template=True);row=self.w.state()['drafts'][0]
        self.w.edit_draft({'id':row['id'],'state':'approved','scheduled_at':(datetime.now(app.CST)-timedelta(minutes=1)).isoformat()})
        with patch.object(self.w,'scan',return_value='合成空扫描'),patch.object(autopilot,'sync_xhs',return_value='合成只读回查'):autopilot.run_cycle(self.w)
        future=(datetime.now(app.CST)+timedelta(days=1)).isoformat(timespec='seconds');self.w.edit_draft({'id':row['id'],'state':'approved','scheduled_at':future})
        with patch.object(self.w,'creator_submit') as submit:self.w.run_one_job()
        submit.assert_not_called();self.assertEqual(self.w.state()['jobs'][0]['state'],'cancelled');self.assertEqual(self.w.draft(row['id'])['scheduled_at'],future)

    def test_cycle_finds_plans_generates_approves_without_buttons(self):
        self.configure()
        def transport(url,payload=None,token='',timeout=120):
            self.assertTrue(url.startswith('https://ark.cn-beijing.volces.com/api/v3/'))
            if 'web_search_options' in payload:return self.model({'results':[]})
            if '规划中文业务获客' in payload['messages'][0]['content']:return self.model(self.selected())
            return self.model(self.variant())
        with patch.object(app,'http_json',side_effect=transport),patch.object(self.w,'xhs',side_effect=self.xhs_fixture()):
            self.w.enqueue('autopilot');self.w.run_one_job()
            row=self.w.state()['drafts'][0]
            self.assertEqual(row['generator'],'ai');self.assertEqual(row['state'],'approved')
            self.assertTrue(all(Path(p).is_file() for p in json.loads(row['assets'])))
            autopilot.run_cycle(self.w)
        self.assertEqual(len(self.w.state()['drafts']),1)
        self.assertEqual(self.w.meta('autonomy_generated_day'),app.now()[:10])

    def test_failed_search_stops_fabricated_content_and_records_blocker(self):
        self.configure()
        with patch.object(self.w,'scan',side_effect=RuntimeError('fixture network blocked')),patch.object(self.w,'xhs',side_effect=RuntimeError('not installed')),patch.object(self.w,'llm') as model:
            self.w.enqueue('autopilot');self.w.run_one_job();model.assert_not_called()
        self.assertEqual(self.w.state()['drafts'],[])
        self.assertEqual(self.w.state()['jobs'][0]['state'],'needs_setup')

    def test_backlog_limit_stops_content_growth(self):
        self.configure();self.w.generate(template=True);self.w.save_settings({'autonomy':{'max_pending_drafts':1}})
        with patch.object(self.w,'scan',return_value='no new leads'),patch.object(autopilot,'sync_xhs',return_value='fixture'),patch.object(self.w,'llm') as model:autopilot.run_cycle(self.w);model.assert_not_called()
        self.assertEqual(len(self.w.state()['drafts']),1)

    def test_readback_confirms_only_matching_account_body_and_time(self):
        row=self.submitted()
        with patch.object(self.w,'xhs',side_effect=self.xhs_fixture(row)):autopilot.sync_xhs(self.w)
        self.assertEqual(self.w.draft(row['id'])['state'],'published')
        self.assertTrue(self.w.draft(row['id'])['public_url'].endswith('/fixture-note'))

    def test_readback_rejects_old_foreign_and_ambiguous_matches(self):
        row=self.submitted()
        for flags in ({'old':True},{'foreign':True},{'duplicate':True}):
            with self.subTest(flags=flags),patch.object(self.w,'xhs',side_effect=self.xhs_fixture(row,**flags)):autopilot.sync_xhs(self.w)
            self.assertEqual(self.w.draft(row['id'])['state'],'submitted_unverified')

    def test_readback_empty_profile_never_marks_content_absent(self):
        row=self.submitted()
        with patch.object(self.w,'xhs',side_effect=self.xhs_fixture()):autopilot.sync_xhs(self.w)
        self.assertEqual(self.w.draft(row['id'])['state'],'submitted_unverified')

    def test_readback_does_not_starve_older_rows_or_returned_feeds(self):
        self.w.meta('xhs_bound_user','fixture-account');oldest=None
        for i in range(11):
            self.w.generate('合成待核对'+str(i),template=True);row=self.w.state()['drafts'][0]
            with self.w.db() as db:
                db.execute("UPDATE drafts SET state='submitted_unverified',target_binding=? WHERE id=?",(self.w.publish_target('xiaohongshu'),row['id']))
                db.execute("INSERT INTO attempts(draft_id,platform,started_at,state) VALUES(?,'xiaohongshu',?,'submitted_unverified')",(row['id'],app.now()))
            if oldest is None:oldest=self.w.draft(row['id'])
        original=self.xhs_fixture(oldest)
        def remote(path,*args,**kwargs):
            result=original(path,*args,**kwargs)
            if path=='/api/v1/user/me':result['data']['feeds']=[{'id':'irrelevant-'+str(i),'xsecToken':'fixture-token','noteCard':{'displayTitle':'其他作品'}} for i in range(31)]+result['data']['feeds']
            return result
        with patch.object(self.w,'xhs',side_effect=remote):autopilot.sync_xhs(self.w)
        self.assertEqual(self.w.draft(oldest['id'])['state'],'published')

    def test_account_rebind_never_reconciles_previous_account_submission(self):
        row=self.submitted();old=row['target_binding'];self.w.meta('xhs_bound_user','different-account')
        def current(path,*args,**kwargs):
            if path=='/api/v1/login/status':return {'is_logged_in':True,'user_id':'different-account'}
            if path=='/api/v1/user/me':return {'data':{'feeds':[{'id':'fixture-note','xsecToken':'fixture-token','noteCard':{'displayTitle':row['title']}}]}}
            raise AssertionError('Previous account submission must not read this new account post')
        with patch.object(self.w,'xhs',side_effect=current):autopilot.sync_xhs(self.w)
        self.assertEqual(self.w.draft(row['id'])['state'],'submitted_unverified');self.assertEqual(self.w.draft(row['id'])['target_binding'],old)

    def test_body_matching_does_not_accept_same_prefix_with_other_claim(self):
        self.assertTrue(autopilot.same_body('原始正文','原始正文 #标签'))
        self.assertFalse(autopilot.same_body('原始正文','原始正文再补充不同承诺'))

    def test_live_native_topic_serialization_reconciles_exact_body(self):
        row=self.submitted()
        original=self.xhs_fixture(row)
        def remote(path,payload=None,runtime_config=None):
            result=original(path,payload,runtime_config)
            if path=='/api/v1/feeds/detail':
                result['data']['note']['desc']=row['body']+'#报表自动化[话题]# #流程梳理[话题]#'
            return result
        with patch.object(self.w,'xhs',side_effect=remote):autopilot.sync_xhs(self.w)
        self.assertEqual(self.w.draft(row['id'])['state'],'published')
        for desc in ['原始正文 #标签[话题]# 新增承诺','原始正文 #标签[错误标记]#','不同正文 #标签[话题]#']:
            with self.subTest(desc=desc):self.assertFalse(autopilot.same_body('原始正文',desc))

    def test_null_profile_through_real_http_decoder_preserves_unverified_note(self):
        row=self.submitted()
        envelopes=[{'success':True,'data':{'is_logged_in':True,'user_id':'fixture-account'}},
                   {'success':True,'data':{'data':{'userBasicInfo':{},'interactions':None,'feeds':None}}}]
        with patch.object(app,'http_json',side_effect=envelopes) as transport:
            self.assertIn('自动核对0条作品',autopilot.sync_xhs(self.w))
        self.assertEqual(transport.call_count,2)
        self.assertEqual(self.w.draft(row['id'])['state'],'submitted_unverified')
        self.assertEqual(self.w.draft(row['id'])['public_url'],'')

    def test_malformed_profile_is_still_blocked(self):
        row=self.submitted()
        for profile in ({},{'feeds':{}},{'feeds':'null'},None):
            envelopes=[{'success':True,'data':{'is_logged_in':True,'user_id':'fixture-account'}},
                       {'success':True,'data':{'data':profile}}]
            with self.subTest(profile=profile),patch.object(app,'http_json',side_effect=envelopes):
                with self.assertRaises(autopilot.SetupRequired):autopilot.sync_xhs(self.w)
            self.assertEqual(self.w.draft(row['id'])['state'],'submitted_unverified')

    def test_cycle_submission_report_uses_current_switch_after_search(self):
        self.configure()
        self.w.meta('autonomy_generated_day',app.now()[:10])
        for before,after,expected in ((False,True,'queued'),(True,False,'blocked')):
            self.w.save_settings({'publishing':{'enabled':before}})
            def search():
                self.w.save_settings({'publishing':{'enabled':after}})
                return 'fixture: no new leads'
            with self.subTest(before=before,after=after),patch.object(autopilot,'sync_xhs',return_value='fixture'),patch.object(self.w,'scan',side_effect=search):
                autopilot.run_cycle(self.w)
                report=json.loads(self.w.meta('autonomy_report'))
                self.assertEqual(next(s for s in report['steps'] if s['step']=='到期内容提交')['status'],expected)

    def prepare_inbox(self):
        self.configure();self.w.meta('xhs_bound_user','fixture-account');self.w.save_settings({'autonomy':{'reply_to_own_comments':True}})
        self.w.generate(template=True);row=self.w.state()['drafts'][0]
        with self.w.db() as db:db.execute("UPDATE drafts SET state='published',public_url='https://www.xiaohongshu.com/explore/own-note' WHERE id=?",(row['id'],))
        self.w.meta('autonomy_started_at',(datetime.now(app.CST)-timedelta(minutes=1)).isoformat())
        items=[{'feed_id':'own-note','comment_id':'fixture-comment','comment_text':'请问报表怎么实现自动化？','time':int(datetime.now(app.CST).timestamp()),'from':{'user_id':'fixture-visitor','nickname':'合成访客'}}]
        calls=[]
        def remote(path,payload=None,runtime_config=None):
            calls.append((path,payload))
            if path=='/api/v1/login/status':return {'is_logged_in':True,'user_id':'fixture-account'}
            if path.startswith('/api/v1/notifications/list'):return {'data':{'items':items}}
            if path=='/api/v1/notifications/reply':return {'data':{'comment_id':payload['comment_id'],'content':payload['content']}}
            if path=='/api/v1/user/me':return {'data':{'feeds':[{'id':'own-note','xsecToken':'fixture-token'}]}}
            if path=='/api/v1/feeds/detail':return {'data':{'note':{'noteId':'own-note','user':{'userId':'fixture-account'}},'comments':{'list':[{'id':'fixture-comment','subComments':[{'id':'fixture-reply','content':answer['results'][0]['text'],'userInfo':{'userId':'fixture-account'}}] if any(p=='/api/v1/notifications/reply' for p,_ in calls) else []}]}}}
            raise AssertionError(path)
        answer={'results':[{'id':1,'action':'reply','evidence':'报表怎么实现自动化','text':'请问当前输入是什么格式，最终希望输出什么结果？','reason':'回答实际问题'}]}
        def native(w,account,feed,cid):
            detail=self.w.xhs('/api/v1/feeds/detail',{})['data'];rows=[]
            def walk(items,root='',parent=''):
                for c in items:
                    rid=root or c['id'];rows.append({'id':c['id'],'content':c.get('content',''),'user_id':(c.get('userInfo') or {}).get('userId','fixture-visitor'),'root_id':rid,'target_comment_id':c.get('target_comment_id',parent),'target_user_id':c.get('target_user_id','fixture-visitor')});walk(c.get('subComments') or [],rid,c['id'])
            walk(detail['comments']['list']);return {'feed_id':'own-note','owner_id':'fixture-account','comments':rows}
        reader=patch.object(autopilot.own_notes,'native_comments',side_effect=native);reader.start();self.addCleanup(reader.stop)
        return items,calls,remote,answer

    def test_own_inbound_comment_is_replied_once_and_persisted(self):
        items,calls,remote,answer=self.prepare_inbox()
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm',return_value=answer):
            self.assertEqual(autopilot.reply_to_own_comments(self.w,'fixture-account'),1)
            self.assertEqual(autopilot.reply_to_own_comments(self.w,'fixture-account'),0)
        self.assertEqual(sum(path.endswith('/notifications/reply') for path,_ in calls),1)
        self.assertEqual(self.w.inbox_state()[0]['state'],'verified')

    def test_manual_comments_job_runs_without_background_autonomy(self):
        items,calls,remote,answer=self.prepare_inbox()
        self.w.save_settings({'autonomy':{'enabled':False}})
        self.w.meta('autonomy_started_at',app.now())
        ident=self.w.enqueue('process_comments')
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm',return_value=answer):self.w.run_one_job()
        self.assertFalse(self.w.config()['autonomy']['enabled'])
        self.assertEqual(next(j for j in self.w.state()['jobs'] if j['id']==ident)['state'],'done')
        self.assertEqual(self.w.inbox_state()[0]['state'],'verified')
        self.assertEqual(self.w.inbox_state()[0]['proof']['reply_id'],'fixture-reply')
        self.assertEqual(sum(p.endswith('/notifications/reply') for p,_ in calls),1)

    def test_success_ack_without_readback_is_unknown_and_not_resent(self):
        items,calls,remote,answer=self.prepare_inbox()
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm',return_value=answer),patch.object(autopilot.own_notes,'verify_reply',side_effect=ValueError('合成夹具：回查未找到')):
            with self.assertRaises(autopilot.SetupRequired):autopilot.reply_to_own_comments(self.w,'fixture-account')
            with self.assertRaises(autopilot.SetupRequired):autopilot.reply_to_own_comments(self.w,'fixture-account')
        self.assertEqual(sum(p.endswith('/notifications/reply') for p,_ in calls),1)
        self.assertEqual(self.w.inbox_state()[0]['state'],'unknown')

    def test_ownership_is_rechecked_before_send_not_only_after(self):
        items,calls,remote,answer=self.prepare_inbox()
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm',return_value=answer),patch.object(autopilot.own_notes,'read_own',side_effect=ValueError('合成夹具：作品非本人')):
            with self.assertRaises(ValueError):autopilot.reply_to_own_comments(self.w,'fixture-account')
        self.assertFalse(any(p.endswith('/notifications/reply') for p,_ in calls))

    def test_imported_existing_manual_reply_is_not_replied_to_again(self):
        items,calls,remote,answer=self.prepare_inbox()
        def existing(path,*args,**kwargs):
            result=remote(path,*args,**kwargs)
            if path=='/api/v1/feeds/detail':result['data']['comments']['list'][0]['subComments']=[{'id':'prior-manual','content':'已有本人回复','userInfo':{'userId':'fixture-account'}}]
            return result
        with patch.object(self.w,'xhs',side_effect=existing),patch.object(self.w,'llm',return_value=answer):self.assertEqual(autopilot.reply_to_own_comments(self.w,'fixture-account'),0)
        self.assertFalse(any(p.endswith('/notifications/reply') for p,_ in calls))
        self.assertEqual(self.w.inbox_state()[0]['state'],'skipped')

    def test_reply_to_another_child_does_not_skip_the_pending_parent(self):
        items,calls,remote,answer=self.prepare_inbox()
        def other(path,*args,**kwargs):
            result=remote(path,*args,**kwargs)
            if path=='/api/v1/feeds/detail' and not any(p=='/api/v1/notifications/reply' for p,_ in calls):result['data']['comments']['list'][0]['subComments']=[{'id':'different-reply','content':'回复别人的内容','target_comment_id':'other-child','userInfo':{'userId':'fixture-account'}}]
            return result
        with patch.object(self.w,'xhs',side_effect=other),patch.object(self.w,'llm',return_value=answer):self.assertEqual(autopilot.reply_to_own_comments(self.w,'fixture-account'),1)
        self.assertEqual(sum(p.endswith('/notifications/reply') for p,_ in calls),1)

    def test_restart_turns_sending_into_unknown_and_blocks_new_sends(self):
        self.prepare_inbox()
        with self.w.db() as db:db.execute("INSERT INTO inbox(account_id,comment_id,feed_id,author,body,state,reply,created_at,updated_at) VALUES('fixture-account','crashed-send','own-note','合成访客','合成需求','sending','可能已发出的回复',?,?)",(app.now(),app.now()))
        restarted=app.Workbench(self.temp.name)
        with patch.object(restarted,'xhs') as transport,patch.object(restarted,'llm') as model:
            with self.assertRaises(autopilot.SetupRequired):autopilot.reply_to_own_comments(restarted,'fixture-account')
        self.assertEqual(restarted.inbox_state()[0]['state'],'unknown');transport.assert_not_called();model.assert_not_called()

    def test_foreign_self_and_old_comments_are_never_sent(self):
        items,calls,remote,answer=self.prepare_inbox()
        original=items[0]
        items[:]=[{**original,'feed_id':'foreign-note'},{**original,'from':{'user_id':'fixture-account'}},{**original,'time':int((datetime.now(app.CST)-timedelta(days=1)).timestamp())}]
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm') as model:
            self.assertEqual(autopilot.reply_to_own_comments(self.w,'fixture-account'),0);model.assert_not_called()

    def test_reply_validation_is_atomic_and_requires_original_evidence(self):
        items,calls,remote,answer=self.prepare_inbox();answer['results'][0]['evidence']='伪造的预算承诺'
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm',return_value=answer):
            with self.assertRaises(ValueError):autopilot.reply_to_own_comments(self.w,'fixture-account')
        self.assertFalse(any(path.endswith('/notifications/reply') for path,_ in calls))

    def test_reply_timeout_is_unknown_and_never_retried(self):
        items,calls,remote,answer=self.prepare_inbox()
        def timeout(path,*args,**kwargs):
            if path.endswith('/notifications/reply'):raise TimeoutError('fixture timeout')
            return remote(path,*args,**kwargs)
        with patch.object(self.w,'xhs',side_effect=timeout),patch.object(self.w,'llm',return_value=answer):
            with self.assertRaises(autopilot.SetupRequired):autopilot.reply_to_own_comments(self.w,'fixture-account')
            with self.assertRaises(autopilot.SetupRequired):autopilot.reply_to_own_comments(self.w,'fixture-account')
        self.assertEqual(self.w.inbox_state()[0]['state'],'unknown')

    def test_reply_rejects_angle_brackets_before_external_send(self):
        items,calls,remote,answer=self.prepare_inbox()
        answer['results'][0]['text']='请使用条件 F2<>空白 来匹配。'
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm',return_value=answer):
            with self.assertRaisesRegex(ValueError,'尖括号'):autopilot.reply_to_own_comments(self.w,'fixture-account')
        self.assertFalse(any(path.endswith('/notifications/reply') for path,_ in calls))
        self.assertEqual(self.w.inbox_state()[0]['state'],'pending')

    def test_automatic_settings_are_strict_and_state_is_redacted(self):
        self.configure();client=RawHTTPClient(self.w)
        status,_=client.request('/api/settings',{'autonomy':{'enabled':'true'}})
        self.assertEqual(status,400)
        status,state=client.request('/api/state');self.assertEqual(status,200)
        self.assertNotIn('fixture-key',json.dumps(state));self.assertIn('autonomy_report',state)

    def test_new_account_binding_can_reapprove_ai_draft_without_manual_click(self):
        self.configure()
        with patch.object(self.w,'llm',return_value=self.variant()):self.w.generate('合成测试选题')
        row=self.w.state()['drafts'][0]
        self.assertEqual(json.loads(row['target_binding'])['user_id'],'')
        self.w.meta('xhs_bound_user','fixture-account')
        autopilot.approve_pending(self.w)
        self.assertEqual(json.loads(self.w.draft(row['id'])['target_binding'])['user_id'],'fixture-account')

    def test_account_switch_never_reapproves_old_ai_authorization(self):
        self.configure();self.w.meta('xhs_bound_user','old-account')
        with patch.object(self.w,'llm',return_value=self.variant()):self.w.generate('合成测试选题')
        row=self.w.state()['drafts'][0];self.w.meta('xhs_bound_user','new-account')
        autopilot.approve_pending(self.w)
        self.assertEqual(json.loads(self.w.draft(row['id'])['target_binding'])['user_id'],'old-account')

    def test_reply_daily_cap_stops_next_comment_and_preserves_real_source(self):
        items,calls,remote,answer=self.prepare_inbox();self.w.save_settings({'autonomy':{'replies_per_day':1}})
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm',return_value=answer):
            self.assertEqual(autopilot.reply_to_own_comments(self.w,'fixture-account'),1)
            items[0]['comment_id']='fixture-comment-two'
            self.assertEqual(autopilot.reply_to_own_comments(self.w,'fixture-account'),0)
        self.assertEqual(len(self.w.state()['leads']),2)
        self.assertTrue(all(l['record_type']=='comment' for l in self.w.state()['leads']))

    def test_switch_off_during_reply_planning_stops_send(self):
        items,calls,remote,answer=self.prepare_inbox()
        def disable(*args,**kwargs):
            self.w.save_settings({'publishing':{'enabled':False}})
            return answer
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm',side_effect=disable):
            self.assertEqual(autopilot.reply_to_own_comments(self.w,'fixture-account'),0)
        self.assertFalse(any(path.endswith('/notifications/reply') for path,_ in calls))

    def test_switch_off_during_native_preread_stops_send(self):
        items,calls,remote,answer=self.prepare_inbox()
        def disable(*args,**kwargs):
            self.w.save_settings({'publishing':{'enabled':False}})
            return {'feed_id':'own-note','owner_id':'fixture-account','comments':[{'id':'fixture-comment','user_id':'fixture-visitor','root_id':'fixture-comment'}]}
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm',return_value=answer),patch.object(autopilot.own_notes,'native_comments',side_effect=disable):self.assertEqual(autopilot.reply_to_own_comments(self.w,'fixture-account'),0)
        self.assertFalse(any(p.endswith('/notifications/reply') for p,_ in calls));self.assertEqual(self.w.inbox_state()[0]['state'],'pending')

    def test_endpoint_change_during_native_preread_stops_send(self):
        items,calls,remote,answer=self.prepare_inbox()
        def change(*args,**kwargs):
            self.w.save_settings({'radar':{'xhs_url':'http://127.0.0.1:19001'}})
            return {'feed_id':'own-note','owner_id':'fixture-account','comments':[{'id':'fixture-comment','user_id':'fixture-visitor','root_id':'fixture-comment'}]}
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm',return_value=answer),patch.object(autopilot.own_notes,'native_comments',side_effect=change):
            with self.assertRaises(autopilot.SetupRequired):autopilot.reply_to_own_comments(self.w,'fixture-account')
        self.assertFalse(any(p.endswith('/notifications/reply') for p,_ in calls))

    def test_unlimited_reply_pipeline_exceeds_old_notification_cycle_and_day_caps(self):
        import limits
        self.prepare_inbox();self.w.save_settings(limits.all_unlimited())
        items=[{'feed_id':'own-note','comment_id':'fixture-'+str(i),'comment_text':'请问报表怎么实现自动化？','time':int(datetime.now(app.CST).timestamp()),'from':{'user_id':'fixture-visitor','nickname':'合成访客'}} for i in range(25)]
        sent={}
        with self.w.db() as db:
            for i in range(4):db.execute("INSERT INTO inbox(account_id,comment_id,feed_id,author,body,state,reply,created_at,updated_at) VALUES('fixture-account',?,'own-note','合成访客','旧合成问题','verified','旧合成回复',?,?)",('old-'+str(i),app.now(),app.now()))
        def remote(path,payload=None,runtime_config=None):
            if path=='/api/v1/login/status':return {'is_logged_in':True,'user_id':'fixture-account'}
            if path.startswith('/api/v1/notifications/list'):return {'data':{'items':items}}
            if path=='/api/v1/user/me':return {'data':{'feeds':[{'id':'own-note','xsecToken':'fixture-token'}]}}
            if path=='/api/v1/feeds/detail':return {'data':{'note':{'noteId':'own-note','user':{'userId':'fixture-account'}},'comments':{'list':[{'id':i['comment_id']} for i in items]}}}
            if path=='/api/v1/notifications/reply':
                self.assertNotIn(payload['comment_id'],sent);sent[payload['comment_id']]=payload['content'];return {'data':payload}
            raise AssertionError(path)
        def native(w,account,feed,cid):
            rows=[{'id':cid,'user_id':'fixture-visitor','root_id':cid}]
            if cid in sent:rows.append({'id':'reply-'+cid,'content':sent[cid],'user_id':account,'root_id':cid,'target_comment_id':cid,'target_user_id':'fixture-visitor'})
            return {'feed_id':'own-note','owner_id':account,'comments':rows}
        def model(system,user):return {'results':[{'id':r['id'],'action':'reply','evidence':'报表怎么实现自动化','text':'表头一致时可先试合并功能，输入文件的列名一样吗？','reason':'合成明确问题'} for r in json.loads(user)['comments']]}
        with patch.object(self.w,'xhs',side_effect=remote),patch.object(self.w,'llm',side_effect=model),patch.object(autopilot.own_notes,'native_comments',side_effect=native):
            self.assertEqual(autopilot.reply_to_own_comments(self.w,'fixture-account'),25)
            self.assertEqual(autopilot.reply_to_own_comments(self.w,'fixture-account'),0)
        self.assertEqual(len(sent),25);self.assertEqual(sum(i['state']=='verified' for i in self.w.inbox_state()),29)


if __name__=='__main__':unittest.main()
