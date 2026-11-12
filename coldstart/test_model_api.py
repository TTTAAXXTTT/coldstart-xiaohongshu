"""Provider configuration and HTTP interoperability regressions, without paid remote calls."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import app
import model_api
import ops


class ModelApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'qa')
        self.w = app.Workbench(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_reinstall_and_enable_preserve_non_ark_configuration(self):
        ops.configure(self.temp.name, key='fixture-deepseek', provider='deepseek')
        before=self.w.config()['ai']
        with patch.dict(os.environ, {'ARK_API_KEY':'fixture-unrelated-ark'}, clear=True):
            ops.configure(self.temp.name)
            ops.enable(self.temp.name)
        self.assertEqual(self.w.config()['ai'],before)
        self.assertFalse(self.w.config()['radar']['ark_search_enabled'])
        self.assertTrue(self.w.config()['autonomy']['enabled'])

    def test_changing_api_origin_clears_previous_key_and_ark_search(self):
        self.w.save_settings({'ai':{'api_key':'fixture-ark'},'radar':{'ark_search_enabled':True}})
        self.w.save_settings({'ai':{'base_url':'https://api.deepseek.com','model':'deepseek-flash','api_key':''}})
        self.assertEqual(self.w.config()['ai']['api_key'],'')
        self.assertFalse(self.w.config()['radar']['ark_search_enabled'])
        with self.assertRaises(ValueError):self.w.llm('JSON','check')
        self.assertEqual(self.w.state()['stats']['calls'],0)

    def test_same_origin_path_change_preserves_key_and_explicit_new_key_is_used(self):
        self.w.save_settings({'ai':{'api_key':'fixture-ark'}})
        self.w.save_settings({'ai':{'base_url':'https://ark.cn-beijing.volces.com:443/api/v3/','api_key':''}})
        self.assertEqual(self.w.config()['ai']['api_key'],'fixture-ark')
        self.w.save_settings({'ai':{'base_url':'https://model.example/v1','api_key':'fixture-new'}})
        self.assertEqual(self.w.config()['ai']['api_key'],'fixture-new')

    def test_interactive_provider_selection_uses_matching_key_environment_only(self):
        with patch.dict(os.environ, {'ARK_API_KEY':'fixture-unrelated-ark'}, clear=True), patch('sys.stdin.isatty',return_value=True), patch('builtins.input',side_effect=['2','','']), patch('getpass.getpass',return_value='fixture-entered'):
            result=ops.configure(self.temp.name,interactive=True)
        self.assertTrue(result['model_configured'])
        self.assertEqual(self.w.config()['ai']['api_key'],'fixture-entered')
        self.assertEqual(model_api.provider_id(self.w.config()['ai']['base_url']),'deepseek')
        self.assertNotIn('fixture-entered',json.dumps(result))

    def test_generic_environment_and_custom_endpoint_survive_reinstall(self):
        with patch.dict(os.environ, {'COLDSTART_BASE_URL':'https://gateway.example/v2','COLDSTART_MODEL':'text-model','COLDSTART_API_KEY':'fixture-custom'}, clear=True):
            ops.configure(self.temp.name,provider='custom')
        ops.configure(self.temp.name,key='')
        config=self.w.config()['ai']
        self.assertEqual((config['base_url'],config['model'],config['api_key']),('https://gateway.example/v2','text-model','fixture-custom'))

    def test_provider_contracts_and_special_parameters_are_scoped(self):
        response={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}],'usage':{'total_tokens':3}}
        cases=[('ark','https://ark.cn-beijing.volces.com/api/v3/chat/completions'),('deepseek','https://api.deepseek.com/chat/completions'),('qwen','https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'),('openai','https://api.openai.com/v1/chat/completions'),('custom','https://gateway.example/v1/chat/completions')]
        for provider,url in cases:
            with self.subTest(provider=provider):
                preset=model_api.PRESETS[provider]
                self.w.save_settings({'ai':{'base_url':preset['base_url'] or 'https://gateway.example','model':preset['model'] or 'fixture-model','api_key':'fixture-'+provider}})
                with patch.object(app,'http_json',return_value=response) as transport:
                    self.assertEqual(self.w.llm('只输出JSON','check',max_tokens=100),{'ok':True})
                args=transport.call_args.args
                self.assertEqual(args[0],url)
                self.assertEqual(args[2],'fixture-'+provider)
                body=args[1]
                self.assertNotIn('web_search_options',body)
                if provider=='openai':
                    self.assertEqual(body['max_completion_tokens'],100)
                    self.assertNotIn('max_tokens',body)
                    self.assertNotIn('temperature',body)
                else:self.assertEqual(body['max_tokens'],100)
                if provider not in ('ark','deepseek'):self.assertNotIn('thinking',body)
        self.assertEqual(self.w.state()['stats']['calls'],5)
        self.assertEqual(self.w.state()['stats']['tokens'],15)

    def test_real_local_http_serialization_and_bearer_auth(self):
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path,self.headers.get('Authorization'),json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                value=json.dumps({'choices':[{'message':{'content':'{"ok":true}'}}],'usage':{'total_tokens':7}}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(value)));self.end_headers();self.wfile.write(value)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.w.save_settings({'ai':{'base_url':f'http://127.0.0.1:{server.server_port}/proxy/v1/chat/completions','model':'fixture','api_key':'fixture-local'}})
            self.assertEqual(self.w.llm('只输出JSON','检查中文',max_tokens=100),{'ok':True})
            self.assertEqual(received[0][:2],('/proxy/v1/chat/completions','Bearer fixture-local'))
            self.assertEqual(received[0][2]['messages'][1]['content'],'检查中文')
        finally:server.shutdown();thread.join();server.server_close()

    def test_non_ark_web_search_fails_before_any_request_or_usage(self):
        ops.configure(self.temp.name,provider='deepseek',key='fixture-key')
        with patch.object(app,'http_json') as transport:
            with self.assertRaisesRegex(ValueError,'火山方舟'):self.w.llm('JSON','check',web_search=True)
            transport.assert_not_called()
        self.assertEqual(self.w.state()['stats']['calls'],0)


if __name__=='__main__':unittest.main()
