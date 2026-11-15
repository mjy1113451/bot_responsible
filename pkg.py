"""
扩展功能模块 - Packet 级别协议
"""
import json as _json
import struct as _struct
from collections import defaultdict as _dd

from astrbot.api import logger


# Only fields whose role is known are named; other numeric fields remain opaque.
_COMMAND = "1"
_SUBCOMMAND = "2"
_BODY = "4"
_ENVELOPE_FIELD_12 = "12"  # Meaning not established.
_GROUP_ID = "1"
_GROUP_REQUEST = "2"
_GROUP_ANSWER = "3"
_GROUP_XML = "4"
_FRIEND_SELF_ID = "1"
_FRIEND_TARGET_UIN = "2"
_FRIEND_VERIFY = "7"
_FRIEND_REMARK = "18"
_FRIEND_ANSWER = "26"
_REMARK_TARGET_UIN = "1"
_REMARK_CONTAINER = "5"
_REMARK_TEXT = "1"

GROUP_VERIFICATION_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<mmg templateID="1" brief="" serviceID="104">\n'
    '    <item layout="2">\n'
    '        <picture cover=""/>\n'
    '        <title>当胁峾</title>\n'
    '    </item>\n'
    '    <source/>\n'
    '</mmg>\n'
)


def _envelope(command, subcommand, body):
    return {_COMMAND: command, _SUBCOMMAND: subcommand, _BODY: body,
            _ENVELOPE_FIELD_12: 1}


def _group_packet(target_gid, answer):
    request = {"1": 3, "2": "", _GROUP_ANSWER: answer,
               _GROUP_XML: GROUP_VERIFICATION_XML,
               "6": {}, "7": {}, "8": {"2": 2}}
    return _envelope(4588, 1, {_GROUP_ID: target_gid, _GROUP_REQUEST: request})


def _friend_packet(target_uin, self_id, verify, remark, answer):
    return _envelope(1986, 5, {
        _FRIEND_SELF_ID: self_id, _FRIEND_TARGET_UIN: target_uin,
        "3": 1, "4": 1, "5": 0, _FRIEND_VERIFY: verify,
        "11": 1, "12": 3, _FRIEND_REMARK: remark, "20": 0,
        _FRIEND_ANSWER: answer, "28": 1, "29": 1,
    })


def _remark_packet(target_uin, remark):
    return _envelope(0xfd8, 1, {
        _REMARK_TARGET_UIN: target_uin, _REMARK_CONTAINER: {_REMARK_TEXT: remark},
    })


class _PT:
    @staticmethod
    def h2j(h):
        try:
            h = h.replace(" ", "").replace("\n", "").replace("\r", "")
            b = bytes.fromhex(h)
            p = b[4:] if len(b) >= 4 and b[0] == 0 else b
            d = _FD()
            d.from_bytes(p)
            return _json.dumps(d.to_json(), indent=4, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Packet hex decoding failed: {e}")
            return _json.dumps({"error": str(e)})

    @staticmethod
    def j2h(j):
        try:
            o = _json.loads(j)
            d = _FD()
            d.from_json(o)
            return d.to_bytes().hex().upper()
        except Exception as e:
            logger.warning(f"Packet JSON encoding failed: {e}")
            raise ValueError(f"Packet JSON encoding failed: {e}") from e


class _FD:
    def __init__(self):
        self.v = _dd(list)

    def from_json(self, o):
        for k, val in o.items():
            if not k.isdigit():
                continue
            key = int(k)
            if isinstance(val, list):
                for i in val:
                    self._put(key, i)
            else:
                self._put(key, val)

    def _put(self, k, v):
        if isinstance(v, dict):
            sub = _FD()
            sub.from_json(v)
            self.v[k].append(sub)
        elif isinstance(v, str):
            if v.startswith("hex->"):
                h = v[5:]
                try:
                    self.v[k].append(bytes.fromhex(h))
                except ValueError:
                    self.v[k].append(v)
            else:
                self.v[k].append(v)
        else:
            self.v[k].append(v)

    def to_json(self):
        r = {}
        for k, vl in self.v.items():
            if not vl:
                continue
            cl = [self._conv(v) for v in vl]
            r[str(k)] = cl[0] if len(cl) == 1 else cl
        return r

    def _conv(self, v):
        return v.to_json() if isinstance(v, _FD) else v

    def from_bytes(self, b):
        p, l = 0, len(b)
        while p < l:
            tag, p = self._rv(b, p)
            if tag == 0:
                break
            fn, wt = tag >> 3, tag & 7
            if wt == 0:
                val, p = self._rv(b, p)
                self.v[fn].append(val)
            elif wt == 1:
                if p + 8 > l: break
                self.v[fn].append(_struct.unpack("<Q", b[p:p+8])[0])
                p += 8
            elif wt == 5:
                if p + 4 > l: break
                self.v[fn].append(_struct.unpack("<I", b[p:p+4])[0])
                p += 4
            elif wt == 2:
                sl, p = self._rv(b, p)
                if p + sl > l: break
                sb = b[p:p+sl]
                p += sl
                try:
                    sub = _FD()
                    sub.from_bytes(sb)
                    if not sub.v and len(sb) > 0:
                        raise ValueError()
                    self.v[fn].append(sub)
                except (IndexError, ValueError, _struct.error):
                    try:
                        d = sb.decode("utf-8")
                        if any(ord(c) < 32 and c not in "\n\r\t" for c in d):
                            raise ValueError()
                        self.v[fn].append(d)
                    except (UnicodeDecodeError, ValueError):
                        self.v[fn].append("hex->" + sb.hex().upper())
            else:
                return

    def to_bytes(self):
        o = bytearray()
        for k, vl in self.v.items():
            for v in vl:
                if isinstance(v, int):
                    o.extend(self._et(k, 0))
                    o.extend(self._ev(v))
                elif isinstance(v, str):
                    o.extend(self._et(k, 2))
                    ub = v.encode("utf-8")
                    o.extend(self._ev(len(ub)))
                    o.extend(ub)
                elif isinstance(v, bytes):
                    o.extend(self._et(k, 2))
                    o.extend(self._ev(len(v)))
                    o.extend(v)
                elif isinstance(v, _FD):
                    sb = v.to_bytes()
                    o.extend(self._et(k, 2))
                    o.extend(self._ev(len(sb)))
                    o.extend(sb)
        return bytes(o)

    def _rv(self, b, p):
        r, s = 0, 0
        while True:
            if p >= len(b):
                raise IndexError()
            bt = b[p]
            p += 1
            r |= (bt & 0x7F) << s
            if not (bt & 0x80):
                return r, p
            s += 7

    def _ev(self, v):
        o = []
        while True:
            tw = v & 0x7F
            v >>= 7
            if v:
                o.append(tw | 0x80)
            else:
                o.append(tw)
                break
        return bytes(o)

    def _et(self, fn, wt):
        return self._ev((fn << 3) | wt)


class ExpansionHandle:
    @staticmethod
    async def add_group(client, target_gid, answer):
        p = _group_packet(target_gid, answer)
        data = _PT.j2h(_json.dumps(p, ensure_ascii=False))
        await client.api.call_action("send_packet", cmd="OidbSvcTrpcTcp.0x11ec_1", data=data)
        lines = ["【加群申请】已发送", f"群号：{target_gid}"]
        try:
            info = await client.get_group_info(group_id=target_gid, no_cache=True)
            if info.get("group_name"):
                lines.append(f"群名：{info['group_name']}")
            mc, mm = info.get("member_count"), info.get("max_member_count")
            if mc and mm:
                lines.append(f"人数：{mc}/{mm}")
        except Exception as exc:
            logger.warning(f"OidbSvcTrpcTcp.0x11ec_1 group info lookup failed for {target_gid}: {exc}")
        if answer:
            lines.append(f"答案：{answer}")
        return "\n".join(lines)

    @staticmethod
    async def add_friend(client, target_uin, self_id, verify="", remark="", answer=""):
        p = _friend_packet(target_uin, self_id, verify, remark, answer)
        data = _PT.j2h(_json.dumps(p, ensure_ascii=False))
        await client.api.call_action("send_packet", cmd="OidbSvcTrpcTcp.0x7c2_5", data=data)
        lines = ["【好友申请】已发送", f"Q号：{target_uin}"]
        try:
            info = await client.get_stranger_info(user_id=target_uin, no_cache=True)
            if info.get("nickname"):
                lines.append(f"昵称：{info['nickname']}")
            if info.get("qqLevel"):
                lines.append(f"等级：{info['qqLevel']}")
        except Exception as exc:
            logger.warning(f"OidbSvcTrpcTcp.0x7c2_5 stranger info lookup failed for {target_uin}: {exc}")
        if verify:
            lines.append(f"验证消息：{verify}")
        if remark:
            lines.append(f"备注：{remark}")
        if answer:
            lines.append(f"答案：{answer}")
        return "\n".join(lines)

    @staticmethod
    async def set_friend_remark(client, target_uin, remark=""):
        """通过 send_packet 实验路径修改好友备注

        对应 OidbSvc.0xfd8_1 (ModifyFriendRemark)。
        该接口在 SnowLuma 等支持 send_packet 的 OneBot 兼容实现下可用,
        不在标准 OneBot v11 action 范围内,可能因协议变更失效。

        Returns:
            True 表示 send_packet 调用成功,False 表示编码或调用失败。
        """
        try:
            p = _remark_packet(target_uin, remark)
            data = _PT.j2h(_json.dumps(p, ensure_ascii=False))
        except Exception as exc:
            logger.warning(f"OidbSvcTrpcTcp.0xfd8_1 payload encoding failed for {target_uin}: {exc}")
            return False
        try:
            await client.api.call_action(
                "send_packet",
                cmd="OidbSvcTrpcTcp.0xfd8_1",
                data=data,
            )
            return True
        except Exception as exc:
            logger.warning(f"OidbSvcTrpcTcp.0xfd8_1 send failed for {target_uin}: {exc}")
            return False
