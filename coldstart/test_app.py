"""离线验收：只使用合成夹具，不会调用真实平台或模型。"""
import json
import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from types import SimpleNamespace
import app

QA = Path(__file__).resolve().parent.parent / "qa"
QA.mkdir(exist_ok=True)


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=QA)
        self.w = app.Workbench(self.temp.name)
        creator = patch.object(self.w,'creator_check',return_value={'creator_ready':True,'fixture':True}); creator.start(); self.addCleanup(creator.stop)
        submit = patch.object(self.w,'creator_submit',side_effect=lambda p:self.w.xhs('/api/v1/publish',json.loads(Path(p).read_text(encoding='utf-8')))); submit.start(); self.addCleanup(submit.stop)

    def tearDown(self):
        self.temp.cleanup()

    def lead(self, **kwargs):
        value = {"platform": "xiaohongshu", "url": "https://www.xiaohongshu.com/explore/fixture001?xsec_token=one", "title": "求推荐：想找人做报表自动化", "excerpt": "每天重复整理表格，想找人协助实现。", "author": "测试夹具，非真实用户"}
        value.update(kwargs)
        return value

    def test_deduplicate_token_rotations_but_keep_distinct_comments(self):
        self.assertEqual(self.w.ingest([self.lead()]), 1)
        self.assertEqual(self.w.ingest([self.lead(url="https://www.xiaohongshu.com/explore/fixture001?xsec_token=two&utm_source=test")]), 0)
        self.assertEqual(self.w.ingest([self.lead(external_id="comment001"), self.lead(external_id="comment002")]), 2)
        self.assertEqual(len(self.w.state()["leads"]), 3)

    def test_seller_ads_are_not_buyer_leads(self):
        result = app.rule_score("AI 自动化接单，欢迎咨询", "专业代做", ["接单", "欢迎咨询", "代做"])
        self.assertEqual(result["intent"], "seller")
        self.assertEqual(result["score"], 0)

    def test_no_model_invented_evidence(self):
        self.w.ingest([self.lead()])
        with patch.object(self.w, "llm", return_value={"results": [{"id": 1, "intent": "buyer", "score": 99, "evidence": "预算十万元，急需供应商", "reply": "请问数据来自哪里？"}]}):
            self.w.qualify()
        lead = self.w.state()["leads"][0]
        self.assertEqual(lead["evidence"], "")
        self.assertLessEqual(lead["score"], 30)
        self.assertEqual(lead["intent"], "unconfirmed")

    def test_keys_masked_and_empty_preserves(self):
        self.w.save_settings({"ai": {"api_key": "fixture-secret-key"}})
        self.w.save_settings({"ai": {"api_key": ""}})
        self.assertEqual(self.w.config()["ai"]["api_key"], "fixture-secret-key")
        self.assertNotIn("fixture-secret-key", json.dumps(self.w.state()))
        self.w.log("error", "请求失败 fixture-secret-key")
        self.assertNotIn("fixture-secret-key", json.dumps(self.w.state()))

    def test_llm_limit_is_durable(self):
        self.w.reserve_usage("llm", 1)
        with self.assertRaises(ValueError):
            self.w.reserve_usage("llm", 1)

    def test_parallel_enqueue_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=5) as executor:
            ids = list(executor.map(lambda _: self.w.enqueue("scan"), range(10)))
        self.assertEqual(len(set(ids)), 1)

    def test_template_cards_are_real_pngs_and_never_autoapproved(self):
        self.w.save_settings({"publishing": {"auto_approve": True}})
        self.w.generate(template=True)
        from PIL import Image
        for draft in self.w.state()["drafts"]:
            self.assertEqual(draft["state"], "draft")
            for asset in json.loads(draft["assets"]):
                with Image.open(asset) as image:
                    self.assertEqual(image.size, (1080, 1440))
                    self.assertEqual(image.format, "PNG")

    def draft(self):
        self.w.meta('xhs_bound_user','fixture-user')
        self.w.save_settings({"publishing": {"channels": ["xiaohongshu"]}})
        self.w.generate(template=True)
        draft = self.w.state()["drafts"][0]
        self.w.edit_draft({"id": draft["id"], "state": "approved"})
        return draft["id"]

    def test_disabled_publish_never_calls_network(self):
        draft_id = self.draft()
        with patch.object(self.w, "xhs") as xhs:
            with self.assertRaises(ValueError):
                self.w.publish(draft_id)
            xhs.assert_not_called()

    def test_publish_timeout_is_unknown_and_cannot_repeat(self):
        draft_id = self.draft()
        self.w.save_settings({"publishing": {"enabled": True}})
        calls = []
        def xhs(path, payload=None, **kwargs):
            calls.append((path, payload))
            if path.endswith("status"):
                return {"is_logged_in": True,'user_id':'fixture-user'}
            raise RuntimeError("模拟：提交后连接中断")
        with patch.object(self.w, "xhs", side_effect=xhs):
            self.w.publish(draft_id)
            with self.assertRaises(ValueError):
                self.w.publish(draft_id)
        self.assertEqual(self.w.draft(draft_id)["state"], "unknown")
        self.assertEqual(len([p for p, _ in calls if p.endswith("publish")]), 1)

    def test_upstream_success_without_link_is_not_verified_published(self):
        draft_id = self.draft()
        self.w.save_settings({"publishing": {"enabled": True}})
        def xhs(path, payload=None, **kwargs):
            return {"is_logged_in": True,'user_id':'fixture-user'} if path.endswith("status") else {"status": "发布完成"}
        with patch.object(self.w, "xhs", side_effect=xhs):
            self.w.publish(draft_id)
        self.assertEqual(self.w.draft(draft_id)["state"], "submitted_unverified")

    def test_xhs_contract_search_detail_comments_and_dedup(self):
        self.w.save_settings({"radar": {"xhs_enabled": True, "max_queries": 1}})
        def xhs(path, payload=None, **kwargs):
            if path.endswith("status"):
                return {"is_logged_in": True}
            if "/feeds/search?" in path:
                return {"feeds": [{"id": "fixture001", "xsecToken": "token", "noteCard": {"displayTitle": "求推荐报表工具", "user": {"nickname": "测试夹具"}}}]}
            self.assertEqual(payload["feed_id"], "fixture001")
            self.assertFalse(payload["load_all_comments"])
            return {"feed_id": "fixture001", "data": {"note": {"desc": "希望减少重复工作"}, "comments": {"list": [{"id": "c1", "content": "求助：怎么实现？", "userInfo": {"nickname": "合成评论"}}]}}}
        with patch.object(self.w, "xhs", side_effect=xhs):
            self.w.scan()
            self.w.scan()
        self.assertEqual(len(self.w.state()["leads"]), 2)
        self.assertTrue(any(x["excerpt"] == "求助：怎么实现？" for x in self.w.state()["leads"]))

    def test_restart_does_not_retry_inflight_publication(self):
        draft_id = self.draft()
        with self.w.db() as db:
            db.execute("UPDATE drafts SET state='publishing' WHERE id=?", (draft_id,))
            db.execute("INSERT INTO jobs(kind,payload,state,created_at,updated_at) VALUES('publish',?,'running',?,?)", (json.dumps({"draft_id": draft_id}), app.now(), app.now()))
        recovered = app.Workbench(self.temp.name)
        self.assertEqual(recovered.draft(draft_id)["state"], "unknown")
        self.assertEqual(recovered.state()["jobs"][0]["state"], "interrupted")

    def test_schedule_starts_disabled_and_tick_does_not_create_jobs(self):
        self.w.tick()
        self.assertEqual(self.w.state()["jobs"], [])


    def test_http_host_csrf_and_empty_real_stats(self):
        # 原始 HTTP 报文驱动实际 Handler，不需要套接字权限。
        class SocketFixture:
            def __init__(self, raw):
                self.incoming, self.output = io.BytesIO(raw), bytearray()
            def makefile(self, *_args, **_kwargs):
                return self.incoming
            def sendall(self, data):
                self.output.extend(data)
        def request(raw):
            connection = SocketFixture(raw)
            app.make_handler(self.w)(connection, ("127.0.0.1", 30000), SimpleNamespace(server_port=18796))
            return bytes(connection.output)
        response = request(b'GET /api/state HTTP/1.0\r\nHost: 127.0.0.1:18796\r\n\r\n')
        self.assertIn(b'200 OK', response.split(b'\r\n')[0])
        data = json.loads(response.split(b'\r\n\r\n', 1)[1])
        self.assertEqual(data["stats"]["won"], 0)
        payload = b'{"kind":"scan"}'
        response = request(b'POST /api/jobs HTTP/1.0\r\nHost: 127.0.0.1:18796\r\nOrigin: https://external.invalid\r\nX-Workbench: local\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload)
        self.assertIn(b'403 Forbidden', response.split(b'\r\n')[0])
        response = request(b'GET /api/state HTTP/1.0\r\nHost: untrusted.invalid\r\n\r\n')
        self.assertIn(b'403 Forbidden', response.split(b'\r\n')[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
