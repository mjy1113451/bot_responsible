import sys
from datetime import datetime, timedelta

from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.star_tools import StarTools
from astrbot.core.star.filter.permission import PermissionType
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

from .utils.text_to_image import text_to_image
from .database import BlacklistDatabase
from .core.config import PluginConfig
from .core.contact import ContactHandle
from .core.normal import NormalHandle
from .core.notice import NoticeHandle
from .core.request import RequestHandle


@register(
    "astrbot_plugin_blacklist_tools",
    "ctrlkk",
    "允许管理员和 LLM 将用户添加到黑名单中，阻止他们的消息，自动拉黑！",
    "1.6",
)
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # 黑名单工具初始化
        data_dir = StarTools.get_data_dir()
        self.db_path = str(data_dir / "blacklist.db")

        # 黑名单最长时长
        self.max_blacklist_duration = config.get(
            "max_blacklist_duration", 1 * 24 * 60 * 60
        )
        # 是否允许永久黑名单
        self.allow_permanent_blacklist = config.get("allow_permanent_blacklist", True)
        # 是否向被拉黑用户显示拉黑状态
        self.show_blacklist_status = config.get("show_blacklist_status", True)
        # 黑名单提示消息
        self.blacklist_message = config.get("blacklist_message", "[连接已中断]")
        # 自动删除过期多久的黑名单
        self.auto_delete_expired_after = config.get("auto_delete_expired_after", 86400)
        # 是否允许拉黑管理员
        self.allow_blacklist_admin = config.get("allow_blacklist_admin", False)

        self.db = BlacklistDatabase(self.db_path, self.auto_delete_expired_after)

        # 人际关系管理初始化
        self.cfg = PluginConfig(config, context)
        self.normal = NormalHandle(self.cfg)
        self.request = RequestHandle(self.cfg)
        self.notice = NoticeHandle(self.cfg)
        self.contact = ContactHandle(self.cfg)

        # 检查扩展功能是否可用
        try:
            from .core.expansion import ExpansionHandle

            self.expansion_handle = ExpansionHandle
            self.expansion_available = True
        except ImportError:
            self.expansion_handle = None
            self.expansion_available = False

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        await self.db.initialize()

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        await self.db.terminate()

    def _format_datetime(
        self, iso_datetime_str, show_remaining=False, check_expire=False
    ):
        """统一格式化日期时间字符串
        Args:
            iso_datetime_str: ISO格式的日期时间字符串
            show_remaining: 是否显示剩余时间
            check_expire: 是否检查是否过期（仅对过期时间有效）
        """
        if not iso_datetime_str:
            return "永久"
        try:
            datetime_obj = datetime.fromisoformat(iso_datetime_str)
            formatted_time = datetime_obj.strftime("%Y-%m-%d %H:%M:%S")
            if check_expire:
                if datetime.now() > datetime_obj:
                    return "已过期"
            if show_remaining:
                if datetime.now() > datetime_obj:
                    return "已过期"
                else:
                    remaining_time = datetime_obj - datetime.now()
                    days = remaining_time.days
                    hours, remainder = divmod(remaining_time.seconds, 3600)
                    minutes, _ = divmod(remainder, 60)
                    return (
                        f"{formatted_time} (剩余: {days}天 {hours}小时 {minutes}分钟)"
                    )
            else:
                return formatted_time
        except Exception as e:
            logger.error(f"格式化日期时间时出错：{e}")
            return "格式错误"

    # ==================== 黑名单功能 ====================
    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize - 1)
    async def on_all_message(self, event: AstrMessageEvent):
        """检查消息是否来自黑名单用户"""
        if not event.is_at_or_wake_command:
            return
        sender_id = event.get_sender_id()
        try:
            if event.is_admin() and not self.allow_blacklist_admin:
                return
            if await self.db.is_user_blacklisted(sender_id):
                event.stop_event()
                if not event.get_messages():
                    pass
                elif self.show_blacklist_status:
                    await event.send(MessageChain().message(self.blacklist_message))
        except Exception as e:
            logger.error(f"检查黑名单时出错：{e}")

    @filter.command_group("blacklist", alias=["black", "bl"])
    def blacklist():
        """黑名单管理命令组"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @blacklist.command("ls")
    async def ls(self, event: AstrMessageEvent, page: int = 1, page_size: int = 10):
        """列出黑名单中的所有用户（支持分页）
        Args:
            page: 页码，从1开始
            page_size: 每页显示的数量
        """
        try:
            total_count = await self.db.get_blacklist_count()
            if total_count == 0:
                yield event.plain_result("黑名单为空。")
                return
            # 计算分页参数
            total_pages = (total_count + page_size - 1) // page_size
            if page < 1:
                page = 1
            elif page > total_pages:
                page = total_pages
            users = await self.db.get_blacklist_users(page, page_size)
            result = "黑名单列表\n"
            result += "=" * 60 + "\n\n"
            result += f"{'ID':<20} {'加入时间':<20} {'过期时间':<20} {'原因':<20}\n"
            result += "-" * 80 + "\n"
            for user in users:
                user_id, ban_time, expire_time, reason = user
                ban_time_str = self._format_datetime(ban_time, check_expire=False)
                expire_time_str = self._format_datetime(expire_time, check_expire=True)
                reason_str = reason if reason else "无"
                result += f"{user_id:<20} {ban_time_str:<20} {expire_time_str:<20} {reason_str:<20}\n"
            result += "-" * 80 + "\n"
            result += f"总计: {total_count} 个用户\n"
            result += f"当前: 第 {page}/{total_pages} 页 (每页 {page_size} 条)"
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"获取黑名单列表时出错：{e}")
            yield event.plain_result(f"获取黑名单列表失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @blacklist.command("add")
    async def add(self, event: AstrMessageEvent, user_id: str, duration: int = 0, reason: str = ""):
        """添加用户到黑名单
        Args:
            user_id: 要添加的用户ID
            duration: 黑名单时长（秒），0表示永久
            reason: 拉黑原因
        """
        try:
            if not user_id:
                yield event.plain_result("请指定用户ID")
                return
            if duration < 0:
                yield event.plain_result("时长不能为负数")
                return
            if self.max_blacklist_duration and duration > self.max_blacklist_duration:
                yield event.plain_result(f"时长不能超过最大限制: {self.max_blacklist_duration}秒")
                return
            if duration == 0 and not self.allow_permanent_blacklist:
                yield event.plain_result("不允许永久黑名单")
                return
            await self.db.add_to_blacklist(user_id, duration, reason)
            expire_time = (
                "永久"
                if duration == 0
                else self._format_datetime(
                    datetime.fromisoformat(datetime.now().isoformat()) + timedelta(seconds=duration),
                    show_remaining=True
                )
            )
            yield event.plain_result(
                f"已将用户 {user_id} 添加到黑名单\n过期时间: {expire_time}\n原因: {reason if reason else '无'}"
            )
        except Exception as e:
            logger.error(f"添加黑名单时出错：{e}")
            yield event.plain_result(f"添加黑名单失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @blacklist.command("rm")
    async def rm(self, event: AstrMessageEvent, user_id: str):
        """从黑名单移除用户
        Args:
            user_id: 要移除的用户ID
        """
        try:
            if not user_id:
                yield event.plain_result("请指定用户ID")
                return
            if await self.db.remove_from_blacklist(user_id):
                yield event.plain_result(f"已将用户 {user_id} 从黑名单移除")
            else:
                yield event.plain_result(f"用户 {user_id} 不在黑名单中")
        except Exception as e:
            logger.error(f"移除黑名单时出错：{e}")
            yield event.plain_result(f"移除黑名单失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @blacklist.command("info")
    async def info(self, event: AstrMessageEvent, user_id: str):
        """查看特定用户黑名单信息
        Args:
            user_id: 要查询的用户ID
        """
        try:
            if not user_id:
                yield event.plain_result("请指定用户ID")
                return
            user_info = await self.db.get_user_blacklist_info(user_id)
            if user_info:
                user_id, ban_time, expire_time, reason = user_info
                result = f"用户: {user_id}\n"
                result += f"加入时间: {self._format_datetime(ban_time, check_expire=False)}\n"
                result += f"过期时间: {self._format_datetime(expire_time, show_remaining=True, check_expire=True)}\n"
                result += f"原因: {reason if reason else '无'}"
                yield event.plain_result(result)
            else:
                yield event.plain_result(f"用户 {user_id} 不在黑名单中")
        except Exception as e:
            logger.error(f"查询黑名单信息时出错：{e}")
            yield event.plain_result(f"查询黑名单信息失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @blacklist.command("clear")
    async def clear(self, event: AstrMessageEvent):
        """清空黑名单"""
        try:
            count = await self.db.clear_blacklist()
            yield event.plain_result(f"已清空黑名单，共移除 {count} 个用户")
        except Exception as e:
            logger.error(f"清空黑名单时出错：{e}")
            yield event.plain_result(f"清空黑名单失败: {str(e)}")

    # ==================== 人际关系管理功能 ====================
    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("群列表")
    async def get_group_list(self, event: AiocqhttpMessageEvent):
        """查看bot加入的所有群聊信息"""
        async for msg in self.normal.get_group_list(event):
            yield msg

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("好友列表")
    async def get_friend_list(self, event: AiocqhttpMessageEvent):
        """查看bot的所有好友信息"""
        async for msg in self.normal.get_friend_list(event):
            yield msg

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("退群")
    async def set_group_leave(self, event: AiocqhttpMessageEvent):
        """退群 <序号|群号|区间> [可批量]"""
        async for msg in self.normal.set_group_leave(event):
            yield msg

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("删好友", alias={"删除好友"})
    async def delete_friend(self, event: AiocqhttpMessageEvent):
        """删好友 <@昵称|QQ|序号|区间> [可批量]"""
        async for msg in self.normal.delete_friend(event):
            yield msg

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_notice(self, event: AiocqhttpMessageEvent):
        """监听群聊相关事件（如管理员变动、禁言、踢出、邀请等），自动处理并反馈"""
        async for msg in self.notice.handle(event):
            yield msg

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_request(self, event: AiocqhttpMessageEvent):
        """监听好友申请或群邀请"""
        async for msg in self.request.handle_raw(event):
            yield msg

    @filter.command("同意")
    async def agree(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """同意好友申请或群邀请"""
        async for msg in self.request.handle_cmd(event, approve=True, extra=extra):
            yield msg

    @filter.command("拒绝")
    async def refuse(self, event: AiocqhttpMessageEvent, extra: str = ""):
        """拒绝好友申请或群邀请"""
        async for msg in self.request.handle_cmd(event, approve=False, extra=extra):
            yield msg

    # 这里是你要求的改动：去掉管理员权限，所有用户可用
    @filter.command("加群")
    async def add_group(self, event: AiocqhttpMessageEvent):
        """加群 [群号] [答案]"""
        if not self.expansion_available:
            yield event.plain_result("该功能不对普通用户开放")
            return
        async for msg in self.expansion_handle.add_group(event):
            yield msg

    # 这里是你要求的改动：去掉管理员权限，所有用户可用
    @filter.command("加好友")
    async def add_friend(self, event: AiocqhttpMessageEvent):
        """加好友 [QQ号/@某人] [验证消息] [备注] [答案]"""
        if not self.expansion_available:
            yield event.plain_result("该功能不对普通用户开放")
            return
        async for msg in self.expansion_handle.add_friend(event):
            yield msg

    @filter.command("推荐")
    async def on_contact(self, event: AiocqhttpMessageEvent):
        """推荐 <群号/@群友/@qq>"""
        await self.contact.contact(event)
