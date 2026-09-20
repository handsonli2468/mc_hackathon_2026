# VLM Target 傳輸介面

> 狀態：**server 與 client 都已實作，2026-09-19 在 Pi 5 ↔ Hackathon-local-server 聯測通過**（約 40 分鐘，296 筆 detect，290 筆 `FOUND`）。
> 第 7 節的參數已依實測更新。進度細節：[server_progress.md](server_progress.md)、[client_progress.md](client_progress.md)；聯測與除錯：[DEBUG.md](DEBUG.md)。

## 1. 流程總覽

```
上級描述 node ──HTTP──► server（保存目前的描述，編 query_version）
                    ▲                 │
      ① JPEG + id   │                 │ ② bbox（+ 選配 mask）+ id + query_version
                    │                 ▼
client (orb_tracker_node)
  ├─ 送圖時把那一幀（灰階、深度、當下 TF）存進本地快取
  ├─ 等待期間繼續用舊 target 追蹤
  └─ bbox 回來：從快取取出同一幀 → 在 bbox 內用深度找出物件範圍建新 target → 在「現在這一幀」確認後才替換
```

設計原則：

- **server 預設只回傳 bbox 兩個角點，不回傳圖片。** client 手上已經有送出去的那一幀和深度，由下游自己用深度切出物件。SAM2 mask 是選配（server 設 `VLM_RETURN_MASK=1`），用來和深度切割結果比較。
- **不需要兩台機器的時鐘同步。** 用 `request_id` 對應請求和回應，時間一律用 client 的時鐘量。
- **同一時間只有一筆請求在路上。** 避免 WiFi 變慢時請求越堆越多，延遲越來越大。

## 2. 傳輸方式：ZeroMQ over TCP

### 為什麼不用 ROS 2 topic / service

- 目前走 WiFi。DDS discovery 依賴 multicast，在 WiFi 上不穩定。
- [config/cyclonedds.xml](../config/cyclonedds.xml) 的 `MaxMessageSize` 是 65500B，一張影像會被切成多個 UDP 封包，WiFi 上只要掉一片就得整張重傳或整張丟掉。
- ROS 2 service 在網路中斷時容易卡住等不到回應。
- 換成 rmw_zenoh 會影響整台機器人的所有 node，而且專案用的是 Humble，binary 支援度需要另外確認。

### 為什麼選 ZeroMQ

- TCP 長連線，斷線會自動重連。
- 多段（multipart）二進位訊息，JPEG 和 PNG 直接傳，不需要 base64，體積也不會多約 33%。
- C++（libzmq）和 Python（pyzmq）都很輕量，server 端大概率是 Python。

### Socket 類型

| 端 | Socket | 動作 |
|---|---|---|
| server | `ROUTER` | `bind("tcp://*:5555")` |
| client | `DEALER` | `connect("tcp://<server_ip>:5555")` |

**不要用 `REQ/REP`。** `REQ` 必須嚴格一送一收，只要一個回應在 WiFi 上遺失，socket 就會卡在等待狀態。

### Socket 設定

**client（DEALER）**

| 選項 | 值 | 用途 |
|---|---|---|
| `ZMQ_LINGER` | `0` | 關閉時不等待未送出的訊息 |
| `ZMQ_IMMEDIATE` | `1` | 連線還沒建立時直接送失敗，不排隊 |
| `ZMQ_SNDHWM` | `1` | 送出佇列最多一筆 |
| `ZMQ_RCVTIMEO` / poll | 依 `vlm.timeout_s` | 逾時判斷 |
| `ZMQ_TCP_KEEPALIVE` | `1` | 偵測 WiFi 斷線 |
| `ZMQ_TCP_KEEPALIVE_IDLE` | `5` | 秒 |
| `ZMQ_TCP_KEEPALIVE_INTVL` | `2` | 秒 |
| `ZMQ_RECONNECT_IVL` | `500` | ms |

**server（ROUTER）**

| 選項 | 值 | 用途 |
|---|---|---|
| `ZMQ_LINGER` | `0` | 關閉時不等待未送出的訊息 |
| `ZMQ_TCP_KEEPALIVE` | `1` | 偵測 WiFi 斷線 |

### 訊息的 frame 結構

DEALER 送出時**不加**空的分隔 frame（這點和 REQ 不同）。

| 方向 | 在 client 端看到的 frame | 在 server ROUTER 看到的 frame |
|---|---|---|
| request | `[header, jpeg]` | `[identity, header, jpeg]` |
| response（FOUND 且 `has_mask`） | `[header, mask_png]` | server 送出時為 `[identity, header, mask_png]` |
| response（其他，含預設的 FOUND） | `[header]` | server 送出時為 `[identity, header]` |

## 3. 網路與 port 設定

| 項目 | 預設 | 說明 |
|---|---|---|
| Port | `5555/tcp` | client 參數 `vlm.endpoint` 可以改；server 端 `VLM_ZMQ_BIND` |
| 描述 API port | `8080/tcp` | HTTP，見 4.5；server 端 `VLM_HTTP_PORT` |
| Server IP | Hackathon-local-server `192.168.68.51` | 2026-09-20 起，預計固定（原本是 `192.168.50.125`）。要確認 router 已設 DHCP 保留 |
| 防火牆 | server 要開放 `5555/tcp`、`8080/tcp` 入站 | Hackathon-local-server 目前不需要另外設定，Pi 已可連線 |
| Docker | client container 已經是 `network_mode: host`，不需要另外映射 port | server 若跑在 container 裡要映射或用 host 網路 |
| ROS_DOMAIN_ID | 不受影響 | 這條連線不走 DDS |

**建議**：機器人上的 ROS 2 流量盡量不要經過 WiFi。如果 server 也要透過 ROS 2 接收上級描述，那是另一條連線，要另外評估。

## 4. 訊息格式

`header` 一律是 UTF-8 JSON。

### 4.1 Request：client → server

```json
{
  "protocol_version": 1,
  "type": "detect",
  "request_id": 42,
  "client_stamp": 1726540000.123,
  "width": 640,
  "height": 362,
  "known_query_version": 3
}
```

| 欄位 | 型別 | 說明 |
|---|---|---|
| `protocol_version` | int | 目前是 `1` |
| `type` | string | `detect` 或 `ping`（見 4.3） |
| `request_id` | uint32 | client 遞增。server 原封不動帶回 |
| `client_stamp` | float | 這一幀的 ROS 時間戳（秒）。只拿來記錄，server 不要用它做判斷 |
| `width`, `height` | int | 上傳 JPEG 的尺寸 |
| `known_query_version` | int | client 目前的 target 是根據哪一版描述建的。還沒有 target 時填 `-1` |

第二個 frame：JPEG bytes，BGR 彩色影像，已經依 `vlm.upload_max_width` 縮小過。

### 4.2 Response：server → client

```json
{
  "protocol_version": 1,
  "request_id": 42,
  "status": "FOUND",
  "query_version": 3,
  "bbox": [212, 98, 332, 258],
  "score": -1.0,
  "num_candidates": 1,
  "has_mask": false,
  "server_ms": 850,
  "error": ""
}
```

| 欄位 | 型別 | 說明 |
|---|---|---|
| `request_id` | uint32 | 照抄 request 的值 |
| `status` | string | 見下表 |
| `query_version` | int | 這筆結果是用哪一版描述算的。描述每換一次就加 1 |
| `bbox` | `[x1, y1, x2, y2]` int 或 `null` | 左上與右下角點，**上傳影像的座標**，`x2`/`y2` 不含（寬 = `x2 − x1`）。只有 `FOUND` 時有值 |
| `score` | float | 有 mask 時是 SAM2 預測的 IoU（0～1）；沒有 mask 時固定 `-1`（LocateAnything 不提供信心值） |
| `num_candidates` | int | VLM 找到幾個符合描述的物件 |
| `has_mask` | bool | 是否附第二個 frame（mask PNG） |
| `server_ms` | int | server 從收到 request 到送出 response 花了多少毫秒 |
| `error` | string | `ERROR` 時放錯誤訊息，其他狀態是空字串 |

有多個候選物件時只回一個：沒開 mask 時取 VLM 輸出的第一個；開 mask 時取 SAM2 分數最高的。

第二個 frame（只有 `has_mask: true` 時有）：mask PNG
- 單通道 8-bit，大小正好是 `(x2 − x1) × (y2 − y1)`（bbox 範圍，不是整張圖）。
- 物件是 `255`，背景是 `0`。

| `status` | 意義 | client 的處理 |
|---|---|---|
| `FOUND` | 找到目標 | 建立候選 target |
| `NOT_FOUND` | 畫面中沒有目標 | 保持目前狀態，依送圖時機再送 |
| `NO_QUERY` | server 還沒收到上級描述 | 暫停送圖，等一段時間再試 |
| `ERROR` | server 端出錯 | 記錄 `error`，依送圖時機再送 |

### 4.3 Ping：量測網路延遲

`type: "ping"` 只有 header，沒有影像。server 立刻回傳：

```json
{"protocol_version": 1, "request_id": 43, "status": "PONG", "query_version": 3, "bbox": null, "score": -1.0, "num_candidates": 0, "has_mask": false, "server_ms": 0, "error": ""}
```

用途有兩個：一是量 WiFi 的來回延遲，不包含 VLM 推論時間；二是在沒有追蹤需求時，也能提早發現 `query_version` 改變。

server 在推論期間也會立刻回 `PONG`、`NO_QUERY`，以及模型還在載入時的 `ERROR`（`error: "model loading"`），這三種不必排隊等推論。

### 4.4 座標換算（client 端）

```
原始座標 = 上傳座標 × (原始寬度 / 上傳寬度)
```

server 不需要知道相機的原始解析度。

### 4.5 上級描述 API（暫定：HTTP）

上級描述 node 還沒定案，目前先用 HTTP（走 WiFi）。之後若改成 ROS 2，只要把這一層換掉，ZMQ 協定不變。

| 方法 | 路徑 | Body | 回傳 |
|---|---|---|---|
| `POST` | `/api/query` | `{"text": "the red cup"}`（1～300 字） | `{"text", "query_version"}`。**每次 POST 都當成新任務**：版本 +1，「是否找到過」清空；文字和目前相同也一樣（2026-09-19 起） |
| `DELETE` | `/api/query` | — | 清空描述，版本 +1，「是否找到過」清空；之後 detect 回 `NO_QUERY` |
| `GET` | `/api/query` | — | 目前的 `{"text", "query_version"}` |
| `GET` | `/api/target` | — | 目前描述是否找到過，見 4.6 |
| `GET` | `/api/status` | — | 模型狀態、最近 50 筆的 `locate_ms` / `sam_ms` / `server_ms`、被丟棄的舊請求數、`target`（同 4.6） |

```bash
curl -X POST http://192.168.68.51:8080/api/query -H 'Content-Type: application/json' -d '{"text":"the red cup"}'
```

`query_version` 在 server 重啟後會從 0 重新開始。

### 4.6 目標是否找到過（給 BT engine）

BT engine 用 `GET /api/target` 詢問：目前這個描述，VLM 有沒有找到過。

```bash
curl http://192.168.68.51:8080/api/target
```

```json
{"text": "the paper water cup", "query_version": 3, "found": true}
```

| 欄位 | 說明 |
|---|---|
| `text` | 目前的描述；沒有描述時是 `null` |
| `query_version` | 目前描述的版本 |
| `found` | 這個描述設定之後，任何一筆 detect 回過 `FOUND` 就是 `true` |

- **清空時機**：`POST /api/query`（包括送一樣的文字）、`DELETE /api/query`、server 重啟（只存在記憶體）。
- **BT engine 要比對 `query_version`**：`POST /api/query` 會回傳新的版本號；之後讀 `/api/target` 時，版本一樣才代表是在回答自己送的描述。
- **換描述前開始跑的推論不算數**：推論約 1.9 秒，這段時間描述可能被換掉。server 只在「推論開始時的版本 = 目前版本」時才記錄 `FOUND`，所以舊描述晚回來的結果不會讓新描述變成 `found: true`。
- `found` 只代表「找到過」，不代表現在還在畫面裡。
- VLM 會誤抓外觀相似的物件（TODO.md 第 3 節），一次誤抓也會讓 `found` 變成 `true`。

## 5. 時間差處理（client 端）

| 情況 | 處理 |
|---|---|
| 送圖 | 那一幀的灰階、深度，以及 camera→map TF 存進快取（最多 `vlm.cache_size` 幀）。TF 在送圖當下就存，因為 tf2 buffer 預設只保留 10 秒 |
| 等待中 | 繼續用舊 target 追蹤，不阻塞影像 callback（網路收發在背景 thread 處理） |
| 逾時（超過 `vlm.timeout_s`） | 放棄這一筆，允許送下一張。之後如果舊的回應才到，`request_id` 對不上就丟棄 |
| 收到 `FOUND` | 用快取裡那一幀加上 bbox 建新 target：在 bbox 內用深度切出物件範圍（有附 mask 時可改用或對照 mask），ORB 只在該範圍（內縮幾 px）內抽特徵，發布點改用範圍質心 |
| 新 target 驗證 | 先在「現在這一幀」偵測，成功才替換；失敗就保留舊 target，再試 N 幀 |
| `query_version` 改變 | 描述換了，舊 target 立刻作廢，馬上送圖 |

### server 端的注意事項

client 逾時後可能會送出新的 request，server 這時可能還在處理舊的那筆。server 的實作（`vlm_server/zmq_server.py`）：

- 每個 client（ZMQ identity）只保留**最新的一筆**等待中的 detect，推論中收到新的就覆蓋舊的，被覆蓋的不回覆（`/api/status` 的 `dropped_requests` 會 +1）。
- **正在跑的推論不會被中斷**。所以 `timeout_s` 太短時，重送的請求會排在還沒跑完的那筆後面。
- 同時只跑一筆推論。多個 client 同時送圖時輪流處理，`server_ms` 會包含排隊時間。
- `ping`、`NO_QUERY`、模型載入中的 `ERROR` 不經過推論，立刻回覆。

## 6. 送圖時機（client 端）

符合任一條件、而且目前沒有請求在路上，就送最新的一幀：

1. 還沒有 target。
2. 連續 `vlm.lost_frames_trigger` 幀追丟。
3. 追蹤中，距離上次送圖超過 `vlm.refresh_period_s`。
4. 上一筆逾時。
5. `query_version` 改變。

client 的實作（`vlm_bridge_node`）：同時只有一筆在路上；第 3 點的間隔從**上一次送出**開始算。
所以 `refresh_period_s` 小於 rtt（包括 `0.0`）就等於「回來一筆、馬上送最新一幀」，GPU 一直在跑，target 約每 2 秒更新一次。
這樣的新鮮度和每幀都送一樣（結果回來時畫面約 2 秒前），但上傳量只有約 1/60，不需要改成每幀都送。

之後可以再加的優化：在最近幾幀中挑最清楚的一幀送（例如 Laplacian variance 最高的，或相機靜止時的那一幀）。

## 7. Client 參數

依 2026-09-19 聯測數據（第 8.1 節）更新。`LA_MODE=fast`、Pi 相機 848×480 縮成 640×362。

| 參數 | 建議值 | 說明 |
|---|---|---|
| `vlm.enable` | `true` | 關閉時沿用 `target_image_path` 的靜態 target |
| `vlm.endpoint` | `tcp://192.168.68.51:5555` | Hackathon-local-server（2026-09-20 起的新 IP），見第 3 節 |
| `vlm.timeout_s` | `5.0` | 實測 rtt 最大 2.83 s。原則是**大於「最慢推論 + 最慢網路」**，否則逾時重送會排在 server 還沒跑完的那筆後面，接著連續逾時。`slow`／`hybrid` 要改成 `12.0` |
| `vlm.refresh_period_s` | `0.0` | 回來一筆馬上送下一筆（第 6 節）。要省 GPU 時才調大 |
| `vlm.upload_max_width` | `640` | 推論時間大致和像素數成正比（第 8.2 節），改了之後要重新量 |
| `vlm.jpeg_quality` | `80` | 640×362 約 27 KB，網路只佔總延遲約 5% |
| `vlm.ping_period_s` | `2.0` | 只在沒有請求在路上時送；連續送圖時主要在 backoff 期間有用 |
| `vlm.no_query_backoff_s` | `1.0` | `NO_QUERY` 立刻回、不跑推論，重試便宜。client 的 backoff 會擋住 ping 發現的描述變更，太長會讓新描述晚生效 |
| `vlm.error_backoff_s` | `2.0` | 主要是 server 重啟後的 `model loading`（約 3～10 秒） |
| `vlm.lost_frames_trigger` | `10` | 連續追丟幾幀就送圖（client 尚未實作；連續送圖時影響不大） |
| `init.cache_s` | `7.0` | client 以時間快取送出的幀，要大於 `timeout_s`（取代原本的 `vlm.cache_size`） |

## 8. 延遲量測

client 每筆請求記錄以下數值：

```
encode_ms   = JPEG 壓縮時間
rtt_ms      = 收到回應時間 − 送出時間
server_ms   = response 帶回的值
network_ms  = rtt_ms − server_ms
handoff_ms  = 建 target + 在目前這一幀驗證的時間
```

### server 環境

Hackathon-local-server：AMD Ryzen AI 7 350，內顯 **AMD Radeon 860M**（RDNA 3.5，`gfx1152`，與 CPU 共用 30 GiB 記憶體）。
VLM 是 LocateAnything-3B（gguf `q8_0`），locate-anything.cpp 走 **Vulkan**。預設 `LA_MODE=fast`、只回 bbox（不跑 SAM2）。

### 8.1 Pi 5 ↔ server 聯測（2026-09-19，主要依據）

Pi 5 走 WiFi，相機 848×480 縮成 640×362（約 27 KB），`fast`，約 40 分鐘，數字來自 client 的 bridge log：

| 指標 | 樣本 | p50 | p95 | p99 | 最大 |
|---|---:|---:|---:|---:|---:|
| `rtt_ms` | 296 | 1988 | 2054 | 2101 | 2826 |
| `server_ms` | 296 | 1883 | 1891 | 1894 | 1914 |
| `network_ms` | 296 | 103 | 170 | 218 | 944 |

ZMQ `ping`（Pi 容器內，100 次，0.2 秒一次）：p50 10.8 ms、p99 69.4 ms、最大 93.8 ms。

- **推論 1.9 秒是穩定值**：server 端 `locate_ms` 50 筆介於 1879～1888 ms。前提是解析度、場景、單一 client 都不變。
- `network_ms`（p50 103 ms）比 ping（約 10 ms）高很多。推測是 detect 間隔長時 WiFi 省電讓網卡睡著，**還沒驗證**。只佔總延遲約 5%。
- 會讓推論時間改變的情況：上傳像素數（8.2）、大量同類物件（8.3）、多個 client 同時送（排隊，`server_ms` 約變兩倍，`locate_ms` 不變）、server 重啟後第一筆（多約 0.6 秒暖機）。

### 8.2 推論時間與上傳像素數

`fast`、1～2 個目標、單一 client，推論時間大致和像素數成正比：

| 上傳尺寸 | 像素數 | 推論 |
|---|---:|---:|
| 640×362（Pi 相機） | 232k | 1.88 s |
| 598×472（實驗室照片） | 282k | 2.35 s |
| 640×656（1 隻狗） | 420k | 3.41 s |
| 640×669（2 隻狗） | 428k | 3.51 s |

照這個趨勢，512 寬（148k 像素）**可能**降到約 1.2 秒。這是外插，**還沒驗證**，小物件的框也可能變差。

### 8.3 `LA_MODE` 比較（2026-09-18，狗的測試圖）

`scripts/smoke_pipeline.py`，不含網路。每組 4 次，取後 3 次的中位數（括號是最小～最大），描述都是 `the dog`：

| 圖片（640 寬） | 目標數 | `slow` | `hybrid` | `fast` |
|---|---:|---:|---:|---:|
| one-dog | 1 | 6.90 s（5.64–7.21） | 6.90 s（6.58–6.92） | **3.41 s**（3.41–3.41） |
| two-dogs | 2 | 7.30 s（4.97–7.79） | 7.39 s（7.01–7.41） | **3.51 s**（3.50–3.51） |
| many-dogs | 15 | 9.98 s（8.25–10.93） | 4.25 s（4.25–4.25） | **3.30 s**（3.30–3.30） |

品質：

- 1～2 個目標時，三種模式的框幾乎一樣（差幾個 px），`fast` 可用。
- 15 個目標時 `fast` 壞掉：回傳 `[0, 234, 640, 355]`，橫跨整張圖；`slow` 和 `hybrid` 都正確框住其中一隻。
- `hybrid` 在 15 個目標時反而比 1 個目標快，原因還不清楚，樣本只有 3 張圖。
- 實驗室照片（598×472）用 `fast` 測紙杯、水瓶，框都緊貼物件，同一張圖三次輸出完全一樣。

結論：**預設 `fast`**；場上可能出現大量同類物件時改 `hybrid`，`timeout_s` 同時改成 `12.0`。

### 8.4 其他

- SAM2 mask（`VLM_RETURN_MASK=1`，SAM 輸入 1024）：每次多約 0.60–0.63 秒，第一次約 2.3 秒。
- server 會拒絕長邊超過 `VLM_MAX_INPUT_SIDE`（預設 1280）的圖並回 `ERROR`：4000×2700 的圖會讓 Locate 嘗試配置 41 GB 記憶體後 segfault。
- 較早的筆電 ↔ server WiFi 測試（2026-09-18，640×656 圖）：rtt p50 3444 ms，`network_ms` p50 36 ms，最大 307 ms。

## 9. 開發順序建議

1. **mock server**（已完成）：`python -m tools.mock_server --query "cup" --delay 0.8 --drop-rate 0.1 [--mask]`，回傳置中或 `--bbox` 指定的框，可加人工延遲和隨機不回應，模擬 WiFi。`python -m tools.test_client` 可以拿來對照 client 行為。
2. **client 網路 thread + 快取 + 逾時**（已完成），先用 `ping` 驗證連線。
3. **handoff**：用 bbox（＋深度）建 target，驗證後替換（已完成，真 server 上的 handoff 成功率待確認）。
4. 接上真的 server，實測延遲後調整參數（已完成聯測，參數見第 7 節）。

## 10. 相依套件與待確認事項

- client 的 Dockerfile 要加 `libzmq3-dev`。
- server 是 Python，用 `pyzmq`（已加進 requirements.txt）。
- 已約定：port 5555（ZMQ）/ 8080（HTTP 描述）、`query_version` 由 server 在描述改變時遞增、bbox 用 `[x1, y1, x2, y2]`、`LA_MODE=fast`。
- 待處理：
  - server IP 在 router 設 DHCP 保留。
  - server 重啟後 `query_version` 從 0 重算，可能和重啟前撞號（server_progress.md 第 7 節）。
  - 上級描述最終走 HTTP 還是 ROS 2。
  - 縮小 `upload_max_width` 的速度與品質驗證。
