# Hackathon-app：機器人任務控制台

讓使用者用瀏覽器發布任務、看機器人執行狀態、中斷任務、校正相機外參，並在任務結束後回饋。
整包打成一個 Docker image（前端靜態檔 + App Gateway），跑在 local server 上。

```
瀏覽器 (React PWA)
   │ REST / MJPEG（同源，預設 :8000）
   ▼
App Gateway (FastAPI + rclpy)          ← 本 repo
   ├─ /api/chat, /api/missions/*  ─▶ 雲端任務 server Manta（規劃後自動執行，見 APP_API.md）
   ├─ /api/bt/*                   ─▶ bt_engine（mc_main_nav，HTTP，帶 X-BT-Token）
   └─ /api/calib/*                ─▶ field_calib_node（ROS 2 Trigger + 影像 + YAML）
```

金鑰與各服務位址只放在 Gateway 的 `.env`，前端碰不到。

| 頁面 | 內容 |
|---|---|
| 任務 `/` | 和 Manta 對話規劃任務（資訊不足時會追問）。**規劃成功後 Manta 會直接開始執行**，App 用回傳的 `run_id` 追蹤；顯示目前動作與執行紀錄，可中斷任務 |
| 校正 `/calibration` | ① 輸入桌子長寬與張數 → ② 確認整張桌子在相機畫面內，開始校正 → ③ 結果：採用率、重投影誤差、相機外參、各桌緣品質。流程狀態會保存，切頁或重整都不會歸零 |
| 回饋 `/feedback/:runId` | 任務結束後自動進入。失敗時列出原因，可改指令在原對話重新規劃；滿意度（必填）、夾取力道、文字意見送到 Manta |

---

## 1. 需要什麼

| 用途 | 需求 | 本專案驗證過的版本 |
|---|---|---|
| 部署（只需要這個） | Docker + Docker Compose v2 | 29.4.3 |
| 前端開發 | Node.js ≥ 20.19 | 24.14.0 |
| Gateway 開發 | Python ≥ 3.10 | 3.12.3（image 內為 3.10.12） |
| 校正功能 | 同一台主機上有 ROS 2 Humble 的 `field_calib_node` 與定位 repo | — |

**校正功能的前提**：Gateway 必須和 `field_calib_node` 在**同一台主機**、**同一個 `ROS_DOMAIN_ID`**，並掛載定位 repo 的資料夾（兩邊用檔案交換桌子設定與校正結果）。不符合時 Gateway 仍會啟動，任務與回饋照常可用，校正頁會顯示原因。

## 2. 部署（local server）

```bash
git clone <repo> Hackathon-app && cd Hackathon-app

cp .env.example .env         # 填 BT_ENGINE_TOKEN，確認 CLOUD_URL 與 ROS_DOMAIN_ID
mkdir -p data                # 給 compose 掛載：runs.json、對話與回饋紀錄
docker compose up -d --build # 改過程式碼一定要加 --build，否則會沿用舊 image

curl -s localhost:8000/api/health
# {"ok":true,"cloud_mode":"http",...}
```

瀏覽器開 `http://<主機 IP>:8000/`。要換埠號就在 `.env` 設 `APP_PORT`。

定位 repo 不在預設的 `../Hackathon-vision-server-localization` 時，在 `.env` 設 `VISION_WS=<路徑>`（相對於 repo 根目錄，或絕對路徑）。**這個路徑或 `data/` 不存在時，`docker compose up` 會直接報 `bind source path does not exist`**——這是刻意的，避免 Docker 默默建出空的 root 資料夾。

`field_calib_node` 啟動時要指定 App 寫的桌子設定檔，建議同時開啟精簡疊圖（10 Hz）：

```bash
ros2 launch field_calib field_calib.launch.py \
  field_file:=/home/vision/vision_ws/tools/calib/field_app.yaml \
  live.compact:=true live.period:=0.1
```

Gateway 第一次啟動時，如果這個檔案還不存在，會從 `src/field_calib/config/field.yaml` 複製一份。

### 部署後檢查

```bash
curl -s localhost:8000/api/cloud/health   # Manta 是否正常
curl -s localhost:8000/api/bt/health      # bt_engine 是否正常（同時驗證 token）
curl -s localhost:8000/api/calib/state    # mode/connected/frame_counts/sources
docker logs -f hackathon-app-gateway
```

`/api/calib/state` 的 `frame_counts` 應該持續增加，`sources.live` 在 field_calib 開了 `live.compact` 時是 `compressed`。

## 3. 本機開發（不需要機器人或 ROS）

三個終端機：

```bash
# ① 假的 bt_engine：同一套 HTTP 介面，約 30% 隨機失敗
FAKE_PORT=8091 python3 tools/fake_bt_engine.py

# ② Gateway（沒有 rclpy 時自動切到校正 mock 模式）
cd gateway
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
BT_ENGINE_URL=http://localhost:8091 CLOUD_MODE=mock MOCK_EXECUTE=true CALIB_MODE=mock \
DATA_DIR=./.devdata FIELD_FILE=./.devdata/field_app.yaml CALIB_OUTPUT_DIR=./.devdata/calib \
  .venv/bin/uvicorn app.main:app --port 8000 --reload

# ③ 前端（Vite dev server，/api 轉到 :8000）
cd web && npm install && npm run dev
```

- `CLOUD_MODE=mock`：不連 Manta。對話與回饋寫進 `DATA_DIR/*.jsonl`；訊息少於 4 個字會模擬「需要追問」。
- `MOCK_EXECUTE=true`：mock 規劃成功後，會把 demo 行為樹送到 `BT_ENGINE_URL`。指到假引擎才安全，**指到真的 bt_engine 會讓機器人動**。
- `CALIB_MODE=mock`：重播 `CALIB_OUTPUT_DIR` 中最新一次的校正結果，沒有即時影像。
- 前端連別台的 Gateway：`GATEWAY_URL=http://<ip>:8000 npm run dev`。

## 4. 測試

```bash
cd gateway && .venv/bin/pytest -q    # 32 passed
cd web && npm run build              # 型別檢查 + 打包
```

測試涵蓋：bt_engine 代理與錯誤轉送、Manta 對話與自動執行（用真實回應當 fixture）、夾爪值換算、run ↔ mission 對應、壓縮／未壓縮影像的退回邏輯、桌子設定 YAML 讀寫、校正結果解析。全部不需要網路、ROS 或機器人。

## 5. 設定（`.env`）

| 變數 | 預設 | 說明 |
|---|---|---|
| `BT_ENGINE_URL` | `http://localhost:8090` | bt_engine 位址。和 Gateway 同主機時用 localhost，換 IP 也不受影響 |
| `BT_ENGINE_TOKEN` | 空 | bt_engine 的 `X-BT-Token`，只存在這裡 |
| `CLOUD_MODE` | `http` | `http` 接 Manta；`mock` 用離線替身 |
| `CLOUD_URL` / `CLOUD_TOKEN` | Manta 位址／空 | 目前 Manta 不需要 token |
| `CLOUD_PIPELINE_MODE` | `hybrid` | 送給 `/api/chat`；`direct`、`compare` 是開發模式 |
| `CLOUD_CHAT_TIMEOUT_S` | `240` | 規劃通常 30–60 秒 |
| `MOCK_EXECUTE` | `false` | 僅 mock 模式；**會讓真的機器人動** |
| `ROS_DOMAIN_ID` | `59` | **必須和定位 server 的容器一致**，否則收不到任何影像 |
| `VISION_WS` | `../Hackathon-vision-server-localization` | 定位 repo 路徑，compose 掛成 `/vision_ws` |
| `APP_PORT` | `8000` | Gateway 對外埠 |
| `CALIB_MODE` | `auto` | `auto`（有 rclpy 就用 ros）／`ros`／`mock`；compose 固定為 `ros` |
| `CALIB_CAMERA_TOPIC` | `/camera/camera/color/image_raw/compressed` | 校正頁「相機畫面」的來源 |
| `CAMERA_STREAM_FPS` / `CAMERA_STREAM_MAX_WIDTH` | `15` / `960` | 相機畫面輸出的幀率與寬度 |

`FIELD_FILE`、`FIELD_TEMPLATE`、`CALIB_OUTPUT_DIR`、`DATA_DIR` 由 compose 指定，只有本機開發才需要自己設。

## 6. Gateway API

前端只打這些，不直接連 Manta 或 bt_engine。

| Method | Path | 說明 |
|---|---|---|
| POST | `/api/chat` | `{message, session_id?}` → Manta。回傳精簡後的 `{session_id, status, message, questions, mission_id, bt_generated, execution: {status: STARTED\|FAILED\|SKIPPED, run_id, preempted_previous, reason, error}, goal, steps, gripper_position, bt_xml}`；`STARTED` 時記下 run ↔ mission |
| POST | `/api/sessions/reset` | 開新對話 |
| POST | `/api/missions/{id}/cancel` | 走 Manta；Manta 連不上時改直接叫 bt_engine `/cancel` |
| POST | `/api/missions/{id}/feedback` | `{rating 1-5（必填）, comment, grip_force: too_weak\|ok\|too_strong}`；夾爪值由 Gateway 依行為樹換算後送出 |
| GET | `/api/runs/{run_id}/mission` | 這個 run 對應的 mission（只有從 App 發出的才有） |
| GET | `/api/bt/status[/{run_id}]`、`/api/bt/runs`、`/api/bt/health` | 原樣轉送 bt_engine（`?trace=full` 取完整紀錄） |
| POST | `/api/bt/cancel` | 直接中斷目前的行為樹 |
| GET/PUT | `/api/calib/field` | 桌子設定 `{length, depth, count, disabled_segments}`（公尺） |
| POST | `/api/calib/run` | 觸發校正（阻塞到結束），回傳 `{success, message, run, result}` |
| GET | `/api/calib/result`、`/api/calib/runs/latest` | 目前生效的外參／最新一次結果 |
| GET | `/api/calib/state` | 模式、連線狀態、`frame_counts`、`sources` |
| GET | `/api/calib/stream/{live\|calib\|camera}` | MJPEG 串流 |
| GET | `/api/calib/snapshot/{live\|calib\|camera}` | 單張 JPEG |
| GET | `/api/calib/image/{final_overlay\|final_strips\|final_residuals}?run=` | 校正輸出的 PNG |
| GET | `/api/health`、`/api/cloud/health` | Gateway 自身／Manta |

互動式文件：`http://<主機>:8000/docs`。

## 7. 專案結構

```
compose.yaml            部署入口（放在根目錄，.env 才會同時用於變數展開與容器）
docker/Dockerfile       multi-stage：node 打包前端 → ros:humble + FastAPI
docker/cyclonedds.xml   Gateway 專用 DDS 設定（放大接收緩衝區）
gateway/app/
  main.py               FastAPI 路由、SPA 靜態檔、lifespan
  config.py             設定（pydantic-settings，讀 .env）
  bt.py                 bt_engine HTTP 代理
  cloud.py              Manta client + 離線 mock、夾爪值換算、run↔mission 索引
  calib.py              rclpy 節點、影像轉 MJPEG、桌子設定與校正結果 YAML
gateway/tests/          pytest（fixtures 取自 Manta 與 field_calib 的真實輸出）
web/src/
  api/                  fetch 封裝與型別
  pages/                TaskPage、CalibrationPage、FeedbackPage
  components/、lib/     對話、串流、進度條、校正流程狀態
tools/fake_bt_engine.py 離線開發用的假 bt_engine
APP_API.md              雲端任務 server（Manta）的介面文件
```

## 8. 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| `docker compose up` 報 `bind source path does not exist` | `data/` 或 `VISION_WS` 路徑不存在。`mkdir -p data`，或在 `.env` 修正 `VISION_WS` |
| 8000 埠開不起來，容器一直重啟 | 看 `docker logs hackathon-app-gateway`。多半是 `VISION_WS` 指到不能寫的路徑 |
| 改了程式卻沒生效 | `docker compose up -d` 沒加 `--build`，沿用了舊 image |
| 校正頁沒有任何畫面 | `ROS_DOMAIN_ID` 和定位容器不一致，或 field_calib 沒啟動。用 `/api/calib/state` 的 `connected`、`frame_counts` 確認 |
| 疊圖每秒只有 1 張 | field_calib 沒帶 `live.compact:=true live.period:=0.1`。Gateway 此時會退回未壓縮影像 |
| `/api/bt/*` 回 502 | bt_engine 位址不對或沒啟動；同主機請用 `http://localhost:8090` |
| `/api/bt/*` 回 401 | `BT_ENGINE_TOKEN` 沒填或不對 |
| 回饋回 409 | 任務還沒結束，Manta 不收回饋 |

## 9. 已知限制

- **送出指令就會執行**：`/api/chat` 規劃成功後 Manta 直接啟動行為樹，中間沒有確認步驟。已有任務在跑時，App 會先詢問再送，因為新任務會中斷舊的。
- **`/api/chat` 不可重送**：逾時或斷線時 App 不會自動重試，只提示先查看任務狀態。每次產生行為樹的 `auto_execution` 原始內容記在 `data/executions.jsonl`。
- **`/api/bt/cancel` 是全域的**：bt_engine 一次只跑一棵樹，中斷會停掉當下那棵，包含別的 client 送的。
- **沒有真正的桌面遮罩**：校正畫面用 field_calib 的桌緣疊圖代替；要真的 mask 需要定位 repo 新增 topic。
- **對話紀錄存在瀏覽器**（localStorage），換裝置看不到先前對話，但 Manta 端的 session 仍在。
- **DDS 接收緩衝區**：未壓縮疊圖一張約 4.2 MB，預設 2 MB 緩衝區會讓每張都掉片段，因此 Gateway 使用 `docker/cyclonedds.xml` 把緩衝區請求調大（實際上限為主機的 `net.core.rmem_max`）。
