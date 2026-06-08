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
    "5.1.0",
    "https://github.com/your-repo/astrbot_plugin_relationship_manager",
)
class RelationshipManager(Star):

    def __init__(self, context: Context):
        super().__init__(context)

        config = self.context.get_config()
        self.data_dir = Path(
            config.get("data_path", "data")
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
                self.blacklist[uid] = dict(
                    time=now, block_msg=True, block_friend=True, block_group_invite=True
                )
                changed = True
            elif isinstance(val, dict) and "reason" in val:
                val.pop("reason", None)
                changed = True
        if changed:
            self._save(self.bl_file, self.blacklist)

    # ───────── 工具 ─────────

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            sender_id = str(event.get_sender_id())
            if hasattr(self.context, "get_admin_list"):
                return sender_id in [str(a) for a in self.context.get_admin_list()]
            if hasattr(self.context, "admin_list"):
                return sender_id in [str(a) for a in self.context.admin_list]
            config = self.context.get_config()
            for key in ["admins", "admin_users", "admin_list"]:
                if key in config:
                    if sender_id in [str(a) for a in config[key]]:
                        return True
        except Exception as e:
            logger.error(f"管理员鉴权异常: {e}")
        return False

    def _blocked(self, uid: str, kind: str = "all") -> bool:
        if uid not in self.blacklist:
            return False
        if kind == "all":
            return True
        return self.blacklist[uid].get(f"block_{kind}", True)

    def _sender_blocked(self, event: AstrMessageEvent) -> bool:
        try:
            uid = str(event.get_sender_id())
            return uid and self._blocked(uid, "msg")
        except Exception:
            return False

    @staticmethod
    def _ids(text: str) -> List[str]:
        return re.findall(r"\d+", text)

    def _get_admins(self) -> List[str]:
        try:
            if hasattr(self.context, "get_admin_list"):
                return [str(a) for a in self.context.get_admin_list()]
            if hasattr(self.context, "admin_list"):
                return [str(a) for a in self.context.admin_list]
            config = self.context.get_config()
            for key in ["admins", "admin_users", "admin_list"]:
                if key in config:
                    return [str(a) for a in config[key]]
        except Exception:
            pass
        return []

    def _add_to_blacklist(self, uid: str):
        """将用户加入黑名单，屏蔽所有"""
        if not uid:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.blacklist[uid] = dict(
            time=now, block_msg=True, block_friend=True, block_group_invite=True
        )
        self._save(self.bl_file, self.blacklist)

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
            for aid in self._get_admins():
                await self._api("send_private_msg", user_id=int(aid), message=msg)

    async def _notify_with_ids(self, msg: str) -> List[str]:
        ids = []
        if self.notify_group:
            res = await self._api("send_group_msg", group_id=int(self.notify_group), message=msg)
            if res and isinstance(res, dict):
                mid = res.get("data", {}).get("message_id")
                if mid:
                    ids.append(str(mid))
        else:
            for aid in self._get_admins():
                res = await self._api("send_private_msg", user_id=int(aid), message=msg)
                if res and isinstance(res, dict):
                    mid = res.get("data", {}).get("message_id")
                    if mid:
                        ids.append(str(mid))
        return ids

    def _get_reply_id(self, event: AstrMessageEvent) -> Optional[str]:
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

    # ───────── 请求事件自动监听 ─────────

    async def handle_event(self, event: AstrMessageEvent) -> Optional[AstrMessageEvent]:
        # 优先处理请求事件（好友申请/群邀请），不受消息屏蔽影响
        try:
            raw = event.message_obj.raw_message
            if isinstance(raw, dict):
                req_type = raw.get("request_type")
                if req_type == "friend":
                    await self._on_friend_req(raw)
                    return None
                elif req_type == "group" and raw.get("sub_type") == "invite":
                    await self._on_group_invite(raw)
                    return None
        except Exception:
            pass

        # 普通消息：检查消息屏蔽
        try:
            uid = str(event.get_sender_id())
            if uid and self._blocked(uid, "msg"):
                return None
        except Exception:
            pass

        return event

    async def _on_friend_req(self, raw: dict):
        uid = str(raw.get("user_id", ""))
        flag = str(raw.get("flag", ""))
        comment = raw.get("comment", "") or ""
        if not uid or not flag:
            return

        # 黑名单自动拒绝（检查 block_friend）
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
            f"💬 引用此消息回复: /同意 或 /拒绝 或 /拉黑审批"
        )
        if msg_ids:
            self.pending[flag]["notify_ids"] = msg_ids
            self._save(self.pd_file, self.pending)

    async def _on_group_invite(self, raw: dict):
        uid = str(raw.get("user_id", ""))
        gid = str(raw.get("group_id", ""))
        flag = str(raw.get("flag", ""))
        comment = raw.get("comment", "") or ""
        sub = raw.get("sub_type", "invite")
        if not flag:
            return

        # 黑名单自动拒绝（检查 block_group_invite）
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
            f"💬 引用此消息回复: /同意 或 /拒绝 或 /拉黑审批"
        )
        if msg_ids:
            self.pending[flag]["notify_ids"] = msg_ids
            self._save(self.pd_file, self.pending)

    # ───────── 查看列表 ─────────

    @filter.command("好友", alias=["fl"])
    async def cmd_friends(self, event: AstrMessageEvent):
        if self._sender_blocked(event):
            return
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
        if self._sender_blocked(event):
            return
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
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        uids = self._ids(args)
        if not uids:
            yield event.plain_result("⚠️ /拉黑 123 [456] ...  或引用通知消息回复 /拉黑审批")
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        added, dup = [], []
        for u in uids:
            if u in self.blacklist:
                dup.append(u)
            else:
                self.blacklist[u] = dict(
                    time=now, block_msg=True, block_friend=True, block_group_invite=True
                )
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
        if self._sender_blocked(event):
            return
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
        if self._sender_blocked(event):
            return
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
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        if not self.pending:
            yield event.plain_result("📋 无待处理请求")
            return

        lines = ["📋 待处理请求（引用对应消息回复 /同意 或 /拒绝 或 /拉黑审批）"]
        for flag, info in self.pending.items():
            t = info.get("time", "?")
            if info["type"] == "friend":
                lines.append(
                    f"🔹 好友 | 用户:{info['user_id']} | 理由:{info.get('comment', '无')} | {t}"
                )
            else:
                lines.append(
                    f"🔸 群邀 | 群:{info['group_id']} | 邀请人:{info['user_id']} | {t}"
                )

        yield event.plain_result("\n".join(lines))

    # ───────── 加好友 / 加群 ─────────

    @filter.command("加好友", alias=["addfriend"])
    async def cmd_add_friend(self, event: AstrMessageEvent, args: str = ""):
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        parts = args.strip().split(maxsplit=1)
        uids = self._ids(args)
        if not uids:
            yield event.plain_result("⚠️ /加好友 QQ号 [备注]")
            return

        uid = uids[0]
        comment = parts[1] if len(parts) > 1 else ""

        res = await self._api("send_friend_add_request", user_id=int(uid), comment=comment)

        if res and res.get("status") == "ok":
            yield event.plain_result(f"✅ 已发送好友申请\n用户: {uid}\n备注: {comment or '无'}")
        else:
            flag = f"fr_{uid}_{int(datetime.now().timestamp())}"
            self.pending[flag] = dict(
                type="friend", user_id=uid, comment=comment,
                time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                notify_ids=[],
            )
            self._save(self.pd_file, self.pending)
            yield event.plain_result(
                f"⚠️ 发送好友申请失败，已记录到待处理\n用户: {uid}"
            )

    @filter.command("加群", alias=["addgroup"])
    async def cmd_add_group(self, event: AstrMessageEvent, args: str = ""):
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        nums = self._ids(args)
        if not nums:
            yield event.plain_result("⚠️ /加群 群号")
            return

        gid = nums[0]

        res = await self._api("send_group_add_request", group_id=int(gid))

        if res and res.get("status") == "ok":
            yield event.plain_result(f"✅ 已发送加群申请\n群号: {gid}")
        else:
            flag = f"gi_{gid}_{int(datetime.now().timestamp())}"
            self.pending[flag] = dict(
                type="group", group_id=gid, user_id="0", sub_type="add",
                comment="主动加群",
                time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                notify_ids=[],
            )
            self._save(self.pd_file, self.pending)
            yield event.plain_result(
                f"⚠️ 加群失败，已记录到待处理\n群号: {gid}"
            )

    # ───────── 删好友 / 退群 ─────────

    @filter.command("删好友", alias=["deletefriend"])
    async def cmd_del_friend(self, event: AstrMessageEvent, args: str = ""):
        if self._sender_blocked(event):
            return
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
        if self._sender_blocked(event):
            return
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

    # ───────── 同意 / 拒绝 / 拉黑（统一审批）─────────

    async def _process_reply(self, event: AstrMessageEvent, action: str):
        """
        通过引用消息统一审批
        action: "accept" | "reject" | "block"
        """
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        # 必须引用消息
        reply_id = self._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("⚠️ 请引用通知消息回复 /同意 或 /拒绝 或 /拉黑审批")
            return

        # 查找对应的 flag
        flag = self._find_flag_by_msg_id(reply_id)
        if not flag:
            yield event.plain_result("❌ 该引用消息未匹配到待处理请求")
            return

        info = self.pending.get(flag)
        if not info:
            yield event.plain_result("❌ 该请求已过期或已处理")
            return

        uid = info.get("user_id", "")
        kind = "好友申请" if info["type"] == "friend" else "群邀请"
        if info["type"] == "friend":
            target = f"用户 {uid}"
        else:
            target = f"群 {info['group_id']} (邀请人: {uid})"

        # ── 拉黑模式：拒绝请求 + 加入黑名单 ──
        if action == "block":
            # 1. 拒绝当前请求
            try:
                if info["type"] == "friend":
                    await self._api("set_friend_add_request", flag=flag, approve=False)
                else:
                    await self._api(
                        "set_group_add_request",
                        flag=flag, approve=False,
                        sub_type=info.get("sub_type", "invite"),
                    )
            except Exception as e:
                logger.error(f"拒绝 {flag} 异常: {e}")

            # 2. 加入黑名单（屏蔽消息+好友+群邀请），且确保 uid 有效
            if uid and uid != "0":
                self._add_to_blacklist(uid)

            # 3. 从待处理移除
            self.pending.pop(flag, None)
            self._save(self.pd_file, self.pending)

            yield event.plain_result(
                f"🚫 已拒绝{kind}并拉黑\n{target}\n"
                f"该用户后续所有好友申请和群邀请将被自动拒绝"
            )
            return

        # ── 同意 / 拒绝模式 ──
        approve = (action == "accept")
        try:
            if info["type"] == "friend":
                r = await self._api("set_friend_add_request", flag=flag, approve=approve)
            else:
                r = await self._api(
                    "set_group_add_request",
                    flag=flag, approve=approve,
                    sub_type=info.get("sub_type", "invite"),
                )

            if r and r.get("status") == "ok":
                self.pending.pop(flag, None)
                self._save(self.pd_file, self.pending)

                act_text = "同意" if approve else "拒绝"
                yield event.plain_result(f"✅ 已{act_text}{kind}\n{target}")
            else:
                yield event.plain_result("❌ 操作失败，平台返回异常")
        except Exception as e:
            logger.error(f"处理 {flag} 异常: {e}")
            yield event.plain_result("❌ 操作异常，请查看日志")

    @filter.command("同意", alias=["accept"])
    async def cmd_accept(self, event: AstrMessageEvent):
        """引用通知消息回复 /同意"""
        async for result in self._process_reply(event, action="accept"):
            yield result

    @filter.command("拒绝", alias=["reject"])
    async def cmd_reject(self, event: AstrMessageEvent):
        """引用通知消息回复 /拒绝"""
        async for result in self._process_reply(event, action="reject"):
            yield result

    @filter.command("拉黑审批", alias=["blockreply"])
    async def cmd_block_reply(self, event: AstrMessageEvent):
        """引用通知消息回复 /拉黑审批，拒绝并拉黑该用户"""
        async for result in self._process_reply(event, action="block"):
            yield result

    # ───────── 生命周期 ─────────

    async def terminate(self):
        logger.info("关系管理插件已停止")
