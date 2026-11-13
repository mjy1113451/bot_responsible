"""Regression tests for the send_packet wire payloads."""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkg import ExpansionHandle


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def number(field, value):
    return varint(field << 3) + varint(value)


def blob(field, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return varint((field << 3) | 2) + varint(len(value)) + value


class PacketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock()
        self.client.api.call_action = AsyncMock()
        self.client.get_group_info = AsyncMock(return_value={})
        self.client.get_stranger_info = AsyncMock(return_value={})

    def assert_packet(self, command, expected):
        self.client.api.call_action.assert_awaited_once_with(
            "send_packet", cmd=command, data=expected.hex().upper()
        )

    async def test_add_group_preserves_xml_and_answer_bytes(self):
        xml = (
            b'<?xml version="1.0" encoding="utf-8"?>\n'
            b'<mmg templateID="1" brief="" serviceID="104">\n'
            b'    <item layout="2">\n'
            b'        <picture cover=""/>\n'
            b'        <title>\xe5\xbd\x93\xe8\x83\x81\xe5\xb3\xbe</title>\n'
            b'    </item>\n'
            b'    <source/>\n'
            b'</mmg>\n'
        )
        answer = "  答案\n🙂  "
        inner = (number(1, 3) + blob(2, "") + blob(3, answer) + blob(4, xml)
                 + blob(6, b"") + blob(7, b"") + blob(8, number(2, 2)))
        expected = number(1, 4588) + number(2, 1) + blob(4, number(1, 12345) + blob(2, inner)) + number(12, 1)
        result = await ExpansionHandle.add_group(self.client, 12345, answer)
        self.assert_packet("OidbSvcTrpcTcp.0x11ec_1", expected)
        self.assertIn(f"答案：{answer}", result)

    async def test_add_friend_preserves_unicode_and_whitespace(self):
        verify, remark, answer = "  验证\n🙂 ", " 备注 ", "答案\t"
        inner = (number(1, 100) + number(2, 200) + number(3, 1) + number(4, 1)
                 + number(5, 0) + blob(7, verify) + number(11, 1) + number(12, 3)
                 + blob(18, remark) + number(20, 0) + blob(26, answer)
                 + number(28, 1) + number(29, 1))
        expected = number(1, 1986) + number(2, 5) + blob(4, inner) + number(12, 1)
        result = await ExpansionHandle.add_friend(self.client, 200, 100, verify, remark, answer)
        self.assert_packet("OidbSvcTrpcTcp.0x7c2_5", expected)
        self.assertIn(f"验证消息：{verify}", result)

    async def test_set_friend_remark_packet_and_failure(self):
        remark = "  备注🙂\n"
        expected = (number(1, 0xfd8) + number(2, 1)
                    + blob(4, number(1, 200) + blob(5, blob(1, remark))) + number(12, 1))
        self.assertTrue(await ExpansionHandle.set_friend_remark(self.client, 200, remark))
        self.assert_packet("OidbSvcTrpcTcp.0xfd8_1", expected)
        self.client.api.call_action.side_effect = RuntimeError("send failed")
        self.assertFalse(await ExpansionHandle.set_friend_remark(self.client, 200, remark))


if __name__ == "__main__":
    unittest.main()
