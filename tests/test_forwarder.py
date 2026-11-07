"""Hardware-free checks; these do not establish NIC or wire-level behavior."""

import errno
import socket
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, call, patch

import forwarder as fw


MACS = bytes.fromhex("00112233445566778899aabb")


def ethernet(ethertype=0x0800, size=60):
    return MACS + struct.pack("!H", ethertype) + bytes(i % 256 for i in range(size - 14))


def auxdata(tci, tpid=0x8100, status=None):
    if status is None:
        status = fw.TP_STATUS_VLAN_VALID | fw.TP_STATUS_VLAN_TPID_VALID
    return [(fw.SOL_PACKET, fw.PACKET_AUXDATA,
             fw.AUXDATA.pack(status, 60, 60, 0, 14, tci, tpid))]


def receiver(frame, ancillary=(), flags=0, packet_type=3):
    port = Mock()
    port.recvmsg.return_value = (frame, ancillary, flags, ("port0", 3, packet_type, 1, b""))
    return port


def sender():
    port = Mock()
    port.send.side_effect = len
    return port


class ForwardingTests(unittest.TestCase):
    def test_opaque_frames_preserved_for_all_incoming_packet_types(self):
        # No protocol handler is needed, including for an unknown EtherType or
        # an IEEE 802.3 length field. Sizes exclude the four-byte Ethernet FCS.
        for kind in (0, 1, 2, 3):
            for ethertype in (0x0806, 0x0800, 0x86DD, 0x88B5, 46):
                for size in (60, 1514):
                    with self.subTest(kind=kind, ethertype=ethertype, size=size):
                        frame = ethernet(ethertype, size)
                        output, counts = sender(), fw.Counters()
                        fw.forward_one(receiver(frame, packet_type=kind), output, counts)
                        output.send.assert_called_once_with(frame)
                        self.assertEqual(counts, fw.Counters(forwarded=1))

    def test_outgoing_capture_is_not_forwarded(self):
        output, counts = sender(), fw.Counters()
        fw.forward_one(receiver(ethernet(), packet_type=4), output, counts)
        output.send.assert_not_called()
        self.assertEqual(counts, fw.Counters())

    def test_vlan_bytes_already_present_are_untouched(self):
        frame = MACS + bytes.fromhex("8100000786dd") + bytes(46)
        output = sender()
        fw.forward_one(receiver(frame, auxdata(0, status=0)), output, fw.Counters())
        output.send.assert_called_once_with(frame)

    def test_stripped_vlan_restored_including_zero_tci_and_custom_tpid(self):
        for tci, tpid in ((0, 0x8100), (0xB123, 0x88A8)):
            with self.subTest(tci=tci, tpid=tpid):
                frame, output = ethernet(), sender()
                fw.forward_one(receiver(frame, auxdata(tci, tpid)), output, fw.Counters())
                output.send.assert_called_once_with(
                    MACS + struct.pack("!HH", tpid, tci) + frame[12:]
                )

    def test_stripped_outer_vlan_preserves_inner_tag(self):
        frame = MACS + bytes.fromhex("810000070800") + bytes(46)
        restored = fw.restore_vlan(frame, auxdata(42, 0x88A8))
        self.assertEqual(restored, MACS + bytes.fromhex("88a8002a") + frame[12:])

    def test_legacy_vlan_metadata_uses_8021q(self):
        frame = ethernet()
        restored = fw.restore_vlan(frame, auxdata(0, 0, fw.TP_STATUS_VLAN_VALID))
        self.assertEqual(restored, MACS + bytes.fromhex("81000000") + frame[12:])

    def test_unrelated_ancillary_data_is_ignored(self):
        frame = ethernet()
        self.assertIs(fw.restore_vlan(frame, [(socket.SOL_SOCKET, 99, b"")]), frame)

    def test_incomplete_receive_is_reported_without_sending_corrupted_bytes(self):
        cases = [
            (ethernet(), [], socket.MSG_TRUNC),
            (ethernet(), [], socket.MSG_CTRUNC),
            (ethernet(), [(fw.SOL_PACKET, fw.PACKET_AUXDATA, b"short")], 0),
            (b"short", auxdata(1), 0),
        ]
        for frame, ancillary, flags in cases:
            with self.subTest(flags=flags, length=len(frame)):
                output, counts = sender(), fw.Counters()
                fw.forward_one(receiver(frame, ancillary, flags), output, counts)
                output.send.assert_not_called()
                self.assertEqual(counts.receive_errors, 1)
                self.assertEqual(counts.forwarded, 0)
                self.assertIn("receive:", counts.last_error)

    def test_stale_readiness_does_not_count_as_error(self):
        incoming, output, counts = receiver(ethernet()), sender(), fw.Counters()
        incoming.recvmsg.side_effect = BlockingIOError(errno.EAGAIN, "try again")
        fw.forward_one(incoming, output, counts)
        self.assertEqual(counts, fw.Counters())
        output.send.assert_not_called()

    def test_receive_error_is_counted_and_next_frame_can_forward(self):
        incoming, output, counts = receiver(ethernet()), sender(), fw.Counters()
        incoming.recvmsg.side_effect = [
            OSError(errno.ENETDOWN, "link down"), incoming.recvmsg.return_value,
        ]
        fw.forward_one(incoming, output, counts)
        fw.forward_one(incoming, output, counts)
        self.assertEqual(counts.receive_errors, 1)
        self.assertEqual(counts.forwarded, 1)

    def test_send_errors_are_counted_without_retry_or_success(self):
        for error in (BlockingIOError(errno.EAGAIN, "full"),
                      OSError(errno.ENOBUFS, "full"), OSError(errno.ENETDOWN, "down")):
            with self.subTest(error=error):
                output, counts = sender(), fw.Counters()
                output.send.side_effect = error
                fw.forward_one(receiver(ethernet()), output, counts)
                self.assertEqual(output.send.call_count, 1)
                self.assertEqual(counts.transmit_errors, 1)
                self.assertEqual(counts.forwarded, 0)

    def test_short_send_does_not_send_remainder(self):
        output, counts = Mock(), fw.Counters()
        output.send.return_value = 12
        fw.forward_one(receiver(ethernet()), output, counts)
        self.assertEqual(output.send.call_count, 1)
        self.assertEqual(counts.transmit_errors, 1)
        self.assertEqual(counts.forwarded, 0)


class InitializationTests(unittest.TestCase):
    @patch.object(fw.socket, "socket")
    def test_socket_does_not_receive_until_bound_and_uses_promiscuous_membership(self, factory):
        raw = factory.return_value
        self.assertIs(fw.open_interface("enp1s0", 7), raw)
        factory.assert_called_once_with(socket.AF_PACKET, socket.SOCK_RAW, 0)
        self.assertEqual(raw.method_calls, [
            call.setsockopt(fw.SOL_PACKET, fw.PACKET_AUXDATA, 1),
            call.setsockopt(fw.SOL_PACKET, fw.PACKET_IGNORE_OUTGOING, 1),
            call.setsockopt(fw.SOL_PACKET, fw.PACKET_ADD_MEMBERSHIP,
                            struct.pack("=iHH8s", 7, fw.PACKET_MR_PROMISC, 0, b"")),
            call.setblocking(False),
            call.bind(("enp1s0", fw.ETH_P_ALL)),
        ])

    @patch.object(fw.socket, "socket")
    def test_unsupported_ignore_outgoing_falls_back(self, factory):
        raw = factory.return_value
        raw.setsockopt.side_effect = [None, OSError(errno.ENOPROTOOPT, "old kernel"), None]
        self.assertIs(fw.open_interface("enp1s0", 7), raw)
        raw.bind.assert_called_once()
        raw.close.assert_not_called()

    @patch.object(fw.socket, "socket")
    def test_setup_failure_or_interrupt_closes_socket(self, factory):
        for failure in (OSError(errno.ENODEV, "removed"), KeyboardInterrupt()):
            with self.subTest(failure=failure):
                raw = Mock()
                factory.return_value = raw
                raw.bind.side_effect = failure
                with self.assertRaises(type(failure)):
                    fw.open_interface("enp1s0", 7)
                raw.close.assert_called_once()

    @patch.object(fw.socket, "socket")
    def test_auxdata_failure_does_not_silently_lose_vlan_information(self, factory):
        factory.return_value.setsockopt.side_effect = OSError(errno.ENOPROTOOPT, "no auxdata")
        with self.assertRaises(OSError):
            fw.open_interface("enp1s0", 7)
        factory.return_value.close.assert_called_once()

    @patch.object(fw.fcntl, "ioctl")
    @patch.object(fw.socket, "if_nametoindex")
    @patch.object(fw.socket, "socket")
    def test_interface_validation(self, factory, index, ioctl):
        def fields(hardware=fw.ARPHRD_ETHER, flags=fw.IFF_UP):
            def answer(control, request, buffer):
                value = hardware if request == fw.SIOCGIFHWADDR else flags
                return buffer[:16] + struct.pack("H", value) + buffer[18:]
            return answer

        index.side_effect = [7, 8]
        ioctl.side_effect = fields()
        self.assertEqual(fw.validate_interfaces(("enp1s0", "enp2s0")), [7, 8])
        for indexes, hardware, flags, message in (
            ([7, 7], 1, 1, "different"),
            ([7], 772, 1, "Ethernet"),
            ([7], 1, 0, "down"),
        ):
            with self.subTest(message=message):
                index.side_effect = indexes
                ioctl.side_effect = fields(hardware, flags)
                with self.assertRaisesRegex(ValueError, message):
                    fw.validate_interfaces(("enp1s0", "enp2s0"))
        index.side_effect = OSError(errno.ENODEV, "missing")
        with self.assertRaisesRegex(ValueError, "Cannot inspect"):
            fw.validate_interfaces(("missing", "enp2s0"))


class LifecycleTests(unittest.TestCase):
    @staticmethod
    def context_port():
        port = MagicMock()
        port.__enter__.return_value = port
        port.send.side_effect = len
        return port

    @patch.object(fw, "validate_interfaces", return_value=[7, 8])
    @patch.object(fw, "open_interface")
    @patch.object(fw.selectors, "DefaultSelector")
    def test_both_directions_and_ctrl_c_cleanup(self, selector_factory, opener, validator):
        ports = [self.context_port(), self.context_port()]
        frames = [ethernet(0x0806), ethernet(0x86DD)]
        for port, frame in zip(ports, frames):
            port.recvmsg.return_value = (frame, [], 0, ("port", 3, 3, 1, b""))
        opener.side_effect = ports
        selector = selector_factory.return_value.__enter__.return_value

        def ready(timeout):
            if selector.select.call_count > 1:
                raise KeyboardInterrupt
            return [(SimpleNamespace(fileobj=args[0], data=args[2]), 1)
                    for args, kwargs in selector.register.call_args_list]

        # Resolve readiness after run() has registered the real direction pairs.
        selector.select.side_effect = ready

        with self.assertLogs(fw.LOG, level="INFO") as captured:
            with self.assertRaises(KeyboardInterrupt):
                fw.run(("enp1s0", "enp2s0"))
        ports[1].send.assert_called_once_with(frames[0])
        ports[0].send.assert_called_once_with(frames[1])
        for port in ports:
            port.__exit__.assert_called_once()
        selector_factory.return_value.__exit__.assert_called_once()
        output = "\n".join(captured.output)
        self.assertIn("enp1s0 -> enp2s0: forwarded=1", output)
        self.assertIn("enp2s0 -> enp1s0: forwarded=1", output)

    @patch.object(fw, "validate_interfaces", return_value=[7, 8])
    @patch.object(fw, "open_interface")
    def test_second_port_failure_releases_first(self, opener, validator):
        port = self.context_port()
        opener.side_effect = [port, OSError(errno.EPERM, "denied")]
        with self.assertRaises(OSError):
            fw.run(("enp1s0", "enp2s0"))
        port.__exit__.assert_called_once()

    @patch.object(fw, "run", side_effect=KeyboardInterrupt)
    def test_ctrl_c_returns_success(self, run):
        with self.assertLogs(fw.LOG, level="INFO") as logs:
            self.assertEqual(fw.main(["enp1s0", "enp2s0"]), 0)
        self.assertIn("Stopped; sockets closed", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
