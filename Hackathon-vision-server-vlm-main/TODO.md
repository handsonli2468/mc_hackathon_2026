# TODO

1. [Client 交接策略：現有作法 vs 預追蹤](#1-client-交接策略現有作法-vs-預追蹤)（現有作法已上線，B 待數據決定）
2. [把 VLM 搬到雲端 AMD MI300X](#2-把-vlm-搬到雲端-amd-mi300x)（2026-09-19 重啟，和第 5 節並行）
3. [嚴格匹配、不硬找](#3-嚴格匹配不硬找)（已部署並在 `.env` 啟用；驗證集待跑）
4. [現場評估集與評估腳本](#4-現場評估集與評估腳本)（完成：84 題）
5. [解析度掃描](#5-解析度掃描)（完成：維持 640 寬）
6. [V3 舊程式整理](#6-v3-舊程式整理)（完成，2026-09-19）
7. [其他注意事項](#7-其他注意事項)

## 1. Client 交接策略：現有作法 vs 預追蹤

> 狀態：**現有作法已由 client 實作，並和真 server 聯測通過（2026-09-19）**。方案 B 待數據決定。
> 完整分析（含已丟棄的方案 A、比較表、判斷規則）已整理到 [server_progress.md](server_progress.md) 第 6 節，這裡只追蹤進度。

### 摘要

- 問題：bbox 回來時描述的是約 2 秒前（約 60 幀前）的畫面（聯測 rtt p50 1988 ms）。
- **現有作法**：client 快取送出的幀，bbox 回來後用同一幀建 target，在現在這一幀驗證後替換。
- **方案 B（預追蹤）**：送圖時撒 KLT 點、等待期間逐幀追，bbox 回來時直接取已在現在這一幀的點。
  最能應付位移，但等待期間每幀都要跑 KLT；client 改成連續送圖（`refresh_period_s = 0`）後等於一直在跑。
- **方案 A（server 抽特徵往下傳）**：已丟棄。
- **決策**：先用現有作法量三項數據（交接驗證失敗率、延遲期間位移、Pi 5 CPU 餘裕），依 server_progress.md 第 6 節的規則決定要不要加 B。

### 目前線索

- tracker debug 畫面：`TRACKING OK KLT`，每幀 6.8 ms、追 134 點，離 33 ms（30 fps）的預算還有空間。
  但 B 要在整張畫面追更多點，bridge 和相機驅動也在用 CPU，**還不能推論 B 可行**。

### 待辦

- [x] 實作現有作法（client：網路 thread + 快取 + 逾時 + 交接驗證）
- [x] 和真 server 聯測（DEBUG.md L0～L3 通過）
- [ ] client 參數改成 server_progress.md 第 5 節的建議（`refresh_period_s = 0.0` 等）
- [ ] 確認 tracker 用真 server 的 bbox 建 target 成功（handoff log）
- [ ] client 加上三項量測的 log
- [ ] 在 Pi 5 上量 `calcOpticalFlowPyrLK`（200／500 點）的每幀耗時
- [ ] 依數據決定是否加 B

## 2. 把 VLM 搬到雲端 AMD MI300X

> 狀態：**2026-09-19 重啟，和第 5 節（解析度掃描）並行**。先做下方「執行順序」的步驟 1、2，需要雲端主機的 SSH 連線資訊。
> （2026-09-18 曾暫緩。）這一節記錄已經查到的事實和要做的順序。
>
> 補充（2026-09-19）：推論時間主要花在視覺部分（見第 5 節），屬於運算密集。MI300X 的運算力遠高於 860M，
> 前提仍是 backend 能在 MI300X 上跑（Vulkan 或 HIP，見下方「運算面」）。

### 為什麼考慮雲端

本地 server 在 Radeon 860M 上，`LA_MODE=fast`、Pi 相機畫面（640×362）的推論時間約 **1.9 秒**，
端到端 rtt 約 2.0 秒（2026-09-19 聯測，見 [vlm_transport.md](vlm_transport.md) §8）。
client 連續送圖時，target 約每 2 秒更新一次；推論時間大致和上傳像素數成正比。

MI300X 有加速的物理基礎：LocateAnything-3B 的解碼階段是**記憶體頻寬受限**，
860M 共用 LPDDR5X 約 120 GB/s，MI300X 是 HBM3 5.3 TB/s，理論差約 40 倍。
但 ggml 在 CDNA3 上的 kernel 成熟度達不到理論值，**實際加速倍率沒有量過，不要當成已知**。

### 前提條件（已確認）

- 雲端 server 可以自己裝 docker、自行編譯，價格不是考量。
- client 是 Raspberry Pi 5 Model B 8GB。
- Pi 5 **不可能自己跑 VLM**：模型權重 5.83 GiB，Pi 5 的 LPDDR4X 頻寬約 17 GB/s，
  解碼每個 token 都要讀一遍語言模型權重，一次推論會是數十秒級。VLM 一定在 server 端。

### 網路面：已分析，成本可接受

用實測的 JPEG 大小算（`tools/test_client.py` 的預設 640 寬、q80）：

| 項目 | 數值 |
|---|---|
| 上傳 JPEG | Pi 相機 640×362 約 27 KB；測試圖 44 KB（單目標）～ 87 KB（15 隻狗的複雜畫面），實測 |
| 回傳 | bbox JSON 約 200 bytes，可忽略 |
| Pi 上傳 87 KB | uplink 5 Mbps ≈ 140 ms；20 Mbps ≈ 35 ms |
| 到區域機房 RTT | 10～40 ms（跨區 150～250 ms） |
| **網路總成本估計** | **50～300 ms** |

結論：只要雲端推論能壓到 1 秒內，多付這些網路時間仍然比本地的 1.9 秒快。
當初分析用的 3.4 秒是 640×656 的測試圖；實際 Pi 畫面（640×362）只要 1.9 秒，所以搬到雲端的效益比當初估計的小。
（聯測時機器人上 WiFi 的 `network_ms` p50 為 103 ms，走外網只會更高。）

協定不需要改：`request_id` 對應、只處理最新一筆、逾時重送、TCP 自動重連都已經實作。
client 換 server 只是改 `vlm.endpoint`。

### 運算面：四條路，依序試

模型：NVIDIA **LocateAnything-3B**（Qwen2.5-3B LM + MoonViT 視覺塔），gguf q8_0，5.83 GiB。

**關鍵限制**：locate-anything.cpp v0.1.0 的 CMake 只有
`LA_GGML_CUDA` / `LA_GGML_METAL` / `LA_GGML_VULKAN` / `LA_GGML_CANN`，
**沒有 HIP/ROCm 選項**（已查 v0.1.0 的 CMakeLists.txt 與 README）。

| 路線 | 做法 | 評估 |
|---|---|---|
| **CPU 路線（先做）** | 不設 `LA_DEVICE`，用 CPU backend | 零 backend 工作量，最快拿到基準線。MI300X 機器通常配 EPYC，記憶體頻寬遠高於筆電，有機會贏過本地的 1.9 秒 |
| **Vulkan 路線** | 沿用現有 Dockerfile（`-DLA_GGML_VULKAN=ON`） | MI300X 是 gfx942 純運算卡，雲端映像通常只有 ROCm 沒有 Mesa。**能不能跑要上機驗證** |
| **HIP 路線** | ggml 上游有 `GGML_HIP`。LA 沒包裝這個選項，但它把 ggml 當子專案，可直接傳 `-DGGML_HIP=ON -DAMDGPU_TARGETS=gfx942` | 中等工作量，可能要改 CMake。Vulkan 不行就走這條 |
| **vLLM 路線（換模型）** | 改用 Qwen2.5-VL 這類原生支援 grounding 的模型，跑 vLLM ROCm | ROCm 支援最成熟，但等於換掉整個 VLM，框的品質要全部重新驗證。最後手段 |

**雲端 image 可以不裝 torch**：[vlm_server/pipeline.py](vlm_server/pipeline.py) 只在
`VLM_RETURN_MASK=1` 時才 import torch（`MaskPredictor.__init__` 內），
預設只回 bbox 的模式完全不需要。目前 image 9.6 GB 大部分是 ROCm torch，
雲端版可以砍到 1～2 GB，建置也簡單很多。

### 安全與維運（上公網前必做）

- **目前完全沒有認證**：ZMQ 是明文 TCP，HTTP `/api/query` 任何人打得到就能改追蹤目標。
  **絕對不要直接把 5555 / 8080 開到公網。**
- 建議 WireGuard 或 Tailscale：一次解決加密、認證、NAT 穿透，程式不用改。
- 5.83 GiB 模型要先上傳，或在雲端直接從 HuggingFace 下載。
- 冷啟動成本：容器啟動 + 模型載入約 3～5 秒（本地實測 `load_s` 約 2～3 秒）。
- **保留本地 fallback**：Demo 當天依賴會場網路有風險，切換只是改 `vlm.endpoint`。

### 執行順序

- [ ] **1. 先量網路（零風險，不需要 GPU 或模型）**
      雲端跑 mock server，從 Pi 量 RTT 和上傳時間，確認網路那一半可行：
      ```bash
      # 雲端（只需要 python + pyzmq + flask，不需要模型）
      python -m tools.mock_server --delay 0.5 --query "the cup"
      # Pi
      python -m tools.test_client --endpoint tcp://<cloud>:5555 --ping 20
      python -m tools.test_client --endpoint tcp://<cloud>:5555 --image frame.jpg --count 10
      ```
      要看的是 `rtt_ms − server_ms`（純網路）的 p50 和 p95。

- [ ] **2. 上機確認 backend**（決定走 Vulkan 還是 HIP）
      ```bash
      rocm-smi; ls -l /dev/kfd /dev/dri; vulkaninfo --summary 2>&1 | head -30; glxinfo -B 2>/dev/null
      ```

- [ ] **3. 建輕量 image 並量 CPU 基準（CPU 路線）**
      複製 Dockerfile，移除 torch / torchvision / sam2 / pyrealsense2 相關層，
      跑 `python -m scripts.smoke_pipeline <image> --target "..." --repeat 5`，
      用同樣 640×362 的圖比對本地的 1.9 秒（推論時間和像素數相關，尺寸要一致才能比）。

- [ ] **4. 依步驟 2 的結果試 Vulkan 或 HIP**，量同一組數字。

- [ ] **5. 決定是否切換**，並補上 VPN 與 HTTP API 的認證。

### 待確認

- 雲端 server 是常開還是按需啟動？
- 有公開 IP 還是要走跳板 / VPN？
- Pi 5 實際的上傳頻寬（會場 WiFi vs 有線）。
- MI300X 的 ROCm 版本與是否有 Mesa/Vulkan。

## 3. 嚴格匹配、不硬找

> 狀態（2026-09-20）：**程式已部署（`5dc8d7d`），設定也已在正式機的 `.env` 啟用；驗證集還沒跑**。結果見「評估結果」，進度見「執行進度」。

### 問題

畫面中沒有目標時，VLM 會抓「有點像」或只符合部分描述的物件。例如描述是 `plastic bottle` 卻抓到金屬瓶。
希望只抓完全符合描述、而且可信度夠高的物件；找不到就回 `NOT_FOUND`，不要硬找。

### 已確認的事實（讀 locate-anything.cpp v0.1.0 原始碼）

模型本身有 system prompt 和信心值，但 C++ 版**都沒有暴露給呼叫端**：

| 項目 | 模型本身 | locate-anything.cpp v0.1.0 |
|---|---|---|
| system prompt | 有（Qwen2.5 chat template：system → user → assistant） | `src/prompt.cpp` 寫死成 `"You are a helpful assistant."`；影像 token 放在 user turn、query 前面；呼叫端只能改 query |
| 信心值 | 有（token 機率） | `src/mtp.cpp` 平行框解碼時會算每個 token 的 softmax，但這些機率**沒有存進偵測結果**。（更正 2026-09-20：`BOX_START` ≥ 0.7 只用來判斷是不是 empty_box，**不是**接受框的門檻；框只要結尾分數 ≥ 0.2 就算合法） |
| 什麼時候停止出框 | — | `src/lm.cpp` `decode_hybrid`：block 第 0 個位置的 **argmax** 是 `IM_END`/`NULL` 才停，沒有機率門檻。「再出一個框」只要比結束的機率高一點點，模型就會硬擠一個框出來，這就是「硬找」的來源 |
| 找不到就不回 | 有 | `src/lm.cpp` 遇到 `IM_END` 或 `NULL_TOK` 就結束，可以回空結果。AR 路徑是 greedy argmax，logits 可取得 |
| C API | — | `la_capi.h` 只有 box 和 `la_capi_get_detection_label`，沒有 score |

另外，我們現在用的 prompt（`vlm_server/locator.py` `Locator.locate`，寫死）是「物件偵測」模板：
`Locate all the instances that matches the following description: {q}.`
model card 另有給指代表達式（帶屬性的描述）用的模板：

| 名稱 | 模板 |
|---|---|
| `detect`（目前） | `Locate all the instances that matches the following description: {q}.` |
| `ground_multi` | `Locate all the instances that match the following description: {q}.` |
| `ground_single` | `Locate a single instance that matches the following description: {q}.` |
| `region` | `Locate the region that matches the following description: {q}.` |

### 做法（已實作，2026-09-20）

1. **patch locate-anything.cpp**：[deploy/patches/locate-anything-v0.1.0.patch](deploy/patches/locate-anything-v0.1.0.patch)。Dockerfile 的 locate stage 在 `git clone` v0.1.0 之後 `git apply`；`--build-arg LA_PATCH=0` 可以 build 回原版做對照。
   - `LA_SYSTEM_PROMPT`：覆寫 system prompt，每次呼叫時讀取；沒設時維持 `"You are a helpful assistant."`。
   - 每個框三個分數，存在 `Box`，由新的 `la_capi_get_detection_score(ctx, i, float out[3])` 取得，JSON 也有：
     - `p_object`：1 − P(`<none>`)，取框的第一個座標位置，也就是模型「框出來 vs 說沒有」的取捨。**唯一有用的分數**（見下方「評估結果」）。
     - `p_start`：`<box>` token 的機率。實測一律約 1.0，因為模型說「沒有」時也會先出 `<box>`。
     - `p_coord`：4 個座標 token 機率的平均。
     - MTP 取該 block 位置的 softmax，AR 取該步的 softmax。
   - 原本規劃的 `LA_START_THRESH` **拿掉**：0.7 不是接受框的門檻（見上表更正），改由 server 端用 `VLM_MIN_SCORE` 過濾分數，效果一樣，而且調門檻不用重 build。
   - 本機 CPU build 通過；patch 可以乾淨套用到 v0.1.0。**還沒在 Hackathon-local-server 上用實際模型驗證。**
2. **server 端**（`vlm_server/locator.py`、`vlm_server/pipeline.py`）：
   - `LA_PROMPT_TEMPLATE`：上表的名稱，或含 `{q}` 的自訂字串；預設 `detect`。
   - `VLM_SCORE_FIELD`（`p_object`／`p_coord`／`p_start`，預設 `p_object`）、`VLM_MIN_SCORE`（預設 0，不過濾）。所有框都低於門檻就回 `NOT_FOUND`；有多個框時取分數最高的。原版 library 沒有分數，會照 Locate 的順序取第一個，和以前一樣。
   - 設了 `VLM_MIN_SCORE` 但 library 沒有分數時，啟動就報錯，避免 server 永遠只回 `NOT_FOUND`。
   - 回應的 `score` 改成 VLM 信心值（沒有分數時是 -1）；SAM 的分數只留在 `smoke_pipeline` 的輸出（`sam_score`）。
   - 所有新設定的預設值都維持現在的行為。
3. 用第 4 節的評估集選出 `LA_PROMPT_TEMPLATE`、`LA_SYSTEM_PROMPT`、`VLM_SCORE_FIELD`、`VLM_MIN_SCORE`，寫進 `compose.yaml`。

### 評估結果

評估集：`eval/cases.yaml`，21 張圖 × 4 個描述 = 84 題（32 題有目標、52 題沒有），用機器人上的 RealSense 拍攝（2026-09-20）。

**分數**：`p_start` 一律約 1.0。模型找不到時也會先出 `<box>`，再接 `<none>`，所以 `p_start` 不能代表「有沒有東西」。`p_coord` 在 0.16～0.50 之間，分不出對錯。
因此 patch 加了 **`p_object` = 1 − P(`<none>`)**，取框的第一個座標位置：這是模型在「框出來」和「說沒有」之間的取捨，也是唯一分得開對錯的分數。`VLM_SCORE_FIELD` 預設改為 `p_object`。

**第 0 輪**（640 寬、`fast`，`p_object` 門檻；命中 = IoU ≥ 0.5）：

| 設定 | 門檻 0：命中／誤抓 | 0.9 | 0.95 | 0.99 | p50 |
|---|---|---|---|---|---|
| `detect`（現在的 server） | 100% / 15.4% | 96.9% / 5.8% | 93.8% / 3.8% | 90.6% / 0% | 1951 ms |
| `ground_multi` | 100% / 13.5% | 96.9% / 5.8% | 93.8% / 3.8% | 90.6% / 0% | 1956 ms |
| `region` | 100% / 32.7% | 100% / 23.1% | 100% / 11.5% | 93.8% / 1.9% | 1998 ms |
| `ground_single` | 100% / 98.1% | — | — | — | 2004 ms |
| **`region` + `, exactly as described`** | 100% / 9.6% | **96.9% / 1.9%** | 93.8% / 0% | 59.4% / 0% | 2108 ms |
| `detect` + `, exactly as described` | 84.4% / 1.9% | 59.4% / 0% | 50% / 0% | 25% / 0% | 2106 ms |

- 誤抓幾乎都是**同類別換材質**（紙杯 ↔ 玻璃杯）。空桌面 20 題，除了 `ground_single`、`region` 以外都正確回 none。
- `ground_single`（「找一個」）幾乎每題都硬給一個框，而且信心接近 1.0，**不能用**。
- `detect` + `exactly` 雖然誤抓少，但會漏掉被切邊的黑色保溫瓶，紙杯也會框錯位置。
- 分數偏低的正確答案，幾乎都是被畫面邊緣切到的黑色保溫瓶（0.85～0.93）。
- 樣本小（誤抓只有個位數），而且只有一個場地，門檻邊界還不穩。定案前要用另一批圖驗證。

**第 1 輪**（system prompt × 兩個基底，640 寬、`fast`）：

| 設定 | 門檻 0 | 0.8 | 0.9 | 0.95 | 誤抓的最高分 | p50 |
|---|---|---|---|---|---|---|
| `detect` + 預設 system prompt | 100% / 15.4% | 100% / 15.4% | 96.9% / 5.8% | 93.8% / 3.8% | 0.981 | 1956 ms |
| `detect` + 其他 3 種 system prompt | 100% / 13.5～15.4% | 同左 | 96.9% / 5.8～7.7% | 93.8% / 3.8% | 0.974～0.981 | 2020～2031 ms |
| **`region`+`exactly` + 預設 system prompt** | 100% / 9.6% | 100% / 9.6% | **96.9% / 1.9%** | 93.8% / 0% | 0.932 | 2113 ms |
| `region`+`exactly` + `sys-exact` | 96.9% / 3.8% | 96.9% / 1.9% | 93.8% / 0% | 87.5% / 0% | 0.874 | 2133 ms |
| `region`+`exactly` + `sys-exact-none` | 96.9% / 5.8% | 96.9% / 3.8% | 87.5% / 0% | 84.4% / 0% | 0.835 | 2136 ms |
| `region`+`exactly` + `sys-short` | 100% / 11.5% | 100% / 7.7% | 96.9% / 1.9% | 90.6% / 0% | 0.924 | 2123 ms |

- 預設設定和第 0 輪的結果一模一樣：推論是確定性的，同樣的輸入每次輸出都相同。
- **system prompt 影響很小**：各組之間只差 1～2 題，在 52 題的樣本裡分不出是不是雜訊。更嚴格的 system prompt 會讓 `p_object` 整體下降，對錯兩邊都降，還會多漏抓。
- **暫定設定**：`region`+`exactly`、**預設 system prompt**、`VLM_MIN_SCORE=0.9`。system prompt 維持模型微調時用的那一句，可以避免讓模型偏離訓練時的條件。
  部署時不需要改程式：`LA_PROMPT_TEMPLATE="Locate the region that matches the following description: {q}, exactly as described."`，產生的 prompt 和評估時一字不差。

### 執行進度

- [x] 階段 1：評估工具（見第 4 節）、模板設定
- [x] 階段 3 程式：patch、Dockerfile、server 串接、單元測試（26 個通過）
- [x] 使用者取圖：20 張，Pi 故障，改用本機接機器人的 RealSense 拍（848×480）
- [x] 上機驗證 patch：3 種模式 × 5 張圖，框和原版完全一樣，耗時差 ±1% 以內，分數都在 0～1
- [x] `--propose`（slow 模式）產生標註草稿 → 看 overlay，把錯的答案改成 none → `eval/cases.yaml`
- [x] 第 0 輪（見上表）。Pi 沒在送圖，所以評估是另開容器跑，沒有停正式 server
- [x] 第 1 輪（system prompt × `detect`／`region`+`exactly`）→ 暫定 `region`+`exactly`、預設 system prompt、`VLM_MIN_SCORE=0.9`（誤抓 15.4% → 1.9%，命中率 100% → 96.9%）
- [x] 第 2 輪（解析度，見第 5 節）：維持 640 寬 + `fast`，降解析度會讓誤抓變成 3～4 倍
- [x] 程式碼部署到正式機（`5dc8d7d`，2026-09-20）：patch 版 library 已載入（`has_scores: true`），設定仍是預設值，行為和原本一樣
- [ ] **驗證集**：用另一批圖（換場地、換物件）確認暫定設定，排除「只適用這組圖」的可能（流程見下）
- [x] 正式機的 `.env` 已啟用設定（2026-09-20，使用者手動加入）：`LA_PROMPT_TEMPLATE`、`VLM_MIN_SCORE=0.9`、`VLM_SCORE_FIELD=p_object`
      **注意：比原訂順序提早，驗證集還沒跑。** 如果驗證沒過，從 `.env` 拿掉這三行、`docker compose up -d server` 就能還原

### 驗證集流程

驗證集只拿來確認，**不拿來挑設定**。看完結果後不再調整門檻或模板；要調的話，得再拍一批新的驗證圖。

1. **使用者**：拍圖放進 `eval/holdout/images/`，檔名規則和 `eval/images/` 相同（`single-`、`distract-`、`decoy-`、`empty-`），每種 3～5 張。
   - 要換場地、換物件，特別是**同類別、不同材質**的組合，例如塑膠杯 ↔ 紙杯、玻璃瓶 ↔ 塑膠瓶、馬克杯 ↔ 紙杯。
   - 告訴 Claude 每種場景裡有哪些物件、要問哪些描述。
2. **Claude**：用 `--propose` 產生標註草稿 → 看 overlay 修正 → 寫進 `eval/holdout/cases.yaml` → 交給使用者確認。
3. **Claude**：跑 `eval/sweeps/holdout.yaml`（現在的設定 vs 暫定設定），不用停機。
4. **判定**（事先定好的標準；以下都是門檻 0.9 的暫定設定，和現在的設定、門檻 0 比較）：
   - 誤抓率 **≤ 5%**，而且**低於**現在的設定。
   - 命中率比現在的設定低 **不超過 5 個百分點**。
   - 抽看誤抓和漏抓的 overlay，確認沒有系統性問題，例如某一類物件全部抓不到。
5. **通過**：在正式機的 `~/Documents/vlm-server/.env` 加上下面三行，再跑 `docker compose up -d server`：
   ```bash
   LA_PROMPT_TEMPLATE="Locate the region that matches the following description: {q}, exactly as described."
   VLM_MIN_SCORE=0.9
   VLM_SCORE_FIELD=p_object
   ```
   **沒通過**：把結果寫進這一節，再決定要換設定（例如只加門檻、不換模板），還是往 finetune 方向走。要回到原本的行為，只要從 `.env` 拿掉這三行。
- [ ] 預設值寫進 `compose.yaml`，更新 vlm_transport.md 裡 `score` 的語意，交給使用者 commit 並部署

### 風險

- 模型微調時用的是 `"You are a helpful assistant."`，換掉 system prompt 可能讓框的品質變差，**一定要用評估集驗證**。
- score 可能分不開「正確物件」和「干擾物」（兩者都很像時，模型可能都很有信心）。

### 暫緩

- 負面描述：用 `</c>` 加對照類別（例如 `plastic bottle</c>metal bottle`），只接受 label 是目標的框。需要上級描述提供要排除的東西。
- 裁切後再驗證一次（推論時間 ∝ 像素數，小的裁切圖推論很便宜）。

### 驗證

- 沒設任何新環境變數時，狗的圖和 `eval/images/lab-desk-cup-bottle.png` 的框要和 patch 前完全一樣，推論時間差在 2% 以內；score 落在 0～1。
- 單元測試：模板格式化（預設名稱、自訂 `{q}`、未知名稱報錯）、`VLM_MIN_SCORE` 過濾、多個框時取最高分。

## 4. 現場評估集與評估腳本

> 狀態（2026-09-20）：**完成**：84 題（`eval/cases.yaml`），第 0～2 輪都用它跑完。第 3 節（選 prompt 與門檻）和第 5 節（選解析度）都需要它。用法見 DEBUG.md §7。

- `tools/grab_frame.py`（**使用者在 Pi 上執行**，Claude 不碰 Pi）：用 rclpy 單次訂閱 `/camera/camera/color/image_rect_raw`，以原生解析度存成 PNG，再放到 `eval/images/`。
- `scripts/eval_locate.py`：
  - `--propose`：產生標註草稿。
  - `--sweep eval/sweeps/roundN.yaml`：掃描各組設定。
  - `--summarize results.csv`：不用模型，重算摘要。
  - 縮圖方式模擬 client（先縮到指定寬度，再轉 JPEG q80）。
- 請現場擺出四種場景，每種 3～5 張：
  1. 目標單獨出現（例如塑膠瓶）
  2. 目標 + 干擾物（塑膠瓶 + 金屬瓶）
  3. **只有干擾物**（只有金屬瓶，應回 `NOT_FOUND`）
  4. 目標不在的一般桌面
- `eval/cases.yaml`：每張圖的描述和預期結果（`none`，或預期框 `[x1, y1, x2, y2]`，從 overlay 目視標註）。
  已有第一張：`eval/images/lab-desk-cup-bottle.png`（紙杯、水瓶，598×472）。
- `scripts/eval_locate.py`（在容器內跑，模型只載入一次；system prompt、門檻用環境變數讀，每種設定分開跑一次程序）。
  組合太多，分兩輪：
  1. 固定 640 寬 + `fast`，掃 system prompt（預設 + 2～3 個嚴格版本）× 模板。
  2. 用第 1 輪最好的設定，掃寬度（640／512／448／384）× `LA_MODE`（`fast`、`hybrid`）。
- 指標：目標存在時的命中率（IoU ≥ 0.5）、目標不存在時的誤抓率、score 分布（存在 vs 不存在、正確物件 vs 干擾物，用來選 `VLM_MIN_SCORE`）、推論時間 p50；每個組合輸出 overlay 圖與 CSV。
- 評估時 GPU 不能被搶：先停掉 Pi bridge 或暫停 server 容器，並確認第 7 節提到的其他容器的負載。

## 5. 解析度掃描

> 狀態（2026-09-20）：**完成，結論是維持 640 寬**（見下方「結果」）。
> 選擇原則：在命中率不低於 640 寬減 3 個百分點的組合中，選最快的一組。

- 根據 locate-anything.cpp 的 README：量化只作用在語言模型，**視覺編碼器維持 f32**；MoonViT 是原生解析度輸入，影像 token 數和像素數成正比。
- 這和實測吻合（`fast`，1～2 個目標）：

  | 上傳尺寸 | 像素數 | 推論 |
  |---|---:|---:|
  | 640×362（Pi 相機） | 232k | 1.88 s |
  | 598×472 | 282k | 2.35 s |
  | 640×656 | 420k | 3.41 s |
  | 640×669 | 428k | 3.51 s |

- 所以**降解析度是最直接的加速方法**；換成 q4 量化（只縮語言模型）幫助不大。
- 用第 4 節的評估腳本掃 640／512／448／384。照趨勢外插，512 寬可能約 1.2 秒，但**還沒驗證**，小物件的框也可能變差。
- 有效的話請 client 調 `vlm.upload_max_width`（client 已有這個參數，server 不用改），並更新 server_progress.md 第 5 節。

### 結果（第 2 輪，2026-09-20）：**維持 640 寬，不調 `upload_max_width`**

評估集同第 3 節（84 題）。設定為 `region`+`exactly`、預設 system prompt，`p_object` 門檻：

| 上傳寬度 | 門檻 0：命中／誤抓 | 門檻 0.9：命中／誤抓 | 平均 IoU | p50（最大） |
|---|---|---|---|---|
| **640** | 100% / 9.6% | **96.9% / 1.9%** | 0.953 | 2051 ms（2633） |
| 512 | 96.9% / 17.3% | 93.8% / 7.7% | 0.945 | 1553 ms（2050） |
| 448 | 96.9% / 15.4% | 93.8% / 5.8% | 0.949 | 1336 ms（1546） |
| 384 | 90.6% / 21.2% | 84.4% / 9.6% | 0.928 | 1176 ms（1326） |

- 速度符合預期，和像素數成正比：448 寬快 35%。
- 但**誤抓變成 3～4 倍**：分辨同類別、不同材質要靠細節，推測縮圖後這些細節不見了（還沒驗證）。
- 照選擇原則（命中率不低於 640 寬減 3 個百分點），512／448 都差了 3.1 個百分點，而且和「不要亂抓」這個目標衝突。
- `hybrid` 在每個寬度的結果都和 `fast` **完全一樣**，耗時也差不多（640 寬：2100 ms 對 2051 ms）。這批圖每張只有一個目標，`hybrid` 的 AR 補救沒有被觸發，所以**看不出兩者的差別**。維持 `fast`；同類物件很多時要用 `hybrid` 的建議（第 7 節）不變。
- 想加速，走雲端 MI300X（第 2 節）比降解析度適合。

## 6. V3 舊程式整理

> 狀態：**完成並已部署**（2026-09-19，commit `18a6d26`）。

- 搬進 `vlm_server/`：`locator.py`（原 `app/models.py` 的 `Locator` + `app/core.py` 的 `valid_box`）、`gpu_check.py`、`gpu_probe.py`。
- 刪除：`app/`、`npu/`、V3 的 Dockerfile 變體與 compose 覆寫檔、V3 的腳本與測試、`README_V3.md`、`VALIDATION.md`、`reference/`。
  `valid_box` 與 `check_gpu` 的測試已移到 `tests/test_vlm_server.py`。
- compose 合併成單一 `compose.yaml`：service `server`、容器 `vlm-server`，拿掉 `privileged` 與 USB（V3 相機用的）。
- Dockerfile 前半段（locate build、apt、ROCm torch、SAM2）沒動，讓 build 走快取；apt 清單裡多餘的 `libusb`、`libgl1` 留待之後需要重建時再清。
- 部署後確認（2026-09-19）：
  - 容器 `vlm-server` 運作中，`privileged=false`，裝置只掛 `/dev/kfd`、`/dev/dri`；舊容器 `vlm-server-tracker-1` 已被 `--remove-orphans` 移除。
  - Vulkan 仍然找得到 Radeon 860M（`ggml_vulkan: Found 1 Vulkan devices`）。
  - SAM2 mask 模式正常（拿掉 opencv 後）：`smoke_pipeline --mask` 在 dogs-640 上 `FOUND`，SAM 分數 0.97，暖機後 Locate 1.90 s + SAM 0.66 s。

## 7. 其他注意事項

- **Hackathon-local-server 上有其他容器**：2026-09-19 看到 `mc-main-nav-engine`、`mc-main-nav-map`、`mc-main-nav-mocks` 在跑（不是這個專案的）。可能搶 CPU／GPU，量測延遲或跑評估前要先確認。

## 代辦筆記
- [x] buffer 這個目標物是否有找到過，BT engine 會需要這個資訊，需要在它請求的時候回覆它；出現新的目標物描述時清空 buffer。
  → 已實作 `GET /api/target`（2026-09-19，待部署），規格見 vlm_transport.md §4.6。
  同樣的描述重送也算新描述：`query_version` +1、清空。
