"""
扩展功能模块 - Packet 级别协议
此模块已混淆，仅供内部使用
"""
import base64
import json as _json
import struct as _struct
from collections import defaultdict as _dd


def _d(s):
    return base64.b64decode(s).decode()


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
            return _json.dumps({"error": str(e)})

    @staticmethod
    def j2h(j):
        try:
            o = _json.loads(j)
            d = _FD()
            d.from_json(o)
            return d.to_bytes().hex().upper()
        except Exception as e:
            return f"Error: {e}"


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
                except:
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
                except:
                    try:
                        d = sb.decode("utf-8")
                        if any(ord(c) < 32 and c not in "\n\r\t" for c in d):
                            raise ValueError()
                        self.v[fn].append(d)
                    except:
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
        p = {
            "1": 4588, "2": 1,
            "4": {"1": target_gid, "2": {"1": 3, "2": "", "3": answer,
                "4": _d("PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0idXRmLTgiPgo8bW1nIHRlbXBsYXRlSUQ9IjEiIGJyaWVmPSIiIHNlcnZpY2VJRD0iMTA0Ij4KICAgIDxpdGVtIGxheW91dD0iMiI+CiAgICAgICAgPHBpY3R1cmUgY292ZXI9IiIvPgogICAgICAgIDx0aXRsZT7lvZPog4Hls748L3RpdGxlPgogICAgPC9pdGVtPgogICAgPHNvdXJjZS8+CjwvbW1nPgo="),
                "6": {}, "7": {}, "8": {"2": 2}}},
            "12": 1}
        data = _PT.j2h(_json.dumps(p, ensure_ascii=False))
        await client.api.call_action("send_packet", cmd=_d("T2lkYlN2Y1RycGNUY3AuMHgxMWVjXzE="), data=data)
        lines = ["【加群申请】已发送", f"群号：{target_gid}"]
        try:
            info = await client.get_group_info(group_id=target_gid, no_cache=True)
            if info.get("group_name"):
                lines.append(f"群名：{info['group_name']}")
            mc, mm = info.get("member_count"), info.get("max_member_count")
            if mc and mm:
                lines.append(f"人数：{mc}/{mm}")
        except:
            pass
        if answer:
            lines.append(f"答案：{answer}")
        return "\n".join(lines)

    @staticmethod
    async def add_friend(client, target_uin, self_id, verify="", remark="", answer=""):
        p = {
            "1": 1986, "2": 5,
            "4": {"1": self_id, "2": target_uin, "3": 1, "4": 1, "5": 0,
                  "7": verify, "11": 1, "12": 3, "18": remark, "20": 0,
                  "26": answer, "28": 1, "29": 1},
            "12": 1}
        data = _PT.j2h(_json.dumps(p, ensure_ascii=False))
        await client.api.call_action("send_packet", cmd=_d("T2lkYlN2Y1RycGNUY3AuMHg3YzJfNQ=="), data=data)
        lines = ["【好友申请】已发送", f"Q号：{target_uin}"]
        try:
            info = await client.get_stranger_info(user_id=target_uin, no_cache=True)
            if info.get("nickname"):
                lines.append(f"昵称：{info['nickname']}")
            if info.get("qqLevel"):
                lines.append(f"等级：{info['qqLevel']}")
        except:
            pass
        if verify:
            lines.append(f"验证消息：{verify}")
        if remark:
            lines.append(f"备注：{remark}")
        if answer:
            lines.append(f"答案：{answer}")
        return "\n".join(lines)
