import unittest
import public_quality as quality

class QualityTests(unittest.TestCase):
    def test_complete_knowledge_post_is_not_a_help_request(self):
        self.assertTrue(quality.initial_skip({'title':'民宿交接卡的做法','target':''},{'message_id':'initial','body':'把预订代号和到店时段整理成表，交班前核对房态。'}))

    def test_implicit_real_problem_can_be_considered(self):
        self.assertFalse(quality.initial_skip({'title':'合并表格太麻烦','target':''},{'message_id':'initial','body':'每天手动合并十几张表太累了。'}))

    def test_specific_question_aimed_at_someone_else_is_skipped(self):
        self.assertTrue(quality.initial_skip({'target':'comment'},{'message_id':'initial','body':'@点点 我的笔记如何'}))

    def test_tracked_followup_is_not_rejected_for_mention(self):
        self.assertFalse(quality.initial_skip({'target':'comment'},{'message_id':'followup','body':'@本人 统一列名后怎么更新？'}))

    def test_mention_on_owned_post_can_still_be_assessed(self):
        self.assertFalse(quality.initial_skip({'target':'comment','owner':'owner','account':'owner'},{'message_id':'initial','body':'@作者 表头统一后怎么更新？'}))

    def test_generic_editorial_praise_is_not_a_specific_reply(self):
        self.assertTrue(quality.wording_reason('这篇完成度不错，痛点切入准，流程拆解清晰。','我的笔记怎么样？',[]))

    def test_rephrased_copy_is_not_new_help(self):
        source='把文件放进同一个文件夹，统一每张表的列名和数据类型，再使用文件夹合并功能，后面每天刷新即可。'
        self.assertTrue(quality.wording_reason('可以把文件放进同一个文件夹，统一每张表的列名和数据类型，再使用文件夹合并功能，后面每天刷新即可。',source,[]))

    def test_repeated_old_advice_is_rejected(self):
        self.assertTrue(quality.wording_reason('先统一列名，再按列名合并。','统一以后还是报错',[{'reply':'先统一列名，再按列名合并。','state':'verified'}]))

    def test_new_specific_step_is_accepted(self):
        self.assertFalse(quality.wording_reason('文件放固定文件夹，后面在数据页点全部刷新。','统一后怎么更新？',[{'reply':'先统一列名。','state':'verified'}]))

    def test_already_checked_format_is_not_asked_again(self):
        self.assertTrue(quality.wording_reason('先改成常规格式再输入。','格式已经是常规了，还是不行。',[]))

    def test_failed_double_click_is_not_suggested_again(self):
        self.assertTrue(quality.wording_reason('可以先双击再回车试试。','双击无效，还是显示原公式。',[]))

    def test_unread_screenshot_does_not_allow_guessed_menu_steps(self):
        self.assertTrue(quality.visual_reason('打开文件选项，勾选公式记忆式键入，你用的哪个版本？'))
        self.assertTrue(quality.visual_reason('勾选这个功能就可以。'))
        self.assertFalse(quality.visual_reason('你说的是编辑栏左边的下拉框，还是单元格里弹出的函数提示？'))

    def test_multiple_questions_are_not_a_short_natural_reply(self):
        self.assertTrue(quality.wording_reason('你用什么版本？具体点哪里？有截图吗？','怎么恢复函数提示？',[]))

    def test_cell_reference_is_not_a_purchase_request(self):
        import public_policy
        self.assertFalse(public_policy.reason('看一下单元格左上角有没有绿色小三角？'))
        self.assertTrue(public_policy.reason('请先下单，我来帮你做。'))

if __name__=='__main__':unittest.main()
