import os
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

from astrbot.api.event import filter, EventMessageType, AstrMessageEvent, GroupRequestEvent, FriendRequestEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Image
from astrbot.api import logger

# 插件元数据
@register(
    "astrbot_plugin_relationship_manager",
    "YourName",
    "AstrBot 关系管理插件 - 批量指令版",
    "1.2.0",
    "https://github.com/your-repo/astrbot_plugin_relationship_manager"
)
class RelationshipManagerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 初始化数据路径
        self.data_path = Path(context.get_astrbot_config().get("data_path", "data")) / "plugins" / "astrbot_plugin_relationship_manager"
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.blacklist_file = self.data_path / "blacklist.json"
        self.pending_file = self.data_path / "pending_requests.json"
        
        # 黑名单格式: {user_id: {"time": "xxx", "block_msg": True, "block_friend": True, "block_group_invite": True}}
        self.blacklist: Dict[str, dict] = self.load_blacklist()
        self.pending_requests: Dict[str, dict] = self.load_pending_requests()
        
        # 通知配置
        self.notify_enabled = True
        self.notify_group_id = None

    def load_blacklist(self) -> Dict[str, dict]:
        """加载黑名单"""
        if self.blacklist_file.exists():
            try:
                with open(self.blacklist_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 兼容旧格式
                    converted = {}
                    for k, v in data.items():
                        if isinstance(v, str):
                            converted[k] = {
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "block_msg": True,
                                "block_friend": True,
                                "block_group_invite": True
                            }
                        elif isinstance(v, dict) and "reason" in v:
                            # 去掉旧版的reason字段
                            converted[k] = {
                                "time": v.get("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                                "block_msg": v.get("block_msg", True),
                                "block_friend": v.get("block_friend", True),
                                "block_group_invite": v.get("block_group_invite", True)
                            }
                        else:
                            converted[k] = v
                    return converted
            except Exception as e:
                logger.error(f"加载黑名单失败: {e}")
                return {}
        return {}

    def save_blacklist(self):
        """保存黑名单"""
        try:
            with open(self.blacklist_file, 'w', encoding='utf-8') as f:
                json.dump(self.blacklist, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存黑名单失败: {e}")

    def load_pending_requests(self) -> Dict[str, dict]:
        """加载待处理请求"""
        if self.pending_file.exists():
            try:
                with open(self.pending_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载待处理请求失败: {e}")
                return {}
        return {}

    def save_pending_requests(self):
        """保存待处理请求"""
        try:
            with open(self.pending_file, 'w', encoding='utf-8') as f:
                json.dump(self.pending_requests, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存待处理请求失败: {e}")

    def is_admin(self, event: AstrMessageEvent) -> bool:
        """检查用户是否为管理员"""
        admin_ids = self.context.get_astrbot_config().get("admins", [])
        sender_id = str(event.get_sender_id())
        return sender_id in [str(admin_id) for admin_id in admin_ids]

    def is_blacklisted(self, user_id: str, check_type: str = "all") -> bool:
        """检查用户是否在黑名单中"""
        if user_id not in self.blacklist:
            return False
        if check_type == "all":
            return True
        info = self.blacklist[user_id]
        if check_type == "msg":
            return info.get("block_msg", True)
        elif check_type == "friend":
            return info.get("block_friend", True)
        elif check_type == "group_invite":
            return info.get("block_group_invite", True)
        return True

    def extract_ids(self, text: str) -> List[str]:
        """从文本中提取所有纯数字ID"""
        return re.findall(r'\d+', text)

    async def call_platform_api(self, api_name: str, **kwargs) -> Optional[dict]:
        """安全调用平台API的包装方法"""
        try:
            return await self.call_api(api_name, **kwargs)
        except Exception as e:
            logger.error(f"调用API {api_name} 失败: {e}")
            return None

    async def send_notification(self, message: str):
        """发送通知到管理员"""
        if self.notify_group_id:
            await self.call_platform_api("send_group_msg", group_id=int(self.notify_group_id), message=message)
        else:
            admin_ids = self.context.get_astrbot_config().get("admins", [])
            for admin_id in admin_ids:
                await self.call_platform_api("send_private_msg", user_id=int(admin_id), message=message)

    # ================= 消息拦截器 =================
    @filter.event_message_type(EventMessageType.ALL)
    async def block_blacklist_messages(self, event: AstrMessageEvent):
        """拦截黑名单用户的所有消息"""
        sender_id = str(event.get_sender_id())
        if self.is_blacklisted(sender_id, "msg"):
            logger.debug(f"已屏蔽黑名单用户 {sender_id} 的消息")
            event.stop_event()
            return

    # ================= 列表查看指令 =================
    @filter.command("好友", alias=["fl"])
    async def friend_list(self, event: AstrMessageEvent, args: str = ""):
        """查看好友列表"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        result = await self.call_platform_api("get_friend_list")
        if result and result.get('status') == 'ok':
            friends = result.get('data', [])
            if not friends:
                yield event.plain_result("📋 当前没有好友。")
                return

            reply = ["📋 **好友列表**\n"]
            for i, friend in enumerate(friends, 1):
                uid = friend.get('user_id', 'Unknown')
                nick = friend.get('nickname', 'Unknown')
                tag = " 🚫" if self.is_blacklisted(str(uid)) else ""
                reply.append(f"{i}. {nick} ({uid}){tag}")

            yield event.plain_result("\n".join(reply))
        else:
            yield event.plain_result("❌ 获取好友列表失败，请检查平台适配器是否支持此功能。")

    @filter.command("群", alias=["gl"])
    async def group_list(self, event: AstrMessageEvent, args: str = ""):
        """查看群列表"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        result = await self.call_platform_api("get_group_list")
        if result and result.get('status') == 'ok':
            groups = result.get('data', [])
            if not groups:
                yield event.plain_result("📋 当前没有加入任何群。")
                return

            reply = ["📋 **群列表**\n"]
            for i, group in enumerate(groups, 1):
                reply.append(f"{i}. {group.get('group_name', 'Unknown')} ({group.get('group_id', 'Unknown')})")

            yield event.plain_result("\n".join(reply))
        else:
            yield event.plain_result("❌ 获取群列表失败，请检查平台适配器是否支持此功能。")

    # ================= 黑名单管理指令（支持批量）=================
    @filter.command("拉黑", alias=["addbl", "屏蔽"])
    async def add_to_blacklist(self, event: AstrMessageEvent, args: str = ""):
        """批量拉黑用户，屏蔽所有消息、好友申请和群邀请
        
        用法: 
          /拉黑 123456
          /拉黑 123456 789012 345678
        """
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        args = args.strip()
        if not args:
            yield event.plain_result("⚠️ 用法: /拉黑 [QQ号1] [QQ号2] ...")
            return

        # 提取所有数字ID
        all_ids = self.extract_ids(args)
        if not all_ids:
            yield event.plain_result("❌ 未检测到有效的QQ号。")
            return

        # 批量写入黑名单
        added = []
        already = []
        for user_id in all_ids:
            if user_id in self.blacklist:
                already.append(user_id)
            else:
                self.blacklist[user_id] = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "block_msg": True,
                    "block_friend": True,
                    "block_group_invite": True
                }
                added.append(user_id)

        self.save_blacklist()

        reply_parts = []
        if added:
            reply_parts.append(f"✅ 已拉黑 {len(added)} 人: {', '.join(added)}")
        if already:
            reply_parts.append(f"⚠️ 已在黑名单: {', '.join(already)}")
        reply_parts.append("屏蔽: 消息 ✅ | 好友申请 ✅ | 群邀请 ✅")

        yield event.plain_result("\n".join(reply_parts))

    @filter.command("解封", alias=["rmbl", "取消屏蔽"])
    async def remove_from_blacklist(self, event: AstrMessageEvent, args: str = ""):
        """批量解封用户
        
        用法:
          /解封 123456
          /解封 123456 789012 345678
        """
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        args = args.strip()
        if not args:
            yield event.plain_result("⚠️ 用法: /解封 [QQ号1] [QQ号2] ...")
            return

        all_ids = self.extract_ids(args)
        if not all_ids:
            yield event.plain_result("❌ 未检测到有效的QQ号。")
            return

        removed = []
        not_found = []
        for user_id in all_ids:
            if user_id in self.blacklist:
                del self.blacklist[user_id]
                removed.append(user_id)
            else:
                not_found.append(user_id)

        self.save_blacklist()

        reply_parts = []
        if removed:
            reply_parts.append(f"✅ 已解封 {len(removed)} 人: {', '.join(removed)}")
        if not_found:
            reply_parts.append(f"⚠️ 不在黑名单: {', '.join(not_found)}")

        yield event.plain_result("\n".join(reply_parts))

    @filter.command("黑名单", alias=["lsbl"])
    async def show_blacklist(self, event: AstrMessageEvent, args: str = ""):
        """查看黑名单"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        if not self.blacklist:
            yield event.plain_result("📋 当前黑名单为空。")
            return

        reply = [f"🚫 **黑名单** (共 {len(self.blacklist)} 人)\n"]
        for user_id, info in self.blacklist.items():
            if isinstance(info, dict):
                block_msg = "✅" if info.get("block_msg", True) else "❌"
                block_friend = "✅" if info.get("block_friend", True) else "❌"
                block_group = "✅" if info.get("block_group_invite", True) else "❌"
                time_str = info.get("time", "未知")
                reply.append(f"- {user_id} | {time_str} | 消息{block_msg} 好友{block_friend} 群邀请{block_group}")
            else:
                reply.append(f"- {user_id}")

        yield event.plain_result("\n".join(reply))

    # ================= 待处理请求列表指令 =================
    @filter.command("待处理", alias=["pending"])
    async def show_pending_requests(self, event: AstrMessageEvent, args: str = ""):
        """查看待处理的好友申请和群邀请"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        if not self.pending_requests:
            yield event.plain_result("📋 当前没有待处理的请求。")
            return

        reply = ["📋 **待处理请求**\n"]
        for flag, info in self.pending_requests.items():
            if info['type'] == 'friend':
                reply.append(
                    f"🔹 好友申请 [{flag}]\n"
                    f"   用户: {info['user_id']}\n"
                    f"   理由: {info.get('comment', '无')}\n"
                    f"   时间: {info.get('time', '未知')}\n"
                    f"   处理: /同意 {flag} 或 /拒绝 {flag}\n"
                )
            elif info['type'] == 'group':
                reply.append(
                    f"🔸 群邀请 [{flag}]\n"
                    f"   群号: {info['group_id']}\n"
                    f"   邀请人: {info['user_id']}\n"
                    f"   理由: {info.get('comment', '无')}\n"
                    f"   时间: {info.get('time', '未知')}\n"
                    f"   处理: /同意群 {flag} 或 /拒绝群 {flag}\n"
                )

        yield event.plain_result("\n".join(reply))

    # ================= 好友与群管理指令（支持批量）=================
    @filter.command("删好友", alias=["deletefriend"])
    async def delete_friend(self, event: AstrMessageEvent, args: str = ""):
        """批量删除好友
        
        用法:
          /删好友 123456
          /删好友 123456 789012 345678
        """
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        args = args.strip()
        if not args:
            yield event.plain_result("⚠️ 用法: /删好友 [QQ号1] [QQ号2] ...")
            return

        all_ids = self.extract_ids(args)
        if not all_ids:
            yield event.plain_result("❌ 未检测到有效的QQ号。")
            return

        success = []
        failed = []
        for user_id in all_ids:
            result = await self.call_platform_api("delete_friend", user_id=int(user_id))
            if result and result.get('status') == 'ok':
                success.append(user_id)
            else:
                failed.append(user_id)

        reply_parts = []
        if success:
            reply_parts.append(f"✅ 已删除好友 {len(success)} 人: {', '.join(success)}")
        if failed:
            reply_parts.append(f"❌ 删除失败: {', '.join(failed)}")

        yield event.plain_result("\n".join(reply_parts))

    @filter.command("退群", alias=["leavegroup"])
    async def leave_group(self, event: AstrMessageEvent, args: str = ""):
        """批量退出群聊
        
        用法:
          /退群 123456
          /退群 123456 789012 345678
        """
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        args = args.strip()
        if not args:
            yield event.plain_result("⚠️ 用法: /退群 [群号1] [群号2] ...")
            return

        all_ids = self.extract_ids(args)
        if not all_ids:
            yield event.plain_result("❌ 未检测到有效的群号。")
            return

        success = []
        failed = []
        for group_id in all_ids:
            result = await self.call_platform_api("set_group_leave", group_id=int(group_id))
            if result and result.get('status') == 'ok':
                success.append(group_id)
            else:
                failed.append(group_id)

        reply_parts = []
        if success:
            reply_parts.append(f"✅ 已退群 {len(success)} 个: {', '.join(success)}")
        if failed:
            reply_parts.append(f"❌ 退群失败: {', '.join(failed)}")

        yield event.plain_result("\n".join(reply_parts))

    # ================= 请求处理指令（支持批量）=================
    @filter.command("同意", alias=["accept"])
    async def accept_request(self, event: AstrMessageEvent, args: str = ""):
        """批量同意好友申请或群邀请
        
        用法:
          /同意 flag1
          /同意 flag1 flag2 flag3
        """
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        args = args.strip()
        if not args:
            yield event.plain_result("⚠️ 用法: /同意 [flag1] [flag2] ... (用 /待处理 查看flag)")
            return

        flags = args.split()
        success = []
        failed = []
        not_found = []

        for flag in flags:
            request_info = self.pending_requests.get(flag)
            if not request_info:
                not_found.append(flag)
                continue

            if request_info['type'] == 'friend':
                result = await self.call_platform_api("set_friend_add_request", flag=flag, approve=True)
            elif request_info['type'] == 'group':
                sub_type = request_info.get('sub_type', 'invite')
                result = await self.call_platform_api("set_group_add_request", flag=flag, approve=True, sub_type=sub_type)
            else:
                not_found.append(flag)
                continue

            if result and result.get('status') == 'ok':
                if flag in self.pending_requests:
                    del self.pending_requests[flag]
                    self.save_pending_requests()
                type_name = "好友" if request_info['type'] == 'friend' else "群"
                success.append(f"{flag}({type_name})")
            else:
                failed.append(flag)

        reply_parts = []
        if success:
            reply_parts.append(f"✅ 已同意 {len(success)} 项: {', '.join(success)}")
        if not_found:
            reply_parts.append(f"⚠️ 未找到: {', '.join(not_found)}")
        if failed:
            reply_parts.append(f"❌ 同意失败: {', '.join(failed)}")

        yield event.plain_result("\n".join(reply_parts))

    @filter.command("拒绝", alias=["reject"])
    async def reject_request(self, event: AstrMessageEvent, args: str = ""):
        """批量拒绝好友申请或群邀请
        
        用法:
          /拒绝 flag1
          /拒绝 flag1 flag2 flag3
        """
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        args = args.strip()
        if not args:
            yield event.plain_result("⚠️ 用法: /拒绝 [flag1] [flag2] ... (用 /待处理 查看flag)")
            return

        flags = args.split()
        success = []
        failed = []
        not_found = []

        for flag in flags:
            request_info = self.pending_requests.get(flag)
            if not request_info:
                not_found.append(flag)
                continue

            if request_info['type'] == 'friend':
                result = await self.call_platform_api("set_friend_add_request", flag=flag, approve=False)
            elif request_info['type'] == 'group':
                sub_type = request_info.get('sub_type', 'invite')
                result = await self.call_platform_api("set_group_add_request", flag=flag, approve=False, sub_type=sub_type)
            else:
                not_found.append(flag)
                continue

            if result and result.get('status') == 'ok':
                if flag in self.pending_requests:
                    del self.pending_requests[flag]
                    self.save_pending_requests()
                type_name = "好友" if request_info['type'] == 'friend' else "群"
                success.append(f"{flag}({type_name})")
            else:
                failed.append(flag)

        reply_parts = []
        if success:
            reply_parts.append(f"✅ 已拒绝 {len(success)} 项: {', '.join(success)}")
        if not_found:
            reply_parts.append(f"⚠️ 未找到: {', '.join(not_found)}")
        if failed:
            reply_parts.append(f"❌ 拒绝失败: {', '.join(failed)}")

        yield event.plain_result("\n".join(reply_parts))

    # ================= 群邀请专用指令（支持批量）=================
    @filter.command("同意群", alias=["acceptgroup"])
    async def accept_group_request(self, event: AstrMessageEvent, args: str = ""):
        """批量同意群邀请
        
        用法:
          /同意群 flag1
          /同意群 flag1 flag2 flag3
        """
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        args = args.strip()
        if not args:
            yield event.plain_result("⚠️ 用法: /同意群 [flag1] [flag2] ...")
            return

        flags = args.split()
        success = []
        failed = []
        not_found = []

        for flag in flags:
            request_info = self.pending_requests.get(flag)
            if not request_info or request_info['type'] != 'group':
                not_found.append(flag)
                continue

            sub_type = request_info.get('sub_type', 'invite')
            result = await self.call_platform_api("set_group_add_request", flag=flag, approve=True, sub_type=sub_type)
            if result and result.get('status') == 'ok':
                if flag in self.pending_requests:
                    del self.pending_requests[flag]
                    self.save_pending_requests()
                success.append(flag)
            else:
                failed.append(flag)

        reply_parts = []
        if success:
            reply_parts.append(f"✅ 已同意群邀请 {len(success)} 项: {', '.join(success)}")
        if not_found:
            reply_parts.append(f"⚠️ 非群邀请或未找到: {', '.join(not_found)}")
        if failed:
            reply_parts.append(f"❌ 同意失败: {', '.join(failed)}")

        yield event.plain_result("\n".join(reply_parts))

    @filter.command("拒绝群", alias=["rejectgroup"])
    async def reject_group_request(self, event: AstrMessageEvent, args: str = ""):
        """批量拒绝群邀请
        
        用法:
          /拒绝群 flag1
          /拒绝群 flag1 flag2 flag3
        """
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        args = args.strip()
        if not args:
            yield event.plain_result("⚠️ 用法: /拒绝群 [flag1] [flag2] ...")
            return

        flags = args.split()
        success = []
        failed = []
        not_found = []

        for flag in flags:
            request_info = self.pending_requests.get(flag)
            if not request_info or request_info['type'] != 'group':
                not_found.append(flag)
                continue

            sub_type = request_info.get('sub_type', 'invite')
            result = await self.call_platform_api("set_group_add_request", flag=flag, approve=False, sub_type=sub_type)
            if result and result.get('status') == 'ok':
                if flag in self.pending_requests:
                    del self.pending_requests[flag]
                    self.save_pending_requests()
                success.append(flag)
            else:
                failed.append(flag)

        reply_parts = []
        if success:
            reply_parts.append(f"✅ 已拒绝群邀请 {len(success)} 项: {', '.join(success)}")
        if not_found:
            reply_parts.append(f"⚠️ 非群邀请或未找到: {', '.join(not_found)}")
        if failed:
            reply_parts.append(f"❌ 拒绝失败: {', '.join(failed)}")

        yield event.plain_result("\n".join(reply_parts))

    # ================= 事件监听器 =================
    async def on_friend_request(self, event: FriendRequestEvent):
        """监听好友申请事件 - 黑名单用户自动拒绝"""
        user_id = str(event.user_id)
        flag = event.flag
        comment = event.comment or ""

        logger.info(f"收到好友申请: 用户 {user_id}, flag: {flag}, 理由: {comment}")

        if self.is_blacklisted(user_id, "friend"):
            await self.call_platform_api("set_friend_add_request", flag=flag, approve=False)
            logger.info(f"🚫 已自动拒绝黑名单用户 {user_id} 的好友申请")
            await self.send_notification(
                f"🚫 已自动拒绝黑名单用户的好友申请\n"
                f"用户: {user_id}\n"
                f"申请理由: {comment}"
            )
            return

        self.pending_requests[flag] = {
            'type': 'friend',
            'user_id': user_id,
            'comment': comment,
            'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.save_pending_requests()
        
        await self.send_notification(
            f"📥 收到新好友申请:\n"
            f"用户: {user_id}\n"
            f"Flag: {flag}\n"
            f"理由: {comment}\n"
            f"处理: /同意 {flag} 或 /拒绝 {flag}"
        )

    async def on_group_request(self, event: GroupRequestEvent):
        """监听群邀请事件 - 黑名单用户自动拒绝"""
        flag = event.flag
        group_id = str(event.group_id)
        user_id = str(event.user_id)
        sub_type = event.sub_type
        comment = event.comment or ""

        if sub_type != 'invite':
            return

        logger.info(f"收到群邀请: 群 {group_id}, 邀请人 {user_id}, flag: {flag}")

        if self.is_blacklisted(user_id, "group_invite"):
            await self.call_platform_api("set_group_add_request", flag=flag, approve=False, sub_type=sub_type)
            logger.info(f"🚫 已自动拒绝黑名单用户 {user_id} 的群邀请")
            await self.send_notification(
                f"🚫 已自动拒绝黑名单用户的群邀请\n"
                f"邀请人: {user_id}\n"
                f"群号: {group_id}"
            )
            return

        self.pending_requests[flag] = {
            'type': 'group',
            'group_id': group_id,
            'user_id': user_id,
            'sub_type': sub_type,
            'comment': comment,
            'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.save_pending_requests()
        
        await self.send_notification(
            f"📥 收到新群邀请:\n"
            f"群号: {group_id}\n"
            f"邀请人: {user_id}\n"
            f"Flag: {flag}\n"
            f"理由: {comment}\n"
            f"处理: /同意群 {flag} 或 /拒绝群 {flag}"
        )

    async def terminate(self):
        """插件卸载时的清理工作"""
        logger.info("关系管理插件已停止")
