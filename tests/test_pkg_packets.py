"""Regression tests for the send_packet wire payloads."""

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from astrbot.api import logger  # noqa: F401
except ImportError:
    # The packet tests also run without an AstrBot installation.
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = Mock()
    astrbot.api = api
    with patch.dict(sys.modules, {"astrbot": astrbot, "astrbot.api": api}):
        import pkg
else:
    import pkg

ExpansionHandle = pkg.ExpansionHandle
_FD = pkg._FD
_PT = pkg._PT


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


class PacketEncodingTests(unittest.TestCase):
    def test_invalid_json_raises_instead_of_returning_error_text(self):
        with self.assertRaisesRegex(ValueError, "Packet JSON encoding failed") as caught:
            _PT.j2h("{")
        self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)

    def test_serializer_failure_preserves_cause(self):
        failure = RuntimeError("encoding failed")
        with patch.object(_FD, "to_bytes", side_effect=failure):
            with self.assertRaisesRegex(ValueError, "Packet JSON encoding failed") as caught:
                _PT.j2h('{"1": 1}')
        self.assertIs(caught.exception.__cause__, failure)

    def test_valid_payload_still_returns_hex(self):
        self.assertEqual(_PT.j2h('{"1": 1}'), "0801")


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

    async def test_encoding_failures_never_send_packets(self):
        operations = (
            (ExpansionHandle.add_group, (12345, "答案")),
            (ExpansionHandle.add_friend, (200, 100)),
            (ExpansionHandle.set_friend_remark, (200, "备注")),
        )
        for operation, args in operations:
            with self.subTest(operation=operation.__name__):
                with patch.object(_FD, "to_bytes", side_effect=RuntimeError("encoding failed")):
                    if operation is ExpansionHandle.set_friend_remark:
                        self.assertFalse(await operation(self.client, *args))
                    else:
                        with self.assertRaisesRegex(ValueError, "Packet JSON encoding failed"):
                            await operation(self.client, *args)
                self.client.api.call_action.assert_not_called()
                self.client.get_group_info.assert_not_called()
                self.client.get_stranger_info.assert_not_called()

    async def test_unencodable_unicode_never_sends_packets(self):
        invalid_text = "\ud800"
        with self.assertRaisesRegex(ValueError, "Packet JSON encoding failed"):
            await ExpansionHandle.add_group(self.client, 12345, invalid_text)
        with self.assertRaisesRegex(ValueError, "Packet JSON encoding failed"):
            await ExpansionHandle.add_friend(self.client, 200, 100, verify=invalid_text)
        self.assertFalse(await ExpansionHandle.set_friend_remark(self.client, 200, invalid_text))
        self.client.api.call_action.assert_not_called()
        self.client.get_group_info.assert_not_called()
        self.client.get_stranger_info.assert_not_called()

    async def test_set_friend_remark_packet_and_failure(self):
        remark = "  备注🙂\n"
        expected = (number(1, 0xfd8) + number(2, 1)
                    + blob(4, number(1, 200) + blob(5, blob(1, remark))) + number(12, 1))
        self.assertTrue(await ExpansionHandle.set_friend_remark(self.client, 200, remark))
        self.assert_packet("OidbSvcTrpcTcp.0xfd8_1", expected)
        self.client.api.call_action.side_effect = RuntimeError("send failed")
        with patch.object(pkg, "logger") as logger:
            self.assertFalse(await ExpansionHandle.set_friend_remark(self.client, 200, remark))
        message = str(logger.warning.call_args)
        for part in ("0xfd8_1", "200", "send failed"):
            self.assertIn(part, message)

    async def test_remark_encoding_failures_log_stage_and_never_send(self):
        for target, method, failure in (
            (pkg, "_remark_packet", TypeError("build failed")),
            (pkg._json, "dumps", TypeError("serialization failed")),
            (pkg._PT, "j2h", ValueError("encoding failed")),
        ):
            with self.subTest(method=method):
                with patch.object(target, method, side_effect=failure):
                    with patch.object(pkg, "logger") as logger:
                        self.assertFalse(await ExpansionHandle.set_friend_remark(
                            self.client, 200, "private remark"))
                self.client.api.call_action.assert_not_called()
                logger.warning.assert_called_once()
                message = logger.warning.call_args.args[0]
                for part in ("OidbSvcTrpcTcp.0xfd8_1", "200", "payload encoding failed", str(failure)):
                    self.assertIn(part, message)
                self.assertNotIn("send failed", message)
                self.assertNotIn("private remark", message)

    async def test_remark_real_unicode_encoding_failure_logs_stage(self):
        with patch.object(pkg, "logger") as logger:
            self.assertFalse(await ExpansionHandle.set_friend_remark(self.client, 200, "\ud800"))
        self.client.api.call_action.assert_not_called()
        message = logger.warning.call_args.args[0]
        for part in ("OidbSvcTrpcTcp.0xfd8_1", "200", "payload encoding failed", "utf-8"):
            self.assertIn(part, message)

    async def test_remark_send_failure_has_distinct_log_stage(self):
        self.client.api.call_action.side_effect = RuntimeError("transport unavailable")
        with patch.object(pkg, "logger") as logger:
            self.assertFalse(await ExpansionHandle.set_friend_remark(self.client, 200, "备注"))
        self.client.api.call_action.assert_awaited_once()
        logger.warning.assert_called_once()
        message = logger.warning.call_args.args[0]
        for part in ("OidbSvcTrpcTcp.0xfd8_1", "200", "send failed", "transport unavailable"):
            self.assertIn(part, message)
        self.assertNotIn("payload encoding failed", message)

    def test_named_packet_fields_and_empty_values(self):
        group = pkg._group_packet  # Known fields are mapped; opaque fields remain intact.
        self.assertEqual(group(42, " \n答 ")["4"]["2"]["3"], " \n答 ")
        self.assertEqual(group(42, "")["4"]["2"]["4"], pkg.GROUP_VERIFICATION_XML)
        self.assertIn("<title>当胁峾</title>", pkg.GROUP_VERIFICATION_XML)
        friend = pkg._friend_packet(200, 100, "", " ", "答")
        self.assertEqual({k: friend["4"][k] for k in ("1", "2", "7", "18", "26")},
                         {"1": 100, "2": 200, "7": "", "18": " ", "26": "答"})
        self.assertEqual(pkg._remark_packet(200, "")["4"], {"1": 200, "5": {"1": ""}})

    async def test_lookup_failures_are_logged_after_successful_send(self):
        for method, lookup, args, command, target in (
            (ExpansionHandle.add_group, self.client.get_group_info, (123, "答"), "0x11ec_1", "123"),
            (ExpansionHandle.add_friend, self.client.get_stranger_info, (456, 100), "0x7c2_5", "456"),
        ):
            with self.subTest(command=command):
                self.client.api.call_action.reset_mock()
                lookup.side_effect = RuntimeError("lookup failed")
                with patch.object(pkg, "logger") as logger:
                    result = await method(self.client, *args)
                self.assertIn("已发送", result)
                self.client.api.call_action.assert_awaited_once()
                message = str(logger.warning.call_args)
                for part in (command, target, "lookup failed"):
                    self.assertIn(part, message)

    def test_conversion_errors_log_and_preserve_results(self):
        with patch.object(pkg, "logger") as logger:
            self.assertIn('"error"', pkg._PT.h2j("not hex"))
            with self.assertRaisesRegex(ValueError, "Packet JSON encoding failed"):
                pkg._PT.j2h("not json")
        self.assertEqual(logger.warning.call_count, 2)

    async def test_add_send_failures_propagate_without_lookup(self):
        self.client.api.call_action.side_effect = RuntimeError("send failed")
        for method, args, lookup in (
            (ExpansionHandle.add_group, (123, ""), self.client.get_group_info),
            (ExpansionHandle.add_friend, (456, 100), self.client.get_stranger_info),
        ):
            with self.subTest(method=method.__name__):
                with self.assertRaisesRegex(RuntimeError, "send failed"):
                    await method(self.client, *args)
                lookup.assert_not_awaited()

    async def test_empty_text_fields_keep_wire_representation(self):
        inner = (number(1, 100) + number(2, 200) + number(3, 1) + number(4, 1)
                 + number(5, 0) + blob(7, "") + number(11, 1) + number(12, 3)
                 + blob(18, "") + number(20, 0) + blob(26, "")
                 + number(28, 1) + number(29, 1))
        await ExpansionHandle.add_friend(self.client, 200, 100)
        self.assert_packet("OidbSvcTrpcTcp.0x7c2_5",
                           number(1, 1986) + number(2, 5) + blob(4, inner) + number(12, 1))


if __name__ == "__main__":
    unittest.main()
