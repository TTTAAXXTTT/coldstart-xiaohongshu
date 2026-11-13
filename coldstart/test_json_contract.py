"""Regression for a connected model returning prose during a writing check."""
import json,tempfile,unittest
from unittest.mock import patch
import app,model_api,sales,autopilot

class JsonContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.w=app.Workbench(self.tmp.name)
        self.w.save_settings({'ai':{'base_url':'https://ark.cn-beijing.volces.com/api/v3','model':'doubao-seed-2-1-turbo-260628','api_key':'fixture-credential'}})

    def test_json_instruction_is_last_and_preserves_task_fields(self):
        with patch.object(app,'http_json',return_value={'choices':[{'message':{'content':'{"reply":"合成回复"}'},'finish_reason':'stop'}]}) as remote:
            self.assertEqual(self.w.llm('只返回reply字段','合成问题'),{'reply':'合成回复'})
        request=remote.call_args.args[1]
        self.assertTrue(request['messages'][0]['content'].endswith(model_api.JSON_INSTRUCTION))
        self.assertIn('只返回reply字段',request['messages'][0]['content'])
        self.assertEqual(request['response_format'],{'type':'json_object'})

    def test_plain_reply_is_rejected_once_with_non_circular_writing_advice(self):
        with patch.object(app,'http_json',return_value={'choices':[{'message':{'content':'这是普通文字，不是JSON。'},'finish_reason':'stop'}]}) as remote,patch.object(self.w,'xhs') as xhs:
            with self.assertRaises(autopilot.SetupRequired):sales.check(self.w,'writing_check')
        self.assertEqual(remote.call_count,1);xhs.assert_not_called()
        record=json.loads(self.w.meta('sales_checks'))['writing_check']
        self.assertIn('模型已响应',record['detail'])
        self.assertNotIn('先做',record['next_action'])
        self.assertIn('不会发送',record['next_action'])
        self.assertFalse(self.w.state()['drafts'])

    def test_mixed_business_sample_names_one_direction(self):
        self.w.save_settings({'business':{'search_focus':'custom','name':'合成用卡与AI答疑','keywords':['giffgaff 无信号','AI工具 报错']}})
        with patch.object(self.w,'llm',return_value={'reply':'手机现在能识别到这张卡吗？先区分识别问题和网络连接问题。'}) as llm:
            sales.check(self.w,'writing_check')
        question=json.loads(llm.call_args.args[1])['synthetic_question']
        self.assertIn('giffgaff 无信号',question)
        self.assertNotIn('这方面',question)
        self.assertNotIn('AI工具',question)
