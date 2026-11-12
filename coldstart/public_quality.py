"""Conversation relevance and wording checks, separate from content policy."""
import re

HELP=re.compile(r'[？?]|求助|求教|求推荐|谁知道|有没有|怎么|如何|想找|不会|不懂|搞不定|卡住|报错|出错|不生效|还是不行|没(?:有)?效果|太麻烦|忙不过来|手动.{0,8}(?:累|烦|耗时)|每天.{0,12}(?:累|烦|耗时)')
BOILERPLATE=re.compile(r'痛点切入|完成度不错|流程拆解清晰|赋能|闭环|综上所述|感谢分享|干货满满|首先.{0,40}其次|可以从以下|建议从以下')
VISUAL=re.compile(r'红框|截图|看图|如图|图里|图中|见图|图片里|箭头处|圈出')

def visual_reason(text):
    if not re.search(r'[？?]',text) or re.search(r'点击|勾选|双击|选中|按下|打开.{0,8}(?:选项|菜单)|输入.{0,8}(?:等号|公式)',text):return '截图未读取，先问清决定解法的位置或信息，不猜点击路径'
    return ''

def initial_skip(thread,turn):
    if turn['message_id']!='initial':return ''
    owned=bool(thread.get('owner')) and thread.get('owner')==thread.get('account')
    if thread.get('target') and not owned and re.match(r'^\s*@\S+',turn['body']):return '原评论明确点名他人，不插入自动回复'
    if not thread.get('target') and not HELP.search(thread.get('title','')+'\n'+turn['body']):return '原帖没有待解问题或明确求助，不复述知识分享'
    return ''

def wording_reason(text,source,history):
    if text.count('？')+text.count('?')>1:return '一次只问一个必要问题，不连续追问'
    if BOILERPLATE.search(text):return '泛泛点评或模板式表达，需改为眼前问题的一条具体答复'
    if re.search(r'格式.{0,5}常规',source) and re.search(r'文本格式|改成常规|改为常规|设成常规|设为常规',text):return '重复或否定了对方已经核对的常规格式'
    if re.search(r'双击.{0,10}(?:不行|无效|没用)',source) and re.search(r'(?:先|再|可以|建议|试着).{0,12}双击|双击.{0,12}(?:回车|试试|试一下)',text):return '重复了对方已说明无效的双击操作'
    clean=lambda s:re.sub(r'\W+','',str(s)).lower()
    reply=clean(text)
    if len(reply)>=8 and any(clean(h.get('reply',''))==reply for h in history if h.get('state')=='verified'):return '重复了这段讨论已经给出的答复'
    original=clean(source)
    if len(reply)>=30:
        grams=[reply[i:i+3] for i in range(len(reply)-2)]
        if sum(g in original for g in grams)/len(grams)>.65:return '主要在复述原文，没有提供新的具体帮助'
    return ''
