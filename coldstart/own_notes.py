"""Import only independently verified own posts; verify actual reply readback."""
import json,time,tempfile
from pathlib import Path
from urllib.parse import quote
import validation

def tree(items):
 for item in items:
  yield item
  yield from tree(item.get('subComments') or [])

def feed_for(w,account,feed_id):
 from autopilot import current_xhs_account
 if current_xhs_account(w)!=account:raise ValueError('账号已变化')
 profile=w.xhs('/api/v1/user/me').get('data') or {}
 feed=next((f for f in profile.get('feeds') or [] if str(f.get('id'))==feed_id and f.get('xsecToken')),None)
 if not feed:raise ValueError('当前本人主页未返回这篇作品，不导入或猜测归属')
 return feed

def read_own(w,account,feed,comments=False):
 result=w.xhs('/api/v1/feeds/detail',{'feed_id':feed['id'],'xsec_token':feed['xsecToken'],'load_all_comments':comments,'comment_config':{'click_more_replies':True,'max_replies_threshold':2147483647,'max_comment_items':2147483647,'scroll_speed':'normal'}}).get('data') or {}
 note=result.get('note') or {}
 if str(note.get('noteId',''))!=str(feed['id']) or str((note.get('user') or {}).get('userId',''))!=account:raise ValueError('原作品身份未与当前绑定账号匹配')
 return result

def import_own(w,feed_id):
 from app import now,dump
 from autopilot import current_xhs_account
 account=current_xhs_account(w);feed=feed_for(w,account,feed_id);detail=read_own(w,account,feed);note=detail['note']
 title=validation.text(note.get('title'),'作品标题',200)
 body=validation.text(note.get('desc'),'作品正文',20000)
 url='https://www.xiaohongshu.com/explore/'+quote(feed_id,safe='')
 stamp=validation.source_time(note.get('time')) or now()
 with w.db() as db:
  db.execute('BEGIN IMMEDIATE')
  existing=db.execute('SELECT id FROM drafts WHERE public_url=?',(url,)).fetchone()
  if existing:return {'id':existing[0],'already_imported':True,'url':url}
  ident=db.execute("INSERT INTO drafts(platform,title,body,tags,cards,assets,state,generator,scheduled_at,receipt,public_url,created_at,updated_at) VALUES('xiaohongshu',?,?, '[]','[]','[]','published','imported',?,?,?, ?,?)",(title,body,stamp,'已核对本人作品：账号、作品ID和完整原文匹配；从既有作品接入评论',url,now(),now())).lastrowid
 proof={'draft_id':ident,'feed_id':feed_id,'account_verified':True,'title_verified':True,'body_read':True,'checked_at':now(),'url':url}
 w.meta('own_import:'+str(ident),dump(proof));return proof

def verify_reply(w,account,row,text):
 feed=feed_for(w,account,row['feed_id'])
 return relation_proof(native_comments(w,account,feed,row['comment_id']),account,row,text)

def native_comments(w,account,feed,comment_id):
 import xhs_session
 from app import dump
 with tempfile.TemporaryDirectory(prefix='comment-readback-',dir=w.data) as directory:
  request=Path(directory)/'request.json';request.write_text(dump({'feed_id':feed['id'],'xsec_token':feed['xsecToken'],'comment_id':comment_id}),encoding='utf-8')
  value=xhs_session.invoke(bound_user=account,mode='read-comments',request_file=request,cancel=w.stop)
 data=value.get('comment_readback') or {}
 if data.get('feed_id')!=feed['id'] or data.get('owner_id')!=account or not isinstance(data.get('comments'),list):raise ValueError('原始评论回查账号或作品不匹配')
 return data

def own_replies(data,account,row):
 if data.get('feed_id')!=row['feed_id'] or data.get('owner_id')!=account:raise ValueError('评论关系的作品归属不匹配')
 parents=[c for c in data.get('comments') or [] if c.get('id')==row['comment_id']]
 if len(parents)!=1:raise ValueError('原始评论回查未唯一匹配目标评论；不自动重发')
 return [c for c in data['comments'] if c.get('target_comment_id')==row['comment_id'] and c.get('root_id')==parents[0].get('root_id') and c.get('user_id')==account and c.get('target_user_id')==parents[0].get('user_id')]

def relation_proof(data,account,row,text):
 from app import now
 replies=[c for c in own_replies(data,account,row) if c.get('content')==text]
 if len(replies)!=1:raise ValueError('未回读到明确回复该评论的本人原文；保持未知，不自动重发')
 return {'parent_comment_id':row['comment_id'],'reply_id':replies[0]['id'],'text_checked':True,'author_checked':True,'target_relationship_checked':True,'checked_at':now(),'url':'https://www.xiaohongshu.com/explore/'+row['feed_id']}
