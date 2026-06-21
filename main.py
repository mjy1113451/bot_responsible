import json
import re
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# 尝试导入 AiocqhttpMessageEvent（用于类型提示）
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
except ImportError:
    AiocqhttpMessageEvent = None

# 导入扩展功能（混淆模块）
try:
    from .pkg import ExpansionHandle
except ImportError:
    try:
        from pkg import ExpansionHandle
    except ImportError:
        ExpansionHandle = None
        logger.warning("expansion 模块未找到，加好友/加群功能将不可用")


@register(
    "astrbot_plugin_relationship_manager",
    "mjy1113451",
    "AstrBot 关系管理插件",
    "5.2.0",
    "https://github.com/mjy1113451/bot_responsible",
)
class RelationshipManager(Star):

    # OneBot v11 CQ 码
    _CQ_REPLY_RE = re.compile(r"\[CQ:reply,id=(\d+)\]")
    # OneBot v12 / 标准 reply 结构匹配
    _MSG_ID_RE = re.compile(r'"message_id"\s*:\s*"?(\d+)"?')

    # 待处理请求过期时间（天）
    PENDING_TTL_DAYS = 7

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

        self.notify_group: Optional[str] = config.get("notify_group", None)
        self._lock = asyncio.Lock()
        self._cleanup_pending()

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
        """
        修复1: 重建字典代替就地修改，避免迭代时修改字典导致的跳跃问题
        修复2: 同时迁移顶层 group_blacklist（如果它是 dict 而非嵌套在 group_blacklist key 下）
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        changed = False

        # 快照当前条目（包含 group_blacklist key 本身）
        entries = list(self.blacklist.items())
        new_blacklist: Dict[str, dict] = {}

        for uid, val in entries:
            # 跳过元数据 key
            if uid == "group_blacklist":
                # group_blacklist 如果是 dict（而不是 {"group_blacklist": {...}} 嵌套），
                # 说明旧版格式没有外层包装，需要规范化
                if isinstance(val, dict):
                    new_blacklist[uid] = val
                continue

            if isinstance(val, str):
                # 字符串 → 完整 dict
                new_blacklist[uid] = dict(
                    time=now, block_msg=True, block_friend=True, block_group_invite=True
                )
                changed = True
            elif isinstance(val, dict):
                # dict 中移除废弃的 reason 字段
                if "reason" in val:
                    val.pop("reason")
                    changed = True
                new_blacklist[uid] = val
            else:
                new_blacklist[uid] = val

        # 检查顶层是否有游离的 group_blacklist entry（修复2）
        # 如果某个 key 的值是群号字符串，迁入 group_blacklist
        to_migrate_groups: Dict[str, dict] = {}
        for uid, val in list(entries):
            if uid == "group_blacklist":
                continue
            # 旧版可能把群号直接放在顶层，格式是 {"123456": "group"} 或 {"123456": {"time": "..."}}
            if isinstance(val, str) and self._valid_gid(uid):
                to_migrate_groups[uid] = {"time": now, "source": "migrated"}
                changed = True
            elif isinstance(val, dict) and "group_name" in val and self._valid_gid(uid):
# 看起来是群条目，不是用户
                to_migrate_groups[uid] = {"time": val.get("time", now), "source": "migrated"}
                changed = True

        if to_migrate_groups:
            existing = new_blacklist.get("group_blacklist", {})
            for gid, ginfo in to_migrate_groups.items():
                if gid not in existing:
                    existing[gid] = ginfo
            new_blacklist["group_blacklist"] = existing
            changed = True

        if changed:
            self.blacklist = new_blacklist
            self._save(self.bl_file, self.blacklist)

    # ───────── 工具 ─────────

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """使用 AstrBot 内置的管理员检测"""
        try:
            return event.is_admin()
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
        return re.findall(r"\b(\d{5,12})\b", text)

    @classmethod
    def _valid_uid(cls, uid: str) -> bool:
        return bool(re.fullmatch(r"\d{5,12}", uid))

    @classmethod
    def _valid_gid(cls, gid: str) -> bool:
        return bool(re.fullmatch(r"\d{5,12}", gid))

    def _get_admins(self) -> List[str]:
        """获取管理员列表"""
        try:
            config = self.context.get_config()
            return [str(a) for a in config.get("admins_id", [])]
        except Exception as e:
            logger.error(f"获取管理员列表异常: {e}")
        return []

    async def _add_to_blacklist(self, uid: str):
        """
        修复3: 添加锁保护，确保线程/协程安全
        """
        if not uid:
            return
        async with self._lock:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if uid in self.blacklist and isinstance(self.blacklist[uid], dict):
                self.blacklist[uid]["time"] = now
                self.blacklist[uid]["block_msg"] = True
                self.blacklist[uid]["block_friend"] = True
                self.blacklist[uid]["block_group_invite"] = True
            else:
                self.blacklist[uid] = dict(
                    time=now, block_msg=True, block_friend=True, block_group_invite=True
                )
            self._save(self.bl_file, self.blacklist)

    async def _api(self, name: str, event: AstrMessageEvent = None, **kw) -> Optional[dict]:
        """调用 OneBot API"""
        try:
            # 方式1: 通过 event.bot 直接获取客户端（推荐）
            if event and hasattr(event, 'bot'):
                client = event.bot
                if client and hasattr(client, name):
                    return await getattr(client, name)(**kw)

            # 方式2: 通过平台获取客户端
            if event:
                platform_id = event.get_platform_id()
                platform = self.context.get_platform_inst(platform_id)
                if platform:
                    client = platform.get_client()
                    if client and hasattr(client, name):
                        return await getattr(client, name)(**kw)

            # 方式3: 遍历所有平台查找支持的客户端
            for platform in self.context.platform_manager.get_insts():
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client and hasattr(client, name):
                        return await getattr(client, name)(**kw)

        except Exception as e:
            logger.error(f"API {name} 失败: {e}")
        return None

    async def _notify(self, msg: str):
        """修复7: 委托给 _notify_with_ids，忽略返回值"""
        await self._notify_with_ids(msg)

    async def _notify_with_ids(self, msg: str) -> List[str]:
        """发送通知消息"""
        ids = []
        try:
            # 尝试获取客户端
            client = None
            try:
                for platform in self.context.platform_manager.get_insts():
                    if hasattr(platform, 'get_client'):
                        client = platform.get_client()
                        if client:
                            break
            except Exception:
                pass

            if client:
                # 使用客户端直接发送
                if self.notify_group:
                    res = await client.send_group_msg(group_id=int(self.notify_group), message=msg)
                    if res and isinstance(res, dict):
                        mid = res.get("data", {}).get("message_id")
                        if mid:
                            ids.append(str(mid))
                else:
                    for aid in self._get_admins():
                        res = await client.send_private_msg(user_id=int(aid), message=msg)
                        if res and isinstance(res, dict):
                            mid = res.get("data", {}).get("message_id")
                            if mid:
                                ids.append(str(mid))
            else:
                # 回退到 send_message
                from astrbot.api.message_components import Plain
                message_chain = [Plain(text=msg)]

                # 获取平台名称
                platform_name = "aiocqhttp"
                try:
                    for platform in self.context.platform_manager.get_insts():
                        if hasattr(platform, 'meta'):
                            meta = platform.meta()
                            if hasattr(meta, 'name'):
                                platform_name = meta.name
                                break
                except Exception:
                    pass

                if self.notify_group:
                    session = f"{platform_name}:GroupMessage:{self.notify_group}"
                    await self.context.send_message(session, message_chain)
                else:
                    for aid in self._get_admins():
                        session = f"{platform_name}:FriendMessage:{aid}"
                        await self.context.send_message(session, message_chain)
        except Exception as e:
            logger.error(f"发送通知失败: {e}")
        return ids

    def _get_reply_id(self, event: AstrMessageEvent) -> Optional[str]:
        """
        修复5: 兼容 dict 形式的 CQ 码（OneBot 某些实现 message 元素是 dict 而非对象）
        """
        try:
            for comp in event.message_obj.message:
                # 对象形式（有 type 属性）
                if hasattr(comp, "type") and comp.type == "reply":
                    return str(comp.data.get("id", "") if hasattr(comp, "data") else "")
                # dict 形式
                if isinstance(comp, dict):
                    if comp.get("type") == "reply":
                        return str(comp.get("data", {}).get("id", ""))
        except Exception:
            pass
        try:
            raw_str = str(event.message_obj.raw_message)
            match = self._CQ_REPLY_RE.search(raw_str)
            if match:
                return match.group(1)
        except Exception:
            pass
        try:
            raw_str = str(event.message_obj.raw_message)
            match = self._MSG_ID_RE.search(raw_str)
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

    def _cleanup_pending(self):
        now = datetime.now()
        expired = []
        for flag, info in self.pending.items():
            try:
                t = datetime.strptime(info.get("time", ""), "%Y-%m-%d %H:%M:%S")
                if now - t > timedelta(days=self.PENDING_TTL_DAYS):
                    expired.append(flag)
            except (ValueError, TypeError):
                expired.append(flag)
        if expired:
            for flag in expired:
                self.pending.pop(flag, None)
            self._save(self.pd_file, self.pending)
            logger.info(f"已清理 {len(expired)} 条过期待处理请求")

    # ───────── 群黑名单管理 ─────────

    def _add_group_to_blacklist(self, gid: str):
        if not gid:
            return
        group_bl_key = "group_blacklist"
        group_blacklist = self.blacklist.get(group_bl_key, {})
        if str(gid) not in group_blacklist:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            group_blacklist[str(gid)] = {"time": now}
            self.blacklist[group_bl_key] = group_blacklist
            self._save(self.bl_file, self.blacklist)
            logger.info(f"群 {gid} 已加入黑名单")

    def _is_group_blocked(self, gid: str) -> bool:
        if not gid:
            return False
        group_bl_key = "group_blacklist"
        group_blacklist = self.blacklist.get(group_bl_key, {})
        return str(gid) in group_blacklist

    # ───────── 请求事件自动监听 ─────────

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_event(self, event: AstrMessageEvent) -> Optional[AstrMessageEvent]:
        try:
            raw = event.message_obj.raw_message
            if isinstance(raw, dict):
                req_type = raw.get("request_type")
                if req_type == "friend":
                    await self._on_friend_req(raw, event)
                    return None
                elif req_type == "group" and raw.get("sub_type") == "invite":
                    await self._on_group_invite(raw, event)
                    return None
        except Exception as e:
            logger.error(f"处理请求事件异常: {e}")

        try:
            uid = str(event.get_sender_id())
            if uid and self._blocked(uid, "msg"):
                return None
        except Exception:
            pass

        return event

    async def _on_friend_req(self, raw: dict, event: AstrMessageEvent = None):
        uid = str(raw.get("user_id", ""))
        flag = str(raw.get("flag", ""))
        comment = raw.get("comment", "") or ""

        if not uid or not flag:
            return

        if not self._valid_uid(uid):
            logger.warning(f"好友申请 uid 格式异常，已忽略: {uid}")
            return

        if self._blocked(uid, "friend"):
            await self._api("set_friend_add_request", event=event, flag=flag, approve=False)
            await self._notify(f"🚫 自动拒绝黑名单好友申请\nQQ号: {uid}")
            return

        nickname = uid
        try:
            info_res = await self._api("get_stranger_info", event=event, user_id=int(uid))
            if info_res and info_res.get("status") == "ok":
                nickname = info_res.get("data", {}).get("nickname", uid)
        except Exception:
            pass

        async with self._lock:
            self.pending[flag] = dict(
                type="friend", user_id=uid, nickname=nickname, comment=comment,
                time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                notify_ids=[],
            )
            self._save(self.pd_file, self.pending)

        msg_ids = await self._notify_with_ids(
            f"【好友申请】同意/拒绝/拉黑：\n"
            f"昵称：{nickname}\n"
            f"QQ号：{uid}\n"
            f"验证信息：{comment if comment else '无'}\n"
            f"💬 引用此消息回复: /同意 或 /拒绝 或 /拉黑"
        )
        if msg_ids:
            async with self._lock:
                self.pending[flag]["notify_ids"] = msg_ids
                self._save(self.pd_file, self.pending)

    async def _on_group_invite(self, raw: dict, event: AstrMessageEvent = None):
        """
        修复4: uid="0"（机器人主动申请加群）场景单独处理，
        走 group_blacklist 校验而非 user blacklist
        """
        uid = str(raw.get("user_id", ""))
        gid = str(raw.get("group_id", ""))
        flag = str(raw.get("flag", ""))
        comment = raw.get("comment", "") or ""
        sub = raw.get("sub_type", "invite")
        if not flag:
            return

        if uid and not self._valid_uid(uid):
            logger.warning(f"群邀请 uid 格式异常，已忽略: {uid}")
            return
        if gid and not self._valid_gid(gid):
            logger.warning(f"群邀请 gid 格式异常，已忽略: {gid}")
            return

        # uid="0" 表示机器人主动申请加群，跳过用户黑名单校验，只走群黑名单
        if uid and uid != "0" and self._blocked(uid, "group_invite"):
            await self._api("set_group_add_request", event=event, flag=flag, approve=False, sub_type=sub)
            await self._notify(f"🚫 自动拒绝黑名单群邀请\n邀请人: {uid}\n群号: {gid}")
            return

        if self._is_group_blocked(gid):
            res = await self._api("set_group_add_request", event=event, flag=flag, approve=False, sub_type=sub)
            if not res or res.get("retcode") != 0:
                logger.warning(f"无法拒绝黑名单群 {gid} 的邀请（可能已被拉入），等待进群后处理")
            else:
                await self._notify(
                    f"🚫 自动拒绝黑名单群邀请\n群号: {gid}\n"
                    f"⚠️ 该群曾将Bot踢出，已自动拒绝邀请"
                )
                return

        inviter_nickname = uid if uid != "0" else "（机器人主动申请）"
        if uid and uid != "0":
            try:
                info_res = await self._api("get_stranger_info", event=event, user_id=int(uid))
                if info_res and info_res.get("status") == "ok":
                    inviter_nickname = info_res.get("data", {}).get("nickname", uid)
            except Exception:
                pass

        group_name = gid
        try:
            group_res = await self._api("get_group_info", event=event, group_id=int(gid))
            if group_res and group_res.get("status") == "ok":
                group_name = group_res.get("data", {}).get("group_name", gid)
        except Exception:
            pass

        async with self._lock:
            self.pending[flag] = dict(
                type="group", group_id=gid, group_name=group_name,
                user_id=uid, inviter_nickname=inviter_nickname,
                sub_type=sub, comment=comment,
                time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                notify_ids=[],
            )
            self._save(self.pd_file, self.pending)

        msg_ids = await self._notify_with_ids(
            f"【群邀请】同意/拒绝/拉黑：\n"
            f"邀请人昵称：{inviter_nickname}\n"
            f"邀请人QQ：{uid}\n"
            f"群名称：{group_name}\n"
            f"群号：{gid}\n"
            f"验证信息：{comment if comment else '无'}\n"
            f"💬 引用此消息回复: /同意 或 /拒绝 或 /拉黑"
        )
        if msg_ids:
            async with self._lock:
                self.pending[flag]["notify_ids"] = msg_ids
                self._save(self.pd_file, self.pending)

    # ───────── 通知事件监听（被踢 / 进群）─────────

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_notice(self, event: AstrMessageEvent):
        """
        修复9: asyncio.sleep(2) 包装在 try/except 中，防止 notice_type 不匹配时异常泄漏
        """
        try:
            raw = event.message_obj.raw_message
            if isinstance(raw, dict):
                notice_type = raw.get("notice_type")
                group_id = str(raw.get("group_id", ""))
                user_id = str(raw.get("user_id", ""))
                operator_id = str(raw.get("operator_id", ""))
                self_id = str(event.get_self_id())

                if notice_type == "group_decrease":
                    sub_type = raw.get("sub_type", "")
                    if sub_type in ("kick", "kick_me") and user_id == self_id:
                        if group_id:
                            self._add_group_to_blacklist(group_id)
                            logger.info(f"Bot被踢出群 {group_id}，已将该群加入黑名单")
                            await self._notify(
                                f"⚠️ Bot被踢出群 {group_id}\n"
                                f"操作者: {operator_id}\n"
                                f"该群已加入黑名单，后续邀请将被自动拒绝"
                            )

                elif notice_type == "group_increase":
                    sub_type = raw.get("sub_type", "")
                    if user_id == self_id and group_id:
                        if self._is_group_blocked(group_id):
                            try:
                                await self._api(
                                    "send_group_msg",
                                    event=event,
                                    group_id=int(group_id),
                                    message="别老是让我进来又给我踢出去，烦不烦啊？！"
                                )
                                await asyncio.sleep(2)
                                await self._api("set_group_leave", event=event, group_id=int(group_id))
                            except Exception as notify_err:
                                logger.error(f"黑名单群 {group_id} 退群异常: {notify_err}")
                            logger.info(f"已自动退出黑名单群 {group_id}")
                            await self._notify(
                                f"🚫 Bot被拉入黑名单群 {group_id}\n"
                                f"已发送提示并自动退出该群"
                            )

        except Exception as e:
            logger.error(f"处理通知事件异常: {e}")

    # ───────── 查看列表 ─────────

    @filter.command("好友", alias=["fl"])
    async def cmd_friends(self, event: AstrMessageEvent):
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        res = await self._api("get_friend_list", event=event)
        if not res:
            yield event.plain_result("❌ 获取失败")
            return

        # API 返回格式：可能是 list 或 dict
        if isinstance(res, list):
            friends = res
        elif isinstance(res, dict):
            friends = res.get("data", [])
        else:
            yield event.plain_result("❌ 获取失败")
            return

        if not friends:
            yield event.plain_result("📋 没有好友")
            return

        lines = ["📋 好友列表"]
        for i, f in enumerate(friends, 1):
            uid = f.get("user_id", "?")
            nickname = f.get("nickname", "?")
            remark = f.get("remark", "")
            # 显示备注名（如果有）
            display_name = f"{remark}({nickname})" if remark and remark != nickname else nickname
            tag = " 🚫" if self._blocked(str(uid)) else ""
            lines.append(f"{i}. {display_name} ({uid}){tag}")

        lines.append(f"\n💡 共 {len(friends)} 人")
        yield event.plain_result("\n".join(lines))

    @filter.command("群", alias=["gl"])
    async def cmd_groups(self, event: AstrMessageEvent):
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        res = await self._api("get_group_list", event=event)
        if not res:
            yield event.plain_result("❌ 获取失败")
            return

        # API 返回格式：可能是 list 或 dict
        if isinstance(res, list):
            groups = res
        elif isinstance(res, dict):
            groups = res.get("data", [])
        else:
            yield event.plain_result("❌ 获取失败")
            return

        if not groups:
            yield event.plain_result("📋 没有群")
            return

        lines = ["📋 群列表"]
        for i, g in enumerate(groups, 1):
            gid = g.get('group_id', '?')
            group_name = g.get('group_name', '?')
            member_count = g.get('member_count', '?')
            tag = " 🚫" if self._is_group_blocked(str(gid)) else ""
            lines.append(f"{i}. {group_name} ({gid}) {member_count}人{tag}")

        lines.append(f"\n💡 共 {len(groups)} 个群")
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
            yield event.plain_result("⚠️ /拉黑 12345 [67890] ...  或引用通知消息回复 /拉黑")
            return

        valid, invalid = [], []
        for u in uids:
            if self._valid_uid(u):
                valid.append(u)
            else:
                invalid.append(u)

        if invalid:
            yield event.plain_result(f"⚠️ 格式无效（需5-12位数字）: {', '.join(invalid)}")
            if not valid:
                return

        async with self._lock:
            added, dup = [], []
            for u in valid:
                if u in self.blacklist:
                    dup.append(u)
                else:
                    self.blacklist[u] = dict(
                        time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        block_msg=True, block_friend=True, block_group_invite=True,
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
            yield event.plain_result("⚠️ /解封 12345 [67890] ...")
            return

        async with self._lock:
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
            if uid == "group_blacklist":
                group_bl = info
                for gid, g_info in group_bl.items():
                    t = g_info.get("time", "?")
                    lines.append(f"- 群 {gid} | 加入黑名单时间: {t}")
                continue

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

        async with self._lock:
            self._cleanup_pending()
            pending_snapshot = dict(self.pending)

        if not pending_snapshot:
            yield event.plain_result("📋 无待处理请求")
            return

        lines = ["📋 待处理请求（引用对应消息回复 /同意 或 /拒绝 或 /拉黑）"]
        for flag, info in pending_snapshot.items():
            t = info.get("time", "?")
            if info["type"] == "friend":
                nickname = info.get('nickname', info['user_id'])
                comment = info.get('comment') or '无'
                lines.append(
                    f"🔹 好友 | 昵称:{nickname} QQ:{info['user_id']} | 验证:{comment} | {t}"
                )
            else:
                inviter_nickname = info.get('inviter_nickname', info['user_id'])
                group_name = info.get('group_name', info['group_id'])
                comment = info.get('comment') or '无'
                lines.append(
                    f"🔸 群邀 | 群:{group_name}({info['group_id']}) | 邀请人:{inviter_nickname}({info['user_id']}) | 验证:{comment} | {t}"
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
            yield event.plain_result("⚠️ /加好友 QQ号 [验证消息] [备注]")
            return

        uid = uids[0]
        if not self._valid_uid(uid):
            yield event.plain_result(f"⚠️ QQ号格式无效（需5-12位数字）: {uid}")
            return

        # 解析参数
        verify = ""
        remark = ""
        if len(parts) > 1:
            sub_args = parts[1].split()
            if len(sub_args) >= 1:
                verify = sub_args[0]
            if len(sub_args) >= 2:
                remark = sub_args[1]

        # 获取客户端
        client = None
        if hasattr(event, 'bot'):
            client = event.bot
        else:
            for platform in self.context.platform_manager.get_insts():
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client:
                        break

        if not client:
            yield event.plain_result("❌ 无法获取客户端")
            return

        try:
            self_id = int(event.get_self_id())
            target_uin = int(uid)
            msg = await ExpansionHandle.add_friend(
                client=client,
                target_uin=target_uin,
                self_id=self_id,
                verify=verify,
                remark=remark,
            )
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"加好友失败: {e}")
            # 检查是否是 Packet 超时错误
            if "timeout" in str(e).lower() or "sendPacket" in str(e):
                yield event.plain_result(
                    f"⚠️ Packet 服务超时，可能原因：\n"
                    f"1. NapCat PacketServer 未正确配置\n"
                    f"2. 当前 NapCat 版本不支持此命令\n"
                    f"3. 网络连接问题\n\n"
                    f"请手动在 QQ 上添加好友: {uid}"
                )
            else:
                yield event.plain_result(f"❌ 加好友失败: {str(e)}")

    @filter.command("加群", alias=["addgroup"])
    async def cmd_add_group(self, event: AstrMessageEvent, args: str = ""):
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        nums = self._ids(args)
        if not nums:
            yield event.plain_result("⚠️ /加群 群号 [答案]")
            return

        gid = nums[0]
        if not self._valid_gid(gid):
            yield event.plain_result(f"⚠️ 群号格式无效（需5-12位数字）: {gid}")
            return

        if self._is_group_blocked(gid):
            yield event.plain_result(
                f"⚠️ 群 {gid} 在黑名单中，无法加入\n"
                f"该群曾将Bot踢出，如需加入请先使用 /解封群 命令"
            )
            return

        # 解析答案参数
        answer = ""
        parts = args.strip().split()
        if len(parts) > 1:
            answer = parts[1]

        # 获取客户端
        client = None
        if hasattr(event, 'bot'):
            client = event.bot
        else:
            for platform in self.context.platform_manager.get_insts():
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client:
                        break

        if not client:
            yield event.plain_result("❌ 无法获取客户端")
            return

        try:
            target_gid = int(gid)
            msg = await ExpansionHandle.add_group(
                client=client,
                target_gid=target_gid,
                answer=answer,
            )
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"加群失败: {e}")
            # 检查是否是 Packet 超时错误
            if "timeout" in str(e).lower() or "sendPacket" in str(e):
                yield event.plain_result(
                    f"⚠️ Packet 服务超时，可能原因：\n"
                    f"1. NapCat PacketServer 未正确配置\n"
                    f"2. 当前 NapCat 版本不支持此命令\n"
                    f"3. 网络连接问题\n\n"
                    f"请手动在 QQ 上加入群: {gid}"
                )
            else:
                yield event.plain_result(f"❌ 加群失败: {str(e)}")

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
            yield event.plain_result("⚠️ /删好友 12345 [67890] ...")
            return

        ok, fail = [], []
        for u in uids:
            r = await self._api("delete_friend", event=event, user_id=int(u))
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
            yield event.plain_result("⚠️ /退群 111222 [333444] ...")
            return

        ok, fail = [], []
        for g in gids:
            r = await self._api("set_group_leave", event=event, group_id=int(g))
            (ok if r and r.get("status") == "ok" else fail).append(g)

        parts = []
        if ok:
            parts.append(f"✅ 已退群 {len(ok)} 个: {', '.join(ok)}")
        if fail:
            parts.append(f"❌ 失败: {', '.join(fail)}")
        yield event.plain_result("\n".join(parts) if parts else "❌ 无结果")

    # ───────── 同意 / 拒绝 / 拉黑（统一审批）─────────

    async def _process_reply(self, event: AstrMessageEvent, action: str):
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        reply_id = self._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("⚠️ 请引用通知消息回复 /同意 或 /拒绝 或 /拉黑")
            return

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
            nickname = info.get("nickname", uid)
            target = f"昵称：{nickname}\nQQ号：{uid}"
        else:
            inviter_nickname = info.get('inviter_nickname', uid)
            group_name = info.get('group_name', info.get('group_id', '?'))
            target = f"群：{group_name}\n邀请人：{inviter_nickname}"

        if action == "block":
            try:
                if info["type"] == "friend":
                    await self._api("set_friend_add_request", event=event, flag=flag, approve=False)
                else:
                    await self._api(
                        "set_group_add_request", event=event, flag=flag, approve=False,
                        sub_type=info.get("sub_type", "invite"),
                    )
            except Exception as e:
                logger.error(f"拒绝 {flag} 异常: {e}")

            # 修复3: _add_to_blacklist 现在是 async，加 await
            if uid and uid != "0":
                await self._add_to_blacklist(uid)

            async with self._lock:
                self.pending.pop(flag, None)
                self._save(self.pd_file, self.pending)

            yield event.plain_result(
                f"🚫 已拒绝{kind}并拉黑\n{target}\n"
                f"该用户后续所有好友申请和群邀请将被自动拒绝"
            )
            return

        approve = (action == "accept")
        try:
            if info["type"] == "friend":
                r = await self._api("set_friend_add_request", event=event, flag=flag, approve=approve)
            else:
                r = await self._api(
                    "set_group_add_request", event=event, flag=flag, approve=approve,
                    sub_type=info.get("sub_type", "invite"),
                )

            if r and r.get("status") == "ok":
                async with self._lock:
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
        async for result in self._process_reply(event, action="accept"):
            yield result

    @filter.command("拒绝", alias=["reject"])
    async def cmd_reject(self, event: AstrMessageEvent):
        async for result in self._process_reply(event, action="reject"):
            yield result

    @filter.command("拉黑请求", alias=["blockreply"])
    async def cmd_block_reply(self, event: AstrMessageEvent):
        async for result in self._process_reply(event, action="block"):
            yield result

    # ───────── 群黑名单管理命令 ─────────

    @filter.command("拉黑群", alias=["addblg"])
    async def cmd_bl_add_group(self, event: AstrMessageEvent, args: str = ""):
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        gids = self._ids(args)
        if not gids:
            yield event.plain_result("⚠️ /拉黑群 群号1 [群号2] ...")
            return

        valid, invalid = [], []
        for g in gids:
            if self._valid_gid(g):
                valid.append(g)
            else:
                invalid.append(g)

        if invalid:
            yield event.plain_result(f"⚠️ 群号格式无效（需5-12位数字）: {', '.join(invalid)}")
            if not valid:
                return

        async with self._lock:
            group_bl_key = "group_blacklist"
            group_blacklist = self.blacklist.get(group_bl_key, {})
            added, dup = [], []

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for g in valid:
                if str(g) in group_blacklist:
                    dup.append(g)
                else:
                    group_blacklist[str(g)] = {"time": now}
                    added.append(g)

            self.blacklist[group_bl_key] = group_blacklist
            self._save(self.bl_file, self.blacklist)

        parts = []
        if added:
            parts.append(f"✅ 已拉黑 {len(added)} 个群: {', '.join(added)}")
        if dup:
            parts.append(f"⚠️ 已存在: {', '.join(dup)}")
        yield event.plain_result("\n".join(parts))

    @filter.command("解封群", alias=["rmblg"])
    async def cmd_bl_rm_group(self, event: AstrMessageEvent, args: str = ""):
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        gids = self._ids(args)
        if not gids:
            yield event.plain_result("⚠️ /解封群 群号1 [群号2] ...")
            return

        async with self._lock:
            group_bl_key = "group_blacklist"
            group_blacklist = self.blacklist.get(group_bl_key, {})
            removed, miss = [], []

            for g in gids:
                if str(g) in group_blacklist:
                    del group_blacklist[str(g)]
                    removed.append(g)
                else:
                    miss.append(g)

            self.blacklist[group_bl_key] = group_blacklist
            self._save(self.bl_file, self.blacklist)

        parts = []
        if removed:
            parts.append(f"✅ 已解封 {len(removed)} 个群: {', '.join(removed)}")
        if miss:
            parts.append(f"⚠️ 不存在: {', '.join(miss)}")
        yield event.plain_result("\n".join(parts))

    # ───────── 通知群设置 ─────────

    @filter.command("通知群", alias=["setnotify", "setgroup"])
    async def cmd_set_notify_group(self, event: AstrMessageEvent, args: str = ""):
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        arg = args.strip()
        if not arg or arg == "查看":
            if self.notify_group:
                yield event.plain_result(f"📢 当前通知群: {self.notify_group}")
            else:
                yield event.plain_result("📢 当前未设置通知群（通知发送给管理员私聊）")
            return

        if arg in ("取消", "清空", "关闭", "none", "null", "off"):
            self.notify_group = None
            config = self.context.get_config()
            config["notify_group"] = None
            self.context.set_config(config)
            yield event.plain_result("✅ 已取消通知群，通知将发送给管理员私聊")
            return

        gids = self._ids(arg)
        if not gids:
            yield event.plain_result("⚠️ /通知群 123456  或  /通知群 取消")
            return

        gid = gids[0]
        if not self._valid_gid(gid):
            yield event.plain_result(f"⚠️ 群号格式无效（需5-12位数字）: {gid}")
            return

        self.notify_group = gid
        config = self.context.get_config()
        config["notify_group"] = gid
        self.context.set_config(config)
        yield event.plain_result(f"✅ 通知群已设置为: {gid}\n后续好友申请和群邀请通知将发送到该群")

    # ───────── 生命周期 ─────────

    async def terminate(self):
        logger.info("关系管理插件已停止")