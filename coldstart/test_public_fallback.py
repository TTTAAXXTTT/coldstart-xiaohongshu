import tempfile,unittest,json
from unittest.mock import patch
import app,public_policy

class PublicFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.w=app.Workbench(self.temp.name)
        self.w.save_settings({'ai':{'base_url':'https://primary.example.invalid/v1','model':'fixture-primary','api_key':'fixture-primary-secret','calls_per_day':-1,'fallback_base_url':'https://secondary.example.invalid/v1','fallback_model':'fixture-secondary','fallback_api_key':'fixture-secondary-secret'}})

    def test_fallback_never_receives_primary_secret(self):
        response={'choices':[{'message':{'content':'{"ok":true}'}}]}
        with patch.object(app,'http_json',side_effect=[RuntimeError('fixture network failure'),response]) as http:
            self.assertTrue(self.w.llm('合成','合成')['ok'])
        self.assertEqual(http.call_args_list[0].args[2],'fixture-primary-secret')
        self.assertEqual(http.call_args_list[1].args[2],'fixture-secondary-secret')
        self.assertEqual(self.w.state()['stats']['calls'],2)

    def test_state_and_errors_redact_fallback_secret(self):
        self.assertNotIn('fixture-secondary-secret',json.dumps(self.w.state()))
        self.assertNotIn('fixture-secondary-secret',self.w.redact('fixture-secondary-secret'))

    def test_switching_fallback_origin_clears_old_key(self):
        self.w.save_settings({'ai':{'fallback_base_url':'https://third.example.invalid/v1'}})
        self.assertEqual(self.w.config()['ai']['fallback_api_key'],'')

    def test_no_fallback_config_means_no_automatic_network_retry(self):
        self.w.save_settings({'ai':{'fallback_api_key':None}})
        with patch.object(app,'http_json',side_effect=RuntimeError('fixture')) as http:
            with self.assertRaises(RuntimeError):self.w.llm('合成','合成')
        self.assertEqual(http.call_count,1)

    def test_educational_financial_field_names_are_not_a_quote(self):
        public_policy.content({'title':'统一列名','body':'把“实付金额”和“收款额”统一成同一个列名。日期20261112应统一格式。'})
        public_policy.check('需要按付款日期排序时，先统一这一列的日期格式。')
        for text in ['需要付款后安排','支持付款','报价100元','私信我','看看我的主页','微信号123456789','联系我','请先下单','免费帮你做','我们可以接单']:
            with self.subTest(text=text),self.assertRaises(ValueError):public_policy.check(text)

if __name__=='__main__':unittest.main()
