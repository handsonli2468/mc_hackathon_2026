# Client 端進度（給 VLM server 端）

> 更新：2026-09-18。對應協定：[vlm_transport.md](vlm_transport.md)（protocol_version 1）。

## 摘要

- **client 端已照 `vlm_transport.md` 實作完成**：送圖、收 bbox、用深度切出物件、交給 tracker 追蹤。
- **已經和真的 server 接通**：client 跑在 Pi 5 上，server 跑在本地 GPU 主機。rtt 約 1.9 秒，其中網路約 35～68 ms（第 4 節）。
- **tracker 已經在 Pi 5 上編譯、執行**，`/tracked_object/point` 約 26 Hz。
- 下一步是確認 tracker 用 server 的 bbox 建 target 成功，並依實測延遲調整送圖間隔。

## 1. 已完成

| 項目 | 狀態 | 說明 |
|---|---|---|
| ZeroMQ client（`vlm_bridge_node`） | 完成 | DEALER，socket 選項照第 2 節；同時只有一筆請求在路上 |
| Request | 完成 | `detect`：JPEG（640 寬、q80）+ JSON header；`ping`：每 2 秒一次 |
| Response 處理 | 完成 | `FOUND` / `NOT_FOUND` / `NO_QUERY` / `ERROR` / `PONG`；`request_id` 對不上的舊回應直接丟掉 |
| bbox `[x1, y1, x2, y2]` | 完成 | `x2`、`y2` 不含；依第 4.4 節換算回原始解析度 |
| mask PNG（`has_mask: true`） | 完成 | 有附就用；沒附就用整個 bbox 當範圍（預設情況） |
| 在 bbox 內用深度切物件 | 完成 | 在 tracker 端做，見第 3 節 |
| 時間差處理 | 完成 | 送出去的幀存在 tracker 的快取裡，bbox 回來後用同一幀建 target |
| 延遲統計 | 完成 | 每 2 秒輸出 `encode_ms`、`rtt_ms`、`server_ms`、`network_ms` |
| mock server | 完成 | `mock_vlm_server.py`，可模擬延遲、掉包、`NOT_FOUND`、`NO_QUERY`、`model loading` |
| Pi 5 部署 | 進行中 | 已編譯、已執行（見第 4 節）；沒有 GUI，debug 畫面改成 image topic |

client 目前用的參數：

| 參數 | 值 | 說明 |
|---|---|---|
| `vlm.endpoint` | `tcp://192.168.50.125:5555` | 照文件暫定值 |
| `vlm.timeout_s` | `6.0` | 照第 8 節建議（`LA_MODE=fast`）；`slow`／`hybrid` 時要改成 `12.0` |
| `vlm.refresh_period_s` | `8.0` | |
| `vlm.upload_max_width` | `640` | |
| `vlm.jpeg_quality` | `80` | |
| `vlm.ping_period_s` | `2.0` | |
| `vlm.no_query_backoff_s` | `5.0` | 收到 `NO_QUERY` 後多久再送（文件沒有，client 新增） |
| `vlm.error_backoff_s` | `2.0` | 收到 `ERROR` 後多久再送（文件沒有，client 新增） |

## 2. 和 `vlm_transport.md` 不同的地方

**協定本身沒有改**，server 端不需要配合修改。下面是 client 內部的實作差異：

| 文件寫法 | 實際做法 | 原因 |
|---|---|---|
| 網路收發寫在 `orb_tracker_node` 裡 | 獨立的 `vlm_bridge_node`，透過 ROS topic 把 mask 交給 tracker | 網路卡住時不影響追蹤；兩種 tracker 都能接 |
| `vlm.lost_frames_trigger`：連續追丟就送圖 | **還沒做** | bridge 目前看不到 tracker 狀態。追丟後最慢要等 `refresh_period_s`（8 秒）才會再送 |
| `vlm.cache_size`：快取幾幀 | tracker 用時間快取，`init.cache_s = 7.0` 秒 | rtt 約 3.4 秒，快取要大於 `timeout_s` |
| `query_version` 改變時舊 target 立刻作廢 | 立刻送圖，但**舊 target 繼續追**，新的 bbox 回來後才替換 | 目前 bridge 沒有通知 tracker 丟掉 target 的管道 |
| `known_query_version` = target 用哪一版描述建的 | 填 client 最後看到的版本（包括 `PONG` 帶回來的） | 同上，bridge 不知道 tracker 目前用的是哪個 target |
| `NOT_FOUND` | 只記錄，tracker 繼續追舊 target | |

## 3. client 怎麼使用 bbox

server 預設只回 bbox，client 在 tracker 端用深度把物件切出來：

1. 取 bbox 內有效深度的**中位數** d0。
2. 去掉深度和 d0 差太多的像素（背景）。
3. 保留最大的連通區域，當成物件範圍。
4. 在這個範圍內抽特徵，建立新的 target。

**已知限制**：bbox 很鬆、或物件只占 bbox 一小部分時，中位數會落在背景上，切出來的是背景。所以 **bbox 越貼近物件越好**。有 SAM2 mask（`VLM_RETURN_MASK=1`）時會改用 mask，這個問題會比較小，但每次多約 0.6 秒。

## 4. 測試狀態

| 測試 | 環境 | 結果 |
|---|---|---|
| bridge + mock server 端到端 | x86 docker，同一台機器 | 通過：送圖 → `FOUND` → tracker 建 target 成功 |
| 失敗情境 | 同上 | 通過：server 沒開、中途重啟、掉包、`NOT_FOUND`、`NO_QUERY`、`model loading`、`query_version` 改變 |
| 接真的 server | client：Pi 5；server：本地 GPU 主機 | 通過：連續 4 筆都是 `FOUND`，mask 已發布（見下）。tracker 端有沒有 handoff 成功**還沒確認** |
| 機器人上的 WiFi 延遲 | — | **還沒確認**：上面那次的網路路徑沒有記錄 |
| tracker 在 Pi 5 上執行 | Pi 5 8GB，D405 | 見下 |

**Pi 5 → 真 server 的延遲**（2026-09-18，4 筆 `detect`）：

| 指標 | 範圍 |
|---|---:|
| `rtt_ms` | 1915～1952 |
| `server_ms` | 1880～1884 |
| `network_ms`（= rtt − server） | 35～68 |
| `encode_ms`（Pi 5 上 JPEG 壓縮） | 5～10 |
| 上傳 JPEG | 640×362，27 KB |

- rtt 比第 8 節的 3.4 秒短。這次 server 的 `LA_MODE` 和 GPU 型號**沒有記錄**，所以不能直接和第 8 節比較。
- 4 筆的 bbox 都是 `[235, 323, 326, 432]`（換算回原始 848×480 的座標）。量測時畫面應該是靜止的，這一點待確認。
- 網路路徑（WiFi 或有線、是否在機器人上）**沒有記錄**。
- 送圖間隔約 8 秒（`refresh_period_s`），推論約 1.9 秒，所以 GPU 大約 3/4 的時間是閒置的。

**Pi 5 上 `/tracked_object/point` 的輸出頻率**（`ros2 topic hz`，約 190 個樣本）：

| 平均頻率 | 間隔 min | 間隔 max | 標準差 |
|---:|---:|---:|---:|
| 26.2 Hz | 9 ms | 234 ms | 26 ms |

- 這是 klt/mask的版本
- 相機設定 30 fps。
- 這是**輸出頻率**，不是追蹤成功率：暫時追丟時 tracker 會繼續發布最後一個可信的點（HOLDING），也算在裡面。
- 量測時 bridge 有沒有一起跑、CPU 使用率多少，**沒有記錄**。

## 5. 需要 server 端確認或協助

1. **正式 IP**：目前寫死 `192.168.50.125`。上機器人之前請確認會不會變（建議在 router 設 DHCP 保留）。
2. **`LA_MODE`**：client 的 `timeout_s = 6.0` 是照 `fast` 設的。如果場上會用 `slow` 或 `hybrid`，請告訴我們，要改成 `12.0`。
3. **bbox 的鬆緊**：client 用深度在 bbox 內切物件，bbox 越貼近物件越準。如果 VLM 的框習慣偏大，請告訴我們，可能要評估開 mask。
4. **這次聯測的 server 設定**：請補上 `LA_MODE`、GPU 型號，以及推論 1.9 秒是不是穩定值。client 的 `timeout_s`、`refresh_period_s` 要依這個調整。
5. **上級描述走 HTTP 還是 ROS 2**：client 不受影響，但聯測時需要知道怎麼設定描述。

## 6. 接下來（client 端）

- 確認 tracker 用真 server 的 bbox 建 target 成功（看 tracker 的 `Mask cleaned`、handoff log）。
- 依實測 rtt 調整 `refresh_period_s`（例如 8 → 3～4 秒，讓 target 更新得更頻繁）；`timeout_s` 等確認 `LA_MODE` 之後再調。
- 在機器人上量 WiFi 延遲，以及 Pi 5 上的 CPU 使用率。
- 讓 bridge 知道 tracker 狀態，補上「連續追丟就送圖」和「`query_version` 改變時丟掉舊 target」。
- 改善 bbox 內的深度切割（例如取最近的主要深度群，不用中位數）。
