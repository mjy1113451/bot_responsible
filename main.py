import os
import json
from pathlib import Path
from typing import Dict, List, Optional

from astrbot.api.event import filter, EventMessageType, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Image
from astrbot.api import logger

# 插件元数据
@register(
    "astrbot_plugin_relationship_manager",
    "YourName",
    "AstrBot 关系管理插件 - 简化指令版",
    "1.0.0",
    "https://github.com/your-repo/astrbot_plugin_relationship_manager"
)
class RelationshipManagerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 初始化黑名单数据路径
        self.data_path = Path(context.get_astrbot_config().get("data_path", "data")) / "plugins" / "astrbot_plugin_relationship_manager"
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.blacklist_file = self.data_path / "blacklist.json"
        self.blacklist: Dict[str, str] = self.load_blacklist()
        # 存储待处理的申请和邀请，格式: {flag: {'type': 'friend'|'group', 'user_id': ..., 'comment': ...}}
        self.pending_requests: Dict[str, dict] = {}
        
    def load_blacklist(self) -> Dict[str, str]:
        """加载黑名单"""
        if self.blacklist_file.exists():
            try:
                with open(self.blacklist_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
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

    def is_admin(self, event: AstrMessageEvent) -> bool:
        """检查用户是否为管理员"""
        admin_ids = self.context.get_astrbot_config().get("admins", [])
        sender_id = str(event.get_sender_id())
        return sender_id in [str(admin_id) for admin_id in admin_ids]

    def get_platform_api_name(self, api_name: str) -> str:
        """根据平台获取正确的API名称（适配不同协议）"""
        # 这里可以根据平台信息进行更精确的适配
        # 简单起见，假设大部分平台兼容OneBot v11的API名称
        return api_name

    async def call_platform_api(self, api_name: str, **kwargs) -> Optional[dict]:
        """安全调用平台API的包装方法"""
        try:
            return await self.call_api(self.get_platform_api_name(api_name), **kwargs)
        except Exception as e:
            logger.error(f"调用API {api_name} 失败: {e}")
            return None

    # ================= 列表查看指令 =================
    @filter.command("好友列表", alias={"好友", "fl"})
    async def friend_list(self, event: AstrMessageEvent):
        """查看好友列表"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        result = await self.call_platform_api("get_friend_list")
        if result and result.get('status') == 'ok':
            friends = result.get('data', [])
            if not friends:
                yield event.plain_result("📋 你当前没有好友。")
                return

            reply = ["📋 **Bot 好友列表**\n"]
            for i, friend in enumerate(friends, 1):
                reply.append(f"{i}. {friend.get('nickname', 'Unknown')} (ID: {friend.get('user_id', 'Unknown')})")
            
            # 如果列表很长，可以考虑分页或生成图片（可集成其他插件）
            yield event.plain_result("\n".join(reply))
        else:
            yield event.plain_result("❌ 获取好友列表失败，请检查平台适配器是否支持此功能。")

    @filter.command("群列表", alias={"群", "gl"})
    async def group_list(self, event: AstrMessageEvent):
        """查看群列表"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        result = await self.call_platform_api("get_group_list")
        if result and result.get('status') == 'ok':
            groups = result.get('data', [])
            if not groups:
                yield event.plain_result("📋 你当前没有加入任何群。")
                return

            reply = ["📋 **Bot 群列表**\n"]
            for i, group in enumerate(groups, 1):
                reply.append(f"{i}. {group.get('group_name', 'Unknown')} (ID: {group.get('group_id', 'Unknown')})")
            
            yield event.plain_result("\n".join(reply))
        else:
            yield event.plain_result("❌ 获取群列表失败，请检查平台适配器是否支持此功能。")

    # ================= 黑名单管理指令 =================
    @filter.command("拉黑", alias={"黑名单", "addbl"})
    async def add_to_blacklist(self, event: AstrMessageEvent, target: str = ""):
        """拉黑用户"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        if not target:
            yield event.plain_result("⚠️ 用法: /拉黑 [QQ号 或 @用户]")
            return

        # 尝试解析QQ号（可能从@消息中提取）
        if target.startswith("@"):
            # 这里需要根据平台协议从@消息中提取真实的QQ号
            # 简化处理，假设用户输入了纯数字QQ号
            user_id = target[1:]
        else:
            user_id = target

        if not user_id.isdigit():
            yield event.plain_result("❌ 无效的用户ID，请输入纯数字QQ号。")
            return

        self.blacklist[user_id] = "手动拉黑"
        self.save_blacklist()
        yield event.plain_result(f"✅ 已将用户 {user_id} 加入黑名单。")

    @filter.command("解封", alias={"解除黑名单", "rmbl"})
    async def remove_from_blacklist(self, event: AstrMessageEvent, target: str = ""):
        """解封用户"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        if not target:
            yield event.plain_result("⚠️ 用法: /解封 [QQ号 或 @用户]")
            return

        if target.startswith("@"):
            user_id = target[1:]
        else:
            user_id = target

        if not user_id.isdigit():
            yield event.plain_result("❌ 无效的用户ID，请输入纯数字QQ号。")
            return

        if user_id in self.blacklist:
            del self.blacklist[user_id]
            self.save_blacklist()
            yield event.plain_result(f"✅ 已将用户 {user_id} 移出黑名单。")
        else:
            yield event.plain_result(f"⚠️ 用户 {user_id} 不在黑名单中。")

    @filter.command("黑名单", alias={"查看黑名单", "lsbl"})
    async def show_blacklist(self, event: AstrMessageEvent):
        """查看黑名单"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        if not self.blacklist:
            yield event.plain_result("📋 当前黑名单为空。")
            return

        reply = ["🚫 **黑名单列表**\n"]
        for user_id, reason in self.blacklist.items():
            reply.append(f"- {user_id} (理由: {reason})")
        
        yield event.plain_result("\n".join(reply))

    # ================= 好友与群管理指令（简化版） =================
    # 注意：以下API调用需根据实际平台适配器调整

    @filter.command("删好友", alias={"deletefriend"})
    async def delete_friend(self, event: AstrMessageEvent, target: str = ""):
        """删除好友"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        if not target:
            yield event.plain_result("⚠️ 用法: /删好友 [序号 或 QQ号 或 @用户]")
            return

        # 尝试从序号解析（需要先获取列表，这里简化为直接使用ID）
        # 实际应用中，可以先调用 `get_friend_list` 获取所有好友，建立序号到ID的映射
        user_id = target.lstrip('@') # 简单去除@

        if not user_id.isdigit():
            yield event.plain_result("❌ 无效的用户ID，请输入纯数字QQ号或序号。")
            return

        result = await self.call_platform_api("delete_friend", user_id=int(user_id))
        if result and result.get('status') == 'ok':
            yield event.plain_result(f"✅ 已删除好友 {user_id}。")
        else:
            yield event.plain_result(f"❌ 删除好友失败: {result.get('msg', '未知错误')}")

    @filter.command("退群", alias={"leavegroup"})
    async def leave_group(self, event: AstrMessageEvent, target: str = ""):
        """退出群聊"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        if not target:
            yield event.plain_result("⚠️ 用法: /退群 [序号 或 群号]")
            return

        group_id = target.lstrip('@')
        if not group_id.isdigit():
            yield event.plain_result("❌ 无效的群号，请输入纯数字群号或序号。")
            return

        result = await self.call_platform_api("set_group_leave", group_id=int(group_id))
        if result and result.get('status') == 'ok':
            yield event.plain_result(f"✅ 已退出群 {group_id}。")
        else:
            yield event.plain_result(f"❌ 退群失败: {result.get('msg', '未知错误')}")

    @filter.command("同意好友", alias={"acceptfriend"})
    async def accept_friend_request(self, event: AstrMessageEvent, flag: str = ""):
        """同意好友申请"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        if not flag:
            yield event.plain_result("⚠️ 用法: /同意好友 [申请序号] (请先使用 /好友列表 查看待处理申请)")
            return

        # 实际中，flag需要从待处理申请中获取，这里简化处理
        # 更好的方式是让管理员引用或回复申请消息来获取flag
        result = await self.call_platform_api("set_friend_add_request", flag=flag, approve=True)
        if result and result.get('status') == 'ok':
            yield event.plain_result("✅ 已同意好友申请。")
        else:
            yield event.plain_result(f"❌ 同意失败: {result.get('msg', '未知错误')}")

    @filter.command("拒绝好友", alias={"rejectfriend"})
    async def reject_friend_request(self, event: AstrMessageEvent, flag: str = ""):
        """拒绝好友申请"""
        if not self.is_admin(event):
            yield event.plain_result("❌ 此命令仅管理员可用。")
            return

        if not flag:
            yield event.plain_result("⚠️ 用法: /拒绝好友 [申请序号]")
            return

        result = await self.call_platform_api("set_friend_add_request", flag=flag, approve=False)
        if result and result.get('status') == 'ok':
            yield event.plain_result("✅ 已拒绝好友申请。")
        else:
            yield event.plain_result(f"❌ 拒绝失败: {result.get('msg', '未知错误')}")

    # ================= 事件监听器 =================
    @filter.event_message_type(EventMessageType.ALL)
    async def handle_requests(self, event: AstrMessageEvent):
        """监听所有消息事件，处理好友申请和群邀请"""
        raw = event.message_obj.raw_message
        
        # 检查是否为好友申请事件 (以OneBot v11为例)
        if isinstance(raw, dict) and raw.get('request_type') == 'friend':
            flag = raw.get('flag')
            user_id = str(raw.get('user_id'))
            comment = raw.get('comment', '')
            
            # 检查黑名单
            if user_id in self.blacklist:
                await self.call_platform_api("set_friend_add_request", flag=flag, approve=False)
                logger.info(f"自动拒绝黑名单用户 {user_id} 的好友申请")
                yield event.plain_result(f"🚫 已自动拒绝黑名单用户 {user_id} 的好友申请。")
            else:
                # 记录待处理申请（实际应存储到数据库或文件，并通知管理员）
                self.pending_requests[flag] = {
                    'type': 'friend',
                    'user_id': user_id,
                    'comment': comment,
                    'time': event.time
                }
                # 通知管理员（这里简化为直接回复，实际应发送到指定通知群）
                yield event.plain_result(f"📥 收到新好友申请:\n用户: {user_id}\n理由: {comment}\n请使用 /同意好友 {flag} 或 /拒绝好友 {flag} 处理")

        # 检查是否为群邀请事件 (以OneBot v11为例)
        elif isinstance(raw, dict) and raw.get('request_type') == 'group':
            flag = raw.get('flag')
            group_id = str(raw.get('group_id'))
            user_id = str(raw.get('user_id'))
            sub_type = raw.get('sub_type')
            comment = raw.get('comment', '')
            
            if sub_type == 'invite':
                # 检查群黑名单（如果实现了）
                # if group_id in self.group_blacklist: ...
                
                # 检查邀请人黑名单
                if user_id in self.blacklist:
                    await self.call_platform_api("set_group_add_request", flag=flag, approve=False, sub_type=sub_type)
                    logger.info(f"自动拒绝黑名单用户 {user_id} 的群邀请")
                    yield event.plain_result(f"🚫 已自动拒绝黑名单用户 {user_id} 的群邀请。")
                else:
                    self.pending_requests[flag] = {
                        'type': 'group',
                        'group_id': group_id,
                        'user_id': user_id,
                        'comment': comment,
                        'time': event.time
                    }
                    yield event.plain_result(f"📥 收到新群邀请:\n群号: {group_id}\n邀请人: {user_id}\n理由: {comment}\n请使用 /同意群 {flag} 或 /拒绝群 {flag} 处理")

    async def terminate(self):
        """插件卸载时的清理工作"""
        logger.info("关系管理插件已停止")
