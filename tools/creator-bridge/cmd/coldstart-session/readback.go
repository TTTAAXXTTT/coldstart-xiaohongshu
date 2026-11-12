package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/go-rod/rod"
	"github.com/xpzouying/xiaohongshu-mcp/xiaohongshu"
	"os"
	"regexp"
)

// Read-only mode retains target-comment relationships omitted by the upstream
// HTTP serializer. It never invokes comment or publishing actions.
func readComments(ctx context.Context, p *rod.Page, r *report, requestPath, cookiePath string) error {
	raw, err := os.ReadFile(requestPath)
	if err != nil || len(raw) > 16384 {
		return fmt.Errorf("评论回查请求不可读")
	}
	var req struct {
		FeedID    string `json:"feed_id"`
        MaxComments int `json:"max_comments"`
		Token     string `json:"xsec_token"`
		CommentID string `json:"comment_id"`
	}
	if json.Unmarshal(raw, &req) != nil || !validCommentRequest(req.FeedID,req.CommentID,req.Token) {
		return fmt.Errorf("评论回查请求无效")
	}
	_, err = xiaohongshu.NewFeedDetailAction(p).GetFeedDetail(ctx, req.FeedID, req.Token, true, xiaohongshu.CommentLoadConfig{ClickMoreReplies: true, MaxRepliesThreshold: 2147483647, MaxCommentItems: func() int { if req.MaxComments > 0 { return req.MaxComments }; return 2147483647 }(), ScrollSpeed: "normal"})
	if err != nil {
		return err
	}
	value, err := p.Eval(`arg=>{const id=arg.feed_id,cid=arg.comment_id;const d=window.__INITIAL_STATE__?.note?.noteDetailMap?.[id];if(!d?.note||d.note.noteId!==id)throw new Error('note mismatch');const out=[];const walk=(items,root,parent)=>{for(const c of items||[]){const rid=root||c.id,t=c.targetComment||c.target_comment;out.push({id:c.id,content:c.content,user_id:c.userInfo?.userId,create_time:c.createTime,root_id:rid,parent_id:parent||'',target_comment_id:t?.id||'',target_user_id:t?.userInfo?.userId||''});walk(c.subComments,rid,c.id)}};walk(d.comments?.list);return JSON.stringify({feed_id:id,owner_id:d.note.user?.userId,title:d.note.title||'',body:d.note.desc||'',published_at:d.note.time,comments:out.filter(c=>!cid||c.id===cid||c.target_comment_id===cid||(!c.target_comment_id&&c.parent_id===cid)),has_more:d.comments?.hasMore===true})}`, map[string]string{"feed_id": req.FeedID, "comment_id": req.CommentID})
	if err != nil {
		return err
	}
	r.CommentReadback = json.RawMessage(value.Value.String())
	if !json.Valid(r.CommentReadback) {
		return fmt.Errorf("原始评论关系无法读取")
	}
	data, err := os.ReadFile(cookiePath)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(data)
	r.CookieSHA256 = hex.EncodeToString(sum[:])
	r.Stage = "comments_read"
	r.Detail = "已独立读取原帖评论关系，未执行发送或发布"
	return nil
}

func validCommentRequest(feed,comment,token string) bool {
 id:=regexp.MustCompile(`^[a-f0-9]{24}$`)
 return id.MatchString(feed) && (comment=="" || id.MatchString(comment)) && token!="" && len(token)<=4096
}
