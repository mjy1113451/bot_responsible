"""非好友/已失效分类的单元测试（PR #82 审查：#80/#81 广播失败归类）。

只验证措辞判定逻辑，不启动 AstrBot 运行时；用最小桩替换 astrbot 模块。
"""

import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _identity_decorator(*args, **kwargs):
    def wrap(obj):
        return obj

    return wrap


def _install_astrbot_stubs():
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    filter_mod = types.ModuleType("astrbot.api.event.filter")
    platform_mod = types.ModuleType("astrbot.api.platform")
    star_mod = types.ModuleType("astrbot.api.star")

    class _Logger:
        def warning(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def debug(self, *args, **kwargs):
            pass

    class _Filter:
        command = staticmethod(_identity_decorator)
        event_message_type = staticmethod(_identity_decorator)
        on_llm_response = staticmethod(_identity_decorator)
        on_decorating_result = staticmethod(_identity_decorator)

    class AstrMessageEvent:
        pass

    class EventMessageType:
        ALL = "all"

    class MessageType:
        GROUP_MESSAGE = "group"
        FRIEND_MESSAGE = "friend"

    class Star:
        def __init__(self, context=None):
            self.context = context

    api.logger = _Logger()
    event_mod.filter = _Filter
    event_mod.AstrMessageEvent = AstrMessageEvent
    filter_mod.EventMessageType = EventMessageType
    platform_mod.MessageType = MessageType
    star_mod.Context = object
    star_mod.Star = Star
    star_mod.register = _identity_decorator

    astrbot.api = api
    api.event = event_mod
    event_mod.filter = _Filter
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.event.filter": filter_mod,
            "astrbot.api.platform": platform_mod,
            "astrbot.api.star": star_mod,
        }
    )


_install_astrbot_stubs()

import main as plugin  # noqa: E402


class NotFriendClassificationTests(unittest.TestCase):
    def test_matches_explicit_not_friend_wordings(self):
        for wording in (
            "对方非好友，无法发送",
            "不是好友，消息发送失败",
            "不是你的好友",
            "不是我的好友",
            "好友不存在",
            "not a friend",
            "not your friend",
            "NOT A FRIEND",
        ):
            with self.subTest(wording=wording):
                self.assertTrue(plugin.NOT_FRIEND_HINT.search(wording))

    def test_ignores_other_failures(self):
        for wording in (
            "好友关系异常",
            "发送失败：频率限制",
            "消息被风控拦截",
            "account deleted",
            "该用户已删除",
        ):
            with self.subTest(wording=wording):
                self.assertFalse(plugin.NOT_FRIEND_HINT.search(wording))

    def test_is_not_friend_response_uses_wording_only(self):
        instance = plugin.RelationshipManager.__new__(plugin.RelationshipManager)
        is_not_friend = instance._is_not_friend_response
        self.assertTrue(is_not_friend({"wording": "对方非好友"}))
        self.assertTrue(is_not_friend({"message": "not your friend"}))
        # 结构化字段里的同名词不算证据
        self.assertFalse(
            is_not_friend({"status": "failed", "retcode": -1, "wording": "频率限制"})
        )
        self.assertFalse(is_not_friend(None))
        self.assertFalse(is_not_friend("对方非好友"))


if __name__ == "__main__":
    unittest.main()
