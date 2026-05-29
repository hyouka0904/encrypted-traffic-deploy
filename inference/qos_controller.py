
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
CLASSES  = CONFIG["classes"]   # dict: label → {rate, ceil, priority}

# tc class id 對應表（label → minor id，從 10 開始）
LABEL_TO_CLASSID: dict[str, int] = {
    label: 10 + i for i, label in enumerate(CLASSES)
}
# iptables mark 與 classid 相同
LABEL_TO_MARK = LABEL_TO_CLASSID

# 記錄已 mark 的 IP，避免重複下 iptables 規則
_marked_ips: dict[str, str] = {}   # ip → label


# ── 執行 shell 指令 ───────────────────────────────────────────────────
def _run(cmd: str, check: bool = True):
    log.debug(f"$ {cmd}")
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True
    )
    if check and result.returncode != 0:
        # 忽略「already exists」類錯誤
        if "already exists" not in result.stderr and "RTNETLINK" not in result.stderr:
            log.warning(f"cmd failed: {cmd}\n{result.stderr.strip()}")
    return result


# ── 初始化 tc HTB ─────────────────────────────────────────────────────
def init_tc():
    """
    建立 HTB root qdisc 和各類別的 class。
    每次啟動時重建（先刪再建），確保乾淨狀態。
    """
    log.info(f"初始化 tc HTB on {IFACE} (total: {TOTAL_BW}Mbit)")

    # 刪除舊的 qdisc（忽略錯誤）
    _run(f"tc qdisc del dev {IFACE} root", check=False)

    # root HTB qdisc，default class = 99（未分類流量）
    _run(f"tc qdisc add dev {IFACE} root handle 1: htb default 99")

    # root class（總頻寬上限）
    _run(f"tc class add dev {IFACE} parent 1: classid 1:1 htb "
         f"rate {TOTAL_BW}mbit ceil {TOTAL_BW}mbit")

    # 各應用類別的 class
    for label, cfg in CLASSES.items():
        classid = LABEL_TO_CLASSID[label]
        _run(f"tc class add dev {IFACE} parent 1:1 classid 1:{classid} htb "
             f"rate {cfg['rate']}mbit ceil {cfg['ceil']}mbit "
             f"prio {cfg['priority']}")
        # SFQ qdisc（公平佇列）掛在每個 leaf class 下
        _run(f"tc qdisc add dev {IFACE} parent 1:{classid} handle {classid}: sfq perturb 10")
        log.info(f"  class {label:<12} id=1:{classid}  "
                 f"rate={cfg['rate']}M ceil={cfg['ceil']}M prio={cfg['priority']}")

    # default class（未分類）
    _run(f"tc class add dev {IFACE} parent 1:1 classid 1:99 htb "
         f"rate 1mbit ceil {TOTAL_BW}mbit prio 7")

    # tc filter：根據 fwmark 導向對應 class
    for label, classid in LABEL_TO_CLASSID.items():
        mark = LABEL_TO_MARK[label]
        _run(f"tc filter add dev {IFACE} parent 1: protocol ip handle {mark} fw classid 1:{classid}")

    log.info("tc 初始化完成")


# ── 套用 QoS ──────────────────────────────────────────────────────────
def _mark_ip(ip: str, label: str):
    """用 iptables 對來源 IP 打上 fwmark"""
    mark = LABEL_TO_MARK.get(label)
    if mark is None:
        log.warning(f"未知 label: {label}，使用 default class")
        return

    prev_label = _marked_ips.get(ip)

    # 同一個 IP 已經是同 label，不需要改
    if prev_label == label:
        return

    # 先刪掉舊的 mark
    if prev_label is not None:
        old_mark = LABEL_TO_MARK[prev_label]
        _run(f"iptables -t mangle -D POSTROUTING -s {ip} -j MARK --set-mark {old_mark}",
             check=False)

    # 加新的 mark
    _run(f"iptables -t mangle -A POSTROUTING -s {ip} -j MARK --set-mark {mark}")
    _marked_ips[ip] = label
    log.info(f"mark {ip:<16} → {label} (mark={mark})")


def apply_batch(results: list[tuple[str, str]]):
    """
    接收 flow_monitor 的推論結果並套用 QoS。
    results: list of (src_ip, label)
    """
    for src_ip, label in results:
        _mark_ip(src_ip, label)


def clear_all():
    """清除所有 iptables mark 規則和 tc qdisc（用於停止服務時清理）"""
    log.info("清除所有 QoS 規則")
    for ip, label in list(_marked_ips.items()):
        mark = LABEL_TO_MARK.get(label, 0)
        _run(f"iptables -t mangle -D POSTROUTING -s {ip} -j MARK --set-mark {mark}",
             check=False)
    _marked_ips.clear()
    _run(f"tc qdisc del dev {IFACE} root", check=False)
    log.info("清除完成")


# ── 啟動時初始化 ──────────────────────────────────────────────────────
init_tc()