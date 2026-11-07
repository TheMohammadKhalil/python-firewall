#!/usr/bin/env python3
"""Milestone 1: Python-controlled, two-port Ethernet forwarding on Linux."""

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
import errno
import fcntl
import logging
import os
import selectors
import socket
import struct
import sys
import time


# Linux UAPI constants (not all are exported by Python's socket module).
ETH_P_ALL = 0x0003
SOL_PACKET = 263
PACKET_ADD_MEMBERSHIP = 1
PACKET_MR_PROMISC = 1
PACKET_AUXDATA = 8
PACKET_IGNORE_OUTGOING = 23
PACKET_OUTGOING = 4
TP_STATUS_VLAN_VALID = 1 << 4
TP_STATUS_VLAN_TPID_VALID = 1 << 6
SIOCGIFFLAGS = 0x8913
SIOCGIFHWADDR = 0x8927
IFF_UP = 0x1
ARPHRD_ETHER = 1
AUXDATA = struct.Struct("=IIIHHHH")
RECEIVE_SIZE = 65535
STATS_INTERVAL = 5.0
LOG = logging.getLogger("forwarder")


@dataclass
class Counters:
    forwarded: int = 0
    receive_errors: int = 0
    transmit_errors: int = 0
    last_error: str = ""


def validate_interfaces(names):
    """Read interface identity, Ethernet type and administrative state only."""
    indexes = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        for name in names:
            encoded = os.fsencode(name)
            if not encoded or len(encoded) >= 16 or b"\0" in encoded:
                raise ValueError(f"Invalid interface name: {name!r}")
            try:
                index = socket.if_nametoindex(name)
                # Large enough for struct ifreq on both 32- and 64-bit Linux.
                request = struct.pack("16s24x", encoded)
                hardware = fcntl.ioctl(control, SIOCGIFHWADDR, request)
                flags = fcntl.ioctl(control, SIOCGIFFLAGS, request)
            except OSError as exc:
                raise ValueError(f"Cannot inspect interface {name!r}: {exc}") from exc
            if struct.unpack_from("H", hardware, 16)[0] != ARPHRD_ETHER:
                raise ValueError(f"{name}: an Ethernet interface is required")
            if not struct.unpack_from("H", flags, 16)[0] & IFF_UP:
                raise ValueError(f"{name}: interface is down; see README setup")
            if index in indexes:
                raise ValueError("Select two different Ethernet interfaces")
            indexes.append(index)
    return indexes


def open_interface(name, index):
    """Create a bound raw socket with socket-scoped promiscuous membership."""
    # Protocol zero prevents reception before binding to the requested port.
    raw = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
    try:
        raw.setsockopt(SOL_PACKET, PACKET_AUXDATA, 1)
        try:
            raw.setsockopt(SOL_PACKET, PACKET_IGNORE_OUTGOING, 1)
        except OSError as exc:
            if exc.errno not in (errno.ENOPROTOOPT, errno.EINVAL):
                raise
            LOG.info("%s: outgoing capture disabled in Python (kernel fallback)", name)
        membership = struct.pack("=iHH8s", index, PACKET_MR_PROMISC, 0, b"")
        raw.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, membership)
        raw.setblocking(False)
        # Python converts this tuple's protocol from host to network byte order.
        raw.bind((name, ETH_P_ALL))
        return raw
    except BaseException:
        raw.close()  # Also releases promiscuous membership on partial startup.
        raise


def restore_vlan(frame, ancillary):
    """Reinsert a stripped outer VLAN tag only when Linux reports one."""
    for level, kind, data in ancillary:
        if level != SOL_PACKET or kind != PACKET_AUXDATA:
            continue
        if len(data) < AUXDATA.size:
            raise ValueError("incomplete PACKET_AUXDATA")
        status, _, _, _, _, tci, tpid = AUXDATA.unpack_from(data)
        if status & TP_STATUS_VLAN_VALID:
            if len(frame) < 14:
                raise ValueError("VLAN metadata without a complete Ethernet header")
            if not status & TP_STATUS_VLAN_TPID_VALID:
                tpid = 0x8100  # Older Linux reports only the 802.1Q TCI.
            return frame[:12] + struct.pack("!HH", tpid, tci) + frame[12:]
    return frame


def forward_one(incoming, outgoing, counters):
    """Receive one frame and send it once on the opposite port; no frame queue."""
    try:
        frame, ancillary, flags, address = incoming.recvmsg(
            RECEIVE_SIZE, socket.CMSG_SPACE(AUXDATA.size)
        )
    except BlockingIOError:
        return  # Readiness can become stale; no frame was received.
    except OSError as exc:
        counters.receive_errors += 1
        counters.last_error = f"receive: {exc}"
        return

    # This is capture-direction handling, not a rule on network frame contents.
    # Keep it even when PACKET_IGNORE_OUTGOING is supported.
    if address[2] == PACKET_OUTGOING:
        return
    try:
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError("truncated frame or ancillary data from recvmsg")
        frame = restore_vlan(frame, ancillary)
    except ValueError as exc:
        counters.receive_errors += 1
        counters.last_error = f"receive: {exc}"
        return

    # The forwarding decision lives here: every received frame goes to the peer.
    try:
        sent = outgoing.send(frame)
        if sent != len(frame):
            raise OSError(errno.EIO, f"short Ethernet send: {sent}/{len(frame)} bytes")
    except OSError as exc:
        counters.transmit_errors += 1
        counters.last_error = f"transmit: {exc}"
        return  # No retry queue and no sending a partial frame's remainder.
    counters.forwarded += 1


def report(names, counters):
    for source, target, counts in (
        (names[0], names[1], counters[0]),
        (names[1], names[0], counters[1]),
    ):
        LOG.info(
            "%s -> %s: forwarded=%d rx_errors=%d tx_errors=%d%s",
            source, target, counts.forwarded, counts.receive_errors,
            counts.transmit_errors,
            f" last_error={counts.last_error}" if counts.last_error else "",
        )


def run(names):
    indexes = validate_interfaces(names)
    counts = (Counters(), Counters())
    with ExitStack() as resources:
        ports = [
            resources.enter_context(open_interface(name, index))
            for name, index in zip(names, indexes)
        ]
        selector = resources.enter_context(selectors.DefaultSelector())
        selector.register(ports[0], selectors.EVENT_READ, (ports[1], counts[0]))
        selector.register(ports[1], selectors.EVENT_READ, (ports[0], counts[1]))
        LOG.info("Initialized %s <-> %s; forwarding active. Ctrl+C to stop.", *names)
        report(names, counts)
        next_report = time.monotonic() + STATS_INTERVAL
        try:
            while True:
                for key, _ in selector.select(timeout=0.5):
                    peer, counters = key.data
                    forward_one(key.fileobj, peer, counters)
                now = time.monotonic()
                if now >= next_report:
                    report(names, counts)
                    next_report = now + STATS_INTERVAL
        finally:
            report(names, counts)
    # ExitStack closes both sockets and releases their promiscuous memberships.


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Forward Ethernet frames in Python between two Linux ports."
    )
    parser.add_argument("port0", help="router-facing Ethernet interface")
    parser.add_argument("port1", help="laptop-facing Ethernet interface")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if not sys.platform.startswith("linux") or not hasattr(socket, "AF_PACKET"):
        LOG.error("Linux with AF_PACKET support is required")
        return 1
    LOG.info("Selected ports: port0=%s, port1=%s", args.port0, args.port1)
    try:
        run((args.port0, args.port1))
    except KeyboardInterrupt:
        LOG.info("Stopped; sockets closed and socket memberships released.")
        return 0
    except PermissionError as exc:
        LOG.error("Raw sockets require root or CAP_NET_RAW; use sudo. %s", exc)
        return 1
    except (OSError, ValueError) as exc:
        LOG.error("Forwarder stopped: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
