"""Wake-on-LAN: the "magic packet" that switches on a sleeping or powered-off machine whose network card listens for it
(Phoenix: BIOS "Power On From S5 by PME#" + NetworkManager `wake-on-lan magic`).

The packet is six 0xFF bytes and then the MAC sixteen times, sent as UDP broadcast. It goes to the home network's own
broadcast address (this PC is on Wi-Fi; the router passes the broadcast on to the wired machines) and to
255.255.255.255, a few times each, since a single UDP packet can go missing. Port 9 ("discard") by convention.
"""
from __future__ import annotations

import ipaddress
import socket

from . import config


def packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", ""))
    if len(raw) != 6:
        raise ValueError("a MAC address is six bytes")
    return b"\xff" * 6 + raw * 16


def targets() -> list[str]:
    """The LAN's broadcast address (assumed /24, as the Virgin hub hands out) and the general one."""
    out = []
    try:
        out.append(str(ipaddress.ip_network(f"{config.lan_ip()}/24", strict=False).broadcast_address))
    except ValueError:
        pass
    return [*out, "255.255.255.255"]


def wake(mac: str, repeat: int = 3) -> None:
    data = packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeat):
            for addr in targets():
                try:
                    s.sendto(data, (addr, 9))
                except OSError:
                    pass
