# Software-Based Network Firewall — Ethernet Forwarding

Senior design project at Princess Sumaya University for Technology, supervised by
Eng. Hazem Marar.

This repository implements the project's bidirectional Ethernet forwarding layer.
The Python application runs on a Linux Mini PC positioned between a router and a
laptop, receiving raw Ethernet frames and transmitting them through the opposite
interface. Forwarding decisions are made explicitly in application code.

```text
Router LAN <-- cable --> Mini PC port0 [Python] port1 <-- cable --> Laptop
```

`port0` and `port1` are configurable interface names. ARP, IPv4, IPv6, DHCP, ICMP,
TCP and UDP use the same protocol-independent forwarding path. The application
preserves the received MAC addresses, EtherType and payload, and restores VLAN
tags supplied through Linux metadata. This implementation performs forwarding
without packet filtering, NAT, kernel bridging or IP routing.

## Files and installation

```text
forwarder.py               One application entry point; standard library only
tests/test_forwarder.py    Focused unittest checks with simulated sockets
README.md                 Architecture, setup and verification
.gitignore
```

Use Linux and Python 3.8 or later. No `requirements.txt`, pip installation or
virtual environment is necessary. On Debian/Ubuntu, install Python and the
configuration/diagnostic tools:

```bash
sudo apt update
sudo apt install python3 iproute2 ethtool tcpdump
cd /path/to/python-firewall
python3 --version
python3 forwarder.py --help
python3 -m unittest discover -s tests -v
python3 -m py_compile forwarder.py tests/test_forwarder.py
```

Root or `CAP_NET_RAW` is needed to open raw packet sockets. The run command below
uses `sudo`.

## Prepare the two Mini PC ports

Use a local console or a separate management connection. The commands below remove
addresses from the two dedicated inline ports, so an SSH session through either
port would disconnect. Record their original addresses, manager configuration,
MTU, IPv6 setting and offload settings if you intend to restore them later.

Identify the actual ports before substituting names:

```bash
ip -brief link
ip -brief address
ip -d link show
# Replace these examples with the Mini PC's actual interfaces:
PORT0=enp1s0
PORT1=enp2s0
sudo ethtool "$PORT0"
sudo ethtool "$PORT1"
sudo ethtool -k "$PORT0"
sudo ethtool -k "$PORT1"
sysctl "net/ipv6/conf/$PORT0/disable_ipv6"
sysctl "net/ipv6/conf/$PORT1/disable_ipv6"
```

Check cabling and `Link detected: yes` after bringing the ports up. Use the two
physical Ethernet devices, not loopback, Wi-Fi, a bridge, bond or VLAN subinterface.
They must not be members of a bridge/bond or have another forwarder attached. Run
only one instance of this application for the pair; two instances would duplicate
traffic. Keep the topology a single inline path: this application does not run a
loop-prevention protocol.

Keep the Mini PC's IP configuration and DHCP clients off these two ports. On a
Mini PC managed by NetworkManager, make just these ports unmanaged first:

```bash
nmcli device status
sudo nmcli device set "$PORT0" managed no
sudo nmcli device set "$PORT1" managed no
```

Those `nmcli` commands apply only when NetworkManager is managing the ports. If
systemd-networkd or another manager is used, configure these two ports as unmanaged
there before proceeding; otherwise it can reapply addresses or offloads. Stop any
standalone DHCP client specifically on the inline ports as well.

Configure only the selected ports:

```bash
for port in "$PORT0" "$PORT1"; do
    sudo ip address flush dev "$port"
    sudo sysctl -w "net/ipv6/conf/$port/disable_ipv6=1"
    sudo ip link set dev "$port" mtu 1500 up
done
ip -brief address show dev "$PORT0"
ip -brief address show dev "$PORT1"
sudo ethtool "$PORT0"
sudo ethtool "$PORT1"
```

Flushing addresses removes the Mini PC's Layer 3 presence on these ports.
Disabling IPv6 **on these Mini PC interfaces only** prevents automatic link-local
addressing and router solicitation by the host; it does not stop `AF_PACKET`
forwarding of the laptop's IPv6 Ethernet frames. The 1500-byte MTU is for ordinary
Ethernet payloads and should match the endpoints. No IP address is needed on either
Mini PC inline port. Do not enable IP forwarding, create a kernel bridge, or add
iptables/nftables rules for this application.

For frame preservation, disable receive aggregation on both Mini PC interfaces:

```bash
for port in "$PORT0" "$PORT1"; do
    sudo ethtool -K "$port" gro off
    sudo ethtool -K "$port" lro off
    sudo ethtool -k "$port"
done
```

Check the resulting settings. If `rx-gro-hw` or `rx-udp-gro-forwarding` is listed
and enabled, disable that feature too on the affected port:

```bash
sudo ethtool -K "$PORT0" rx-gro-hw off
sudo ethtool -K "$PORT0" rx-udp-gro-forwarding off
# Repeat for PORT1 if those features are present and enabled there.
```

An unsupported feature already reported as `off [fixed]` needs no change.
GRO/LRO combines wire packets, so receive aggregation must be disabled to retain
individual Ethernet frames. If a feature is fixed on, check the NIC driver's
configuration options. See the
[Linux offload documentation](https://docs.kernel.org/networking/segmentation-offloads.html).

For VLAN tests, also inspect `rx-vlan-filter` in `ethtool -k`. If enabled and
changeable, use `sudo ethtool -K "$PORT0" rx-vlan-filter off` and repeat for
`PORT1` so hardware does not reject VLANs before Python sees them. VLAN reception
depends on the NIC's supported settings. Linux may supply a stripped VLAN tag
through auxiliary metadata even when hardware `rxvlan` stripping is disabled.

The application makes none of these persistent/manual configuration changes. It
requests promiscuous reception through each socket so it can receive frames
addressed to the laptop/router MACs. Linux releases those membership references
when the sockets close; pre-existing promiscuous users remain unaffected.

## Run on the Mini PC

From the project directory, with the variables above still defined:

```bash
sudo python3 forwarder.py "$PORT0" "$PORT1"
# Equivalent with example names:
# sudo python3 forwarder.py enp1s0 enp2s0
```

Startup reports the selected ports and successful initialization. It checks that
both names exist, refer to distinct Ethernet interfaces and are administratively
up; check physical carrier separately with `ethtool`. Every five seconds it prints
each direction's `forwarded`, `rx_errors` and `tx_errors` totals, plus the latest
error if any. There is no per-frame printing. Ctrl+C prints final counters, closes
both sockets and releases their memberships. Startup failures also close any
socket already opened.

`forwarded` counts complete frames accepted by `send()`. Errors are aggregated
until the next report. A full transmit buffer, link failure or other send error
is counted without retrying or queuing frames. Use endpoint captures to confirm
delivery and NIC statistics to inspect losses occurring before `recvmsg()`.

## Connectivity verification

1. Wire the topology shown above. The router's DHCP server should be enabled.
   Start the application and confirm both interface names and `forwarding active`.

2. On a Linux laptop with NetworkManager, identify its Ethernet interface, then
   create a temporary DHCP connection. These commands assume Debian/Ubuntu tools
   and a profile name that does not already exist:

   ```bash
   sudo apt install network-manager iproute2 iputils-ping curl
   ip -brief link
   LAPTOP_PORT=enp3s0  # Replace with the laptop's Ethernet interface
   nmcli radio wifi off
   sudo nmcli connection add type ethernet ifname "$LAPTOP_PORT" \
       con-name firewall-test connection.autoconnect no \
       ipv4.method auto ipv6.method auto
   sudo nmcli connection up firewall-test
   ip -4 address show dev "$LAPTOP_PORT"
   ip -4 route show dev "$LAPTOP_PORT"
   nmcli -f IP4,DHCP4 device show "$LAPTOP_PORT"
   ```

   Disable other laptop uplinks/VPNs during testing too. Confirm a DHCP address
   from the router's subnet and a default gateway through this Ethernet port.
   To repeat DHCP acquisition while observing traffic, take the test connection
   down and up with `sudo nmcli connection down firewall-test` followed by
   `sudo nmcli connection up firewall-test`. The server may renew a cached
   lease instead of doing a full four-message DHCP exchange.

3. On the laptop, substitute the router's actual LAN address, then test LAN,
   external IP connectivity and DNS/HTTPS:

   ```bash
   ROUTER_IP=192.168.1.1  # Replace using the default gateway shown above
   ping -I "$LAPTOP_PORT" -c 4 "$ROUTER_IP"
   ip -4 route get 1.1.1.1
   ping -I "$LAPTOP_PORT" -c 4 1.1.1.1
   getent ahostsv4 example.com
   curl -4 --interface "$LAPTOP_PORT" --max-time 20 -I https://example.com
   ```

   Verify `ip route get` selects the Ethernet port. Some networks block external
   ICMP; distinguish that from a forwarding failure using HTTPS and captures.
   IPv6 connectivity additionally depends on the router/upstream providing IPv6.

4. In two additional Mini PC terminals, use the actual port names to observe
   Ethernet headers and packet contents during the ping/DHCP tests:

   ```bash
   # Terminal A, router-facing port:
   sudo tcpdump -p -i enp1s0 -nn -e -s 0 -XX -c 40
   # Terminal B, laptop-facing port:
   sudo tcpdump -p -i enp2s0 -nn -e -s 0 -XX -c 40
   ```

   `-p` avoids having tcpdump itself enable promiscuous mode, which could mask
   an application setup error. No capture file is written. Compare the same
   requests/replies by ARP addresses, DHCP transaction IDs or ICMP sequence IDs.
   A laptop request should arrive on port1 and leave port0; its reply should
   arrive on port0 and leave port1, preserving MAC addresses, EtherType and data.
   To isolate a direction, add `-Q in` or `-Q out` on Linux: for example, compare
   port1 `-Q in` with port0 `-Q out`, then reverse the pair for replies.
   Captures on the Mini PC observe the kernel path; endpoint captures or a
   physical tap provide stronger evidence of actual wire transmission. tcpdump
   is only an observer, never the forwarding mechanism.

5. Confirm both directional `forwarded` counts increase during these tests.
   Broadcast/background traffic also contributes, so the two totals need not
   match. Investigate increasing error totals using the displayed last error,
   `ip -s -s link show dev enp1s0` and `sudo ethtool -S enp1s0`, repeating for
   port1. Check interface names, link state, MTU and aggregation settings first.

6. Press Ctrl+C in the forwarder terminal and confirm final counters and the
   clean shutdown message. With other laptop uplinks disabled, fresh router
   pings should now fail; this checks that Python was the forwarding path.
   Restart the same command to verify recovery. Forwarding is active while the
   Python application is running.

7. On the laptop, remove the temporary profile and restore Wi-Fi if it was
   originally enabled:

   ```bash
   sudo nmcli connection down firewall-test
   sudo nmcli connection delete firewall-test
   nmcli radio wifi on
   ```

   Restore the Mini PC's recorded interface/offload settings manually if returning
   it to its previous use. For ports originally managed by NetworkManager,
   `sudo nmcli device set "$PORT0" managed yes` (and `PORT1`) returns management;
   it does not itself restore every offload or sysctl setting changed above.

## Implementation

Two nonblocking `AF_PACKET` / `SOCK_RAW` sockets are bound to the selected ports.
A selector watches both. Python receives one frame, restores a reported VLAN tag
if needed, and immediately sends it through the opposite socket. `forward_one()`
contains the application forwarding decision. Each receive operation delivers a
complete frame to Python. Storage consists of the current frame and the Linux
socket/driver buffers; there is no additional application frame queue.

The socket asks Linux to omit local outgoing captures with
`PACKET_IGNORE_OUTGOING`; Python also checks `PACKET_OUTGOING` as a fallback.
This prevents recirculating local transmissions. Truncated socket data or unusable
VLAN metadata is reported as a reception error rather than sent as a corrupted
frame. The relevant Linux
definitions are in [if_packet.h](https://raw.githubusercontent.com/torvalds/linux/master/include/uapi/linux/if_packet.h).

Unmodified frames are sent with the same bytes, including source/destination
MACs and any VLAN headers already present. `PACKET_AUXDATA` reports a stripped
outer VLAN tag; Python reinserts it at byte 12, preserving TCI (including zero),
TPID when available, and any inner tag. For older metadata without valid TPID,
the fallback is 0x8100. Reconstruction requires the driver to provide the stripped
tag's metadata; the interface exposes one stripped outer tag. The metadata and
send behavior follow [Linux's packet socket implementation](https://raw.githubusercontent.com/torvalds/linux/master/net/packet/af_packet.c).

NICs commonly check Ethernet CRC and discard corrupt frames before userspace;
they also commonly remove FCS on receive and generate a new FCS on transmit.
Python therefore neither receives nor validates the original FCS in the usual
configuration. Standard 64–1518-byte untagged Ethernet frames correspond to
60–1514 bytes without the four-byte FCS (padding visibility varies by driver).
Preamble, inter-frame gaps and FCS generation are handled by the hardware.
NIC hardware can consume MAC control frames or discard unsupported traffic before
Python sees it. Driver CRC/missed-packet counters provide diagnostics; see
[Linux interface statistics](https://docs.kernel.org/networking/statistics.html).

`AF_PACKET` supplies a copy of incoming traffic; it does not prevent the local
host network stack or other services from seeing it. This is why the inline ports
are dedicated, unaddressed and unmanaged. The application does not automatically
alter the host's routes, services, firewall, offloads or system-wide settings.

## Automated tests

The test suite uses simulated sockets to check frame preservation, both forwarding
directions, VLAN reconstruction, outgoing-capture suppression, error accounting,
interface initialization and resource cleanup. Frame-preservation cases include
60-byte and 1514-byte frames, excluding FCS.

```bash
python3 -m unittest discover -s tests -v
```

The 20 automated tests passed with Python 3.12.3. These results cover application
logic; use the connectivity procedure above to validate the physical installation.
