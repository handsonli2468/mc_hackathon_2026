# 架構：VLM 目標偵測 server

> 2026-09-20。協定細節見 [vlm_transport.md](vlm_transport.md)，除錯與部署見 [DEBUG.md](DEBUG.md)，待辦與評估數據見 [TODO.md](TODO.md)。
> 圖是 Mermaid 格式，GitHub、VS Code（Markdown Preview Mermaid Support）都能直接顯示。

## 1. 系統全貌

上級（BT engine）用 HTTP 設定「要找什麼」，Pi 持續送影像，server 回 bbox，下游拿 bbox 做視覺追蹤。

```mermaid
flowchart LR
    subgraph UP["上級決策"]
        BT["BT engine<br/>行為樹"]
    end

    subgraph PI["Raspberry Pi 5 (機器人上)"]
        CAM["RealSense D435i<br/>848x480 RGB + 深度"]
        BRIDGE["vlm_bridge_node<br/>ZMQ DEALER<br/>縮成 640 寬 JPEG q80"]
        TRK["object_tracker<br/>KLT 光流 + 深度"]
        CAM --> BRIDGE
        CAM --> TRK
        BRIDGE -- "bbox" --> TRK
    end

    subgraph GPU["Hackathon-local-server 192.168.68.51"]
        subgraph C["容器 vlm-server"]
            HTTP["HTTP API :8080<br/>Flask + waitress"]
            ZMQ["ZMQ ROUTER :5555<br/>I/O 執行緒"]
            WORKER["Worker 執行緒<br/>一次只做一筆推論"]
            STORE[("QueryStore<br/>描述 / 版本 / 找到過")]
            HTTP <--> STORE
            ZMQ --> WORKER
            WORKER <--> STORE
        end
        VK["Radeon 860M<br/>Vulkan + ROCm"]
        MODELS[("~/Documents/vlm<br/>gguf 5.83 GiB + SAM2")]
        WORKER -- "推論" --> VK
        MODELS -- "啟動時載入" --> WORKER
    end

    BT -- "POST /api/query 設描述<br/>GET /api/target 問找到沒" --> HTTP
    BRIDGE -- "JPEG + JSON header" --> ZMQ
    ZMQ -- "bbox / NOT_FOUND" --> BRIDGE
```

| 介面 | 埠 | 用途 |
|---|---|---|
| ZMQ ROUTER | 5555 | Pi 送影像、收 bbox；也收 ping |
| HTTP | 8080 | 設定描述、查詢是否找到、查狀態 |

## 2. 一次請求的流程

同一時間只跑一筆推論。推論進行中又收到新的圖時，**每個 client 只留最新的那一筆**，舊的直接丟掉不回覆；client 逾時後會自己重送。ping、沒有描述、模型還沒好這三種情況由 I/O 執行緒立刻回覆，不用排隊。

```mermaid
sequenceDiagram
    participant P as Pi bridge
    participant Z as ZMQ I/O 執行緒
    participant S as LatestSlot
    participant W as Worker
    participant G as GPU

    P->>Z: ping (每 2 秒)
    Z-->>P: PONG + query_version

    P->>Z: detect #101 (JPEG)
    Note over Z: 沒描述 → NO_QUERY<br/>模型沒好 → ERROR<br/>都不會進佇列
    Z->>S: put(#101)
    S->>W: take() 取出
    W->>G: 推論 約 2 秒

    P->>Z: detect #102
    Z->>S: put(#102)
    P->>Z: detect #103
    Z->>S: put(#103) → #102 被丟棄
    P->>Z: ping
    Z-->>P: PONG (推論中也能回)

    G-->>W: bbox + 分數
    W-->>P: FOUND #101 + bbox
    W->>S: take() → #103
```

## 3. 推論內部

```mermaid
flowchart TD
    JPEG["JPEG bytes + 描述文字"] --> GUARD{"長邊 > 1280 px?"}
    GUARD -- 是 --> ERR["ERROR：先縮小再送<br/>(4000x2700 曾讓原生層崩潰)"]
    GUARD -- 否 --> PROMPT["format_prompt()<br/>LA_PROMPT_TEMPLATE"]
    PROMPT --> CAPI["Locator (ctypes)<br/>liblocate_anything.so"]

    subgraph LA["locate-anything.cpp v0.1.0 + 我們的 patch"]
        VIT["MoonViT 視覺編碼<br/>f32，token 數 ∝ 像素數"]
        PROJ["projector → 視覺 token"]
        LM["Qwen2.5-3B 解碼<br/>fast/hybrid: MTP 平行出框<br/>slow: 逐字 AR"]
        PARSE["parse_boxes()<br/>token 串 → 框 + 機率"]
        VIT --> PROJ --> LM --> PARSE
    end

    CAPI --> VIT
    PARSE --> SCORES["每個框：bbox + p_object / p_coord / p_start"]
    SCORES --> RANK["rank_candidates()<br/>濾掉 p_object < VLM_MIN_SCORE<br/>取分數最高"]
    RANK -- "全被濾掉" --> NF["NOT_FOUND"]
    RANK -- "有框" --> MASK{"VLM_RETURN_MASK=1?"}
    MASK -- 否 --> OUT["Detection: bbox + score"]
    MASK -- "是 (+0.66 秒)" --> SAM["SAM2.1 hiera tiny<br/>ROCm PyTorch"] --> OUT2["Detection: bbox + mask PNG"]
```

**時間**（640×362、`fast`、Radeon 860M）：視覺編碼加解碼約 1.9～2.1 秒，和像素數大致成正比。降解析度會變快，但誤抓變多，所以維持 640 寬（[TODO.md](TODO.md) §5）。

**分數**（我們 patch 加的，[TODO.md](TODO.md) §3）：

| 分數 | 意義 | 有沒有用 |
|---|---|---|
| `p_object` | 1 − P(`<none>`)，模型在「框出來」和「說沒有」之間的取捨 | **有**，正確和誤抓分得開 |
| `p_coord` | 4 個座標 token 機率的平均 | 沒有，0.16～0.50 混在一起 |
| `p_start` | `<box>` token 的機率 | 沒有，一律約 1.0 |

## 4. 描述與「找到過」的狀態

`QueryStore` 用同一把鎖保護描述、版本、是否找到過。**每次 POST 都算新任務**：版本加一、清空找到過的狀態。推論開始時記下版本，結果回來時只有版本沒變才會標記成功，所以換了描述之後，舊描述晚回來的結果不會算進去。

```mermaid
stateDiagram-v2
    state "無描述 (NO_QUERY)" as none
    state "有描述，還沒找到" as searching
    state "有描述，找到過" as found

    [*] --> none: server 啟動
    none --> searching: POST /api/query，version+1
    searching --> found: 推論 FOUND 且版本相符
    searching --> searching: 推論 NOT_FOUND
    found --> searching: POST 任何描述（含同一句），version+1，清空
    found --> none: DELETE /api/query
    searching --> none: DELETE /api/query
    none --> [*]: 重啟後全部歸零（狀態只在記憶體）
```

`GET /api/target` 回 `{"text": ..., "query_version": N, "found": true|false}`。BT engine 可以比對 `query_version`，確認拿到的答案對應的是哪一個描述。

## 5. 部署與評估

部署一律走 git；評估另外開容器跑，不動正式服務。

```mermaid
flowchart LR
    subgraph DEV["本機"]
        CODE["程式碼變更"] --> TEST["unittest 26 項"] --> PUSH["git push"]
    end
    subgraph SRV["Hackathon-local-server"]
        direction TB
        DEPLOY["deploy/deploy.sh<br/>fetch → checkout → build"]
        BUILD["Dockerfile locate stage<br/>clone v0.1.0 → 套 patch"]
        UT["容器內跑單元測試"]
        UP["up -d server → 等 model_ready"]
        MARK["寫 output/DEPLOYED"]
        DEPLOY --> BUILD --> UT --> UP --> MARK
    end
    subgraph EVAL["評估（vlm-server:dev，另一個容器）"]
        IMGS["eval/images 84 題"] --> SWEEP["scripts/eval_locate.py --sweep"]
        SWEEP --> CSV["results.csv + summary_*.csv + overlay"]
    end
    PUSH --> DEPLOY
    MARK -.同一台機器，評估時 GPU 要夠用.-> SWEEP
```

- 測試沒過或 build 失敗時 `deploy.sh` 會中止，服務繼續跑舊版本。
- 調整 prompt 模板或門檻只要改 `.env` 再 `docker compose up -d server`，不用重新 build。
- `LA_PATCH=0` 可以 build 回沒有 patch 的原版做對照。

## 6. 程式碼地圖

```mermaid
flowchart TD
    MAIN["vlm_server/main.py<br/>起三個執行緒"]
    MAIN --> HTTPF["http_api.py<br/>/api/query /target /status"]
    MAIN --> ZMQF["zmq_server.py<br/>ZmqServer / LatestSlot / Worker / Stats"]
    MAIN --> PIPE["pipeline.py<br/>TargetFinder / rank_candidates / MaskPredictor"]
    HTTPF --> QUERY["query.py<br/>QueryStore"]
    ZMQF --> QUERY
    ZMQF --> PROTO["protocol.py<br/>header 解析與組裝"]
    PIPE --> LOC["locator.py<br/>ctypes + prompt 模板"]
    PIPE --> GPUC["gpu_check.py / gpu_probe.py<br/>子程序驗 ROCm"]
    LOC --> PATCH["deploy/patches/<br/>locate-anything-v0.1.0.patch"]
```

| 檔案 | 責任 |
|---|---|
| `vlm_server/main.py` | 進入點；I/O、推論、HTTP 三個執行緒 |
| `vlm_server/zmq_server.py` | ROUTER 迴圈、只留最新一筆、單一 worker、統計 |
| `vlm_server/query.py` | 描述、版本、是否找到過 |
| `vlm_server/pipeline.py` | 輸入尺寸保護、分數過濾、SAM2 mask |
| `vlm_server/locator.py` | prompt 模板、ctypes 包裝、讀分數 |
| `scripts/eval_locate.py` | 評估：掃描設定、標註草稿、門檻取捨表 |
| `tools/test_client.py`、`tools/mock_server.py` | 不靠 Pi 或不靠 GPU 的測試工具 |
| `tools/grab_frame.py` | 在機器人端抓影像，建立評估集 |
