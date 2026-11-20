"""Announcement recipient regression tests without an AstrBot runtime."""

import unittest
from collections import Counter
from unittest.mock import AsyncMock, Mock, patch

from test_not_friend_classification import plugin


class BroadcastRecipientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = plugin.RelationshipManager.__new__(plugin.RelationshipManager)
        self.manager._sender_blocked = Mock(return_value=False)
        self.manager._is_admin = Mock(return_value=True)
        self.manager._self_id = None
        self.manager.broadcast_exclude_groups = []
        self.manager.blacklist_groups = []
        self.event = Mock()
        self.event.get_sender_id.return_value = "100"
        self.event.get_sender_name.return_value = "发布者"
        self.event.get_self_id.return_value = "200"
        self.event.plain_result.side_effect = lambda text: text
        self.friends = []
        self.groups = []
        self.responses = {}
        self.manager._api = AsyncMock(side_effect=self.api)

    async def api(self, action, **kwargs):
        if action == "get_friend_list":
            return {"status": "ok", "data": self.friends}
        if action == "get_group_list":
            return {"status": "ok", "data": self.groups}
        if action == "get_login_info":
            return {"status": "ok", "data": {"user_id": 200}}
        if action == "send_private_msg":
            replies = self.responses.get(kwargs["user_id"], [])
            if replies:
                return replies.pop(0)
        return {"status": "ok", "data": {}}

    async def broadcast(self):
        with patch.object(plugin.asyncio, "sleep", new_callable=AsyncMock):
            return [reply async for reply in self.manager.cmd_broadcast(self.event, "测试公告")]

    def calls_for(self, action):
        return [call for call in self.manager._api.await_args_list if call.args[0] == action]

    async def test_excludes_sender_and_bot_with_normalized_ids(self):
        self.event.get_sender_id.return_value = " 100 "
        self.event.get_self_id.return_value = " 200 "
        self.friends = [{"user_id": uid} for uid in (100, " 100 ", 200, " 200 ", 300, "", None)]
        replies = await self.broadcast()
        sends = self.calls_for("send_private_msg")
        self.assertEqual([call.kwargs["user_id"] for call in sends], [300])
        self.assertEqual(sends[0].kwargs["message"], "发布者（100） 发布了公告：\n测试公告")
        self.assertEqual(len(replies), 1)
        self.assertIn("好友：成功 1，失败 0", replies[0])
        self.event.plain_result.assert_called_once()

    async def test_empty_or_only_excluded_friends_have_zero_counts(self):
        for ids in ([], [100, 200]):
            with self.subTest(ids=ids):
                self.manager._api.reset_mock()
                self.friends = [{"user_id": uid} for uid in ids]
                replies = await self.broadcast()
                self.assertEqual(self.calls_for("send_private_msg"), [])
                self.assertIn("好友：成功 0，失败 0", replies[0])
                self.assertNotIn("非好友/已失效", replies[0])

    async def test_login_info_fallback_excludes_bot(self):
        self.event.get_self_id.side_effect = RuntimeError("self id unavailable")
        self.manager._find_dict_in_node = Mock(return_value=None)
        self.friends = [{"user_id": uid} for uid in (100, 200, 300)]
        await self.broadcast()
        self.assertEqual(len(self.calls_for("get_login_info")), 1)
        self.assertEqual([call.kwargs["user_id"] for call in self.calls_for("send_private_msg")], [300])

    async def test_group_exclusions_and_history_behavior_are_unchanged(self):
        self.groups = [{"group_id": gid} for gid in (10, 20, 30)]
        self.manager.broadcast_exclude_groups = ["10"]
        self.manager.blacklist_groups = ["20"]
        replies = await self.broadcast()
        sends = self.calls_for("send_group_msg")
        self.assertEqual([call.kwargs["group_id"] for call in sends], [30])
        self.assertIs(sends[0].kwargs["cache_history"], False)
        self.assertIn("群：成功 1，失败 0", replies[0])

    async def test_remaining_friends_keep_retry_and_failure_classification(self):
        self.friends = [{"user_id": uid} for uid in (100, 200, 300, 400, 500)]
        self.responses = {
            300: [{"status": "failed", "wording": "频率限制"}],
            400: [{"status": "failed", "wording": "不是好友"}],
            500: [{"status": "failed", "wording": "频率限制"}] * 3,
        }
        replies = await self.broadcast()
        counts = Counter(call.kwargs["user_id"] for call in self.calls_for("send_private_msg"))
        self.assertEqual(counts, {300: 2, 400: 1, 500: 3})
        self.assertIn("好友：成功 1，失败 1，非好友/已失效 1", replies[0])
        self.assertIn("失败QQ号：500", replies[0])
        self.assertIn("非好友/已失效（协议端好友列表含已失效记录，未计入失败）：400", replies[0])


if __name__ == "__main__":
    unittest.main()
