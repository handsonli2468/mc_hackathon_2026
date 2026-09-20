# Server 端進度（給 client 端）

> 更新：2026-09-19。對應協定：[vlm_transport.md](vlm_transport.md)（protocol_version 1）。回覆：[client_progress.md](client_progress.md)（2026-09-18 版）。
> 聯測步驟與除錯方法：[DEBUG.md](DEBUG.md)。

## 摘要

- **server 已部署在 Hackathon-local-server，和 Pi 5 聯測通過**：約 40 分鐘、296 筆有推論的 detect，290 筆 `FOUND`，只逾時 1 次（server 手動重啟造成）。
- **設定**：`LA_MODE=fast`、只回 bbox（不跑 SAM2）、GPU 是 **AMD Radeon 860M**（內顯）。
- **推論 1.9 秒是穩定值**，前提是解析度和場景不變：296 筆的 `server_ms` p50 1883 ms、p99 1894 ms、最大 1914 ms。第 3 節說明什麼情況會變。
- **建議 client 改成連續送圖**（`refresh_period_s = 0.0`），`timeout_s` 改 5.0、`no_query_backoff_s` 改 1.0，見第 5 節。
- **協定沒有改**，client 不需要配合修改程式。

## 1. 回覆 client_progress.md 第 5 節

| # | client 的問題 | 回覆 |
|---|---|---|
| 1 | 正式 IP | **`192.168.68.51`**（2026-09-20 起，預計固定；原本是 `192.168.50.125`）。**client 的 `vlm.endpoint` 要改成新 IP**。要確認 router 已設 DHCP 保留 |
| 2 | `LA_MODE` | **`fast`**，之後也維持 `fast`。只有場上可能出現大量同類物件時才會考慮 `hybrid`（第 3 節），改之前會先通知，屆時 client 的 `timeout_s` 要改成 12.0 |
| 3 | bbox 的鬆緊 | 目前觀察是**貼的**。實驗室照片測紙杯、水瓶，框都緊貼物件（DEBUG.md L2）；聯測時 tracker debug 畫面的範圍也落在紙杯上。樣本還不多，遇到偏大的情況再評估開 mask |
| 4 | 這次聯測的 server 設定 | 見第 2、3 節：`LA_MODE=fast`、Radeon 860M、1.9 秒在固定解析度下是穩定值 |
| 5 | 上級描述走 HTTP 還是 ROS 2 | 目前走 **HTTP**：`POST http://192.168.68.51:8080/api/query`，body `{"text": "..."}`（vlm_transport.md 第 4.5 節）。之後改成 ROS 2 也只換這一層，ZMQ 協定不變 |

另外，client_progress.md 第 4 節提到 4 筆 bbox 都是 `[235, 323, 326, 432]`，懷疑畫面是靜止的：
**同一張圖、同一個描述，VLM 每次輸出的框完全一樣**（L2 實測三次一致）。所以 4 筆完全相同，代表畫面幾乎沒變，這個推論是對的。

## 2. Server 環境與設定

| 項目 | 值 |
|---|---|
| 主機 | Hackathon-local-server（`192.168.68.51`，2026-09-20 起，預計固定） |
| CPU / GPU | AMD Ryzen AI 7 350，內顯 **AMD Radeon 860M**（RDNA 3.5，`gfx1152`） |
| 記憶體 | 30 GiB，CPU 與 GPU 共用 |
| VLM | NVIDIA **LocateAnything-3B**（Qwen2.5-3B + MoonViT），gguf `q8_0`，5.83 GiB |
| 推論後端 | locate-anything.cpp（ggml），**Vulkan**（RADV GFX1152） |
| `LA_MODE` | **`fast`** |
| SAM2 mask | **關閉**（`VLM_RETURN_MASK=0`），只回 bbox，`score` 固定 `-1` |
| 容器 | `vlm-server`（2026-09-19 從 `vlm-server-tracker-1` 改名），`restart=unless-stopped`，主機開機會自動啟動 |
| 同時推論數 | 1。多個 client 同時送圖會輪流排隊（第 3 節） |

## 3. 推論 1.9 秒是不是穩定值？

**是。在「解析度、場景、只有一個 client」都不變的前提下非常穩定。**

聯測時（Pi 相機 848×480，縮成 640×362 上傳，描述 `the paper water cup`）：

| 指標 | 樣本數 | p50 | p99 | 最大 | 最小 |
|---|---:|---:|---:|---:|---:|
| `server_ms`（client log） | 296 | 1883 | 1894 | 1914 | — |
| `locate_ms`（server `/api/status`，純推論） | 50 | 1882 | — | 1888 | 1879 |

變動範圍不到 2%。會讓它改變的情況：

| 情況 | 影響 | 根據 |
|---|---|---|
| **上傳圖片的像素數** | 1～2 個目標時，推論時間大致跟像素數成正比 | 見下表 |
| **畫面中有大量同類物件** | `fast` 模式框會壞掉（一條橫跨整張圖的框） | 15 隻狗的圖：回傳 `[0, 234, 640, 355]` |
| **另一個 client 同時送圖** | `server_ms` 變成約兩倍（包含排隊時間），但 `locate_ms` 不變 | L2 測試時兩個 client 同時送，出現 4.6 秒 |
| **server 重啟後的第一筆** | 慢約 0.6 秒（暖機） | 4038 ms，之後回到 3406 ms（one-dog 圖） |
| **`LA_MODE` 改成 `slow`／`hybrid`** | 約 2 倍，而且抖動大（5～7.8 秒） | vlm_transport.md 第 8 節 |

推論時間 vs 像素數（`fast`，1～2 個目標）：

| 上傳尺寸 | 像素數 | 推論 |
|---|---:|---:|
| 640×362（Pi 相機，聯測） | 232k | **1.88 秒** |
| 598×472（實驗室照片） | 282k | 2.35 秒 |
| 640×656（1 隻狗） | 420k | 3.41 秒 |
| 640×669（2 隻狗） | 428k | 3.51 秒 |

所以 client 如果改了 `upload_max_width` 或相機解析度，1.9 秒就不再適用。
照這個趨勢，縮到 512 寬（148k 像素）**可能**降到約 1.2 秒，但這是外插，**還沒驗證**，小物件的框也可能變差。

## 4. 聯測結果

| 層 | 內容 | 結果 |
|---|---|---|
| L0 | server 健康 | 通過 |
| L1 | Pi → server ZMQ ping | 通過：100 次 p50 10.8 ms、p99 69.4 ms、最大 93.8 ms，沒有掉包 |
| L2 | 實驗室照片，從筆電送 | 通過：紙杯、水瓶的框都緊貼物件，沒有選錯瓶子 |
| L3 | Pi bridge 接真 server（約 40 分鐘） | 通過，見下 |
| L4 | 失敗情境 | 部分通過：清描述、server 重啟、`NOT_FOUND` 都符合預期；換描述、斷網重連**還沒測** |

L3（2026-09-19 09:49～10:30 UTC，Pi 5 走 WiFi，從 bridge log 統計）：

| 指標 | 數值 |
|---|---|
| 結果 | `FOUND` 290、`NO_QUERY` 11、`NOT_FOUND` 4、`TIMEOUT` 1 |
| `rtt_ms` | p50 1988、p95 2054、p99 2101、最大 2826 |
| `network_ms` | p50 **103**、p95 170、p99 218、最大 944 |
| server 丟棄的請求 | 0 |
| tracker debug 畫面 | `TRACKING OK KLT`，134 點全部 inlier，每幀 6.8 ms |

- 唯一一次 `TIMEOUT` 在 09:57:40，server 剛好在 09:57:36 被手動重啟，在路上的那筆遺失，client 照設計逾時後繼續。
- client_progress.md 記錄的 `network_ms` 35～68 ms 只有 4 筆；40 分鐘的統計 p50 是 103 ms。
  比 ZMQ ping（約 10 ms）高很多，推測和 WiFi 省電有關（detect 間隔長，網卡在空檔睡著），**還沒驗證**。只佔總延遲約 5%。

## 5. 建議的 client 參數

```yaml
vlm.enable: true
vlm.endpoint: "tcp://192.168.68.51:5555"
vlm.timeout_s: 5.0
vlm.refresh_period_s: 0.0
vlm.upload_max_width: 640
vlm.jpeg_quality: 80
vlm.ping_period_s: 2.0
vlm.no_query_backoff_s: 1.0
vlm.error_backoff_s: 2.0
```

| 參數 | 建議 | 原本 | 理由 |
|---|---|---|---|
| `timeout_s` | **5.0** | 6.0 | 實測 rtt 最大 2.83 秒；就算推論慢到 3.5 秒、網路再到最差的 0.94 秒，也才 4.4 秒。原則是 **timeout 要大於「最慢推論 + 最慢網路」**，否則逾時重送的請求會排在 server 還沒跑完的那筆後面，接著連續逾時 |
| `refresh_period_s` | **0.0** | 8.0 | 讓 GPU 一直在跑，每次送的都是當下最新的一幀。原本 8 秒時 GPU 約 3/4 時間閒置。見下方說明 |
| `no_query_backoff_s` | **1.0** | 5.0 | server 回 `NO_QUERY` 不跑推論、立刻回，重試成本只有一次上傳。另外 `should_send()` 會先檢查 backoff，就算 ping 看到描述換了也要等 backoff 結束才送，5 秒會讓新描述最多晚 5 秒生效 |
| `error_backoff_s` | 2.0 | 2.0 | 主要是 server 重啟後的 `model loading`（約 3～10 秒），2 秒重試一次剛好 |
| `ping_period_s` | 2.0 | 2.0 | 連續送圖時幾乎不會送 ping（只在沒有請求在路上時才送），只在 backoff 期間有用 |
| `upload_max_width` | 640 | 640 | 先維持；改了之後推論時間會變（第 3 節） |
| `init.cache_s` | ≥ 5.0 | 7.0 | 要大於 `timeout_s`，7.0 不用改 |

### 為什麼不用每幀都送

server 對每個 client 只保留「最新的一筆」等待中的請求，推論中收到新圖時會覆蓋舊的（舊的不回覆），推論結束就處理最新那張。
但 client 本來就一次只送一筆，`refresh_period_s = 0` 已經等於「回來一筆、馬上送最新一幀」，結果的新鮮度和每幀都送一樣：

| | 一次一筆、連續送 | 每幀都送 |
|---|---|---|
| 結果回來時畫面的年紀 | 約 2.0 秒 | 約 2.0 秒 |
| 更新頻率 | 每 1.99 秒一次 | 每 1.88 秒一次（只快約 5%） |
| Pi 上傳量 | 約 14 KB/s | 30 fps × 27 KB ≈ 810 KB/s |
| client 程式 | 不用改 | 要改：多筆在路上、被丟棄的請求不會回覆 |

### 連續送圖要注意

- **tracker 每約 2 秒就會收到一次新的 bbox**，而且是用約 2 秒前的畫面建的。如果每次都直接替換 target，可能會跳動。建議只在「現在這一幀」驗證通過、或和目前 target 差很多時才替換。
- **GPU 會一直滿載**。這段時間如果有人用筆電的 `test_client` 測試，雙方的延遲都會變兩倍。

## 6. 交接方式：現有作法 vs 預追蹤（方案 B）

bbox 回來時描述的是約 2 秒前（約 60 幀前）的畫面。這段時間機器人和物體都可能在動，client 要把「舊畫面上的 bbox」轉成「現在畫面上的 target」。

### 已排除：server 端抽特徵往下傳

曾考慮 server 在 bbox 內抽特徵傳給 client，讓 client 直接做 KLT、不存舊圖。**已丟棄**：

- KLT 是逐幀追蹤，需要前一幀的影像 patch，而且只能處理數十 px 的位移。座標屬於約 60 幀前的畫面，直接跳到現在會追丟；逐幀推又得存更多幀。
- 改傳 ORB 描述子雖然可行，但特徵是從 q80 縮圖 JPEG 抽的，比對率會比 client 原始畫面差；server 還得跟 tracker 的參數與 OpenCV 版本綁死。換來的只是省下約 1.2 MB 的灰階快取。

### 兩種作法

**現有作法（client 已實作）：快取 + 當前幀驗證**

1. 送圖時把該幀存進快取（`init.cache_s = 7.0`）。
2. bbox 回來：從快取取出同一幀，在 bbox 內用深度切出物件、抽特徵建新 target。
3. 在「現在這一幀」偵測新 target，成功才替換。

**方案 B：預追蹤**

1. 送圖的同時，在整張畫面撒一批 KLT 點（`goodFeaturesToTrack` 或規則網格）。
2. 等待期間逐幀用 `calcOpticalFlowPyrLK` 追這些點，加 forward-backward 檢查剔除壞點。
3. bbox 回來：挑出「起點在 bbox 內、而且還活著」的點，這些點**已經在現在這一幀的位置上**。
4. 用這些點的位移估出變換，把 bbox 搬到現在的畫面，用**現在這一幀的深度**切出物件，直接開始追蹤。B 不需要保留舊幀的影像、深度和 TF。

| 面向 | 現有作法 | 方案 B |
|---|---|---|
| 快取 | 要存舊幀（灰階 + 深度 + TF） | 只存點軌跡 |
| 應付約 2 秒的位移 | 靠外觀在當前幀重新偵測；視角、尺度變化大時可能失敗 | **最好**：點是一路追過來的 |
| Pi 5 運算 | 只在交接時做一次 | **等待期間每幀都要跑 KLT**。連續送圖時等於**一直在跑** |
| 失敗模式 | 驗證失敗 → 保留舊 target | 目標上的點全部追丟（遮擋、快速轉動、模糊）→ 這筆作廢，只能等下一筆 |
| 實作複雜度 | 較低 | 較高：點的生命週期、剔除壞點、估計變換 |
| server | 不用改 | 不用改 |

### 怎麼選：先用現有作法量數據

現有作法已經實作，**先不要急著做 B**。建議在 client 加上這三項記錄，用數據決定：

| 要量的 | 怎麼量 | 看到什麼就該考慮 B |
|---|---|---|
| 交接驗證失敗率 | 統計「在現在這一幀偵測新 target」的成功與失敗次數 | 失敗率明顯偏高，而且多發生在機器人或物體移動時 |
| 延遲期間的位移 | 用 TF 算送圖到收到回應之間的相機平移與旋轉；或 bbox 中心在畫面上的位移 | 常常超過 bbox 寬度的一半 |
| Pi 5 CPU 餘裕 | 在 Pi 5 上量 `calcOpticalFlowPyrLK`（200／500 點）每幀耗時，加上現有 tracker 的負載 | B 的前提：每幀要在 33 ms 內跑完（30 fps） |

判斷規則：

- **失敗率可接受** → 維持現有作法。
- **失敗率高、主因是位移、Pi 5 有餘裕** → 加 B，現有作法留著當 fallback（B 追丟時沒有後備）。
- **失敗率高、但 Pi 5 沒有餘裕** → 先試「用舊幀的深度 + TF 把目標投影到現在的畫面，當作搜尋起點」（成本低，只對靜態物件有效）。
- **失敗主因是外觀**（光線、角度、遮擋）→ B 也救不了，要改善 tracker 的特徵或驗證條件。

目前的線索：tracker debug 畫面顯示每幀 6.8 ms、追 134 點。離 33 ms 的預算還有空間，但 B 要在整張畫面追更多點，而且 bridge、相機驅動也在搶 CPU，**不能直接推論 B 可行**，還是要實測。

## 7. server 端已知問題

| 問題 | 影響 | 暫時的處理 |
|---|---|---|
| server IP 換過一次（2026-09-20 改成 `192.168.68.51`） | IP 變了 client 全部逾時 | 確認 router 已設 DHCP 保留 |
| 重啟後描述消失、`query_version` 從 0 重算 | 重啟後一律 `NO_QUERY`；版本號可能和重啟前撞號，client 可能沒發現描述換了 | 重啟後等 2 秒以上再設描述，client 會看到 1→0→1；或設定 `VLM_INITIAL_QUERY`。之後要改 server |
| 同時只能跑一筆推論 | 多個 client 同時送會排隊 | 聯測時只讓 Pi 送圖 |
| `fast` 模式遇到大量同類物件會壞 | 回傳橫跨整張圖的框 | 場上有這種情況再改 `hybrid` |
| 沒有認證 | 同網段任何人都能改描述 | 內網暫時可接受 |

**協定行為變更（2026-09-19，部署後生效）**：上級用 `POST /api/query` 重送**一樣的描述**時，`query_version` 也會 +1（以前文字相同時版本不變）。
client 會當成描述改變：馬上送圖，新的 bbox 回來後替換 target。這是為了讓 BT engine 能用重送描述來開始一個新任務（`GET /api/target`，vlm_transport.md §4.6）。

## 8. 接下來

**server 端**

- 修 `query_version` 重啟歸零的問題。
- 驗證縮小上傳尺寸（512 寬）的速度和框的品質，有效的話再通知 client 調 `upload_max_width`。
- 配合 client 補測 L4 的「換描述」和「斷網重連」。

**需要 client 配合**

- 參數改成第 5 節的建議，特別是 `refresh_period_s = 0.0`。
- 確認 tracker 用真 server 的 bbox 建 target 成功（`Mask cleaned`、handoff log）。
- 加上第 6 節的三項記錄。
- 聯測時同時記錄 Pi 5 的 CPU 使用率。
