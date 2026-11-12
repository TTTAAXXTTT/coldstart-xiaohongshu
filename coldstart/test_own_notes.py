"""Synthetic fixtures: ownership, idempotent imports, and independently read replies."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import app,own_notes

class OwnNotesTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'qa');self.w=app.Workbench(self.temp.name);self.w.meta('xhs_bound_user','fixture-owner')
  self.note={'noteId':'fixture-note','title':'合成作品','desc':'这是既有作品的完整原文。','user':{'userId':'fixture-owner'},'time':1794366000000}
  self.feeds=[{'id':'fixture-note','xsecToken':'fixture-private-token'}];self.comments=[{'id':'parent','subComments':[{'id':'reply','content':'合成回复','userInfo':{'userId':'fixture-owner'}}]}]
  self.calls=[]
  native=patch('xhs_session.invoke',side_effect=self.native_fixture);native.start();self.addCleanup(native.stop)
 def native_fixture(self,**kwargs):
  rows=[]
  def walk(items,root='',parent=''):
   for item in items:
    rid=root or item['id'];rows.append({'id':item['id'],'content':item.get('content',''),'user_id':(item.get('userInfo') or {}).get('userId','visitor'),'root_id':rid,'target_comment_id':item.get('target_comment_id',parent),'target_user_id':item.get('target_user_id','visitor')});walk(item.get('subComments') or [],rid,item['id'])
  walk(self.comments)
  return {'comment_readback':{'feed_id':'fixture-note','owner_id':'fixture-owner','comments':rows}}
 def tearDown(self):self.temp.cleanup()
 def remote(self,path,payload=None,runtime_config=None):
  self.calls.append(path)
  if path=='/api/v1/login/status':return {'is_logged_in':True,'user_id':'fixture-owner'}
  if path=='/api/v1/user/me':return {'data':{'feeds':self.feeds}}
  if path=='/api/v1/feeds/detail':return {'data':{'note':self.note,'comments':{'list':self.comments}}}
  raise AssertionError(path)
 def test_import_verifies_account_body_and_is_idempotent_without_reposting(self):
  with patch.object(self.w,'xhs',side_effect=self.remote):
   proof=own_notes.import_own(self.w,'fixture-note');second=own_notes.import_own(self.w,'fixture-note')
  rows=self.w.state()['drafts'];self.assertEqual(len(rows),1);row=rows[0]
  self.assertEqual((row['state'],row['generator'],row['body']),('published','imported',self.note['desc']))
  self.assertEqual(json.loads(row['assets']),[]);self.assertTrue(second['already_imported']);self.assertEqual(second['id'],proof['draft_id'])
  self.assertNotIn('fixture-private-token',self.w.meta('own_import:'+str(row['id'])))
  self.assertFalse(any('publish' in p for p in self.calls));self.assertTrue(proof['account_verified'])
  with self.assertRaises(ValueError):self.w.edit_draft({'id':row['id'],'state':'approved'})
 def test_missing_profile_note_foreign_owner_or_mismatched_id_never_imported(self):
  for mode in ('missing','foreign','wrong_id'):
   with self.subTest(mode=mode),patch.object(self.w,'xhs',side_effect=self.remote):
    self.feeds=[] if mode=='missing' else [{'id':'fixture-note','xsecToken':'fixture-token'}]
    self.note['user']['userId']='foreign' if mode=='foreign' else 'fixture-owner';self.note['noteId']='different' if mode=='wrong_id' else 'fixture-note'
    with self.assertRaises(ValueError):own_notes.import_own(self.w,'fixture-note')
   self.assertEqual(len(self.w.state()['drafts']),0)
 def test_reply_requires_exact_nested_parent_author_and_text(self):
  with patch.object(self.w,'xhs',side_effect=self.remote):proof=own_notes.verify_reply(self.w,'fixture-owner',{'feed_id':'fixture-note','comment_id':'parent'},'合成回复')
  self.assertEqual(proof['reply_id'],'reply');self.assertTrue(proof['author_checked']);self.assertTrue(proof['text_checked'])
  for mode in ('wrong_author','wrong_text','other_parent','duplicate'):
   replies=[{'id':'reply','content':'合成回复','userInfo':{'userId':'fixture-owner'}}]
   if mode=='wrong_author':replies[0]['userInfo']['userId']='visitor'
   if mode=='wrong_text':replies[0]['content']='不同回复'
   if mode=='duplicate':replies.append({**replies[0],'id':'second'})
   self.comments=[{'id':'different' if mode=='other_parent' else 'parent','subComments':replies}]
   with self.subTest(mode=mode),patch.object(self.w,'xhs',side_effect=self.remote),patch.object(self.w.stop,'wait',return_value=False):
    with self.assertRaisesRegex(ValueError,'不自动重发'):own_notes.verify_reply(self.w,'fixture-owner',{'feed_id':'fixture-note','comment_id':'parent'},'合成回复')
 def test_import_job_rejects_invalid_identifiers_before_network(self):
  with self.assertRaises(ValueError):self.w.enqueue('import_own',{'feed_id':'https://foreign.example/a'})

 def relation(self):
  return {'feed_id':'fixture-note','owner_id':'fixture-owner','comments':[{'id':'parent','user_id':'visitor','root_id':'root'},{'id':'reply','user_id':'fixture-owner','root_id':'root','target_comment_id':'parent','target_user_id':'visitor','content':'合成回复'}]}
 def test_flat_nested_reply_uses_exact_target_relation(self):
  self.comments=[{'id':'root','subComments':[{'id':'parent','subComments':[]},{'id':'reply','content':'合成回复','userInfo':{'userId':'fixture-owner'}}]}]
  with patch.object(self.w,'xhs',side_effect=self.remote),patch.object(own_notes,'native_comments',return_value=self.relation()) as raw:
   proof=own_notes.verify_reply(self.w,'fixture-owner',{'feed_id':'fixture-note','comment_id':'parent'},'合成回复')
  raw.assert_called_once();self.assertTrue(proof['target_relationship_checked']);self.assertEqual(proof['reply_id'],'reply')
 def test_top_level_same_text_reply_to_another_child_is_not_proof(self):
  self.comments[0]['subComments'][0]['target_comment_id']='another-child'
  with patch.object(self.w,'xhs',side_effect=self.remote),self.assertRaisesRegex(ValueError,'不自动重发'):
   own_notes.verify_reply(self.w,'fixture-owner',{'feed_id':'fixture-note','comment_id':'parent'},'合成回复')
 def test_nested_wrong_target_author_or_thread_never_verified(self):
  for key,value in [('target_comment_id','different'),('target_user_id','different'),('root_id','different'),('user_id','visitor'),('content','different')]:
   data=self.relation();data['comments'][1][key]=value
   with self.subTest(key=key),self.assertRaises(ValueError):own_notes.relation_proof(data,'fixture-owner',{'feed_id':'fixture-note','comment_id':'parent'},'合成回复')
 def test_native_readback_request_is_temporary_and_secret_is_not_in_proof(self):
  calls=[]
  def invoke(**kwargs):
   calls.append(kwargs);request=json.loads(Path(kwargs['request_file']).read_text(encoding='utf-8'));self.assertEqual(request['comment_id'],'parent');self.assertEqual(request['xsec_token'],'fixture-private-token');return {'comment_readback':self.relation()}
  with patch('xhs_session.invoke',side_effect=invoke):data=own_notes.native_comments(self.w,'fixture-owner',self.feeds[0],'parent')
  self.assertFalse(Path(calls[0]['request_file']).exists());self.assertEqual(calls[0]['mode'],'read-comments');self.assertNotIn('fixture-private-token',json.dumps(data))

if __name__=='__main__':unittest.main()
