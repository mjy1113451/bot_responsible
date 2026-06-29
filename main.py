import json
import re
import asyncio
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

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
    _CQ_REPLY_RE = re.compile(r"\[CQ:reply,id=(-?\d+)\]")
    # OneBot v12 / 标准 reply 结构匹配
    _MSG_ID_RE = re.compile(r'"message_id"\s*:\s*"?(-?\d+)"?')
    _REPLY_ID_KEYS = ("id", "message_id", "messageId", "msg_id", "msgId")
    _REPLY_CONTAINER_KEYS = ("reply", "source", "quote", "message_reference")
    _REPLY_TYPE_NAMES = ("reply", "source", "quote")

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
        logger.info(
            "关系管理插件初始化完成: data_dir=%s, pending_file=%s, pending_count=%s",
            self.data_dir,
            self.pd_file,
            len(self.pending),
        )

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
        # 旧版只有在 value 明确带群特征时才迁移，避免把普通用户黑名单误判成群黑名单
        to_migrate_groups: Dict[str, dict] = {}
        for uid, val in list(entries):
            if uid == "group_blacklist":
                continue
            if not self._valid_gid(uid):
                continue

            # 仅迁移带明显群信息的旧条目；字符串值没有足够信息，保守保留为用户黑名单
            if isinstance(val, dict) and (
                "group_name" in val or "group_id" in val or str(val.get("type", "")).lower() == "group"
            ):
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
                if client and hasattr(client, "api") and hasattr(client.api, "call_action"):
                    return await client.api.call_action(name, **kw)

            # 方式2: 通过平台获取客户端
            if event:
                platform_id = event.get_platform_id()
                platform = self.context.get_platform_inst(platform_id)
                if platform:
                    client = platform.get_client()
                    if client and hasattr(client, name):
                        return await getattr(client, name)(**kw)
                    if client and hasattr(client, "api") and hasattr(client.api, "call_action"):
                        return await client.api.call_action(name, **kw)

            # 方式3: 遍历所有平台查找支持的客户端
            for platform in self.context.platform_manager.get_insts():
                if hasattr(platform, 'get_client'):
                    client = platform.get_client()
                    if client and hasattr(client, name):
                        return await getattr(client, name)(**kw)
                    if client and hasattr(client, "api") and hasattr(client.api, "call_action"):
                        return await client.api.call_action(name, **kw)

        except Exception as e:
            logger.error(f"API {name} 失败: {e}")
        return None

    async def _notify(self, msg: str):
        """修复7: 委托给 _notify_with_ids，忽略返回值"""
        await self._notify_with_ids(msg)

    def _collect_message_ids(self, payload: Any) -> List[str]:
        ids: List[str] = []
        seen: Set[int] = set()

        def walk(node: Any):
            if node is None:
                return

            if isinstance(node, (str, int, float, bool, bytes)):
                return

            node_id = id(node)
            if node_id in seen:
                return
            seen.add(node_id)

            if isinstance(node, dict):
                for key in ("message_id", "real_id", "message_seq", "messageId", "realId", "messageSeq"):
                    normalized = self._normalize_msg_id(node.get(key))
                    if normalized and normalized not in ids:
                        ids.append(normalized)
                for value in node.values():
                    walk(value)
                return

            if isinstance(node, (list, tuple, set)):
                for item in node:
                    walk(item)
                return

            for key in ("message_id", "real_id", "message_seq", "messageId", "realId", "messageSeq"):
                normalized = self._normalize_msg_id(getattr(node, key, None))
                if normalized and normalized not in ids:
                    ids.append(normalized)

            for key in ("data", "message", "messages", "raw_message"):
                walk(getattr(node, key, None))

        walk(payload)
        return ids

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
                        ids.extend(self._collect_message_ids(res))
                else:
                    for aid in self._get_admins():
                        res = await client.send_private_msg(user_id=int(aid), message=msg)
                        if res and isinstance(res, dict):
                            ids.extend(self._collect_message_ids(res))
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
        return list(dict.fromkeys(ids))

    @staticmethod
    def _normalize_msg_id(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _new_request_id() -> str:
        return uuid.uuid4().hex[:10]

    def _find_dict_in_node(self, node: Any, predicate) -> Optional[dict]:
        seen: Set[int] = set()

        def walk(current: Any) -> Optional[dict]:
            if current is None:
                return None

            if isinstance(current, str):
                text = current.strip()
                if text[:1] in ("{", "["):
                    try:
                        current = json.loads(text)
                    except Exception:
                        return None
                else:
                    return None

            if isinstance(current, (int, float, bool, bytes)):
                return None

            current_id = id(current)
            if current_id in seen:
                return None
            seen.add(current_id)

            if isinstance(current, dict):
                try:
                    if predicate(current):
                        return current
                except Exception:
                    pass

                for key in (
                    "message_obj",
                    "raw_message",
                    "message",
                    "messages",
                    "content",
                    "elements",
                    "segments",
                    "data",
                    "payload",
                    "extra",
                    "quote",
                    "source",
                    "reply",
                    "message_reference",
                ):
                    result = walk(current.get(key))
                    if result is not None:
                        return result

                for value in current.values():
                    result = walk(value)
                    if result is not None:
                        return result
                return None

            if isinstance(current, (list, tuple, set)):
                for item in current:
                    result = walk(item)
                    if result is not None:
                        return result
                return None

            for attr in (
                "message_obj",
                "raw_message",
                "message",
                "messages",
                "content",
                "elements",
                "segments",
                "data",
                "payload",
                "extra",
                "quote",
                "source",
                "reply",
                "message_reference",
            ):
                try:
                    result = walk(getattr(current, attr, None))
                    if result is not None:
                        return result
                except Exception:
                    continue

            return None

        return walk(node)

    @staticmethod
    def _looks_like_friend_request(payload: dict) -> bool:
        request_type = str(payload.get("request_type", "")).lower()
        post_type = str(payload.get("post_type", "")).lower()
        has_flag = bool(payload.get("flag"))
        has_user = bool(payload.get("user_id"))
        has_group = bool(payload.get("group_id"))
        return (
            request_type == "friend"
            or (post_type == "request" and has_flag and has_user and not has_group)
            or (has_flag and has_user and not has_group)
        )

    @staticmethod
    def _looks_like_group_request(payload: dict) -> bool:
        request_type = str(payload.get("request_type", "")).lower()
        post_type = str(payload.get("post_type", "")).lower()
        has_flag = bool(payload.get("flag"))
        has_user = bool(payload.get("user_id"))
        has_group = bool(payload.get("group_id"))
        return (
            request_type == "group"
            or (post_type == "request" and has_flag and has_group)
            or (has_flag and has_group and has_user)
        )

    @staticmethod
    def _looks_like_notice(payload: dict) -> bool:
        notice_type = str(payload.get("notice_type", "")).lower()
        post_type = str(payload.get("post_type", "")).lower()
        return bool(notice_type) or post_type == "notice"

    def _extract_event_payload(self, event: AstrMessageEvent, predicate) -> Optional[dict]:
        return self._find_dict_in_node(event, predicate)

    def _extract_reply_id_from_text(self, text: str) -> Optional[str]:
        if not text:
            return None

        match = self._CQ_REPLY_RE.search(text)
        if match:
            return self._normalize_msg_id(match.group(1))

        lower_text = text.lower()
        if any(name in lower_text for name in self._REPLY_TYPE_NAMES):
            match = self._MSG_ID_RE.search(text)
            if match:
                return self._normalize_msg_id(match.group(1))

        return None

    def _extract_reply_id_from_node(
        self, node: Any, seen: Set[int], allow_direct_id: bool = False
    ) -> Optional[str]:
        if node is None:
            return None

        if isinstance(node, str):
            return self._extract_reply_id_from_text(node)

        if isinstance(node, (int, float, bool, bytes)):
            return None

        node_identity = id(node)
        if node_identity in seen:
            return None
        seen.add(node_identity)

        if isinstance(node, dict):
            if allow_direct_id:
                for key in self._REPLY_ID_KEYS:
                    reply_id = self._normalize_msg_id(node.get(key))
                    if reply_id:
                        return reply_id

            node_type = str(node.get("type", "")).lower()
            if node_type in self._REPLY_TYPE_NAMES:
                data = node.get("data")
                if isinstance(data, dict):
                    for key in self._REPLY_ID_KEYS:
                        reply_id = self._normalize_msg_id(data.get(key))
                        if reply_id:
                            return reply_id
                for key in self._REPLY_ID_KEYS:
                    reply_id = self._normalize_msg_id(node.get(key))
                    if reply_id:
                        return reply_id

            for key in self._REPLY_CONTAINER_KEYS:
                if key in node:
                    reply_id = self._extract_reply_id_from_node(
                        node.get(key), seen, allow_direct_id=True
                    )
                    if reply_id:
                        return reply_id

            for key in ("message", "messages", "content", "elements", "segments", "data"):
                if key in node:
                    reply_id = self._extract_reply_id_from_node(node.get(key), seen)
                    if reply_id:
                        return reply_id

            return self._extract_reply_id_from_text(str(node))

        if isinstance(node, (list, tuple, set)):
            for item in node:
                reply_id = self._extract_reply_id_from_node(item, seen)
                if reply_id:
                    return reply_id
            return None

        component_type = str(getattr(node, "type", "") or getattr(node, "component_type", "")).lower()
        class_name = node.__class__.__name__.lower()
        if component_type in self._REPLY_TYPE_NAMES or any(name in class_name for name in self._REPLY_TYPE_NAMES):
            for key in self._REPLY_ID_KEYS:
                reply_id = self._normalize_msg_id(getattr(node, key, None))
                if reply_id:
                    return reply_id
            reply_id = self._extract_reply_id_from_node(
                getattr(node, "data", None), seen, allow_direct_id=True
            )
            if reply_id:
                return reply_id

        for key in self._REPLY_CONTAINER_KEYS:
            reply_id = self._extract_reply_id_from_node(
                getattr(node, key, None), seen, allow_direct_id=True
            )
            if reply_id:
                return reply_id

        for key in ("message", "messages", "content", "elements", "segments", "raw_message", "data"):
            reply_id = self._extract_reply_id_from_node(getattr(node, key, None), seen)
            if reply_id:
                return reply_id

        return self._extract_reply_id_from_text(str(node))

    def _get_reply_id(self, event: AstrMessageEvent) -> Optional[str]:
        """
        兼容 AstrBot 组件对象、OneBot array 上报、CQ 码字符串和原始事件 dict 中的引用消息结构。
        """
        for candidate in (
            getattr(event, "message_obj", None),
            getattr(getattr(event, "message_obj", None), "message", None),
            getattr(getattr(event, "message_obj", None), "raw_message", None),
            event,
        ):
            try:
                reply_id = self._extract_reply_id_from_node(candidate, set())
                if reply_id:
                    return reply_id
            except Exception:
                continue
        return None

    def _find_flag_by_msg_id(self, msg_id: str) -> Optional[str]:
        target_msg_id = self._normalize_msg_id(msg_id)
        if not target_msg_id:
            return None
        for flag, info in self.pending.items():
            notify_ids = {
                normalized
                for normalized in (
                    self._normalize_msg_id(item) for item in info.get("notify_ids", [])
                )
                if normalized
            }
            if target_msg_id in notify_ids:
                return flag
        return None

    def _find_flag_by_request_id(self, request_id: str) -> Optional[str]:
        target_request_id = self._normalize_msg_id(request_id)
        if not target_request_id:
            return None
        for flag, info in self.pending.items():
            if self._normalize_msg_id(info.get("request_id")) == target_request_id:
                return flag
        return None

    def _find_flag_by_notice_text(self, text: str) -> Optional[str]:
        if not text:
            return None
        if "好友申请" not in text and "群邀请" not in text:
            return None

        candidates = self._extract_pending_candidates_from_text(text)
        if not candidates:
            return None

        for flag, info in reversed(list(self.pending.items())):
            request_id = str(info.get("request_id", "")).strip()
            values = {
                str(info.get("user_id", "")).strip(),
                str(info.get("group_id", "")).strip(),
                str(info.get("nickname", "")).strip(),
                str(info.get("inviter_nickname", "")).strip(),
                str(info.get("group_name", "")).strip(),
                request_id,
                str(flag).strip(),
            }
            if any(candidate in values for candidate in candidates):
                return flag

        return None

    def _find_flag_from_args(self, args: str) -> Optional[str]:
        text = str(args or "").strip()
        if not text:
            return None

        candidates = self._extract_pending_candidates_from_text(text)
        for candidate in candidates:
            flag = self._find_flag_by_request_id(candidate)
            if flag:
                return flag
            if candidate in self.pending:
                return candidate
        return None

    def _extract_pending_candidates_from_text(self, text: str) -> List[str]:
        if not text:
            return []

        candidates: List[str] = []

        for uid in self._ids(text):
            if uid not in candidates:
                candidates.append(uid)

        for pattern in (
            r"编号[:：]\s*([^\n\r]+)",
            r"QQ号[:：]\s*([^\n\r]+)",
            r"邀请人QQ[:：]\s*([^\n\r]+)",
            r"群号[:：]\s*([^\n\r]+)",
            r"昵称[:：]\s*([^\n\r]+)",
            r"邀请人昵称[:：]\s*([^\n\r]+)",
            r"群名称[:：]\s*([^\n\r]+)",
        ):
            for match in re.findall(pattern, text):
                value = str(match).strip()
                if value and value not in candidates:
                    candidates.append(value)

        return candidates

    def _find_flag_by_quote_text(self, event: AstrMessageEvent) -> Optional[str]:
        texts: List[str] = []

        for candidate in (
            event,
            getattr(event, "message_obj", None),
            getattr(getattr(event, "message_obj", None), "message", None),
            getattr(getattr(event, "message_obj", None), "raw_message", None),
        ):
            for text in self._collect_text_fragments(candidate):
                if text and text not in texts:
                    texts.append(text)

        try:
            message_str = str(event.get_message_str() or "")
            if message_str and message_str not in texts:
                texts.append(message_str)
        except Exception:
            pass

        for text in texts:
            flag = self._find_flag_by_notice_text(text)
            if flag:
                return flag

        return None

    async def _find_flag_by_replied_message(self, event: AstrMessageEvent, reply_id: str) -> Optional[str]:
        if not reply_id:
            return None

        try:
            res = await self._api("get_msg", event=event, message_id=int(reply_id))
        except ValueError:
            res = await self._api("get_msg", event=event, message_id=reply_id)
        except Exception as e:
            logger.warning(f"拉取引用消息失败: reply_id={reply_id}, err={e}")
            return None

        if not res:
            logger.warning(f"拉取引用消息无结果: reply_id={reply_id}")
            return None

        texts = self._collect_text_fragments(res)
        logger.info(f"已拉取引用消息用于匹配: reply_id={reply_id}, text_fragments={len(texts)}")
        for text in texts:
            flag = self._find_flag_by_notice_text(text)
            if flag:
                return flag

        return None

    def _collect_text_fragments(self, node: Any) -> List[str]:
        texts: List[str] = []
        seen: Set[int] = set()

        def add_text(value: Any):
            if value is None:
                return
            if isinstance(value, bytes):
                try:
                    value = value.decode("utf-8", errors="ignore")
                except Exception:
                    return
            if not isinstance(value, str):
                value = str(value)
            text = value.strip()
            if text and text not in texts:
                texts.append(text)

        def walk(current: Any):
            if current is None:
                return

            if isinstance(current, str):
                add_text(current)
                stripped = current.strip()
                if stripped[:1] in ("{", "["):
                    try:
                        current = json.loads(stripped)
                    except Exception:
                        return
                else:
                    return

            if isinstance(current, (int, float, bool, bytes)):
                if isinstance(current, bytes):
                    add_text(current)
                return

            current_id = id(current)
            if current_id in seen:
                return
            seen.add(current_id)

            if isinstance(current, dict):
                for key in ("text", "content", "summary", "raw_message", "message"):
                    value = current.get(key)
                    if isinstance(value, (str, bytes)):
                        add_text(value)
                if str(current.get("type", "")).lower() == "text":
                    data = current.get("data")
                    if isinstance(data, dict):
                        add_text(data.get("text"))
                    elif isinstance(data, (str, bytes)):
                        add_text(data)
                for value in current.values():
                    walk(value)
                return

            if isinstance(current, (list, tuple, set)):
                for item in current:
                    walk(item)
                return

            for attr in (
                "text",
                "content",
                "summary",
                "raw_message",
                "message",
                "messages",
                "data",
                "elements",
                "segments",
                "quote",
                "source",
                "reply",
                "message_reference",
            ):
                try:
                    walk(getattr(current, attr, None))
                except Exception:
                    continue

        walk(node)
        return texts

    def _cleanup_pending(self):
        now = datetime.now()
        expired = []
        for flag, info in self.pending.items():
            try:
                t = datetime.strptime(info.get("time", ""), "%Y-%m-%d %H:%M:%S")
                if now - t > timedelta(days=self.PENDING_TTL_DAYS):
                    expired.append(flag)
            except (ValueError, TypeError):
                logger.warning(f"待处理时间格式异常，保留不删: flag={flag}, time={info.get('time')!r}")
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
            raw = self._extract_event_payload(event, self._looks_like_friend_request)
            if raw:
                logger.info("捕获好友申请事件: keys=%s", sorted(raw.keys()))
                await self._on_friend_req(raw, event)
                return None

            raw = self._extract_event_payload(event, self._looks_like_group_request)
            if raw and str(raw.get("sub_type", "invite")).lower() == "invite":
                logger.info("捕获群邀请事件: keys=%s", sorted(raw.keys()))
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
        request_id = self._new_request_id()

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
                request_id=request_id,
                request_flag=flag,
                request_type="friend",
            )
            self._save(self.pd_file, self.pending)
        logger.info(f"已记录好友申请待处理: flag={flag}, request_id={request_id}, uid={uid}, nickname={nickname}")

        msg_ids = await self._notify_with_ids(
            f"【好友申请】同意/拒绝/拉黑：\n"
            f"编号：{request_id}\n"
            f"昵称：{nickname}\n"
            f"QQ号：{uid}\n"
            f"验证信息：{comment if comment else '无'}\n"
            f"💬 引用此消息回复: /同意 或 /拒绝 或 /拉黑"
        )
        if msg_ids:
            async with self._lock:
                self.pending[flag]["notify_ids"] = msg_ids
                self._save(self.pd_file, self.pending)
            logger.info(f"好友申请通知消息ID已记录: flag={flag}, ids={msg_ids}")
        else:
            logger.warning(f"好友申请通知未获取到可匹配的消息ID: flag={flag}, uid={uid}")

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
        request_id = self._new_request_id()
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
                request_id=request_id,
                request_flag=flag,
                request_type="group",
            )
            self._save(self.pd_file, self.pending)
        logger.info(f"已记录群邀请待处理: flag={flag}, request_id={request_id}, gid={gid}, uid={uid}")

        msg_ids = await self._notify_with_ids(
            f"【群邀请】同意/拒绝/拉黑：\n"
            f"编号：{request_id}\n"
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
            logger.info(f"群邀请通知消息ID已记录: flag={flag}, ids={msg_ids}")
        else:
            logger.warning(f"群邀请通知未获取到可匹配的消息ID: flag={flag}, gid={gid}")

    # ───────── 通知事件监听（被踢 / 进群）─────────

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_notice(self, event: AstrMessageEvent):
        """
        修复9: asyncio.sleep(2) 包装在 try/except 中，防止 notice_type 不匹配时异常泄漏
        """
        try:
            raw = self._extract_event_payload(event, self._looks_like_notice)
            if isinstance(raw, dict):
                logger.info("捕获通知事件: keys=%s", sorted(raw.keys()))
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
            tag = " 🚫" if self._is_group_blocked(str(gid)) else ""
            lines.append(f"{i}. {g.get('group_name', '?')} ({gid}){tag}")

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
            if self._get_reply_id(event):
                async for result in self._process_reply(event, action="block"):
                    yield result
                return
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
        logger.info(f"待处理查询: pending_count={len(pending_snapshot)}, file={self.pd_file}")

        if not pending_snapshot:
            yield event.plain_result("📋 无待处理请求")
            return

        lines = ["📋 待处理请求（引用对应消息回复 /同意 或 /拒绝 或 /拉黑）"]
        for flag, info in pending_snapshot.items():
            t = info.get("time", "?")
            request_id = info.get("request_id", flag)
            if info["type"] == "friend":
                nickname = info.get('nickname', info['user_id'])
                comment = info.get('comment') or '无'
                lines.append(
                    f"🔹 好友 | 编号:{request_id} | 昵称:{nickname} QQ:{info['user_id']} | 验证:{comment} | {t}"
                )
            else:
                inviter_nickname = info.get('inviter_nickname', info['user_id'])
                group_name = info.get('group_name', info['group_id'])
                comment = info.get('comment') or '无'
                lines.append(
                    f"🔸 群邀 | 编号:{request_id} | 群:{group_name}({info['group_id']}) | 邀请人:{inviter_nickname}({info['user_id']}) | 验证:{comment} | {t}"
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

    async def _process_reply(self, event: AstrMessageEvent, action: str, args: str = ""):
        if self._sender_blocked(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可用")
            return

        flag = self._find_flag_from_args(args)
        reply_id = self._get_reply_id(event) if not flag else None
        flag = flag or (self._find_flag_by_msg_id(reply_id) if reply_id else None)
        if not flag:
            flag = self._find_flag_by_quote_text(event)
        if not flag and reply_id:
            flag = await self._find_flag_by_replied_message(event, reply_id)
        if not flag:
            if args:
                yield event.plain_result("❌ 未匹配到对应待处理编号")
                return
            if not reply_id:
                yield event.plain_result("⚠️ 请引用通知消息回复 /同意 或 /拒绝 或 /拉黑，或使用 /同意 编号")
                return
            if not self.pending:
                yield event.plain_result("❌ 当前没有待处理请求记录；请确认好友申请事件已被插件捕获并写入 pending")
                return
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
    async def cmd_accept(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._process_reply(event, action="accept", args=args):
            yield result

    @filter.command("拒绝", alias=["reject"])
    async def cmd_reject(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._process_reply(event, action="reject", args=args):
            yield result

    @filter.command("拉黑请求", alias=["blockreply"])
    async def cmd_block_reply(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._process_reply(event, action="block", args=args):
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
