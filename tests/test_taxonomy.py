import unittest
from taxonomy import primary_category


class CategoryTests(unittest.TestCase):
    def test_one_main_category_handles_existing_archive_and_keeps_model_choice(self):
        cases=[('教室甜甜歌杂',['中文歌杂','巴哥梗'],'歌回'),
            ('示例午间直播',['视觉小说','水仙'],'剧情朗读'),
            ('老实开车很枯燥吧',['教妹妹开车','植物大战僵尸'],'游戏'),
            ('诶呀诶呀聊聊倒霉的一天',['退房','信用卡重复扣款','生化危机'],'杂谈')]
        for title,tags,expected in cases:
            self.assertEqual(primary_category({'tags':tags},title),expected)
        self.assertEqual(primary_category({'category':'杂谈','tags':['游戏']},'昨天聊的游戏'),'杂谈')

    def test_incidental_overview_mentions_do_not_create_categories(self):
        summary={'tags':['日常'],'overview':'聊起看电影、散步和游戏的往事'}
        self.assertEqual(primary_category(summary,'晚安闲聊'),'杂谈')
