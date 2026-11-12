// Coldstart session bridge. Uses the pinned upstream browser and session format.
// Login/check modes never upload. Publish mode requires an explicit local request file.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/go-rod/rod"
	"github.com/sirupsen/logrus"
	"github.com/xpzouying/xiaohongshu-mcp/browser"
	"github.com/xpzouying/xiaohongshu-mcp/configs"
	"github.com/xpzouying/xiaohongshu-mcp/cookies"
	"github.com/xpzouying/xiaohongshu-mcp/xiaohongshu"
)

type report struct {
	CommentReadback   json.RawMessage        `json:"comment_readback,omitempty"`
	BrowserPath       string                 `json:"browser_path,omitempty"`
	VerificationType  string                 `json:"verification_type,omitempty"`
	ContinuationProof map[string]interface{} `json:"continuation_proof,omitempty"`
	CreatorUserID     string                 `json:"creator_user_id,omitempty"`
	Version           string                 `json:"version"`
	Mode              string                 `json:"mode"`
	MainOK            bool                   `json:"main_ok"`
	CreatorReady      bool                   `json:"creator_ready"`
	UserID            string                 `json:"user_id,omitempty"`
	CheckedAt         string                 `json:"checked_at"`
	CookieSHA256      string                 `json:"cookie_sha256,omitempty"`
	Stage             string                 `json:"stage"`
	Detail            string                 `json:"detail"`
	Status            string                 `json:"status,omitempty"`
	InventoryLoaded   bool                   `json:"inventory_loaded,omitempty"`
	InventoryEmpty    bool                   `json:"inventory_empty,omitempty"`
	InventoryView     string                 `json:"inventory_view,omitempty"`
}

func allNotesAbsent(page *rod.Page, expected, title string) (bool, int, error) {
	checkID := func() bool {
		value, err := page.Eval(`() => { try { return JSON.parse(localStorage.getItem('USER_INFO_FOR_BIZ')||'null').userId || ''; } catch { return ''; } }`)
		return err == nil && value.Value.String() == expected
	}
	if !checkID() {
		return false, 0, fmt.Errorf("核对创作者账号失败")
	}
	menu, err := page.Timeout(10*time.Second).ElementR("a,span,div", "^(笔记管理|内容管理)$")
	if err != nil {
		return false, 0, err
	}
	if err := menu.Click("left", 1); err != nil {
		return false, 0, err
	}
	time.Sleep(5 * time.Second)
	first, err := page.Eval(`() => document.body.innerText`)
	if err != nil {
		return false, 0, err
	}
	if err := page.Reload(); err != nil {
		return false, 0, err
	}
	_ = page.WaitLoad()
	time.Sleep(5 * time.Second)
	second, err := page.Eval(`() => document.body.innerText`)
	if err != nil {
		return false, 0, err
	}
	firstTotal, firstOK := inventoryAbsence(first.Value.String(), title)
	secondTotal, secondOK := inventoryAbsence(second.Value.String(), title)
	if dir := os.Getenv("COLDSTART_DEBUG_DIR"); dir != "" {
		_ = os.WriteFile(filepath.Join(dir, "continuation-inventory-first.txt"), []byte(first.Value.String()), 0600)
		_ = os.WriteFile(filepath.Join(dir, "continuation-inventory-second.txt"), []byte(second.Value.String()), 0600)
	}
	return firstOK && secondOK && firstTotal == secondTotal && checkID(), secondTotal, nil
}

// Require a fully rendered small inventory, including review/rejected tabs.
// A missing title in a partially loaded/paginated list is never absence evidence.
func inventoryAbsence(text, title string) (int, bool) {
	if title == "" || strings.Contains(text, title) || !strings.Contains(text, "审核中") || !strings.Contains(text, "未通过") {
		return 0, false
	}
	matches := regexp.MustCompile(`(?m)^全部\s*(\d+)\s*$`).FindAllStringSubmatch(text, -1)
	if len(matches) != 1 {
		return 0, false
	}
	total, err := strconv.Atoi(matches[0][1])
	if err != nil {
		return 0, false
	}
	if total == 0 {
		return 0, strings.Contains(text, "没有找到相关笔记")
	}
	dates := regexp.MustCompile(`(?m)^20\d{2}-\d{2}-\d{2} \d{2}:\d{2}\s*$`).FindAllString(text, -1)
	return total, total <= 20 && len(dates) == total && !strings.Contains(text, "下一页")
}

func execute(r *report, seconds int, expected string, requestPath string) (exitCode int) {
	operationStarted := time.Now()
	defer func() {
		if recover() != nil {
			if r.Stage == "pre_submit" {
				r.Detail = "浏览器检查中断，未进入上传或发布"
			} else {
				r.Detail = "上传或提交过程中断，必须核对平台结果"
			}
			exitCode = 1
		}
	}()
	if r.Mode == "install-browser" {
		bin, err := browser.EnsureBrowser()
		if err != nil {
			r.Detail = err.Error()
			return 1
		}
		r.BrowserPath = bin
		r.Stage = "browser_ready"
		r.Detail = "浏览器依赖已准备；未打开账号页面或读取登录会话"
		return 0
	}
	store := cookies.NewLoadCookie(cookies.GetCookiesFilePath())
	seed := configs.ResolveFingerprintSeed(store)
	b := browser.NewBrowser(r.Mode != "login" && r.Mode != "publish", browser.WithFingerprintSeed(seed), browser.WithProxy(configs.ProxyFromEnv()))
	defer b.Close()
	p := b.NewPage()
	defer p.Close()
	storagePath := filepath.Join(filepath.Dir(cookies.GetCookiesFilePath()), "creator-storage.json")
	if data, err := os.ReadFile(storagePath); err == nil && json.Valid(data) {
		var storage map[string]interface{}
		if json.Unmarshal(data, &storage) == nil && storage["origin"] == "https://creator.xiaohongshu.com" {
			_, _ = p.EvalOnNewDocument(`(() => { const s = ` + string(data) + `; if(location.origin === s.origin){ for(const [k,v] of Object.entries(s.local || {})) localStorage.setItem(k,v); for(const [k,v] of Object.entries(s.session || {})) sessionStorage.setItem(k,v); } })()`)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(seconds)*time.Second)
	defer cancel()
	p = p.Context(ctx)
	login := xiaohongshu.NewLogin(p)
	mainOK, err := login.CheckLoginStatus(ctx)
	if err != nil || !mainOK {
		if r.Mode != "login" {
			r.Detail = "主站登录失效，请使用登录入口"
			return 2
		}
		fmt.Fprintln(os.Stderr, "请在浏览器中完成小红书主站登录。")
		if err := login.Login(ctx); err != nil {
			r.Detail = "等待主站登录超时"
			return 2
		}
	}
	user, err := login.CurrentUser(ctx)
	if err != nil || user.UserID == "" {
		r.Detail = "无法核对主站账号，未保存会话"
		return 2
	}
	if expected != "" && user.UserID != expected {
		r.Detail = "当前账号与工作台绑定身份不同，未保存会话"
		return 2
	}
	r.MainOK, r.UserID = true, user.UserID
	if r.Mode == "read-comments" {
		if err := readComments(ctx, p, r, requestPath, cookies.GetCookiesFilePath()); err != nil {
			r.Detail = err.Error()
			return 1
		}
		return 0
	}
	if err := p.Navigate("https://creator.xiaohongshu.com/?source=official"); err != nil {
		r.Detail = "无法打开创作者中心"
		return 1
	}
	_ = p.WaitLoad()
	if r.Mode == "login" {
		fmt.Fprintln(os.Stderr, "请在本窗口完成创作者中心登录。程序会保存会话并自动关闭；无需手动关闭窗口。")
	}
	for {
		if info, err := p.Info(); err == nil {
			if err == nil && strings.HasPrefix(info.URL, "https://creator.xiaohongshu.com/") && !strings.Contains(info.URL, "/login") {
				time.Sleep(2 * time.Second)
				break
			}
		}
		select {
		case <-ctx.Done():
			r.Detail = "创作者中心未授权；未上传或发布任何内容"
			return 2
		case <-time.After(500 * time.Millisecond):
		}
	}
	// The constructor opens the form but never uploads.
	action, err := xiaohongshu.NewPublishImageAction(p)
	if err != nil {
		r.Detail = "创作者中心发布页检查未通过；未上传或发布"
		return 2
	}
	info, err := p.Info()
	if err != nil || strings.Contains(info.URL, "/login") {
		r.Detail = "发布页登录校验未通过"
		return 2
	}
	creatorUser, err := p.Eval(`() => { try { const a = JSON.parse(localStorage.getItem('USER_INFO_FOR_BIZ') || 'null'); const b = JSON.parse(localStorage.getItem('USER_INFO') || 'null'); const first = a && a.userId; const second = b && b.user && b.user.value && b.user.value.userId; return first && second && first === second ? first : ''; } catch { return ''; } }`)
	if err != nil || creatorUser.Value.String() != r.UserID {
		r.Detail = "创作者中心账号与绑定主站身份不一致或无法核实，停止操作"
		return 2
	}
	r.CreatorUserID = creatorUser.Value.String()
	if r.Mode == "login" {
		storage, err := p.Eval(`() => JSON.stringify({origin:location.origin,local:Object.fromEntries(Object.keys(localStorage).map(k=>[k,localStorage.getItem(k)])),session:Object.fromEntries(Object.keys(sessionStorage).map(k=>[k,sessionStorage.getItem(k)]))})`)
		if err != nil {
			r.Detail = "无法保存创作者中心会话状态"
			return 1
		}
		// Verify the account once more after the creator login, before persisting.
		ok, err := login.CheckLoginStatus(ctx)
		if err != nil || !ok {
			r.Detail = "保存前主站校验失败，原会话保留"
			return 2
		}
		current, err := login.CurrentUser(ctx)
		if err != nil || current.UserID != r.UserID {
			r.Detail = "登录期间账号发生变化，原会话保留"
			return 2
		}
		cs, err := p.Browser().GetCookies()
		if err != nil {
			r.Detail = "无法读取会话"
			return 1
		}
		data, err := json.Marshal(cs)
		if err != nil || store.SaveCookies(data) != nil {
			r.Detail = "会话保存失败"
			return 1
		}
		if store.LoadSeed() != seed {
			r.Detail = "会话标识校验失败"
			return 1
		}
		temporaryStorage := storagePath + ".tmp"
		if err := os.WriteFile(temporaryStorage, []byte(storage.Value.String()), 0600); err != nil {
			r.Detail = "创作者会话写入失败"
			return 1
		}
		if err := os.Rename(temporaryStorage, storagePath); err != nil {
			r.Detail = "创作者会话替换失败"
			return 1
		}

	}
	data, err := os.ReadFile(cookies.GetCookiesFilePath())
	if err != nil {
		r.Detail = "无法校验会话文件"
		return 1
	}
	sum := sha256.Sum256(data)
	r.CookieSHA256 = hex.EncodeToString(sum[:])
	r.CreatorReady = true
	if r.Mode == "audit" {
		menu, err := p.Timeout(10*time.Second).ElementR("a,span,div", "^(笔记管理|内容管理)$")
		if err != nil {
			r.Detail = "未找到创作者中心内容管理入口"
			return 1
		}
		if err := menu.Click("left", 1); err != nil {
			r.Detail = "无法打开创作者中心内容管理"
			return 1
		}
		time.Sleep(5 * time.Second)
		first, _ := p.Eval(`() => document.body.innerText`)
		if err := p.Reload(); err != nil {
			r.Detail = "内容管理刷新失败"
			return 1
		}
		_ = p.WaitLoad()
		time.Sleep(5 * time.Second)
		second, _ := p.Eval(`() => document.body.innerText`)
		empty := func(text string) bool {
			return (strings.Contains(text, "全部 0") || strings.Contains(text, "全部0")) && strings.Contains(text, "没有找到相关笔记") && strings.Contains(text, "审核中") && strings.Contains(text, "未通过")
		}
		if first != nil && second != nil && empty(first.Value.String()) && empty(second.Value.String()) {
			r.InventoryLoaded = true
			r.InventoryEmpty = true
			r.InventoryView = "all"
		}
		debugDir := os.Getenv("COLDSTART_DEBUG_DIR")
		if debugDir != "" {
			_ = os.MkdirAll(debugDir, 0700)
			if screen, e := p.Screenshot(false, nil); e == nil {
				_ = os.WriteFile(filepath.Join(debugDir, "creator-inventory.png"), screen, 0600)
			}
			if text, e := p.Context(context.Background()).Timeout(5 * time.Second).Eval(`() => document.body.innerText`); e == nil {
				_ = os.WriteFile(filepath.Join(debugDir, "creator-inventory.txt"), []byte(text.Value.String()), 0600)
			}
		}
		r.Detail = "已打开绑定账号的创作者内容管理，未上传或发布"
		return 0
	}
	if r.Mode == "publish" || r.Mode == "prepare" {
		raw, err := os.ReadFile(requestPath)
		if err != nil || len(raw) > 1024*1024 {
			r.Detail = "无法读取已批准内容的提交快照"
			return 1
		}
		var req struct {
			Title   string   `json:"title"`
			Content string   `json:"content"`
			Images  []string `json:"images"`
			Tags    []string `json:"tags"`
		}
		if json.Unmarshal(raw, &req) != nil || req.Title == "" || req.Content == "" || len(req.Images) == 0 || len(req.Images) > 9 {
			r.Detail = "提交快照校验失败"
			return 1
		}
		r.Stage = "uploading"
		ctx = context.WithValue(ctx, xiaohongshu.PublishPhaseKey{}, func(phase string) { r.Stage = phase })
		ctx = context.WithValue(ctx, xiaohongshu.VerificationHookKey{}, func(kind string) {
			r.Stage = "verification_required"
			r.VerificationType = kind
			r.CheckedAt = time.Now().Format(time.RFC3339)
			r.Detail = "请在可见 Chromium 窗口使用已登录该账号的小红书 App 扫码验证；验证后继续原操作，不重复点击发布"
			if kind == "sms" {
				r.Detail = "平台要求绑定手机的短信验证码，请在原 Chromium 窗口按平台提示完成验证；程序保留当前操作，不重复点击发布"
			}
			if debugDir := os.Getenv("COLDSTART_DEBUG_DIR"); debugDir != "" {
				_ = os.MkdirAll(debugDir, 0700)
				if data, e := json.Marshal(r); e == nil {
					_ = os.WriteFile(filepath.Join(debugDir, "progress.json"), data, 0600)
				}
				if shot, e := p.Screenshot(false, nil); e == nil {
					_ = os.WriteFile(filepath.Join(debugDir, "creator-verification.png"), shot, 0600)
				}
			}
			fmt.Fprintln(os.Stderr, r.Detail)
		})
		ctx = context.WithValue(ctx, xiaohongshu.VerificationContinueKey{}, func() (bool, error) {
			progress := func(stage, detail string) {
				r.Stage = stage
				r.CheckedAt = time.Now().Format(time.RFC3339)
				r.Detail = detail
				if dir := os.Getenv("COLDSTART_DEBUG_DIR"); dir != "" {
					if data, e := json.Marshal(r); e == nil {
						_ = os.WriteFile(filepath.Join(dir, "progress.json"), data, 0600)
					}
				}
			}
			progress("verification_passed", "身份验证已完成，正在核对原内容是否已提交，请保留窗口")
			if remaining := 3*time.Minute - time.Since(operationStarted); remaining > 0 {
				select {
				case <-time.After(remaining):
				case <-ctx.Done():
					return false, ctx.Err()
				}
			}
			snapshot, err := p.Eval(`() => JSON.stringify({origin:location.origin,local:Object.fromEntries(Object.keys(localStorage).map(k=>[k,localStorage.getItem(k)])),session:Object.fromEntries(Object.keys(sessionStorage).map(k=>[k,sessionStorage.getItem(k)]))})`)
			if err != nil {
				return false, err
			}
			check := b.NewPage().Context(ctx)
			defer check.Close()
			if _, err := check.EvalOnNewDocument(`(() => { const s = ` + snapshot.Value.String() + `; if(location.origin === 'https://creator.xiaohongshu.com'){ for(const [k,v] of Object.entries(s.session||{})) sessionStorage.setItem(k,v); } })()`); err != nil {
				return false, err
			}
			if err := check.Navigate("https://creator.xiaohongshu.com/?source=official"); err != nil {
				return false, err
			}
			_ = check.WaitLoad()
			time.Sleep(2 * time.Second)
			absent, total, err := allNotesAbsent(check, r.UserID, req.Title)
			if err != nil || !absent {
				progress("verification_review", "验证已通过，但无法确认这篇原稿未提交；保持原窗口，需核对具体作品")
				return false, nil
			}
			form, err := p.Eval(`(expected) => { const norm=s=>String(s||'').replace(/\s+/g,' ').trim(); const title=document.querySelector('input[placeholder="填写标题会有更多赞哦"]'); const body=document.querySelector('[contenteditable=true]'); return title && body && title.value===expected.title && norm(body.innerText).startsWith(norm(expected.content)); }`, map[string]string{"title": req.Title, "content": req.Content})
			if err != nil || !form.Value.Bool() {
				return false, fmt.Errorf("验证期间原稿内容变化，停止继续提交")
			}
			identity := xiaohongshu.NewLogin(check)
			ok, err := identity.CheckLoginStatus(ctx)
			if err != nil || !ok {
				return false, fmt.Errorf("保存验证会话前主站身份校验失败")
			}
			current, err := identity.CurrentUser(ctx)
			if err != nil || current.UserID != r.UserID {
				return false, fmt.Errorf("验证期间主站账号发生变化")
			}
			if cs, err := p.Browser().GetCookies(); err == nil {
				if data, err := json.Marshal(cs); err == nil {
					if store.SaveCookies(data) != nil || store.LoadSeed() != seed {
						return false, fmt.Errorf("保存验证会话失败")
					}
				}
			}
			if err := os.WriteFile(storagePath+".tmp", []byte(snapshot.Value.String()), 0600); err != nil {
				return false, err
			}
			if err := os.Rename(storagePath+".tmp", storagePath); err != nil {
				return false, err
			}
			r.ContinuationProof = map[string]interface{}{"type": "same_account_complete_inventory_twice_specific_title_absent_after_verification", "checked_at": time.Now().Format(time.RFC3339), "user_id": r.UserID, "view": "all", "total": total, "title": req.Title, "continuations": 1}
			if dir := os.Getenv("COLDSTART_DEBUG_DIR"); dir != "" {
				if data, e := json.Marshal(r.ContinuationProof); e == nil {
					_ = os.WriteFile(filepath.Join(dir, "continuation-proof.json"), data, 0600)
				}
			}
			fmt.Fprintln(os.Stderr, "同一账号完整笔记管理两次刷新均无本篇原稿，继续原页面一次提交；保留核对证据")
			// The original form stays intact. Continue once only after specific absence in two complete inventories.
			r.Stage = "final_click_started"
			if dir := os.Getenv("COLDSTART_DEBUG_DIR"); dir != "" {
				if data, e := json.Marshal(r); e == nil {
					_ = os.WriteFile(filepath.Join(dir, "progress.json"), data, 0600)
				}
			}
			if err := xiaohongshu.ContinueVerifiedPublish(p); err != nil {
				return false, err
			}
			return true, nil
		})
		ctx = context.WithValue(ctx, xiaohongshu.BeforePublishKey{}, func() error {
			ready, reason, err := xiaohongshu.InspectPublishControl(p)
			if err != nil || !ready {
				if info, e := p.Eval(`() => JSON.stringify(Array.from(document.querySelectorAll('button,[role="button"],xhs-publish-btn')).filter(e => (e.innerText || e.textContent || '').trim()==='发布' || e.tagName==='XHS-PUBLISH-BTN').map(e => ({tag:e.tagName,class:e.className,role:e.getAttribute('role'),disabled:e.getAttribute('disabled'),'aria-disabled':e.getAttribute('aria-disabled'),'is-publish':e.getAttribute('is-publish'),'submit-disabled':e.getAttribute('submit-disabled')})))`); e == nil {
					if dir := os.Getenv("COLDSTART_DEBUG_DIR"); dir != "" {
						_ = os.MkdirAll(dir, 0700)
						_ = os.WriteFile(filepath.Join(dir, "publish-control.json"), []byte(info.Value.String()), 0600)
					}
				}
				return fmt.Errorf("最终发布控件未通过可用性检查: %s", reason)
			}

			if debugDir := os.Getenv("COLDSTART_DEBUG_DIR"); debugDir != "" {
				_ = os.MkdirAll(debugDir, 0700)
				if screen, e := p.Screenshot(false, nil); e == nil {
					_ = os.WriteFile(filepath.Join(debugDir, "before-publish.png"), screen, 0600)
				}
			}
			if r.Mode == "prepare" {
				r.Stage = "prepared"
				return errors.New("coldstart: prepared without publish click")
			}
			r.Stage = "final_click_started"
			return nil
		})
		if err := action.Publish(ctx, xiaohongshu.PublishImageContent{Title: req.Title, Content: req.Content, ImagePaths: req.Images, Tags: req.Tags}); err != nil {
			if r.Stage == "prepared" {
				r.Status = "prepared"
				r.Detail = "真实上传、标题、正文与标签准备通过，未调用发布按钮"
				return 0
			}
			r.Detail = "平台阶段 " + r.Stage + ": " + err.Error()
			fmt.Fprintln(os.Stderr, r.Detail)
			if debugDir := os.Getenv("COLDSTART_DEBUG_DIR"); debugDir != "" {
				_ = os.MkdirAll(debugDir, 0700)
				if screen, e := p.Context(context.Background()).Timeout(5*time.Second).Screenshot(false, nil); e == nil {
					_ = os.WriteFile(filepath.Join(debugDir, "creator-failure.png"), screen, 0600)
				}
				if text, e := p.Context(context.Background()).Timeout(5 * time.Second).Eval(`() => document.body.innerText`); e == nil {
					_ = os.WriteFile(filepath.Join(debugDir, "creator-visible-error.txt"), []byte(text.Value.String()), 0600)
				}
			}
			return 1
		}
		// Preserve the same account's refreshed creator state after identity verification.
		if creator, e := p.Eval(`() => { try { return JSON.parse(localStorage.getItem('USER_INFO_FOR_BIZ') || 'null').userId || ''; } catch { return ''; } }`); e == nil && creator.Value.String() == r.UserID {
			if storage, e := p.Eval(`() => JSON.stringify({origin:location.origin,local:Object.fromEntries(Object.keys(localStorage).map(k=>[k,localStorage.getItem(k)])),session:Object.fromEntries(Object.keys(sessionStorage).map(k=>[k,sessionStorage.getItem(k)]))})`); e == nil {
				if cs, e := p.Browser().GetCookies(); e == nil {
					if data, e := json.Marshal(cs); e == nil {
						if store.SaveCookies(data) == nil && store.LoadSeed() == seed {
							_ = os.WriteFile(storagePath+".tmp", []byte(storage.Value.String()), 0600)
							_ = os.Rename(storagePath+".tmp", storagePath)
						}
					}
				}
			}
		}
		if debugDir := os.Getenv("COLDSTART_DEBUG_DIR"); debugDir != "" {
			if shot, e := p.Screenshot(false, nil); e == nil {
				_ = os.WriteFile(filepath.Join(debugDir, "creator-success.png"), shot, 0600)
			}
		}
		r.Status = "submitted"
		r.Stage = "submitted"
		r.Detail = "适配器完成提交，作品链接与审核状态仍需核对"
		return 0
	}
	r.Detail = "主站身份与创作者中心发布页通过；未上传或发布内容"
	return 0
}

func main() {
	mode := flag.String("mode", "check", "login, check, publish or install-browser")
	seconds := flag.Int("timeout", 60, "maximum seconds")
	expected := flag.String("expected-user", "", "bound main-site user ID")
	requestPath := flag.String("request-file", "", "approved local publish request")
	output := flag.String("result-file", "", "optional local JSON result")
	flag.Parse()
	if (*mode != "check" && *mode != "login" && *mode != "publish" && *mode != "prepare" && *mode != "audit" && *mode != "install-browser" && *mode != "read-comments") || *seconds < 15 || *seconds > 1800 {
		os.Exit(2)
	}
	logrus.SetOutput(os.Stderr)
	r := report{Version: "0.2.2", Mode: *mode, Stage: "pre_submit"}
	code := execute(&r, *seconds, *expected, *requestPath)
	r.CheckedAt = time.Now().Format(time.RFC3339)
	data, _ := json.Marshal(r)
	if *output != "" {
		if err := os.WriteFile(*output, data, 0600); err != nil {
			code = 1
		}
	}
	fmt.Println(string(data))
	os.Exit(code)
}
