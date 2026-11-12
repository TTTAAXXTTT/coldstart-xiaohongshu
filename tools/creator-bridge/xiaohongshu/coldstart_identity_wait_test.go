package xiaohongshu

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/go-rod/rod"
	"github.com/go-rod/rod/lib/launcher"
	"github.com/go-rod/rod/lib/proto"
)

func coldstartLocalPage(t *testing.T, html string) *rod.Page {
	t.Helper()
	bin := os.Getenv("COLDSTART_TEST_BROWSER_BIN")
	if bin == "" {
		t.Skip("Set the explicit installed test-browser path to run local-browser tests")
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if r.URL.Path == "/done" {
			_, _ = w.Write([]byte("done"))
			return
		}
		_, _ = w.Write([]byte(html))
	}))
	t.Cleanup(server.Close)
	launch := launcher.New().Bin(bin).Headless(true)
	url, err := launch.Launch()
	if err != nil {
		t.Fatal(err)
	}
	browser := rod.New().ControlURL(url)
	if err := browser.Connect(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = browser.Close(); launch.Cleanup() })
	page, err := browser.Page(proto.TargetCreateTarget{URL: server.URL + "/publish/publish"})
	if err != nil {
		t.Fatal(err)
	}
	if err := page.WaitLoad(); err != nil {
		t.Fatal(err)
	}
	return page
}

func TestColdstartIdentityWaitPreservesOriginalAction(t *testing.T) {
	page := coldstartLocalPage(t, `<body>扫码验证 为保护账号安全 请使用小红书APP扫码<script>setTimeout(()=>location.href='/done',1800)</script></body>`)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var notified atomic.Int32
	ctx = context.WithValue(ctx, VerificationHookKey{}, func(kind string) {
		if kind != "qr" {
			t.Errorf("Expected QR challenge, got %q", kind)
		}
		notified.Add(1)
	})
	if err := waitPublishSuccessWithContext(ctx, page, 200*time.Millisecond); err != nil {
		t.Fatal(err)
	}
	if notified.Load() != 1 {
		t.Fatalf("Expected one identity handoff, got %d", notified.Load())
	}
}

func TestColdstartUnrelatedTextDoesNotExtendPublishWait(t *testing.T) {
	page := coldstartLocalPage(t, `<body>扫码验证 只是无关说明文字</body>`)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var notified atomic.Int32
	ctx = context.WithValue(ctx, VerificationHookKey{}, func(string) { notified.Add(1) })
	started := time.Now()
	if err := waitPublishSuccessWithContext(ctx, page, 200*time.Millisecond); err == nil {
		t.Fatal("Unconfirmed publication must fail")
	}
	if notified.Load() != 0 || time.Since(started) > 2*time.Second {
		t.Fatal("Unrelated text triggered an identity handoff")
	}
}

func TestColdstartIdentityWaitRespectsCancellation(t *testing.T) {
	page := coldstartLocalPage(t, `<body>扫码验证 为保护账号安全 请使用小红书APP扫码</body>`)
	ctx, cancel := context.WithTimeout(context.Background(), 1200*time.Millisecond)
	defer cancel()
	started := time.Now()
	if err := waitPublishSuccessWithContext(ctx, page, 200*time.Millisecond); err == nil {
		t.Fatal("Unresolved identity check must not be reported as published")
	}
	if time.Since(started) > 3*time.Second {
		t.Fatal("Identity wait ignored its deadline")
	}
}

func TestColdstartSMSWaitPreservesOriginalAction(t *testing.T) {
	page := coldstartLocalPage(t, `<body>短信验证码验证 验证码已发送至 +86 fixture-phone<script>setTimeout(()=>location.href='/done',1800)</script></body>`)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var notified atomic.Int32
	ctx = context.WithValue(ctx, VerificationHookKey{}, func(kind string) {
		if kind != "sms" {
			t.Errorf("Expected SMS challenge, got %q", kind)
		}
		notified.Add(1)
	})
	if err := waitPublishSuccessWithContext(ctx, page, 200*time.Millisecond); err != nil {
		t.Fatal(err)
	}
	if notified.Load() != 1 {
		t.Fatalf("Expected one SMS handoff, got %d", notified.Load())
	}
}

func TestColdstartUnrelatedSMSDescriptionDoesNotExtendWait(t *testing.T) {
	page := coldstartLocalPage(t, `<body>短信验证码验证功能介绍</body>`)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	var notified atomic.Int32
	ctx = context.WithValue(ctx, VerificationHookKey{}, func(string) { notified.Add(1) })
	started := time.Now()
	if err := waitPublishSuccessWithContext(ctx, page, 200*time.Millisecond); err == nil {
		t.Fatal("Unconfirmed publication must fail")
	}
	if notified.Load() != 0 || time.Since(started) > 2*time.Second {
		t.Fatal("Unrelated SMS text triggered an identity handoff")
	}
}

func TestColdstartVerifiedIdentityContinuesOnceWhenOriginalPageDoesNotNavigate(t *testing.T) {
	page := coldstartLocalPage(t, `<body>扫码验证 为保护账号安全 请使用小红书APP扫码<script>setTimeout(()=>document.body.innerText='验证成功，仍在编辑页',600)</script></body>`)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	var resumed atomic.Int32
	ctx = context.WithValue(ctx, VerificationContinueKey{}, func() (bool, error) {
		resumed.Add(1)
		info, err := page.Info()
		if err != nil {
			return false, err
		}
		return true, page.Navigate(strings.Replace(info.URL, "/publish/publish", "/done", 1))
	})
	if err := waitPublishSuccessWithContext(ctx, page, 200*time.Millisecond); err != nil {
		t.Fatal(err)
	}
	if resumed.Load() != 1 {
		t.Fatalf("Expected one verified continuation, got %d", resumed.Load())
	}
}
