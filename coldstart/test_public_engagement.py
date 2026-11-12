"""Public conversation business regressions; synthetic transport, no real sends."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import app,public_engagement as pub,public_policy as policy

class PublicTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.w=app.Workbench(self.temp.name)
        self.w.save_settings({'business':{'name':'合成内容','offer':'表格知识','audience':'合成读者'},'public':{'enabled':True,'send_enabled':True},'ai':{'calls_per_day':-1}})
        self.w.meta('xhs_bound_user','owner')
        with self.w.db() as db:
            ident=db.execute("INSERT INTO public_threads(account,feed,token,target,title,context,created_at) VALUES('owner','feed','fixture-token','','求助合并','表头不一致怎么合并？',?)",(app.now(),)).lastrowid
            self.thread=dict(db.execute('SELECT * FROM public_threads WHERE id=?',(ident,)).fetchone())
        self.comments=[];self.sent=[]
        def read(w,thread):return {'feed_id':'feed','owner_id':'visitor','body':'表头不一致怎么合并？','comments':[dict(c) for c in self.comments],'has_more':False}
        def transport(path,payload=None,**kwargs):
            if path.endswith('/login/status'):return {'is_logged_in':True,'user_id':'owner'}
            self.assertIn(path,('/api/v1/feeds/comment','/api/v1/feeds/comment/reply'))
            self.sent.append((path,payload.copy()));target=payload.get('comment_id','')
            parent=next((c for c in self.comments if c['id']==target),{})
            cid='own'+str(len(self.sent))
            self.comments.append({'id':cid,'content':payload['content'],'user_id':'owner','root_id':parent.get('root_id',cid),'parent_id':target,'target_comment_id':target,'target_user_id':parent.get('user_id','')})
            return {'success':True}
        for obj,key,func in ((pub,'read',read),(self.w,'xhs',transport)):
            p=patch.object(obj,key,side_effect=func);p.start();self.addCleanup(p.stop)
        p=patch.object(self.w,'llm',side_effect=lambda *a,**k:{'action':'reply','text':'先把列名统一，再按列名合并。','evidence':'怎么','reason':'回答原问题'});p.start();self.addCleanup(p.stop)

    def turns(self):
        with self.w.db() as db:return [dict(r) for r in db.execute('SELECT * FROM public_turns ORDER BY id')]

    def test_initial_send_and_independent_readback(self):
        pub.process_thread(self.w,self.thread)
        self.assertEqual(len(self.sent),1);self.assertEqual(self.turns()[0]['state'],'verified')
        self.assertTrue(json.loads(self.turns()[0]['proof'])['target_checked'])

    def test_repeated_refresh_never_repeats_comment(self):
        for _ in range(3):pub.process_thread(self.w,self.thread)
        self.assertEqual(len(self.sent),1)

    def test_followup_uses_correct_target_and_history(self):
        pub.process_thread(self.w,self.thread)
        self.comments.append({'id':'newquestion','content':'统一后怎么更新？','user_id':'visitor','root_id':'own1','parent_id':'own1','target_comment_id':'own1','target_user_id':'owner'})
        self.w.llm.side_effect=lambda *a,**k:{'action':'reply','text':'文件放固定文件夹，后面在数据页点全部刷新。','evidence':'怎么','reason':'补充后续更新方式'}
        pub.process_thread(self.w,self.thread)
        self.assertEqual(self.sent[-1][1]['comment_id'],'newquestion')
        self.assertEqual(self.sent[-1][1]['user_id'],'visitor')
        payload=json.loads(self.w.llm.call_args.args[1]);self.assertTrue(payload['history']);self.assertEqual(len(self.sent),2)

    def test_unrelated_new_comment_is_not_treated_as_reply(self):
        pub.process_thread(self.w,self.thread)
        self.comments.append({'id':'unrelated','content':'怎么设置？','user_id':'someone','root_id':'other','parent_id':'','target_comment_id':'','target_user_id':''})
        pub.process_thread(self.w,self.thread);self.assertEqual(len(self.sent),1)

    def test_reply_to_specific_initial_comment(self):
        self.comments.append({'id':'question','content':'表头怎么统一？','user_id':'visitor','root_id':'question','parent_id':'','target_comment_id':'','target_user_id':''})
        self.thread['target']='question'
        with self.w.db() as db:db.execute("UPDATE public_threads SET target='question' WHERE id=?",(self.thread['id'],))
        pub.process_thread(self.w,self.thread)
        self.assertEqual(self.sent[0][1]['comment_id'],'question');self.assertEqual(self.turns()[0]['state'],'verified')

    def test_commercial_question_stops_without_model_or_send(self):
        self.thread['context']='怎么收费？'
        with patch.object(pub,'read',return_value={'owner_id':'visitor','comments':[],'feed_id':'feed'}):pub.process_thread(self.w,self.thread)
        self.assertEqual(len(self.sent),0);self.w.llm.assert_not_called()

    def test_public_handoff_stops_without_reading_private_messages(self):
        self.thread['context']='我私信你了'
        with patch.object(pub,'read',return_value={'owner_id':'visitor','comments':[],'feed_id':'feed'}):pub.process_thread(self.w,self.thread)
        self.assertFalse(self.sent)
        with self.w.db() as db:self.assertEqual(db.execute('SELECT state FROM public_threads').fetchone()[0],'stopped')

    def test_private_invitation_repaired_before_send(self):
        self.w.llm.side_effect=[{'action':'reply','text':'可以私信我帮你做','evidence':'怎么'}, {'action':'reply','text':'先把列名统一再合并。','evidence':'怎么'}, {'action':'reply','text':'先把列名统一再合并。','evidence':'怎么'}]
        pub.process_thread(self.w,self.thread);self.assertEqual(len(self.sent),1);self.assertNotIn('私信',self.sent[0][1]['content'])

    def test_invalid_repair_never_sends(self):
        self.w.llm.side_effect=lambda *a,**k:{'action':'reply','text':'报价100元','evidence':'怎么'}
        pub.process_thread(self.w,self.thread)
        self.assertFalse(self.sent)
        self.assertEqual(self.turns()[0]['state'],'skipped')

    def test_public_reader_cannot_insert_private_invitation(self):
        self.w.llm.side_effect=[{'action':'reply','text':'先把列名统一再合并。','evidence':'怎么'},{'action':'reply','text':'可以私信我帮你做','evidence':'怎么'}]*2
        pub.process_thread(self.w,self.thread)
        self.assertFalse(self.sent)

    def test_unknown_feed_does_not_block_other_feed(self):
        with self.w.db() as db:
            db.execute("INSERT INTO public_turns(thread,message_id,body,author,state,created_at,updated_at) VALUES(?,'initial','原问题','visitor','unknown',?,?)",(self.thread['id'],app.now(),app.now()))
            ident=db.execute("INSERT INTO public_threads(account,feed,token,target,title,context,created_at) VALUES('owner','another-feed','fixture','','合并求助','表头不一致怎么合并？',?)",(app.now(),)).lastrowid
            other=dict(db.execute('SELECT * FROM public_threads WHERE id=?',(ident,)).fetchone())
        pub.process_thread(self.w,other)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0][1]['feed_id'],'another-feed')
        self.assertEqual(self.turns()[0]['state'],'unknown')

    def test_unknown_feed_holds_other_targets_without_model_calls(self):
        with self.w.db() as db:
            db.execute("INSERT INTO public_turns(thread,message_id,body,author,state,created_at,updated_at) VALUES(?,'initial','原问题','visitor','unknown',?,?)",(self.thread['id'],app.now(),app.now()))
            ident=db.execute("INSERT INTO public_threads(account,feed,token,target,title,context,created_at) VALUES('owner','feed','fixture','another-target','合并求助','怎么合并？',?)",(app.now(),)).lastrowid
            other=dict(db.execute('SELECT * FROM public_threads WHERE id=?',(ident,)).fetchone())
        pub.process_thread(self.w,other)
        self.assertFalse(self.sent);self.w.llm.assert_not_called()

    def test_reader_reuses_validated_original_quote(self):
        self.w.llm.side_effect=[{'action':'reply','text':'先统一列名。','evidence':'怎么'},{'action':'reply','text':'先统一列名，再按列名合并。','evidence':'这是改写过的非原文'}]
        pub.process_thread(self.w,self.thread)
        self.assertEqual(len(self.sent),1);self.assertEqual(self.turns()[0]['state'],'verified')

    def test_complete_share_is_skipped_without_model_or_send(self):
        thread={**self.thread,'title':'交接卡的做法','context':'把预订代号和到店时段整理成表，交班前核对房态。'}
        with patch.object(pub,'read',return_value={'owner_id':'visitor','comments':[],'feed_id':'feed'}):pub.process_thread(self.w,thread)
        self.assertFalse(self.sent);self.w.llm.assert_not_called()

    def test_observe_mode_drafts_without_sending(self):
        self.w.save_settings({'public':{'send_enabled':False}})
        pub.process_thread(self.w,self.thread);self.assertFalse(self.sent);self.assertEqual(self.turns()[0]['state'],'draft')

    def test_manual_handoff_blocks_submission(self):
        pub.control(self.w,'handoff',self.thread['id'])
        pub.process_thread(self.w,self.thread);self.assertFalse(self.sent)

    def test_lost_response_recovers_existing_comment_without_resend(self):
        old=self.w.xhs.side_effect
        def lost(path,payload=None,**kwargs):
            value=old(path,payload,**kwargs)
            if path.endswith('/comment'):raise TimeoutError('fixture lost response')
            return value
        self.w.xhs.side_effect=lost
        with self.assertRaises(TimeoutError):pub.process_thread(self.w,self.thread)
        self.assertEqual(self.turns()[0]['state'],'unknown')
        self.w.xhs.side_effect=old
        pub.process_thread(self.w,self.thread)
        self.assertEqual(self.turns()[0]['state'],'verified');self.assertEqual(len(self.sent),1)

    def test_unknown_submission_with_no_readback_blocks_new_send(self):
        self.w.xhs.side_effect=lambda path,payload=None,**kw:({'is_logged_in':True,'user_id':'owner'} if path.endswith('/status') else (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaises(TimeoutError):pub.process_thread(self.w,self.thread)
        pub.process_thread(self.w,self.thread);self.assertEqual(self.turns()[0]['state'],'unknown')

    def test_restart_sending_becomes_unknown(self):
        with self.w.db() as db:db.execute("INSERT INTO public_turns(thread,message_id,body,author,state,created_at,updated_at) VALUES(?,'initial','合成','visitor','sending',?,?)",(self.thread['id'],app.now(),app.now()))
        restarted=app.Workbench(self.temp.name)
        with restarted.db() as db:self.assertEqual(db.execute('SELECT state FROM public_turns').fetchone()[0],'unknown')

    def test_independent_search_monitor_and_ui_fields(self):
        self.w.tick();kinds=[r['kind'] for r in self.w.state()['jobs']]
        self.assertIn('public_scan',kinds);self.assertIn('public_monitor',kinds)
        self.w.tick();self.assertEqual(len(kinds),len(self.w.state()['jobs']))
        public=pub.state(self.w);self.assertNotIn('fixture-token',json.dumps(public));self.assertNotIn('owner',json.dumps(public))

    def test_outbound_articles_and_cards_respect_same_policy(self):
        for item in ({'title':'合成','body':'请私信我'}, {'title':'合成','body':'普通说明','cards':[{'title':'知识','body':'报价100元'}]}):
            with self.subTest(item=item),self.assertRaises(ValueError):policy.content(item)
        policy.content({'title':'订单表格整理','body':'按列名合并订单数据。','cards':[{'title':'字段','body':'价格列要保留数值格式。'}]})

    def test_post_generation_rejects_commerce_before_saving(self):
        self.w.llm.side_effect=lambda *a,**kw:{'variants':[{'platform':'xiaohongshu','title':'合成','body':'服务报价100元','tags':[],'cards':[{'title':'合成','body':'说明'}]}]}
        with self.assertRaises(ValueError):self.w.generate('合成主题')
        self.assertFalse(self.w.state()['drafts'])

    def test_no_old_own_comment_duplicate(self):
        self.comments.append({'id':'old-own','content':'已有建议','user_id':'owner','root_id':'old-own','parent_id':'','target_comment_id':'','target_user_id':''})
        pub.process_thread(self.w,self.thread);self.assertFalse(self.sent);self.assertEqual(self.turns()[0]['state'],'verified')
        self.assertTrue(json.loads(self.turns()[0]['proof'])['adopted'])

    def test_adopted_old_comment_keeps_watching_new_public_replies(self):
        self.comments.append({'id':'old-own','content':'已有建议','user_id':'owner','root_id':'old-own','parent_id':'','target_comment_id':'','target_user_id':''})
        self.comments.append({'id':'later-question','content':'表头怎么统一？','user_id':'visitor','root_id':'old-own','parent_id':'old-own','target_comment_id':'old-own','target_user_id':'owner'})
        pub.process_thread(self.w,self.thread)
        self.assertEqual(len(self.sent),1);self.assertEqual(self.sent[0][1]['comment_id'],'later-question')

    def test_sender_uses_changed_config_check(self):
        initial=pub.stamp(self.w)
        self.w.save_settings({'business':{'offer':'新的合成方向'}})
        with self.assertRaisesRegex(ValueError,'变化'):pub.submit(self.w,self.thread,{'id':999,'message_id':'initial'},'先统一列名。',{'comments':[]},initial)
        self.assertFalse(self.sent)

    def test_start_preflight_and_independent_comment_mode(self):
        self.w.save_settings({'business':{'keywords':['表格 求助']},'ai':{'base_url':'https://fixture.invalid/v1','model':'fixture','api_key':'fixture-key'}})
        with patch.object(self.w,'llm',return_value={'ok':True}),patch.object(self.w,'creator_check') as creator:
            pub.control(self.w,'comments_only')
            c=self.w.config();self.assertTrue(c['public']['send_enabled']);self.assertFalse(c['publishing']['enabled']);creator.assert_not_called()
            pub.control(self.w,'start');self.assertTrue(self.w.config()['publishing']['auto_approve']);creator.assert_called_once()

    def test_failed_model_probe_leaves_sending_disabled(self):
        self.w.save_settings({'business':{'keywords':['表格 求助']},'public':{'send_enabled':False},'ai':{'base_url':'https://fixture.invalid/v1','model':'fixture','api_key':'fixture-key'}})
        with patch.object(self.w,'llm',return_value={'ok':False}),self.assertRaises(ValueError):pub.control(self.w,'comments_only')
        self.assertFalse(self.w.config()['public']['send_enabled'])

    def test_previously_empty_source_is_discovered_after_detail_arrives(self):
        self.w.ingest([{'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/enrich?xsec_token=fixture','title':'合成原帖'}])
        pub.discover(self.w)
        with self.w.db() as db:before=db.execute('SELECT count(*) FROM public_threads').fetchone()[0]
        self.w.ingest([{'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/enrich?xsec_token=fixture-new','title':'合成原帖','excerpt':'表头怎么统一？'}],authoritative=True)
        pub.discover(self.w)
        with self.w.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM public_threads').fetchone()[0],before+1)

    def test_public_state_is_isolated_to_bound_account(self):
        pub.process_thread(self.w,self.thread)
        self.w.meta('xhs_bound_user','different-owner')
        result=pub.state(self.w);self.assertEqual(result['threads'],[]);self.assertEqual(result['turns'],[]);self.assertEqual(result['counts'],{})

    def test_owned_public_posts_rotate_and_join_unified_threads(self):
        self.w.save_settings({'public':{'own_comments_enabled':True,'own_posts_per_round':1}})
        feeds=[{'id':'own-a','xsecToken':'fixture-a'},{'id':'own-b','xsecToken':'fixture-b'}]
        seen=[]
        def own_read(w,t):
            seen.append(t['feed'])
            return {'feed_id':t['feed'],'owner_id':'owner','title':'本人知识','body':'合成知识','comments':[{'id':'foreign-'+t['feed'],'root_id':'foreign-'+t['feed'],'user_id':'visitor','content':'表头怎么统一？'}]}
        with patch.object(self.w,'xhs',return_value={'data':{'feeds':feeds}}),patch.object(pub,'read',side_effect=own_read):
            pub.discover_owned(self.w,'owner');pub.discover_owned(self.w,'owner')
        self.assertEqual(seen,['own-a','own-b'])
        with self.w.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM public_threads WHERE owner='owner'").fetchone()[0],2)

    def test_disabled_owned_comments_do_not_process_old_owned_threads(self):
        with self.w.db() as db:db.execute("UPDATE public_threads SET owner='owner'")
        self.w.save_settings({'public':{'own_comments_enabled':False,'discover_per_round':0}})
        pub.cycle(self.w)
        self.assertFalse(self.sent)

    def test_disabling_owned_comments_blocks_final_dispatch(self):
        thread={**self.thread,'owner':'owner'}
        self.assertFalse(pub.submit(self.w,thread,{'id':999,'message_id':'initial'},'先统一列名。',{'comments':[]},pub.stamp(self.w)))
        self.assertFalse(self.sent)

    def test_first_reply_on_own_post_uses_reply_allowance(self):
        self.w.save_settings({'public':{'own_comments_enabled':True,'comments_per_day':0,'replies_per_day':1}})
        self.comments.append({'id':'question','content':'表头怎么统一？','user_id':'visitor','root_id':'question','parent_id':'','target_comment_id':'','target_user_id':''})
        thread={**self.thread,'owner':'owner','target':'question'}
        with self.w.db() as db:db.execute("UPDATE public_threads SET owner='owner',target='question' WHERE id=?",(thread['id'],))
        with patch.object(pub,'read',side_effect=lambda w,t:{'feed_id':'feed','owner_id':'owner','body':'表头不一致怎么合并？','comments':[dict(c) for c in self.comments]}):pub.process_thread(self.w,thread)
        self.assertEqual(len(self.sent),1)
        with self.w.db() as db:self.assertEqual(db.execute("SELECT count(*) FROM usage WHERE kind='public_reply'").fetchone()[0],1)

    def test_commercial_legacy_draft_can_be_cancelled_but_never_approved(self):
        self.w.generate('合成模板',template=True)
        self.w.edit_draft({'id':1,'body':'报价100元','state':'draft'})
        with self.assertRaises(ValueError):self.w.edit_draft({'id':1,'state':'approved'})
        self.w.edit_draft({'id':1,'state':'cancelled'})
        self.assertEqual(self.w.draft(1)['state'],'cancelled')

    def test_second_model_review_cannot_insert_commercial_reply(self):
        import sales
        self.w.ingest([{'platform':'xiaohongshu','url':'https://www.xiaohongshu.com/explore/fixture-review','title':'合成问题','excerpt':'已经试过统一表头还是不行，怎么合并？'}])
        self.w.llm.side_effect=[{'reply':'你用的是哪个版本？'},{'reply':'报价100元我来做。'}]
        with self.assertRaises(ValueError):sales.draft_reply(self.w,1)
        with self.w.db() as db:self.assertEqual(db.execute('SELECT reply FROM leads WHERE id=1').fetchone()[0],'')

if __name__=='__main__':unittest.main()
