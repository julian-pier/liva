import json
import socket
import time
from smart_home.config import is_govee_enabled

MCAST_GRP = "239.255.255.250"
MCAST_PORT = 4001
LISTEN_PORT = 4002

SCAN_MSG = {
    "msg": {
        "cmd": "scan",
        "data": {
            "account_topic": "reserve"
        }
    }
}


def main():
    if not is_govee_enabled():
        print("Govee scanning disabled via LIVA_GOVEE_ENABLED=false")
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(5)
    sock.bind(("", LISTEN_PORT))

    payload = json.dumps(SCAN_MSG).encode("utf-8")
    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    send_sock.sendto(payload, (MCAST_GRP, MCAST_PORT))

    print("Warte auf Govee-Antworten...\n")
    start = time.time()
    seen = set()

    while time.time() - start < 5:
        try:
            data, addr = sock.recvfrom(4096)
            text = data.decode("utf-8", errors="ignore")
            if text in seen:
                continue
            seen.add(text)
            print(f"Von {addr}:")
            print(text)
            print("-" * 60)
        except socket.timeout:
            break

    sock.close()
    send_sock.close()


if __name__ == "__main__":
    main()
