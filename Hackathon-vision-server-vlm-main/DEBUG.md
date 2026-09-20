# DEBUG：VLM server 部署與聯測

> 更新：2026-09-20。**server 換 IP：`192.168.68.51`**（原本是 `192.168.50.125`），ssh alias 改成 `Hackathon-local-server`；下面 2026-09-19 的量測紀錄是在舊 IP 上做的。
> 原更新：2026-09-19。對應協定：[vlm_transport.md](vlm_transport.md)（protocol_version 1）；client 進度：[client_progress.md](client_progress.md)。

## 結論：可以聯測，server 已經在 Hackathon-local-server 上跑

2026-09-19 實際查核的狀態：

| 項目 | 狀態 | 證據 |
|---|---|---|
| server 容器 | 執行中，已連續跑 14 小時以上 | `vlm-server-tracker-1`，`Up 14 hours`，對外開 `5555`、`8080` |
| 程式版本 | 和本機 repo 一致 | `vlm_server/*.py`、`compose.vlm.yaml` 的 md5 兩邊相同（2026-09-19 查核當時；之後 compose 已合併成單一 `compose.yaml`、容器改名 `vlm-server`） |
| 開機自動啟動 | 會 | 容器 `restart=unless-stopped`，`docker.service` 為 `enabled` |
| 模型 | 就緒，`LA_MODE=fast`，只回 bbox | `/api/status`：`model_ready: true` |
| Pi → server 連線 | 通 | Pi（`192.168.50.67`，wlan0）連得到 `tcp 5555`，`GET /api/query` 正常 |
| Pi → server ZMQ ping | p50 **9.0 ms**、p90 12.6 ms、max 111.7 ms（30 次） | 從 Pi 的 `hackathon-vision-client-ws` 容器內量測 |
| Pi 的 bridge | 已接真 server（L3 通過，見第 3 節） | `vlm_bridge.launch.py vlm.endpoint:=tcp://192.168.68.51:5555`，mock 已關閉 |

也就是說，**server 端不需要再部署**。剩下的是把 Pi bridge 的 endpoint 換成真的 server，然後照第 3 節一層一層驗證。

## 1. 聯測前要處理的風險

| 風險 | 影響 | 處理方式 |
|---|---|---|
| **server IP**：2026-09-20 起改成 `192.168.68.51`（原本是 `192.168.50.125`），預計固定 | IP 變了，client 全部逾時 | 確認 router 已經幫 Hackathon-local-server 設好 DHCP 保留。Pi 的 `vlm.endpoint` 要改成新 IP |
| **server 重啟後描述會消失**，`query_version` 從 0 重算 | 重啟後一律回 `NO_QUERY`，直到有人重新設定描述 | 聯測時用 `.env` 設 `VLM_INITIAL_QUERY`（見第 5 節），或重啟後馬上 `POST /api/query` |
| **重啟後版本號可能撞號** | 重啟前是 v1「the dog」，重啟後設「the cup」又是 v1。如果 client 剛好沒看到中間的 v0，會誤以為描述沒變 | client 每 2 秒 ping 一次，只要重啟後等 2 秒以上再設描述，client 就會看到 1→0→1 的變化。根治要改 server（見第 8 節） |
| **GPU 被其他工作占用** | 推論從 3.4 秒變慢，client 開始逾時 | 聯測期間不要在 Hackathon-local-server 上跑 benchmark 或 V3 tracker。之前兩組 benchmark 同時跑時，數字就互相干擾過 |
| **其他服務占用 5555／8080** | server 起不來（port 衝突） | Hackathon-local-server 上原本的 V3 demo（`~/Documents/locate-sam2-d435i-v3`）也用 8080，不要同時啟動；啟動失敗時用 `ss -ltnp` 查是誰占用 |
| **沒有認證** | 同網段任何人都能改描述或送圖 | 內網聯測可以接受；上公網前必須處理（見 [TODO.md](TODO.md) 第 2 節） |

## 2. 部署與更新流程（server 端）

Hackathon-local-server 的 `~/Documents/vlm-server` 是這個 repo 的 git 工作目錄（從 GitHub clone），部署一律走 git，**不要再用 rsync 直接改那邊的檔案**
（`deploy.sh` 發現有被追蹤的檔案被改過就會中止）。

```bash
# 本機
git push

# 部署 origin/main（build → 容器內單元測試 → 重啟 → 等 model_ready → 記錄到 output/DEPLOYED）
ssh Hackathon-local-server '~/Documents/vlm-server/deploy/deploy.sh'

# 回滾到指定 commit
ssh Hackathon-local-server '~/Documents/vlm-server/deploy/deploy.sh <commit>'

# 目前部署的是哪個 commit
ssh Hackathon-local-server 'cat ~/Documents/vlm-server/output/DEPLOYED'
```

build 或測試失敗時 `deploy.sh` 會中止，**不會重啟**，server 繼續跑舊版本。

- 模型放在 `~/Documents/vlm`（2026-09-19 從 `~/models/vlm` 搬來），由 `.env` 的 `LOCATE_MODELS_DIR` 指定；搬移模型後一定要同步改 `.env`，否則容器會找不到模型（`model_error: Missing LocateAnything model`）。它和 V3 資料夾的模型是 hardlink，刪掉 V3 資料夾也不影響。沒有模型時可用 `docker compose run --rm models` 下載。
- `.env`、`output/` 被 gitignore，部署不會動到。可設定的項目見 `.env.example`。
- **重啟後描述會消失**，記得重設（或在 `.env` 設 `VLM_INITIAL_QUERY`）。

## 3. 聯測步驟：由下往上，一層過了才往上

每一層都有「通過條件」。沒過就停下來查，不要跳層，否則出問題時分不出是哪一層。

### L0：server 自己健康

```bash
ssh Hackathon-local-server 'curl -s localhost:8080/api/status'
```

通過條件：`model_ready: true`、`model_error: null`、`model_info.la_mode` 是預期的模式。

這一層**不看 `recent`**。`recent` 只保留最近 50 筆請求，這時候可能是空的，也可能是之前測試留下的舊資料
（看 `client_stamp`，和 `date +%s` 相差很多就是舊的）。Pi 的請求要到 L3 換 endpoint 之後才會出現。
`query.text` 也可能是之前測試留下的描述，L2 之前會重設。

### L1：Pi 到 server 的網路

在 Pi 上用 client 容器裡的 pyzmq 量 ZMQ ping（Pi 主機本身沒裝 pyzmq）。

**整段指令貼到筆電或 Pi 的 shell 執行，不要貼進 Python 互動模式（`>>>`）。**
互動模式裡 `for` 迴圈後面要多按一次空白行的 Enter 才會結束，直接貼上會變成 `SyntaxError`，
迴圈一次都沒跑，最後出現 `no median for empty data`。這不是網路問題。

```bash
ssh Hackathon-pi 'docker exec -i hackathon-vision-client-ws python3 -' <<'EOF'
import json, time, statistics, zmq
s = zmq.Context().socket(zmq.DEALER); s.setsockopt(zmq.LINGER, 0)
s.connect('tcp://192.168.68.51:5555'); time.sleep(0.3)
rtts = []
for i in range(30):
    t = time.monotonic()
    s.send(json.dumps(dict(protocol_version=1, type='ping', request_id=900000 + i)).encode())
    if s.poll(2000):
        r = json.loads(s.recv_multipart()[0]); rtts.append((time.monotonic() - t) * 1000)
    time.sleep(0.2)
rtts.sort()
print(f"n={len(rtts)} p50={statistics.median(rtts):.1f}ms max={rtts[-1]:.1f}ms status={r['status']} qv={r['query_version']}")
EOF
```

通過條件：`n=30`（沒有掉包）、p50 在 20 ms 以內、`status=PONG`。
參考值（2026-09-19，實驗室）：

| 次數 | p50 | p90 | p99 | max | 超過 100 ms |
|---:|---:|---:|---:|---:|---:|
| 30 | 9.0 ms | 12.6 ms | — | 111.7 ms | — |
| 30 | 9.5 ms | — | — | 641.9 ms | 單次尖峰 |
| 100 | 10.8 ms | 23.0 ms | 69.4 ms | 93.8 ms | 0 次 |

偶爾會有單次數百 ms 的尖峰，但遠低於 `timeout_s = 6.0`，不影響。

> 用 `ping`（ICMP）量會高很多：同一時間 ICMP 平均 97 ms、最高 142 ms，但 ZMQ ping 的 p50 只有 9 ms。
> 推測是 Pi 的 WiFi（brcmfmac）省電模式：ICMP 每秒一包，網卡有空檔進入睡眠；上面的腳本每 0.2 秒一包，網卡保持醒著。
> **這只是推測，還沒驗證**（Pi 沒裝 `iw`）。如果 client 的 `network_ms` 常常出現 100 ms 以上的尖峰，
> 裝 `iw` 看 `iw dev wlan0 get power_save`，必要時 `sudo iw dev wlan0 set power_save off`。
> 在 3.4 秒的推論面前，100 ms 不是主要問題。

### L2：server 推論正確（不經過 Pi）

從本機用 test client 送一張圖，確認框出來的東西是對的：

```bash
curl -X POST http://192.168.68.51:8080/api/query -H 'Content-Type: application/json' -d '{"text":"the red cup"}'
.venv/bin/python -m tools.test_client --endpoint tcp://192.168.68.51:5555 \
  --image <現場拍的照片>.jpg --count 3 --timeout 20 --out overlay.jpg
```

通過條件：`FOUND`、`overlay.jpg` 裡的框貼著目標物、`server_ms` 約 2400～3400（`fast`）。
**請用現場的目標物和背景拍一張來測**，不要只用狗的照片；VLM 對描述用詞很敏感。

注意：
- **先 `POST` 描述再送圖**。server 上可能還留著之前測試的描述，直接送圖只會拿到 `NOT_FOUND`。
- **同一時間只讓一個 client 送圖**。server 一次只做一筆推論，兩個 client 同時送會輪流排隊，
  `server_ms` 會包含排隊時間而變成約兩倍。判斷推論本身快不快，要看 `/api/status` 裡的 `locate_ms`。

結果（2026-09-19，`test.png` 598×472，實驗室桌面，`fast`，各 3 次）：

| 描述 | bbox | 判讀 | `locate_ms` |
|---|---|---|---:|
| `the paper cup` | `[128, 316, 246, 446]` | 貼住前景紙杯 | 2351～2360 |
| `the water bottle` | `[279, 199, 344, 383]` | 貼住前景水瓶，沒有選到右後方的果汁瓶 | 2351～2360 |

同一張圖、同一個描述，三次的框完全一樣。現場照片的推論（約 2.35 秒）比狗的測試圖（約 3.41 秒）快。
測試時剛好有兩個 client 同時送圖，`server_ms` 出現約 4.6 秒的值，但 `locate_ms` 一直穩定在 2.35 秒，確認是排隊造成的。

### L3：Pi bridge 接真 server

1. 在 Pi 上停掉 mock server（`mock_vlm_server.py`）和目前的 bridge。
2. 用真的 endpoint 重新啟動 bridge（沿用 client 現在的啟動方式，只換參數）：
   ```bash
   ros2 launch object_tracker vlm_bridge.launch.py vlm.endpoint:=tcp://192.168.68.51:5555
   ```
3. 在 server 端設定描述，並看請求有沒有進來：
   ```bash
   curl -X POST http://192.168.68.51:8080/api/query -H 'Content-Type: application/json' -d '{"text":"<目標描述>"}'
   watch -n 2 "curl -s http://192.168.68.51:8080/api/status | python3 -m json.tool | tail -30"
   ```

通過條件：
- server 的 `recent` 裡出現 Pi 送來的請求（`client_stamp` 是現在的時間），`status` 是 `FOUND`。
- client 每 2 秒的延遲統計中，`server_ms` 約 3400、`network_ms` 在 100 ms 以內、沒有逾時。
- tracker 建出 target，`/tracked_object/point` 指在目標物上。

結果（2026-09-19 09:49～10:30 UTC，實驗室，Pi 5 走 WiFi，`fast`，只回 bbox）：

| 項目 | 數值 |
|---|---|
| detect 結果 | `FOUND` 290、`NO_QUERY` 11、`NOT_FOUND` 4、`TIMEOUT` 1 |
| `rtt_ms`（296 筆有推論的） | p50 1988、p95 2054、p99 2101、max 2826 |
| `server_ms` | p50 1883、p99 1894、max 1914（極穩定） |
| `network_ms` | p50 **103**、p95 170、p99 218、max 944（超過 200 ms 共 6 次） |
| server `dropped_requests` | 0 |
| tracker 畫面 | `TRACKING OK KLT`，134 點全部 inlier，每幀 6.8 ms，距離 0.329 m |

- 相機是 848×480，縮成 640×362 上傳。推論 1.88 秒，比 L2 的 598×472 圖（2.35 秒）快。
- `rtt` 最大 2.8 秒，`timeout_s = 6.0` 餘裕足夠。
- `network_ms` p50 103 ms，比 L1 的 ZMQ ping（約 10 ms）和筆電 WiFi（36 ms）都高。
  可能原因是 detect 每 8 秒才送一次，WiFi 省電讓網卡在空檔睡著；也可能是 Pi 5 的上傳頻寬比較差。**還沒驗證**。
  只佔總延遲約 5%，不急著處理。
- 唯一一次 `TIMEOUT` 發生在 09:57:40，正好是 server 在 09:57:36 被重啟的時候。在路上的那筆請求遺失，client 照設計逾時後繼續。

### L4：失敗情境

照 client 和 mock 測過的情境，在真 server 上重跑一次：

| 情境 | 怎麼做 | 預期 |
|---|---|---|
| 沒有描述 | `curl -X DELETE http://192.168.68.51:8080/api/query` | client 收到 `NO_QUERY`，退避 5 秒再送 |
| 換描述 | `POST /api/query` 換一個文字 | `query_version` +1，client 馬上送圖，新 bbox 回來後替換 target |
| server 重啟 | `ssh Hackathon-local-server 'docker restart vlm-server'` | 模型載入期間 client 收到 `ERROR`（`model loading`）；之後因為描述消失而收到 `NO_QUERY`，**要重設描述** |
| 目標不在畫面 | 把目標物拿走 | `NOT_FOUND`，tracker 繼續追舊 target |
| 斷網 | 暫時關掉 server 的 WiFi，或 `docker stop` | client 逾時（6 秒）、ZMQ 自動重連；恢復後不用重啟 bridge 就能繼續 |

L4 實際觀察（從 bridge log 推斷當時做了這些測試，時間為 UTC）：

| 時間 | client 看到的 | 對應情境 | 判讀 |
|---|---|---|---|
| 09:55:17 起 | `NO_QUERY`（v8） | 描述被清掉 | 符合預期 |
| 09:57:40 | 1 次 `TIMEOUT`，之後 `NO_QUERY`（v0） | server 重啟 | 符合預期：重啟時在路上的請求遺失；重啟後描述消失 |
| 09:59:01 起 | 4 次 `NOT_FOUND`（v1），之後轉為 `FOUND` | 重設描述為 `the paper water cup` | 前幾次沒找到，原因不確定（可能物件當時不在畫面） |

尚未測到的情境：換描述（v+1）時 target 的替換、斷網後自動重連。

### L5：實測數據（給調參數用）

在機器人上實際跑 5～10 分鐘，記錄：

- client 的 `rtt_ms`、`server_ms`、`network_ms` 分布（p50 / p95 / max），用來確認 `vlm.timeout_s = 6.0` 夠不夠。
- `/tracked_object/point` 的頻率，以及 bridge 有跑和沒跑時 Pi 的 CPU 使用率（client_progress.md 說還沒記錄）。
- server `/api/status` 的 `dropped_requests`：應該接近 0。持續增加表示 client 在推論完成前一直重送，也就是 `timeout_s` 太短。

## 4. 症狀 → 可能原因 → 查法

| 症狀 | 可能原因 | 查法 / 處理 |
|---|---|---|
| client 全部逾時，ping 也逾時 | IP 變了、server 沒開、網路不通 | L1；`ssh Hackathon-local-server 'ip -4 addr show wlp98s0; docker ps'` |
| ping 正常，detect 全部逾時 | 推論比 `timeout_s` 慢 | server `/api/status` 的 `server_ms`；檢查 `la_mode` 是不是被改成 `slow`；檢查 GPU 有沒有被占用（見第 5 節） |
| 一直收到 `NO_QUERY` | 沒設描述，或 server 重啟後描述消失 | `curl http://192.168.68.51:8080/api/query`，`text` 是 `null` 就重設 |
| 一直收到 `ERROR: model loading` | 模型還在載入，或載入失敗 | `/api/status` 的 `model_error`；`docker logs --tail 100 vlm-server` |
| `ERROR: image larger than 1280px` | client 沒縮圖 | client 的 `vlm.upload_max_width` 要是 640 |
| `server_ms` 忽高忽低，大約是兩倍，但 `/api/status` 的 `locate_ms` 很穩定 | 有其他 client 同時在送圖，請求在排隊 | 看 `recent` 裡有沒有交錯的 request_id；聯測時關掉筆電上的 `test_client` |
| `server_ms` 突然從 3.4 秒跳到 7～10 秒 | `LA_MODE` 變成 `slow`／`hybrid`，或 GPU 被其他程式占用 | `/api/status` 的 `model_info.la_mode`；`docker ps` 看有沒有其他容器 |
| `network_ms` 偶發 100～300 ms 尖峰 | WiFi 抖動或 Pi 的 WiFi 省電 | 見 L1 的說明；在 3.4 秒面前影響不大 |
| `FOUND` 但框很寬、橫跨整張圖 | `fast` 模式遇到大量同類物件（實測 15 隻狗時會出現） | 換 `LA_MODE=hybrid`（第 5 節）；client `timeout_s` 要一起改成 12.0 |
| `FOUND` 但框到別的東西 | 描述太模糊，或場上有多個符合描述的物件（server 只回第一個） | `/api/status` 看 `num_candidates`；描述寫具體一點（顏色、位置） |
| `FOUND` 框也對，但 tracker 追到背景 | bbox 比物件大很多，client 的深度中位數落在背景 | 開 mask 模式試試（第 5 節）；或改善 client 的深度切割（client_progress.md 第 6 節已列入） |
| 換了描述，client 沒反應 | 重啟後版本號撞號（第 1 節） | 看 client 記錄的 `query_version`；先 `DELETE` 再 `POST`，強迫版本號變兩次 |
| 主機重開機後 server 沒起來 | docker 沒啟動，或容器啟動失敗 | `systemctl status docker`；`docker ps -a`；`docker logs vlm-server` |

## 5. 常用指令（在 Hackathon-local-server 上）

```bash
cd ~/Documents/vlm-server

# 狀態與 log
curl -s localhost:8080/api/status | python3 -m json.tool
docker logs -f --tail 50 vlm-server

# 描述
curl -s localhost:8080/api/query
curl -s localhost:8080/api/target      # 目前描述是否找到過（給 BT engine）
curl -s -X POST localhost:8080/api/query -H 'Content-Type: application/json' -d '{"text":"the red cup"}'
curl -s -X DELETE localhost:8080/api/query

# GPU 是否被占用（0～100）
cat /sys/class/drm/card1/device/gpu_busy_percent
docker ps

# 重啟
docker restart vlm-server
```

**切換模式要寫進 `.env`**，只在指令前面加環境變數的話，下次 `up -d` 會變回預設值：

```bash
# ~/Documents/vlm-server/.env（保留原本的 LOCATE_MODELS_DIR 那一行）
# 預設 fast；場上同類物件多時改 hybrid，client timeout_s 要改 12.0
LA_MODE=hybrid
# 附 SAM2 mask，每次多約 0.6 秒，第一次約 2.3 秒
VLM_RETURN_MASK=1
# 重啟後自動帶入的描述
VLM_INITIAL_QUERY="the red cup"

# 套用（這只會重建容器，不會重 build image）
docker compose up -d server
```

## 6. 回覆 client_progress.md 第 5 節

| client 的問題 | 回覆 |
|---|---|
| 1. 正式 IP | **`192.168.68.51`**（2026-09-20 起，預計固定；原本是 `192.168.50.125`）。要確認 router 已設 DHCP 保留 |
| 2. `LA_MODE` | 預設 `fast`，`timeout_s = 6.0` 正確。只有場上可能出現大量同類物件時才會改 `hybrid`，改的時候會通知，client 要改 `12.0` |
| 3. bbox 的鬆緊 | 單、雙目標實測時 `fast` 的框貼著物件，和 `slow` 只差幾個 px（見 vlm_transport.md 第 8 節）。**樣本只有狗的照片，現場物件還沒測**，L2 用現場照片確認。偏大的話就開 mask |
| 4. 聯測時間 | server 已就緒，隨時可以。照第 3 節 L1 → L5 進行 |
| 5. 上級描述 | 目前走 HTTP：`POST http://192.168.68.51:8080/api/query`（見 vlm_transport.md 第 4.5 節） |

## 7. 評估：選 prompt、門檻、解析度（TODO.md §3～§5）

### 7.1 取圖（Pi 端，由使用者執行）

`tools/grab_frame.py` 是單一檔案，只需要 rclpy、numpy，以及 PIL 或 cv2 其中一個。複製到 Pi 上可以跑 ROS 2 的容器裡執行：

```bash
python3 grab_frame.py distract-1.png          # 預設 topic /camera/camera/color/image_rect_raw
python3 grab_frame.py decoy-2.png --topic /camera/camera/color/image_raw
```

存下來的是原生解析度的 PNG。檔名開頭照場景分類：`single-`、`distract-`、`decoy-`、`empty-`（見 `eval/cases.yaml` 的說明），每種 3～5 張。圖片放到本機的 `eval/images/`。

### 7.2 開發版 image（不動正式容器）

```bash
# 本機：把工作目錄同步到 dev 資料夾（不含 .venv、output）
rsync -a --delete --exclude .venv --exclude output --exclude .git ./ Hackathon-local-server:~/Documents/vlm-server-dev/
# Hackathon-local-server：build 開發版 image（torch 那幾層走快取，只重 build locate stage）
cd ~/Documents/vlm-server-dev && docker build -t vlm-server:dev \
  --build-arg TORCH_INDEX=https://stable.repo.amd.com/rocm/whl-next/ \
  --build-arg 'TORCH_SPEC=torch[device-gfx1152]==2.13.0+rocm10.0.0' \
  --build-arg 'TORCHVISION_SPEC=torchvision[device-gfx1152]==0.28.0+rocm10.0.0' .
```

### 7.3 跑評估（要先停掉正式 server，GPU 才不會被搶）

```bash
cd ~/Documents/vlm-server && docker compose stop server       # Pi 這段時間會收到逾時
cd ~/Documents/vlm-server-dev
EVAL="docker run --rm --device /dev/kfd --device /dev/dri \
  -v $HOME/Documents/vlm:/models:ro -v $PWD/eval:/app/eval -v $PWD/output:/output \
  -e LOCATE_MODEL=/models/locate-anything-q8_0.gguf -e LA_DEVICE=Vulkan0 vlm-server:dev"

# 標註草稿：slow 模式的框 + 編號 overlay，確認後把條目抄進 eval/cases.yaml
$EVAL python -m scripts.eval_locate --propose --query "the plastic bottle" --query "the paper cup" --out /output/eval/propose
# 各輪掃描（round1/2 要先把上一輪最好的設定填進 sweep 檔）
$EVAL python -m scripts.eval_locate --sweep /app/eval/sweeps/round0.yaml --overlay --out /output/eval/round0

cd ~/Documents/vlm-server && docker compose up -d server      # 跑完一定要啟動回來
```

結果放在 `output/eval/<run>/`：
- `results.csv`：每組設定 × 每個 case，包含所有框、`p_object`/`p_coord`/`p_start`、`locate_ms`。
- `summary_p_object.csv`（主要看這個）、`summary_p_coord.csv`、`summary_p_start.csv`：各設定 × 門檻的命中率、誤抓率、`locate_ms` p50。

要改門檻或指標時不用重跑推論，本機就能重算：`python -m scripts.eval_locate --summarize output/eval/round0/results.csv`。

原版（沒有 patch）的 library 沒有分數，三個分數都是 -1，只能看門檻 0 那一列。要比較 patch 前後，就用 `--build-arg LA_PATCH=0` 另外 build 一個 tag。

## 8. server 端已知問題（之後修）

- [ ] **`query_version` 重啟後歸零，可能撞號**：改成從啟動時間戳開始編號，或把描述和版本存到檔案。
- [ ] **描述不會保存**：重啟後遺失，目前靠 `VLM_INITIAL_QUERY` 暫時頂著。
- [ ] **沒有認證**：內網暫時可以接受。
- [ ] **server IP 固定**：要在 router 設定，不是改程式。
