# Hackathon Vision Server（VLM）

機器人視覺追蹤的「找目標」服務：client（Pi 5 上的 `vlm_bridge_node`）用 ZeroMQ 傳 JPEG 上來，
server 依上級給的描述（例如 `the white paper cup`）用 **LocateAnything-3B** 找出目標，回傳 bbox（可選 SAM2 mask），
client 再用深度在 bbox 內切出物件並追蹤。

| 文件 | 內容 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 架構流程圖：資料流、執行緒、推論內部、狀態機、部署與評估 |
| [vlm_transport.md](vlm_transport.md) | ZMQ／HTTP 協定規格 |
| [DEBUG.md](DEBUG.md) | 部署、聯測步驟（L0～L5）、症狀對照表、評估操作 |
| [TODO.md](TODO.md) | 待辦、評估數據、設計決策的理由 |
| [server_progress.md](server_progress.md) / [client_progress.md](client_progress.md) | 兩端的進度回報 |

---

## 1. 需要什麼

| 項目 | 需求 | 備註 |
|---|---|---|
| 作業系統 | Linux + Docker（含 compose v2） | 實際跑在 Ubuntu |
| GPU | AMD GPU，需要 `/dev/kfd` 和 `/dev/dri` | 實測 Radeon 860M（gfx1152）。**Locate 走 Vulkan**，SAM2 mask 才需要 ROCm |
| 記憶體 | 16 GB 以上 | 模型權重 5.83 GiB |
| 硬碟 | 約 25 GB | image 9.4 GB + 模型 6.3 GB + build 快取 |
| 模型 | `locate-anything-q8_0.gguf`、`sam2.1_hiera_tiny.pt` | 不在 repo 裡，用下面的指令下載 |

**換不同的 AMD GPU 時**，要改 `compose.yaml` 裡三個 build 參數（`TORCH_SPEC`、`TORCHVISION_SPEC`、`TORCH_INDEX`）和 `EXPECTED_GPU_ARCH`，因為 ROCm 的 wheel 是綁架構的。只用 bbox（不開 mask）的話，torch 根本不會被載入，裝錯也不影響。

沒有 GPU 也能做大部分開發：見 [§6 本機開發](#6-本機開發)。

## 2. 從零跑起來

```bash
# 1. 取得程式碼
git clone <repo> ~/Documents/vlm-server && cd ~/Documents/vlm-server

# 2. 設定模型位置（.env 不進 git，部署不會動到）
cp .env.example .env
$EDITOR .env          # 至少要設 LOCATE_MODELS_DIR，例如 /home/wildbot/Documents/vlm

# 3. build image（第一次約 20～40 分鐘：編 locate-anything.cpp + 裝 ROCm torch）
docker compose build server

# 4. 下載模型到 LOCATE_MODELS_DIR（約 6.3 GB；已存在的檔案不會重下）
docker compose run --rm models

# 5. 啟動
docker compose up -d server

# 6. 等模型就緒（約 3～5 秒），確認
curl -s localhost:8080/api/status | python3 -m json.tool
```

`model_ready: true`、`model_error: null` 就成功了。失敗時先看 `docker logs vlm-server`，常見原因見 [§8 常見問題](#8-常見問題)。

### 送第一張圖

```bash
# 設定要找什麼（重啟後會消失）
curl -X POST localhost:8080/api/query -H 'Content-Type: application/json' -d '{"text":"the paper cup"}'

# 不經過 ZMQ，直接在容器內用真模型跑單張圖，會輸出框好的 overlay
# image 裡沒有 eval/，所以先把圖放到 output/（它掛成容器內的 /output）
cp eval/images/single-1.png output/
docker compose exec server python -m scripts.smoke_pipeline /output/single-1.png \
  --target "the paper cup" --repeat 3 --out /output/smoke.png
```

預期輸出（Radeon 860M、`fast`）：`FOUND`、`candidates: 1`，overlay 上的框貼著紙杯（2026-09-20 實測 `bbox: [344, 322, 447, 447]`、`score: 0.998`）。
`output/` 在主機上就是 repo 裡的 `./output`，直接開 `output/smoke.png` 就能看。

**`locate_ms` 約 3700～4300，不是 1900。** `smoke_pipeline` 不會縮圖，這張是相機原生的 848×480（407k 像素），
而 client 上傳的是 640×362（232k 像素）。推論時間大致和像素數成正比，所以差了約 1.75 倍。
要對照 §10 的數字，先把圖縮成 640 寬再跑。

### 從別台機器測

```bash
.venv/bin/python -m tools.test_client --endpoint tcp://192.168.68.51:5555 --ping 5
.venv/bin/python -m tools.test_client --endpoint tcp://192.168.68.51:5555 \
  --image eval/images/distract-1.png --count 3 --out overlay.jpg
```

## 3. 日常部署

正式機 `Hackathon-local-server`（`wildbot@192.168.68.51`）上的 `~/Documents/vlm-server` 是這個 repo 的 git 工作目錄。**部署一律走 git，不要直接改那邊的檔案**（`deploy.sh` 偵測到被追蹤的檔案有改動就會中止）。

```bash
git push                                                    # 本機
ssh Hackathon-local-server '~/Documents/vlm-server/deploy/deploy.sh'
```

`deploy.sh` 依序做：checkout `origin/main` → build image → **在新 image 裡跑單元測試** → 重啟 → 等 `model_ready` → 把 commit 寫進 `output/DEPLOYED`。
build 或測試失敗就中止，不重啟，server 繼續跑舊版本。

```bash
ssh Hackathon-local-server '~/Documents/vlm-server/deploy/deploy.sh <commit>'   # 回滾
ssh Hackathon-local-server 'cat ~/Documents/vlm-server/output/DEPLOYED'         # 目前的版本
```

**重啟後描述會消失**，要重設（或在 `.env` 設 `VLM_INITIAL_QUERY`）。

## 4. 設定

改 `.env` 之後 `docker compose up -d server`（只重建容器，不重新 build）。完整說明見 `.env.example`。

| 變數 | 預設 | 作用 |
|---|---|---|
| `LOCATE_MODELS_DIR` | 必填 | 模型資料夾，掛成容器內的 `/models` |
| `LA_MODE` | `fast` | `fast`／`hybrid`／`slow`。同類物件很多時用 `hybrid`，client 的 `timeout_s` 要改 12.0 |
| `LA_PROMPT_TEMPLATE` | `detect` | `detect`／`ground_multi`／`ground_single`／`region`，或含 `{q}` 的自訂字串 |
| `LA_SYSTEM_PROMPT` | 未設 | 覆寫 system prompt（需要 patch 版 library） |
| `VLM_MIN_SCORE` | `0` | 低於這個信心的框全部丟掉，回 `NOT_FOUND`。`0` = 不過濾 |
| `VLM_SCORE_FIELD` | `p_object` | 用哪個分數，見 [TODO.md](TODO.md) §3 |
| `VLM_RETURN_MASK` | `0` | `1` = 另外跑 SAM2 附上 mask PNG（每次多約 0.66 秒） |
| `VLM_INITIAL_QUERY` | 未設 | 啟動時自動帶入的描述，避免重啟後處於 `NO_QUERY` |

**嚴格比對的設定**（誤抓 15.4% → 1.9%，命中 100% → 96.9%，實測見 [TODO.md](TODO.md) §3）。
2026-09-20 起已在正式機的 `.env` 啟用，但**驗證集還沒跑**，所以這組數字只在挑設定用的那 84 題上成立：

```bash
LA_PROMPT_TEMPLATE="Locate the region that matches the following description: {q}, exactly as described."
VLM_MIN_SCORE=0.9
VLM_SCORE_FIELD=p_object
```

## 5. 上游與下游怎麼接

**上游（BT engine）走 HTTP 8080**：

```bash
curl -X POST localhost:8080/api/query -H 'Content-Type: application/json' -d '{"text":"the paper cup"}'
curl -s localhost:8080/api/target    # {"text": "...", "query_version": 3, "found": true}
curl -X DELETE localhost:8080/api/query
```

每次 `POST` 都算新任務：`query_version` 加一，「找到過」的狀態清空，**即使送的是同一句描述**。

**下游（Pi bridge）走 ZMQ 5555**：DEALER 連上去，送 JSON header + JPEG，收 bbox。
欄位定義、狀態碼、逾時與重送規則見 [vlm_transport.md](vlm_transport.md)；client 參數建議見 [server_progress.md](server_progress.md) §5。

## 6. 本機開發

不需要 GPU，也不需要模型：

```bash
python3 -m venv .venv                       # Python 3.12
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest tests.test_vlm_server     # 26 項

# 假 server：協定、延遲行為都和真的一樣，只是回固定的框
.venv/bin/python -m tools.mock_server --query "the cup" --delay 1.9
.venv/bin/python -m tools.test_client --endpoint tcp://127.0.0.1:5555 --ping 5 \
  --image eval/images/lab-desk-cup-bottle.png --out overlay.jpg
```

## 7. 重現評估結果

[TODO.md](TODO.md) §3 和 §5 的數字都可以重跑。評估集（21 張圖、84 題）和標註都在 repo 裡。

```bash
# 1. 把工作目錄同步到 dev 資料夾（不動正式容器）
rsync -a --delete --exclude .venv --exclude output --exclude .git --exclude __pycache__ \
  ./ Hackathon-local-server:~/Documents/vlm-server-dev/

# 2. build 開發版 image（build 參數要和 compose.yaml 一致）
ssh Hackathon-local-server 'cd ~/Documents/vlm-server-dev && docker build -t vlm-server:dev \
  --build-arg TORCH_INDEX=https://stable.repo.amd.com/rocm/whl-next/ \
  --build-arg "TORCH_SPEC=torch[device-gfx1152]==2.13.0+rocm10.0.0" \
  --build-arg "TORCHVISION_SPEC=torchvision[device-gfx1152]==0.28.0+rocm10.0.0" .'

# 3. 跑掃描（8 種設定 × 84 題約 25 分鐘）
ssh Hackathon-local-server 'cd ~/Documents/vlm-server-dev && docker run --rm \
  --device /dev/kfd --device /dev/dri \
  -v $HOME/Documents/vlm:/models:ro -v $PWD/eval:/app/eval:ro -v $PWD/output:/output \
  -e LOCATE_MODEL=/models/locate-anything-q8_0.gguf -e LA_DEVICE=Vulkan0 \
  vlm-server:dev python -m scripts.eval_locate --sweep /app/eval/sweeps/round0.yaml --overlay --out /output/eval/round0'
```

結果在 `output/eval/round0/`：`results.csv`（每組設定 × 每題的框、分數、耗時）、`summary_p_object.csv`（各門檻的命中率與誤抓率）、`overlay/`。
改門檻或指標不用重跑推論：`python -m scripts.eval_locate --summarize output/eval/round0/results.csv`。

推論是確定性的，同樣的 image 和設定會得到一模一樣的框，數字可以直接對照 [TODO.md](TODO.md)。

要建立自己的評估集：`tools/grab_frame.py`（在機器人上抓圖）→ `--propose` 產生標註草稿 → 看 overlay 修正 → 寫進 `cases.yaml`。詳細流程見 [DEBUG.md](DEBUG.md) §7。

### locate-anything.cpp 的 patch

`deploy/patches/locate-anything-v0.1.0.patch` 在 build 時套用到上游的 v0.1.0，加了兩件事：可覆寫 system prompt、每個框回傳信心值（上游都沒有暴露）。
`docker build --build-arg LA_PATCH=0` 可以 build 出沒有 patch 的原版做對照；實測在不設任何新環境變數時，兩者的框完全一樣、耗時差在 ±1% 以內。

## 8. 常見問題

| 症狀 | 原因 | 處理 |
|---|---|---|
| `model_error: Missing LocateAnything model` | `.env` 的 `LOCATE_MODELS_DIR` 指錯，或模型被搬走 | 改 `.env` → `docker compose up -d server` |
| 一直回 `NO_QUERY` | 沒設描述，或 server 重啟後描述消失 | 重新 `POST /api/query`，或設 `VLM_INITIAL_QUERY` |
| 所有 detect 都回 `ERROR: model loading` | 模型還在載入，或載入失敗 | `docker logs vlm-server`；`model_error` 不是 null 就是載入失敗 |
| `server_ms` 變成兩倍 | 多個 client 同時送圖在排隊 | 看 `/api/status` 的 `locate_ms`，那才是推論本身的時間 |
| 大圖被拒絕 | 長邊超過 1280 px（原生層曾因此崩潰） | client 端先縮圖，預設上傳 640 寬 |
| GPU 被其他工作占用，推論變慢 | 同一台機器上有其他容器 | `cat /sys/class/drm/card1/device/gpu_busy_percent`、`docker ps` |

更完整的對照表和聯測步驟見 [DEBUG.md](DEBUG.md)。

## 9. 目錄

| 路徑 | 內容 |
|---|---|
| `vlm_server/` | server 本體：ZMQ ROUTER（5555）、HTTP API（8080）、推論 pipeline、LocateAnything 的 ctypes 包裝 |
| `scripts/` | `smoke_pipeline.py`（單張圖跑真模型）、`eval_locate.py`（評估）、`download_models.py` |
| `tools/` | `mock_server.py`（不用模型的假 server）、`test_client.py`（量延遲、畫框）、`grab_frame.py`（機器人端抓圖） |
| `tests/` | 單元測試，不需要 GPU 與模型 |
| `eval/` | `images/` 評估圖、`cases.yaml` 標註、`sweeps/` 各輪設定、`holdout/` 驗證集 |
| `deploy/` | `deploy.sh`、`patches/`（locate-anything.cpp 的 patch） |
| `compose.yaml` | service `server`（容器 `vlm-server`）與 `models`（下載模型） |
| `requirements*.txt` | image 的相依套件／本機開發多裝的套件 |

## 10. 實測數據（Radeon 860M、`LA_MODE=fast`、640 寬上傳）

| 項目 | 數值 |
|---|---|
| 推論時間 | p50 約 1.95 秒（`detect` 模板）／2.11 秒（`region`+`exactly`）；很穩定 |
| 端到端 RTT（Pi 走 WiFi） | p50 1988 ms、p95 2054 ms |
| SAM2 mask | 每次多約 0.66 秒 |
| 模型載入 | 約 2～3 秒 |
| 推論時間與解析度 | 大致和像素數成正比：448 寬快 35%，但誤抓變成 3～4 倍，所以維持 640 寬 |

完整的量測條件與評估方法見 [vlm_transport.md](vlm_transport.md) §8 和 [TODO.md](TODO.md) §3、§5。
