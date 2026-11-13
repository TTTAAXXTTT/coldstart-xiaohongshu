"""Editable business limits. -1 means no local count ceiling; intervals use 0."""
FIELDS=[
 ('public','own_watch_minutes','本人作品评论监测间隔（分钟）','interval',5),
 ('public','own_posts_per_round','每轮读取的本人公开作品数','count',3),
 ('public','thread_display','显示公开讨论数','count',100),
 ('public','turn_display','显示公开评论记录数','count',100),
 ('public','watch_minutes','公开讨论监测间隔（分钟）','interval',5),
 ('public','search_minutes','公开互动搜索间隔（分钟）','interval',30),
 ('public','threads_per_round','每轮检查的公开讨论数','count',5),
 ('public','discover_per_round','每轮接入的公开来源数','count',20),
 ('public','replies_per_round','每段讨论每轮处理问题数','count',3),
 ('public','comments_per_day','每日主动评论次数','count',10),
 ('public','replies_per_day','每日公开续答次数','count',20),
 ('ai','calls_per_day','每天模型调用次数','count',8),
 ('ai','analysis_per_run','每次需求分析条数','count',8),
 ('ai','max_output_tokens','普通模型任务输出token上限','count',8000),
 ('radar','searches_per_day','每天搜索次数','count',12),
 ('radar','max_queries','每轮关键词数','count',2),
 ('radar','max_results','每个词读取的帖子数','count',10),
 ('radar','max_details','每轮读取的原帖详情数','count',4),
 ('radar','comments_per_post','每帖读取的评论数','count',20),
 ('radar','import_per_request','每次导入记录条数','count',100),
 ('publishing','max_posts_per_day','每天提交次数','count',2),
 ('publishing','approvals_per_cycle','每轮恢复待发排期条数','count',20),
 ('publishing','min_gap_minutes','提交间隔（分钟）','interval',120),
 ('autonomy','replies_per_day','每天自己作品评论回复次数','count',3),
 ('autonomy','replies_per_cycle','每轮自己作品评论处理条数','count',3),
 ('autonomy','notification_items','每轮读取评论通知条数','count',20),
 ('autonomy','max_pending_drafts','待处理内容积压上限','count',12),
 ('autonomy','generations_per_day','每天自动创作次数','count',1),
 ('autonomy','cycle_minutes','自动流程间隔（分钟）','interval',360),
 ('schedule','scan_minutes','定时搜索间隔（分钟）','interval',360),
 ('display','leads','显示线索条数','count',150),
 ('display','drafts','显示稿件条数','count',60),
 ('display','jobs','显示任务条数','count',40),
 ('display','events','显示日志条数','count',60),
 ('display','inbox','显示收到的评论条数','count',30),
]

def defaults(config):
 for section,key,label,kind,value in FIELDS:config.setdefault(section,{}).setdefault(key,value)

def validate(value,kind):
 if isinstance(value,bool) or not isinstance(value,(int,str)):raise ValueError('限制需要整数；次数填-1表示无限制')
 if isinstance(value,str) and not value.lstrip('-').isdigit():raise ValueError('限制需要整数')
 result=int(value)
 if result < (0 if kind=='interval' else -1):raise ValueError('次数应为-1或非负整数；间隔不能为负数')
 return result

def exhausted(used,limit):return limit>=0 and used>=limit
def take(values,limit):return values if limit<0 else values[:limit]
def remaining(limit,used):return -1 if limit<0 else max(0,limit-used)
def combine(a,b):return b if a<0 else a if b<0 else min(a,b)
def all_unlimited():
 patch={}
 for section,key,label,kind,value in FIELDS:patch.setdefault(section,{})[key]=0 if kind=='interval' else -1
 return patch

def public():return [{'section':s,'key':k,'label':label,'kind':kind} for s,k,label,kind,_ in FIELDS]

def analyze(w):
 from app import dump,now
 with w.db() as db:available=db.execute("SELECT COUNT(*) FROM leads WHERE platform='xiaohongshu' AND state='candidate' AND analyzed_at='' ").fetchone()[0]
 limit=w.config()['ai']['analysis_per_run'];target=available if limit<0 else min(available,limit);done=0
 w.meta('analysis_progress',dump({'processed':0,'total':target,'status':'running'}))
 try:
  while done<target and not w.stop.is_set() and w.meta('pause_requested')!='1':
   text=w._qualify_batch(min(8,target-done))
   import re
   match=re.search(r'完成 (\d+)',text);n=int(match[1]) if match else 0
   if not n:break
   done+=n;w.meta('analysis_progress',dump({'processed':done,'total':target,'status':'running'}))
 except Exception:
  w.meta('analysis_progress',dump({'processed':done,'total':target,'status':'needs_review'}));raise
 w.meta('analysis_progress',dump({'processed':done,'total':target,'status':'paused' if done<target and (w.stop.is_set() or w.meta('pause_requested')=='1') else 'done'}))
 return f'完成 {done} 条候选分析；购买意向仍需实际核实'
