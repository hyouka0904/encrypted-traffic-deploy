import argparse
import subprocess
import yaml
import logging
from pathlib import Path


CONFIG_PATH = Path(__file__).parent.parent / "configs" / "qos_policy.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="[qos] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


CONFIG   = load_config()
IFACE    = CONFIG["interface"]
TOTAL_BW = CONFIG["total_bandwidth"]
CLASSES  = CONFIG["classes"]

LABEL_TO_CLASSID: dict[str, int] = {
    label: 10 + i for i, label in enumerate(CLASSES)
}
LABEL_TO_MARK = LABEL_TO_CLASSID

_marked_ips: dict[str, str] = {}   # ip → label


def _run(cmd: str, check: bool = True):
    log.debug(f"$ {cmd}")
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True
    )
    if check and result.returncode != 0:
        if "already exists" not in result.stderr and "RTNETLINK" not in result.stderr:
            log.warning(f"cmd failed: {cmd}\n{result.stderr.strip()}")
    return result


def init_tc():
    log.info(f"初始化 tc HTB on {IFACE} (total: {TOTAL_BW}Mbit)")

    _run(f"tc qdisc del dev {IFACE} root", check=False)
    _run(f"tc qdisc add dev {IFACE} root handle 1: htb default 99")
    _run(f"tc class add dev {IFACE} parent 1: classid 1:1 htb "
         f"rate {TOTAL_BW}mbit ceil {TOTAL_BW}mbit")

    for label, cfg in CLASSES.items():
        classid = LABEL_TO_CLASSID[label]
        _run(f"tc class add dev {IFACE} parent 1:1 classid 1:{classid} htb "
             f"rate {cfg['rate']}mbit ceil {cfg['ceil']}mbit "
             f"prio {cfg['priority']}")
        _run(f"tc qdisc add dev {IFACE} parent 1:{classid} handle {classid}: sfq perturb 10")
        log.info(f"  class {label:<12} id=1:{classid}  "
                 f"rate={cfg['rate']}M ceil={cfg['ceil']}M prio={cfg['priority']}")

    _run(f"tc class add dev {IFACE} parent 1:1 classid 1:99 htb "
         f"rate 1mbit ceil {TOTAL_BW}mbit prio 7")

    for label, classid in LABEL_TO_CLASSID.items():
        mark = LABEL_TO_MARK[label]
        _run(f"tc filter add dev {IFACE} parent 1: protocol ip "
             f"handle {mark} fw classid 1:{classid}")

    log.info("tc 初始化完成")


def _mark_ip(ip: str, label: str):
    """對目標 IP（下行封包）在 POSTROUTING chain 打 fwmark"""
    mark = LABEL_TO_MARK.get(label)
    if mark is None:
        log.warning(f"未知 label: {label}，跳過")
        return

    # 不管 _marked_ips，直接查 iptables 刪除該 IP 所有舊規則
    result = _run("iptables -t mangle -S POSTROUTING", check=False)
    for line in result.stdout.splitlines():
        if f"-d {ip}/32" in line and "-j MARK" in line:
            del_cmd = line.replace("-A ", "-D ", 1)
            _run(f"iptables -t mangle {del_cmd}", check=False)

    # POSTROUTING：封包即將離開介面前打 mark，確保 tc egress 能讀到
    _run(
        f"iptables -t mangle -A POSTROUTING -d {ip} -o {IFACE} "
        f"-j MARK --set-mark {mark}"
    )
    _marked_ips[ip] = label
    log.info(f"mark {ip:<16} → {label} (mark={mark})")


def apply_batch(results: list[tuple[str, str]]):
    for src_ip, label in results:
        _mark_ip(src_ip, label)


def clear_all():
    log.info("清除所有 QoS 規則")
    _run("iptables -t mangle -F POSTROUTING", check=False)
    _marked_ips.clear()
    _run(f"tc qdisc del dev {IFACE} root", check=False)
    log.info("清除完成")


init_tc()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="手動測試單一 IP 的 QoS 限速")
    parser.add_argument("--ip",     help="client IP，例如 192.168.4.2")
    parser.add_argument("--label", 
                        choices=list(CLASSES.keys()),
                        help="流量類別")
    parser.add_argument("--clear", action="store_true",
                        help="清除所有規則後離開")
    args = parser.parse_args()

    if args.clear:
        clear_all()
    elif args.ip is None or args.label is None:
        parser.error("--ip and --label are required unless --clear is used")
    else:
        _mark_ip(args.ip, args.label)
        log.info(f"已套用：{args.ip} → {args.label}")
        log.info("執行 'tc -s class show dev wlan1' 確認 class 是否有流量")