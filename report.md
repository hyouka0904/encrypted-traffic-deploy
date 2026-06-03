# 基於機器學習的加密流量分類與 QoS 管理系統

---

## 1. 前言

隨著 HTTPS、VPN 等加密協定的普及，網路流量的內容層已無法直接檢視，傳統以 port number 或 payload 特徵為基礎的流量分類方法逐漸失效。然而，網路管理者仍需區分不同類型的流量——例如讓 VoIP 享有低延遲，讓 P2P 下載受到限速——才能有效實施 QoS 策略。以 flow-level 的統計特徵（封包頻率、封包大小、inter-arrival time 等）訓練機器學習模型，是目前在不解密內容的前提下仍能進行分類的主流方法之一。

本專案以 Raspberry Pi 5 作為 WiFi AP 節點，在邊緣端直接執行加密流量分類模型，並依推論結果對各連線裝置套用對應的 QoS 限速規則。整個系統分為兩個 repository：

- **rpi_ap_train**：以 ISCX-VPN 2016 資料集訓練分類模型，匯出 ONNX 格式供部署端使用
- **encrypted-traffic-deploy**：在 Raspberry Pi 5 上架設 AP、執行即時推論、管理 Captive Portal 與 QoS 管道

---

## 2. 系統架構

整體 pipeline 如下：

```
rpi_ap_train（訓練端）
  └─ 訓練 → ONNX export → 發布至 GitHub Releases
        │
        ▼
encrypted-traffic-deploy（部署端）
  └─ 下載 ONNX 模型至 models/
        │
        ▼
Raspberry Pi 5（MadChicken）
  ├─ hostapd + dnsmasq → WiFi AP（my_rpi_AP）
  ├─ Captive Portal（Flask, portal/app.py）
  │     └─ iptables HTTP 攔截 → 強制登入 → 白名單放行
  ├─ flow_monitor（inference/flow_monitor.py）
  │     └─ scapy 抓 wlan1 封包 → 抽 15 特徵 → ONNX Runtime 推論
  └─ qos_controller（inference/qos_controller.py）
        └─ iptables mangle POSTROUTING 打 fwmark → tc HTB 限速
```

Client 連上 AP 後，首先被 Captive Portal 攔截強制登入；通過認證後，flow_monitor 持續監控該 IP 的流量並進行分類，qos_controller 依分類結果動態調整下行頻寬。

---

## 3. 訓練端

### 3.1 資料集

使用 [ISCX-VPN-NonVPN 2016](https://www.unb.ca/cic/datasets/vpn.html) 的 Scenario B，檔案為 `TimeBasedFeatures-Dataset-15s-AllinOne.arff`。清洗後共 18,758 筆，7 個流量類別，以 80/20 stratified split 分為訓練集與測試集：

| 類別 | 筆數 | 比例 |
|------|------|------|
| VOIP | 5,097 | 27.2% |
| BROWSING | 5,000 | 26.7% |
| FT | 2,950 | 15.7% |
| CHAT | 2,086 | 11.1% |
| P2P | 1,928 | 10.3% |
| STREAMING | 957 | 5.1% |
| MAIL | 740 | 3.9% |

### 3.2 特徵工程

ISCX ARFF 原始共有 23 個特徵欄位，但其中 8 個 fiat/biat 欄位（`total_fiat`、`total_biat`、`min_fiat`、`min_biat`、`max_fiat`、`max_biat`、`mean_fiat`、`mean_biat`）存在明顯的數值異常：`min_fiat` 數值大於 `max_fiat`（min > max），`total_fiat` 的數值看起來像封包計數而非 inter-arrival time 總和，`duration` 欄位的數值為微秒而非秒。這些欄位的語意與實際數值不一致，部署端以 scapy 即時計算出的對應值無法與其對齊，因此從訓練特徵中全數排除。

最終保留 15 個可在部署端正確重現的特徵：

| 特徵 | 說明 |
|------|------|
| `duration` | flow 持續時間（μs） |
| `flowPktsPerSecond` | 每秒封包數 |
| `flowBytesPerSecond` | 每秒位元組數 |
| `min/max/mean/std_flowiat` | 所有封包的 inter-arrival time 統計（μs） |
| `min/mean/max/std_active` | flow active 期間長度統計（μs） |
| `min/mean/max/std_idle` | flow idle 期間長度統計（μs） |

### 3.3 訓練 Pipeline

訓練流程由 `training/preprocess.py` 與 `training/main.py` 兩個 entry point 組成。`preprocess.py` 讀取 ARFF 原始資料，清洗並以 stratified split 輸出 `train.csv`、`test.csv`、`features.txt`。`main.py` 讀取 YAML 設定檔，動態 import 對應模型模組，依序執行 fit → evaluate → ONNX export，最後輸出 `<model>.onnx` 與 `<model>_results.json`。

所有模型統一透過 `training/onnx_utils.py` 匯出 ONNX：sklearn 模型使用 skl2onnx，DL 模型使用 `torch.onnx.export` 加 argmax wrapper。匯出後的模型透過 GitHub Releases 發布，部署端直接以 `wget` 下載。

### 3.4 模型比較

本專案針對三個 tree-based 模型完成完整的訓練與評估：

| 模型 | 演算法 | Macro F1（15 特徵） | ONNX 輸出類型 |
|------|--------|---------------------|---------------|
| rf | Random Forest | 0.8411 | 字串 label |
| xgb | XGBoost | 0.8533 | int index |
| lgb | LightGBM | 0.8501 | int index |

離線分類效能上，XGBoost 略優，LightGBM 次之，Random Forest 稍低。

### 3.5 部署模型選擇

儘管 XGBoost 與 LightGBM 的離線 macro F1 較高，本專案部署端選用 **Random Forest**。原因在於 skl2onnx 對 Random Forest 匯出的 ONNX 模型，其 `label` 輸出節點直接為字串類別（如 `"VOIP"`），部署端 flow_monitor 拿到推論結果即可直接使用。XGBoost 與 LightGBM 的 ONNX 輸出為 int index，需要額外載入 `label_classes.txt` 查表轉換，增加部署整合的複雜度。為求系統整合簡單可靠，選用 Random Forest 作為主要部署模型，XGBoost 與 LightGBM 作為對照實驗使用。

---

## 4. 部署端

### 4.1 AP 環境

硬體為 Raspberry Pi 5（8GB，hostname: MadChicken）。WiFi AP 使用外接 USB 網卡 Edimax N150（RTL8188EU，USB ID `7392:b811`）作為 `wlan1`，內建 `wlan0` 連接家用 WiFi 作為上網出口。

RTL8188EU 的原廠驅動（`rtl8xxxu`）在 AP 模式下不穩定，需替換為第三方驅動（`rtl8188eus`，來自 aircrack-ng）。該驅動在 AP 模式下無法支援 WPA2 加密，因此 AP 設定為開放網路（無密碼），並以 Captive Portal 作為存取控制機制。

網路設定上，`wlan1` 透過 systemd-networkd 設定靜態 IP `192.168.4.1/24`，並將 `wlan1` 加入 NetworkManager 的 unmanaged 清單，避免 NetworkManager 干擾 AP 介面。hostapd 負責 AP 廣播（SSID: `my_rpi_AP`，channel 6），dnsmasq 提供 DHCP（派發範圍 `192.168.4.10`~`192.168.4.50`）。

### 4.2 Captive Portal

Captive Portal 以 Flask 實作，監聽 `192.168.4.1:8080`。運作流程如下：

1. Client 連上 `my_rpi_AP` 取得 IP 後，OS 自動發送 HTTP connectivity check
2. `portal/setup_iptables.sh` 預先設置 iptables 規則，將所有來自 `wlan1` 的 port 80 流量導向 Flask
3. Flask 回傳 302，OS 跳出 Captive Portal 視窗
4. 使用者透過 Portal 頁面註冊帳號，等待 Admin 在 Dashboard 審核
5. 審核通過並登入後，該 IP 自動加入 iptables 白名單，放行正常上網
6. 登入後進入 Status 頁面，可在此啟動流量分類實驗

Portal 使用純 iptables HTTP 攔截機制，不採用 DNS hijacking，避免干擾多網卡 client 的其他連線。

### 4.3 flow_monitor 設計

`inference/flow_monitor.py` 以 scapy 監聽 `wlan1` 介面，對每條 flow（以 src IP、dst IP、src port、dst port、protocol 五元組識別）持續累積封包時間戳，並定期執行推論。主要設計如下：

**Sliding window**：每條 flow 保留最近 15 秒的封包時間戳，每 5 秒執行一次推論（tick）。推論後不清空，僅丟棄 window 外的舊時間戳。

**Active/idle 計算**：封包間隔超過 5,000,000 μs（5 秒）視為 idle，觸發 active period 結算。無 active 或 idle 資料時，對應特徵填 -1，與 ISCX 資料集的處理方式對齊。

**特徵近似限制**：`flowBytesPerSecond`、`flowPktsPerSecond`、active/idle 統計目前為整條 flow 歷史的近似值，並非嚴格的 15 秒 window 計算，這是已知的特徵保真度限制之一。

**多數決**：同一個 client IP 可能同時存在多條 flow（不同 src/dst port），每個 tick 對同一 IP 的所有 flow 各自推論後，取票數最多的 label 作為該 IP 的分類結果。

**穩定機制**：每個 IP 維護最近 3 個 tick 的推論結果，連續 3 次相同才呼叫 qos_controller 更新限速規則，避免短暫的分類波動導致頻繁切換。

**推論 log**：每個 stable tick 將結果 append 至 `logs/flow_infer_log.csv`（格式：`timestamp,ip,label,model`），作為後續評估的原始資料來源。

### 4.4 qos_controller 設計

`inference/qos_controller.py` 負責初始化 tc HTB（Hierarchical Token Bucket）並透過 iptables fwmark 控制各 IP 的限速 class。

**Chain 選擇**：fwmark 打在 `mangle POSTROUTING` 而非 `FORWARD`。tc egress 在封包即將離開介面時才套用，`FORWARD` 打的 mark 在某些情況下會被 conntrack 清掉，導致 tc fw filter 無法讀取。`POSTROUTING` 是封包離開前的最後一個 netfilter hook，確保 mark 保留到 tc 讀取。

**方向修正**：iptables 規則使用 `-d <client_ip>` 比對下行封包（Pi 傳給 client 的方向），對應 tc 在 wlan1 egress 的整形方向，兩者一致。早期版本錯誤使用 `-s <client_ip>`（上行方向），導致 fwmark 打在上行封包，tc egress 無法命中，此 bug 已修正。

**QoS 頻寬配置**（來自 `configs/qos_policy.yaml`）：

| Class | Rate | Ceil | Priority |
|-------|------|------|----------|
| VOIP | 10 Mbit | 30 Mbit | 0（最高）|
| STREAMING | 20 Mbit | 60 Mbit | 1 |
| CHAT | 5 Mbit | 20 Mbit | 2 |
| BROWSING | 10 Mbit | 50 Mbit | 3 |
| MAIL | 2 Mbit | 20 Mbit | 5 |
| FT | 5 Mbit | 40 Mbit | 6 |
| P2P | 2 Mbit | 10 Mbit | 7（最低）|

---

## 5. 流量分類實驗設計

### 5.1 流量產生方式

為了控制實驗變因，流量由部署端 Portal 的 Status 頁面內嵌 JavaScript 腳本自動按固定序列產生，不需在 client 端安裝額外工具。採用瀏覽器端產生流量有以下優點：流量必須在通過 Captive Portal 登入後才能送出（未登入的流量被 iptables FORWARD DROP 擋下），由 status 頁面發起天然滿足此前提；序列由腳本控制，三個模型跑同一份固定序列，排除人為操作的時序差異。

三種流量類別的模擬方式如下：

| 類別 | 模擬方式 |
|------|---------|
| BROWSING | 反覆對 Portal 發短 HTTP GET，間隔 4 秒，模擬瀏覽行為 |
| FT | 持續 fetch 大檔案（`/static/testfile.bin`，100MB），完成後立刻再發，模擬持續大流量的檔案傳輸 |
| P2P | 每 8 秒以 Range header 取 `testfile.bin` 前 32KB，模擬 P2P 的小量持續流量 |

VOIP、STREAMING、CHAT 三類未納入實驗：VOIP 需 RTP/UDP 封包特徵與特定封包間隔，瀏覽器 fetch 無法產生；STREAMING 需持續大流量加上特定 IAT 模式，與單純 fetch 大檔案的特徵不同；CHAT 需短封包、低頻率的雙向互動，與一般 HTTP GET 的特徵差異微妙，難以忠實模擬。這三類列為 future work。

### 5.2 Ground Truth 序列

實驗使用預先固定的 ground truth 序列（`experiment/ground_truth.csv`），以相對時間偏移定義各流量段：

| offset_sec | label |
|------------|-------|
| 0 | BROWSING |
| 45 | FT |
| 115 | P2P |
| 170 | FT |
| 205 | BROWSING |
| 285 | P2P |
| 325 | BROWSING |
| 385 | FT |
| 435 | P2P |
| 510 | END |

共 9 段、510 秒。同種流量刻意不連續排列，避免模型靠順序規律預測。序列使用相對偏移而非絕對時間戳，可預先推 git，三輪實驗共用同一份序列。

### 5.3 評估邏輯

實驗結束後，由 `experiment/eval.py` 對 `logs/flow_infer_log.csv` 中該次實驗區間內的推論紀錄進行評估。對齊邏輯：每筆推論的 `offset_sec` 落在哪個 ground truth 區間，即以該區間的 label 為 ground truth 比對。整體準確度為所有推論中預測正確的比例；per-label 準確度為落在該 label 區間內的推論中預測正確的比例，另記錄 `predicted_as` 分布以定位錯誤模式。

---

## 6. 實驗結果與分析

### 6.1 三模型準確度

三個模型各自完成一輪完整實驗，整體準確度如下：

| 模型 | 總推論筆數 | 正確筆數 | 整體準確度 |
|------|-----------|---------|-----------|
| RandomForest | 100 | 31 | 31.0% |
| XGBoost | 97 | 30 | 30.9% |
| LightGBM | 95 | 30 | 31.6% |

Per-label 準確度：

| 模型 | BROWSING | FT | P2P |
|------|----------|----|-----|
| RandomForest | 0.0% | 100.0% | 0.0% |
| XGBoost | 0.0% | 100.0% | 0.0% |
| LightGBM | 0.0% | 100.0% | 0.0% |

### 6.2 現象觀察

三個模型的預測結果完全一致：所有推論均輸出 FT，FT 區間內的推論全部正確，BROWSING 與 P2P 區間內的推論全部錯誤。三者的整體準確度（約 31%）實際上等同於 FT 段佔總推論筆數的比例，並非模型具有區分能力的表現。

### 6.3 根本原因分析：Training-Serving Skew

三個模型在離線測試集上的 macro F1 均達 0.84 以上，但在即時部署環境下退化為只輸出單一類別，原因是 **training-serving skew**——訓練資料與即時特徵提取之間存在系統性的數值分布差距。

主要來源有以下幾點：

**特徵近似值問題**：flow_monitor 的 `flowBytesPerSecond`、active/idle 統計目前為整條 flow 歷史的近似值，而非嚴格的 15 秒滑動視窗計算。對於長時間運行的 flow，這些近似值會與訓練資料的分布產生偏差。

**Active/idle threshold 未對齊**：flow_monitor 以 5 秒作為 idle 門檻，但 ISCX ARFF 資料集使用的門檻尚未確認。若兩者不同，active/idle 特徵的分布將從根本上不一致。

**ARFF 資料本身的異常**：如前述，ARFF 中多個特徵欄位存在數值與語意不符的問題（min > max、數量級錯誤），在排除 fiat/biat 八個欄位後，保留的 15 個特徵是否也存在類似的潛在異常，仍有待驗證。

上述問題共同導致模型在即時環境下接收到的特徵向量落在訓練分布之外，模型只能持續輸出訓練時最常見或在這個特徵空間中最近的類別（FT）。這是已知的系統性偏差，並非偶發錯誤。

### 6.4 QoS 功能驗證

QoS 管道的功能以 iperf3 單獨驗證，不依賴 flow_monitor 的即時分類路徑。在 Pi 端啟動 iperf3 server，client 以 reverse 模式（`-R`）測量下行吞吐量，確認 tc HTB 限速生效。以 P2P class（ceil = 10 Mbit）為例，實測吞吐量壓在 11.5 Mbit 附近，限速功能正常運作。STREAMING class（ceil = 60 Mbit）因 WiFi 實際吞吐低於 ceil 值，限速無從發揮，屬正常現象。

iptables 方向 bug 修正後（從 `-s` 改為 `-d <client_ip>`），fwmark 正確打在下行封包，tc egress 能正確讀取並套用對應 class，整條 QoS 管道運作正常。

---

## 7. 已知限制與 Future Work

### 7.1 即時特徵保真度

目前「排除不可重現的 fiat/biat 欄位、保留 15 個特徵」是折衷做法，並未從根本上解決 training-serving skew 問題。即時推論準確度低的根本原因在於訓練資料的特徵提取方式與部署端的即時提取方式之間存在系統性差距，具體表現為：

- Active/idle threshold 未對齊（訓練端 ARFF 的門檻未知）
- 部分特徵為整條 flow 的近似值，非嚴格 sliding window
- ARFF 資料集本身可能存在其他未被識別的數值異常

根本解法是取得 ISCX-VPN 2016 的原始 PCAP，以自行控制、定義明確的特徵提取程式碼重新計算特徵並重訓模型，讓訓練端與部署端共用同一份特徵提取邏輯，從源頭消除 skew。

### 7.2 未納入的流量類別

VOIP、STREAMING、CHAT 三類無法以瀏覽器 fetch 忠實模擬，需找到能產生對應網路特徵的工具（如 RTP 封包產生器）或使用真實流量重放，才能納入實驗評估。

### 7.3 其他系統層面

Captive Portal 的認證狀態目前僅存放於記憶體，Portal 重啟或斷線後即消失，需持久化至磁碟以改善使用體驗。Portal、flow_monitor 目前均需手動啟動，尚未設定 systemd 開機自啟。

---

## 8. 結論

本專案成功建立一套從訓練到邊緣部署的完整 pipeline：以 ISCX-VPN 2016 訓練 tree-based 分類模型，匯出 ONNX 後部署至 Raspberry Pi 5，搭配 Captive Portal 與 tc HTB QoS 管道，實現對連入裝置的流量分類與頻寬管理。

系統整合層面，AP 架設、Captive Portal、flow_monitor 即時推論迴路、qos_controller 限速管道均已完整運作，iperf3 驗證 QoS 限速功能正常。

分類效能層面，三個模型（RF / XGB / LGB）在即時部署環境下的整體準確度均約 31%，且一致表現為只輸出 FT 一類，根本原因為 training-serving skew。這是在現有 ISCX ARFF 資料集與即時 scapy 特徵提取之間存在的系統性差距所導致，並非模型本身的問題——三個模型在離線測試集上的 macro F1 均達 0.84 以上。解決此問題的路徑明確：取得原始 PCAP，以統一的特徵提取程式碼重訓，讓訓練端與部署端的特徵分布真正對齊。