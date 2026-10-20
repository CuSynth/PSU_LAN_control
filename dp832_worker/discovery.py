"""Find Rigol DP800 supplies on the LAN by probing TCP/5555 with *IDN?."""
from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from typing import Callable, Iterable, Optional, Union

from .models import ALL_PORTS, PORT_RIGOL, ModelProfile, profile_for
from .scpi import ScpiConnection, ScpiError

DEFAULT_PORT = PORT_RIGOL


@dataclass
class DeviceInfo:
    host: str
    port: int
    idn: str
    vendor: str
    model: str
    serial: str
    firmware: str

    @property
    def id(self) -> str:
        return self.serial or f"{self.host}:{self.port}"

    @property
    def profile(self) -> ModelProfile:
        return profile_for(self.model)

    @property
    def n_channels(self) -> int:
        return self.profile.n_channels

    def to_dict(self) -> dict:
        return asdict(self)


def parse_idn(host: str, port: int, idn: str) -> DeviceInfo:
    parts = [p.strip() for p in idn.split(",")]
    parts += [""] * (4 - len(parts))
    return DeviceInfo(host, port, idn, parts[0], parts[1], parts[2], parts[3])


def probe(host: str, port: Union[int, Iterable[int]] = DEFAULT_PORT,
          timeout: float = 0.5) -> Optional[DeviceInfo]:
    """Return DeviceInfo if an SCPI instrument answers *IDN? at host:port (port may be a list)."""
    if not isinstance(port, int):
        for p in port:
            info = probe(host, p, timeout)
            if info:
                return info
        return None
    c = ScpiConnection(host, port, timeout=timeout)
    try:
        c.open()
        idn = c.query("*IDN?")
    except ScpiError:
        return None
    finally:
        c.close()
    if not idn or "," not in idn:
        return None
    return parse_idn(host, port, idn)


def local_networks(max_prefix_hosts: int = 1024) -> list[ipaddress.IPv4Network]:
    """IPv4 networks of the local interfaces (capped to keep the scan quick)."""
    nets: list[ipaddress.IPv4Network] = []
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family != socket.AF_INET or not a.netmask:
                    continue
                if a.address.startswith(("127.", "169.254.")):
                    continue
                try:
                    n = ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
                except ValueError:
                    continue
                nets.append(_cap(n, a.address, max_prefix_hosts))
    except ImportError:
        pass
    if not nets:  # fallback: primary interface, assume /24
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))  # no packet is sent
            ip = s.getsockname()[0]
            s.close()
            nets.append(ipaddress.ip_network(f"{ip}/24", strict=False))
        except OSError:
            pass
    uniq: dict[str, ipaddress.IPv4Network] = {str(n): n for n in nets}
    return list(uniq.values())


def _cap(net, addr, max_hosts):
    if net.num_addresses <= max_hosts:
        return net
    # huge network: scan only the /24 around our own address
    return ipaddress.ip_network(f"{addr}/24", strict=False)


def discover(
    extra_hosts: Iterable[str] = (),
    networks: Optional[Iterable[ipaddress.IPv4Network]] = None,
    port: Union[int, Iterable[int]] = ALL_PORTS,
    timeout: float = 0.4,
    workers: int = 96,
    progress: Optional[Callable[[int, int], None]] = None,
    found: Optional[Callable[[DeviceInfo], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> list[DeviceInfo]:
    """Scan local subnets (plus explicit hosts) on the given port(s) and return the instruments found.

    Default ports: 5555 (Rigol DP800) and 5025 (R&S/HAMEG HMP)."""
    ports = [port] if isinstance(port, int) else list(port)
    hosts: list[str] = list(dict.fromkeys(extra_hosts))
    nets = list(networks) if networks is not None else local_networks()
    for n in nets:
        hosts.extend(str(h) for h in n.hosts() if str(h) not in hosts)
    total, done = len(hosts), 0
    results: list[DeviceInfo] = []
    if not total:
        return results
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe, h, ports, timeout): h for h in hosts}
        for f in as_completed(futs):
            done += 1
            if cancel and cancel():
                for g in futs:
                    g.cancel()
                break
            info = f.result()
            if info:
                results.append(info)
                if found:
                    found(info)
            if progress:
                progress(done, total)
    return results
