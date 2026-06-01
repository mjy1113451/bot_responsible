import json
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register(
    "astrbot_plugin_relationship_manager",
    "YourName",
    "AstrBot 关系管理插件",
    "2.1.0",
    "https://github.com/your-repo/astrbot_plugin_relationship_manager",
)
class RelationshipManager(Star):

    def __init__(self, context: Context):
        super().__init__(context)

        self.data_dir = Path(
            context.get_astrbot_config().get("data_path", "data")
        ) / "plugins" / "astrbot_plugin_relationship_manager"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.bl_file = self.data_dir / "blacklist.json"
        self.pd_file = self.data_dir / "pending.json"

        self.blacklist: Dict[str, dict] = self._load(self.bl_file, {})
        self.pending: Dict[str, dict] = self._load(self.pd_file, {})
        self._migrate_blacklist()

        self.notify_group: Optional[str] = None

    # ───────── 持久化 ─────────

    @staticmethod
    def _load(path: Path, default):
        if not path.exists():
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取 {path.name} 失败: {e}")
            return default

    def _save(self, path: Path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存 {path.name} 失败: {e}")

    def _migrate_blacklist(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        changed = False
        for uid, val in list(self.blacklist.items()):
            if isinstance(val, str):
                self.blacklist[uid] = dict(time=now, block_msg=True, block_friend=True, block_group_invite=True)
                changed = True
            elif isinstance(val, dict) and "reason" in val:
                val.pop("reason", None)
                changed = True
        if changed:
            self._save(self.bl_file, self.blacklist)

    # ───────── 工具 ─────────

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admins = self.context.get_astrbot_config().get("admins", [])
        return str(event.get_sender_id()) in [str(a) for a in admins]

    def _blocked(self, uid: str, kind: str = "all") -> bool:
        if uid not in self.blacklist:
            return False
        if kind == "all":
            return True
        return self.blacklist[uid].get(f"block_{kind}", True)

    @staticmethod
    def _ids(text: str) -> List[str]:
        return re.findall(r"\d+", text)

    async def _api(self, name: str, **kw) -> Optional[dict]:
        try:
            if hasattr(self.context, "call_api"):
                return await self.context.call_api(name, **kw)
            if hasattr(self, "call_api"):
                return await self.call_api(name, **kw)
        except Exception as e:
            logger.error(f"API {name} 失败: {e}")
        return None

    async def _notify(self, msg: str):
        if self.notify_group:
            await self._api("send_group_msg", group_id=int(self.notify_group), message=msg)
        else:
            for aid in self.context.get_astrbot_config().get("admins", []):
                await self._api("send_private_msg", user_id=int(aid), message=msg)

    async def _notify_with_ids(self, msg: str) -> List[str]:
        """发送通知并收集消息ID"""
        ids = []
        if self.notify_group:
            res = await self._api("send_group_msg", group_id=int(self.notify_group), message=msg)
            if res and isinstance(res, dict):
                mid = res.get("data", {}).get("message_id")
                if mid:
                    ids.append(str(mid))
        else:
            for aid in self.context.get_astrbot_config().get("admins", []):
                res = await self._api("send_private_msg", user_id=int(aid), message=msg)
                if res and isinstance(res, dict):
                    mid = res.get("data", {}).get("message_id")
                    if mid:
                        ids.append(str(mid))
        return ids

    def _stop(self, event: AstrMessageEvent):
        try:
            event.stop_event()
        except Exception:
            pass

    def _get_reply_id(self, event: AstrMessageEvent) -> Optional[str]:
        """安全提取被引用消息的ID"""
        try:
            for comp in event.message_obj.message:
                if hasattr(comp, "type") and comp.type == "reply":
                    return str(comp.data.get("id", ""))
        except Exception:
            pass
        try:
            raw_str = str(event.message_obj.raw_message)
            match = re.search(r"\[CQ:reply,id=(\d+)\]", raw_str)
            if match:
                return match.group(1)
        except Exception:
            pass
        return None

    def _find_flag_by_msg_id(self, msg_id: str) -> Optional[str]:
        if not msg_id:
            return None
        for flag, info in self.pending.items():
            if msg_id in info.get("notify_ids", []):
                return flag
        return None

    # ───────── 消息拦截 ─────────

    @filter.on_all_message
    async def on_message(self, event: AstrMessageEvent):
        """拦截黑名单用户消息"""
        sender = str(event.get_sender_id())
        if sender and self._blocked(sender, "msg"):
            self._stop(event)
            return

    # ───────── 请求事件处理 ─────────

    @filter.on_request
    async def on_request(self, event: AstrMessageEvent):
        """监听好友申请和群邀请"""
        try:
            raw = event.message_obj.raw_message
        except Exception:
            return
        if not isinstance(raw, dict):
            return

        req = raw.get("request_type")
        if req == "friend":
            await self._handle_friend_req(raw)
            self._stop(event)
        elif req == "group" and raw.get("sub_type") == "invite":
            await self._handle_group_req(raw)
            self._stop(event)

    async def _handle_friend_req(self, raw: dict):
        uid = str(raw.get("user_id", ""))
        flag = str(raw.get("flag", ""))
        comment = raw.get("comment", "") or ""
        if not uid or not flag:
            return

        if self._blocked(uid, "friend"):
            await self._api("set_friend_add_request", flag=flag, approve=False)
            await self._notify(f"🚫 自动拒绝黑名单好友申请\n用户: {uid}")
            return

        self.pending[flag] = dict(
            type="friend", user_id=uid, comment=comment,
            time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            notify_ids=[],
        )
        self._save(self.pd_file, self.pending)

        msg_ids = await self._notify_with_ids(
            f"📥 新好友申请\n用户: {uid}\n理由: {comment}\n"
            f"💬 引用此消息回复: /同意 或 /拒绝"
        )
        if msg_ids:
            self.pending[flag]["notify_ids"] = msg_ids
            self._save(self.pd_file, self.pending)

    async def _handle_group_req(self, raw: dict):
        uid = str(raw.get("user_id", ""))
        gid = str(raw.get("group_id", ""))
        flag = str(raw.get("flag", ""))
        comment = raw.get("comment", "") or ""
        sub = raw.get("sub_type", "invite")
        if not flag:
            return

        if self._blocked(uid, "group_invite"):
            await self._api("set_group_add_request", flag=flag, approve=False, sub_type=sub)
            await self._notify(f"🚫 自动拒绝黑名单群邀请\n邀请人: {uid}\n群号: {gid}")
            return

        self.pending[flag] = dict(
            type="group", group_id=gid, user_id=uid, sub_type=sub, comment=comment,
            time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            notify_ids=[],
        )
        self._save(self.pd_file, self.pending)

        msg_ids = await self._notify_with_ids(
            f"📥 新群邀请\n群号: {gid}\n邀请人: {uid}\n理由: {comment}\n"
            f"💬 引用此消息回复: /同意群 或 /拒绝群"
        )
        if msg_ids:
            self.pending[flag]["notify_ids"] = msg_ids
            self._save(self.pd_file, self.pending)

    # ───────── 查看列表 ─────────

    @filter.command("好友", alias=["fl"])
    async def cmd_friends(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        res = await self._api("get_friend_list")
        if not res or res.get("status") != "ok":
            yield event.plain_result("❌ 获取失败")
            return

        friends = res.get("data", [])
        if not friends:
            yield event.plain_result("📋 没有好友")
            return

        lines = ["📋 好友列表"]
        for i, f in enumerate(friends, 1):
            uid = f.get("user_id", "?")
            tag = " 🚫" if self._blocked(str(uid)) else ""
            lines.append(f"{i}. {f.get('nickname', '?')} ({uid}){tag}")

        yield event.plain_result("\n".join(lines))

    @filter.command("群", alias=["gl"])
    async def cmd_groups(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        res = await self._api("get_group_list")
        if not res or res.get("status") != "ok":
            yield event.plain_result("❌ 获取失败")
            return

        groups = res.get("data", [])
        if not groups:
            yield event.plain_result("📋 没有群")
            return

        lines = ["📋 群列表"]
        for i, g in enumerate(groups, 1):
            lines.append(f"{i}. {g.get('group_name', '?')} ({g.get('group_id', '?')})")

        yield event.plain_result("\n".join(lines))

    # ───────── 黑名单 ─────────

    @filter.command("拉黑", alias=["addbl", "屏蔽"])
    async def cmd_bl_add(self, event: AstrMessageEvent, args: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        uids = self._ids(args)
        if not uids:
            yield event.plain_result("⚠️ /拉黑 123 [456] ...")
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        added, dup = [], []
        for u in uids:
            if u in self.blacklist:
                dup.append(u)
            else:
                self.blacklist[u] = dict(time=now, block_msg=True, block_friend=True, block_group_invite=True)
                added.append(u)
        self._save(self.bl_file, self.blacklist)

        parts = []
        if added:
            parts.append(f"✅ 已拉黑 {len(added)} 人: {', '.join(added)}")
        if dup:
            parts.append(f"⚠️ 已存在: {', '.join(dup)}")
        yield event.plain_result("\n".join(parts))

    @filter.command("解封", alias=["rmbl", "取消屏蔽"])
    async def cmd_bl_rm(self, event: AstrMessageEvent, args: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        uids = self._ids(args)
        if not uids:
            yield event.plain_result("⚠️ /解封 123 [456] ...")
            return

        removed, miss = [], []
        for u in uids:
            if u in self.blacklist:
                del self.blacklist[u]
                removed.append(u)
            else:
                miss.append(u)
        self._save(self.bl_file, self.blacklist)

        parts = []
        if removed:
            parts.append(f"✅ 已解封 {len(removed)} 人: {', '.join(removed)}")
        if miss:
            parts.append(f"⚠️ 不存在: {', '.join(miss)}")
        yield event.plain_result("\n".join(parts))

    @filter.command("黑名单", alias=["lsbl"])
    async def cmd_bl_ls(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        if not self.blacklist:
            yield event.plain_result("📋 黑名单为空")
            return

        lines = [f"🚫 黑名单 ({len(self.blacklist)} 人)"]
        for uid, info in self.blacklist.items():
            m = "✅" if info.get("block_msg", True) else "❌"
            fr = "✅" if info.get("block_friend", True) else "❌"
            gi = "✅" if info.get("block_group_invite", True) else "❌"
            lines.append(f"- {uid} | 消息{m} 好友{fr} 群邀请{gi}")

        yield event.plain_result("\n".join(lines))

    # ───────── 待处理 ─────────

    @filter.command("待处理", alias=["pending"])
    async def cmd_pending(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        if not self.pending:
            yield event.plain_result("📋 无待处理请求")
            return

        lines = ["📋 待处理请求"]
        for flag, info in self.pending.items():
            t = info.get("time", "?")
            if info["type"] == "friend":
                lines.append(
                    f"🔹 好友 用户:{info['user_id']} 理由:{info.get('comment', '无')} {t}\n"
                    f"   /同意 {flag} 或 /拒绝 {flag}"
                )
            else:
                lines.append(
                    f"🔸 群邀 群:{info['group_id']} 邀请人:{info['user_id']} {t}\n"
                    f"   /同意群 {flag} 或 /拒绝群 {flag}"
                )

        yield event.plain_result("\n".join(lines))

    # ───────── 删好友 / 退群 ─────────

    @filter.command("删好友", alias=["deletefriend"])
    async def cmd_del_friend(self, event: AstrMessageEvent, args: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        uids = self._ids(args)
        if not uids:
            yield event.plain_result("⚠️ /删好友 123 [456] ...")
            return

        ok, fail = [], []
        for u in uids:
            r = await self._api("delete_friend", user_id=int(u))
            (ok if r and r.get("status") == "ok" else fail).append(u)

        parts = []
        if ok:
            parts.append(f"✅ 已删除 {len(ok)} 人: {', '.join(ok)}")
        if fail:
            parts.append(f"❌ 失败: {', '.join(fail)}")
        yield event.plain_result("\n".join(parts) if parts else "❌ 无结果")

    @filter.command("退群", alias=["leavegroup"])
    async def cmd_leave_group(self, event: AstrMessageEvent, args: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        gids = self._ids(args)
        if not gids:
            yield event.plain_result("⚠️ /退群 111 [222] ...")
            return

        ok, fail = [], []
        for g in gids:
            r = await self._api("set_group_leave", group_id=int(g))
            (ok if r and r.get("status") == "ok" else fail).append(g)

        parts = []
        if ok:
            parts.append(f"✅ 已退群 {len(ok)} 个: {', '.join(ok)}")
        if fail:
            parts.append(f"❌ 失败: {', '.join(fail)}")
        yield event.plain_result("\n".join(parts) if parts else "❌ 无结果")

    # ───────── 同意 / 拒绝 ─────────

    async def _process_flags(self, event, args, approve: bool, allow_type: str = "all"):
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        flags = args.strip().split()

        if not flags:
            reply_id = self._get_reply_id(event)
            if reply_id:
                flag = self._find_flag_by_msg_id(reply_id)
                if flag:
                    flags = [flag]
                else:
                    yield event.plain_result("❌ 该引用消息未匹配到待处理请求")
                    return
            else:
                yield event.plain_result("⚠️ 引用通知消息回复，或输入 flag（/待处理 查看）")
                return

        ok, fail, miss = [], [], []

        for flag in flags:
            info = self.pending.get(flag)

            if not info:
                miss.append(flag)
                continue

            if allow_type != "all" and info.get("type") != allow_type:
                miss.append(flag)
                continue

            try:
                if info["type"] == "friend":
                    r = await self._api("set_friend_add_request", flag=flag, approve=approve)
                else:
                    r = await self._api(
                        "set_group_add_request",
                        flag=flag,
                        approve=approve,
                        sub_type=info.get("sub_type", "invite"),
                    )

                if r and r.get("status") == "ok":
                    self.pending.pop(flag, None)
                    ok.append(flag)
                else:
                    fail.append(flag)
            except Exception as e:
                logger.error(f"处理 {flag} 异常: {e}")
                fail.append(flag)

        if ok:
            self._save(self.pd_file, self.pending)

        action = "同意" if approve else "拒绝"
        parts = []
        if ok:
            parts.append(f"✅ 已{action} {len(ok)} 项")
        if miss:
            parts.append(f"⚠️ 未找到: {len(miss)} 项")
        if fail:
            parts.append(f"❌ 失败: {len(fail)} 项")
        yield event.plain_result("\n".join(parts) if parts else "❌ 无结果")

    @filter.command("同意", alias=["accept"])
    async def cmd_accept(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._process_flags(event, args, approve=True, allow_type="all"):
            yield result

    @filter.command("拒绝", alias=["reject"])
    async def cmd_reject(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._process_flags(event, args, approve=False, allow_type="all"):
            yield result

    @filter.command("同意群", alias=["acceptgroup"])
    async def cmd_accept_group(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._process_flags(event, args, approve=True, allow_type="group"):
            yield result

    @filter.command("拒绝群", alias=["rejectgroup"])
    async def cmd_reject_group(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._process_flags(event, args, approve=False, allow_type="group"):
            yield result

    # ───────── 生命周期 ─────────

    async def terminate(self):
        logger.info("关系管理插件已停止")
