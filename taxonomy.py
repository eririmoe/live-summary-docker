"""One broad broadcast category; detailed tags remain searchable keywords."""
import re

CATEGORIES=('杂谈','歌回','游戏','剧情朗读','同步视听','户外','联动','其他')


def primary_category(summary,title=''):
    if summary.get('category') in CATEGORIES:
        return summary['category']
    # Compatibility for older, already published summaries. Avoid scanning the
    # overview: merely mentioning a film or a game is not its main activity.
    text=title+' '+' '.join(summary.get('tags',[]))
    rules=(('歌回',r'歌回|歌杂|歌会|音乐电台'),
           ('同步视听',r'同步视听|一起看番|看番'),
           ('户外',r'户外直播|户外'),
           ('剧情朗读',r'剧情朗读|视觉小说|水仙|Narcissu|连言连语'),
           ('游戏',r'游戏|实况|植物大战僵尸|数字僵尸|教妹妹开车'),
           ('联动',r'联动'))
    return next((category for category,pattern in rules if re.search(pattern,text,re.I)),'杂谈')
