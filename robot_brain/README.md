# Robot Brain v6.4.0

這是一套部署於 Manta LAB 的機器人任務規劃與執行系統。使用者以自然語言下達任務後，系統會依序完成需求解析、typed RAG、行為規劃、BehaviorTree.CPP v4 XML 生成與驗證，最後自動把有效的 BT 送到團隊的 BT Engine 執行。任務完成後，使用者可提交評分、文字建議、夾爪位置與導航速度，作為下一次規劃可檢索的 Experience RAG。

> 目前軟體版本是 `6.4.0`。資料夾名稱若仍為 `robot-bt-agent-manta-v6.3.0`，不代表程式版本；請以 `/health.version` 與 `agent/app/settings.py` 為準。

## 1. 系統概觀

```text
App / Web UI
    |
    | HTTP + WebSocket :8000
    v
FastAPI Agent
    |
    +--> Qwen3.6 Planner (vLLM :8101)
    |      需求解析、retrieval sketch、compact semantic plan
    |
    +--> Typed RAG (SQLite FTS5)
    |      Skill / Pattern / Scene / Location / Experience
    |
    +--> deterministic policies and validators
    |      capability、location provenance、ports、BT structure
    |
    +--> BTGenBot-2 Compiler (vLLM :8102)
    |      TaskPlanIR -> BehaviorTree.CPP v4 XML
    |
    +--> BT Engine HTTP API
           node sync -> validate -> execute -> poll status
                               |
                               v
                    local robot / ROS-side engine

execution result + user feedback
    -> JSON episode + SQLite index
    -> Experience RAG for later missions
```

正常的 `hybrid` 流程是：

1. Qwen 將使用者訊息解析成任務需求與 RAG 查詢線索。
2. 本機 SQLite FTS5 檢索可用技能、BT pattern、場景、真實地點與過往經驗。
3. Qwen 產生 compact semantic plan；程式以 deterministic logic 補成完整 TaskPlanIR。
4. BTGenBot-2 逐 phase 編譯，程式組裝並驗證 BT XML。
5. 成功且驗證通過的 XML 寫入 mission record。
6. 預設自動呼叫 BT Engine `/execute`；App 不需要再提供人工 Execute 按鈕。
7. Agent 輪詢執行狀態並透過 REST/WebSocket 提供結果。
8. 終態後接受使用者 feedback，寫入 Experience RAG。

## 2. 目前的 BT vocabulary

啟動 Agent 時會同步 BT Engine 的 `GET /nodes?builtin=0`，取得正式的自訂 Action/Condition contract，再與本機 semantic overlay、skill policy 與 BehaviorTree.CPP 原生節點合併。

自訂 leaf nodes 共 7 個：

- `VisualizeObject`
- `IsObjectFound`
- `NavigateToDetectedObject`
- `NavigateToPoint`
- `Patrol`
- `RotateInPlace`
- `SetGripper`

原生 structural nodes 共 13 個：

- `Delay`, `Fallback`, `ForceFailure`, `ForceSuccess`, `Inverter`
- `KeepRunningUntilFailure`, `ReactiveFallback`, `ReactiveSequence`
- `Repeat`, `RetryUntilSuccessful`, `Sequence`, `SubTree`, `Timeout`

因此健康檢查中：

- `engine_node_count = 7`：BT Engine 匯出的自訂 leaf nodes，這是正確值。
- `builtin_node_count = 13`：Agent 已知的 BehaviorTree.CPP 原生節點。
- `effective_node_count = 20`：Planner/validator 的完整 vocabulary。
- `planner_skill_count = 7`：Planner 可選擇的機器人技能，不包含 structural nodes。

物件搜尋必須採用以下語意：

```xml
<Sequence>
  <VisualizeObject object_name="blue can"/>
  <Timeout msec="60000">
    <ReactiveFallback>
      <IsObjectFound object_name="blue can"/>
      <Patrol speed="normal"/>
    </ReactiveFallback>
  </Timeout>
</Sequence>
```

`VisualizeObject` 只負責啟動相機的持續搜尋；`ReactiveFallback` 每個 tick 重新檢查 `IsObjectFound`，未找到時讓 `Patrol` 繼續運作。超時會回傳 FAILURE，外層 `Sequence` 會停止後續任務。

其他重要約束：

- `SetGripper.position` 範圍為 0–100；0 是完全打開，100 是完全閉合。一般抓取若沒有經驗可用，先以 50 為基準。
- 目前沒有 `IsObjectHeld`，不可生成或宣稱以它驗證抓取。
- `NavigateToPoint` 的座標必須來自當次 Location RAG 的可執行 candidate，不可由模型自行猜測。
- 正式 node contract 與規則請見 [docs/BT_NODES_V6_4.md](docs/BT_NODES_V6_4.md) 與 [docs/BT_ENGINE_INTERFACE.md](docs/BT_ENGINE_INTERFACE.md)。

## 3. 專案目錄

```text
robot-bt-agent-manta-v6.3.0/
├── agent/
│   ├── app/                     # FastAPI、LangGraph、RAG、compiler、驗證與執行橋接
│   ├── defaults/                # 新 runtime 的設定、prompt、BT contract 與 RAG seed
│   └── tests/                   # 單元、回歸與 Manta timing 測試
├── docker/
│   ├── Dockerfile               # ROCm vLLM 基底映像
│   ├── start_*.sh               # model/agent 啟動腳本
│   ├── stop_*.sh, status.sh     # 維運腳本
│   ├── upgrade_*.sh             # 舊 persistent runtime 升級腳本
│   └── validate_*.py            # release data 與 typed RAG 驗收
├── frontend/index.html          # 內建測試 UI，由 FastAPI 提供
├── docs/                        # API、BT Engine、RAG、node 與 location 詳細文件
├── examples/                    # 目前 contract 可接受的 BT XML 範例
├── structure.md                 # 更細的 active/legacy/stale 程式盤點
└── agent-runtime/               # 執行後建立；持久化設定、DB、模型、log、結果
```

核心程式：

| 檔案 | 職責 |
|---|---|
| `agent/app/main.py` | REST/WebSocket API、session、mission、feedback、執行狀態 |
| `agent/app/graph.py` | LangGraph planning pipeline |
| `agent/app/rag.py` | Typed RAG、SQLite FTS5、experience writer |
| `agent/app/location_policy.py` | Location RAG provenance 與座標 gate |
| `agent/app/compiler.py` | BTGenBot phase compilation 與 deterministic assembly |
| `agent/app/bt_xml.py` | BehaviorTree.CPP XML 結構與語意驗證 |
| `agent/app/bt_node_sync.py` | live node contract 同步與 merged registry 重建 |
| `agent/app/execution_bridge.py` | BT Engine run 狀態、polling 與事件轉接 |
| `agent/app/store.py` | session、mission、execution SQLite 儲存 |

`agent/defaults/` 是建立全新 runtime 時的 source defaults。實際執行後，operator-owned 設定與知識在 `agent-runtime/`；更新 source 不會自動覆寫既有 runtime，升級時必須使用對應 upgrade/refresh script。

## 4. 復現前提

本專案的正式目標環境是 Linux Manta LAB，不是本機 Windows。需要：

- 可執行 ROCm vLLM 的 AMD GPU 環境，且顯示記憶體足以同時載入兩個模型。
- 可從 Hugging Face 下載 Qwen 與 BTGenBot-2；首次部署需要網路。
- Hugging Face 帳號已接受 gated BTGenBot-2 的使用條款。
- 可連線的團隊 BT Engine URL，以及受保護 API 使用的 token。
- 專案被放在 `/mlsteam/workspace`。腳本預設使用此絕對路徑。
- `bash`, `curl`, `jq`, `sqlite3`, Python 3；使用本專案映像時已安裝。

預設服務：

| 服務 | Port | 預設 model |
|---|---:|---|
| FastAPI Agent / Web UI | 8000 | — |
| Planner vLLM | 8101 | `Qwen/Qwen3.6-35B-A3B-FP8` |
| Compiler vLLM | 8102 | `AIRLab-POLIMI/llama-3.2-1b-it-ft-lora-bt` |

## 5. 全新 Manta LAB 復現

### 5.1 建立並進入 LAB

把本專案完整內容 clone、解壓或同步到 Manta 的：

```bash
/mlsteam/workspace
```

若平台需要自行建立 image，可在專案根目錄建置：

```bash
docker build -f docker/Dockerfile -t robot-brain:6.4.0 .
```

Manta 的 LAB/GPU/device/mount 建立方式依平台設定而異；容器內必須能看到 ROCm GPU，且 workspace 必須位於 `/mlsteam/workspace`。`docker/Dockerfile` 不會複製 source，設計上由 Manta workspace 掛載程式碼與 persistent runtime。

進入 LAB 後：

```bash
cd /mlsteam/workspace
chmod +x docker/*.sh
```

### 5.2 初始化 persistent runtime

只建立目錄與 default files，不啟動服務：

```bash
bash -lc 'cd /mlsteam/workspace && source docker/common.sh && bootstrap_runtime'
```

主要持久化位置如下：

```text
/mlsteam/workspace/agent-runtime/
├── config/                      # 實際 settings、prompts、merged registries
├── rag-knowledge/               # pattern、scene、location、experience source docs
├── state/
│   ├── brain.db                 # sessions、missions、execution runs/events
│   ├── rag.db                   # FTS5 index
│   └── experience-feedback/     # 每次 user feedback 的 JSON
├── models/huggingface/          # HF model cache 與登入資訊
├── logs/                        # planner/compiler/agent logs
├── pids/                        # process PID files
└── evaluations/timing/          # timing traces 與報告
```

### 5.3 設定模型與 BT Engine

編輯：

```bash
nano /mlsteam/workspace/agent-runtime/config/settings.env
```

至少確認：

```dotenv
BT_ENGINE_ENABLED=1
BT_ENGINE_AUTO_EXECUTE=1
BT_ENGINE_URL=https://your-bt-engine.example
BT_ENGINE_TOKEN=replace-with-real-token

# 整合測試可用 integration；真實部署應在地點完成校正後用 validated。
LOCATION_KNOWLEDGE_MODE=integration
ROBOT_MAP_VERSION=
LOCATION_REQUIRE_ACTIVE_MAP_VERSION=0
```

如需替換模型、port、GPU memory ratio，再修改同一檔案的 `PLANNER_*` 與 `BT_COMPILER_*`。預設配置以單 GPU、planner `0.72`、compiler `0.12` 為基準，仍需依實際 GPU 容量調整。

### 5.4 登入 Hugging Face

```bash
export HF_HOME=/mlsteam/workspace/agent-runtime/models/huggingface
hf auth login
hf auth whoami
```

若未先接受 BTGenBot-2 的 gated model 條款，compiler 仍可能下載失敗。只做 planner/direct pipeline 的暫時性開發可把 `ENABLE_BTGENBOT=0`，但這不算完整 hybrid 系統復現。

### 5.5 啟動完整系統

```bash
cd /mlsteam/workspace
bash docker/start_all.sh
```

首次下載與載入模型可能需要較久。啟動順序是 planner → compiler → Agent；Agent 啟動時會同步 live BT nodes 並重建 Skill RAG。

確認狀態：

```bash
bash docker/status.sh
```

常用維運指令：

```bash
bash docker/restart_agent.sh       # prompt/config/RAG 改動後重啟 Agent
bash docker/stop_all.sh            # 停止 Agent 與兩個 vLLM server
bash docker/start_models.sh planner
bash docker/start_models.sh compiler
```

## 6. 啟動驗收

### 6.1 健康檢查

```bash
curl -sS http://127.0.0.1:8000/health | jq '{
  ok,
  version,
  planner_ok: .planner.ok,
  compiler_ok: .compiler.ok,
  engine_ok: .bt_engine.ok,
  engine_auth_ok: .bt_engine.auth_ok,
  auto_execute: .bt_engine.auto_execute,
  contract_valid: .bt_engine_contract.valid,
  semantic_complete: .bt_engine_contract.semantic_complete,
  custom_nodes: .bt_engine_contract.engine_node_count,
  builtin_nodes: .bt_engine_contract.builtin_node_count,
  effective_nodes: .bt_engine_contract.effective_node_count,
  planner_skills: .bt_engine_contract.planner_skill_count,
  node_sync_ok: .bt_node_sync.ok,
  rag_ok: .rag.ok
}'
```

完整環境預期：`ok=true`、版本 `6.4.0`、planner/compiler/engine/RAG/contract/node sync 都正常，且 node counts 為 custom 7、builtin 13、effective 20、planner skills 7。

### 6.2 不會驅動機器人的驗收

```bash
cd /mlsteam/workspace
python3 docker/validate_v6_4_release.py
python3 docker/validate_typed_rag.py
bash docker/test_unit.sh
bash docker/test_system.sh
bash docker/test_rag.sh
bash docker/test_bt_engine_connection.sh
python3 docker/test_continuous_search.py --engine-validate
bash docker/run_timing_profile.sh 5
```

這些 timing/continuous-search 測試會明確送出 `options.auto_execute=false`；`--engine-validate` 只要求 live BT Engine 驗證 XML，不會執行機器人。

結果位置：

```text
agent-runtime/logs/agent.log
agent-runtime/logs/planner-vllm.log
agent-runtime/logs/btgenbot-vllm.log
agent-runtime/evaluations/timing/
```

## 7. App API 快速整合

完整 request/response schema 與狀態流請見 [docs/APP_API.md](docs/APP_API.md)。FastAPI 也提供互動式文件：

```text
http://MANTA_HOST:8000/docs
```

### 7.1 先做安全的生成測試

`auto_execute=false` 會完成規劃、生成與 validation，但不送往機器人：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "尋找藍色罐子並靠近它",
    "session_id": "app-demo-001",
    "pipeline_mode": "hybrid",
    "world_state": null,
    "options": {
      "allow_vision": false,
      "auto_execute": false
    }
  }' | jq '{
    session_id,
    status: .candidate.status,
    mission_id: .candidate.mission_id,
    bt_generation: .candidate.bt_generation,
    auto_execution: .candidate.auto_execution,
    bt_xml: .candidate.bt_xml
  }'
```

成功時，`.candidate.bt_generation.succeeded` 與 `.validated` 都是 `true`，並包含 `mission_id` 與 `bt_xml`。若需要補充資料，`status` 會是 `NEED_MORE_INFO`，App 應用相同 `session_id` 回覆問題。

### 7.2 正式自動執行

以下呼叫可能讓真實機器人動作。只有在現場安全條件已確認時執行：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "把夾爪完全打開",
    "session_id": "app-live-001",
    "pipeline_mode": "hybrid",
    "options": {"allow_vision": false, "auto_execute": true}
  }' | jq '.candidate | {
    status,
    mission_id,
    bt_generation,
    auto_execution
  }'
```

Agent 只有在 BT 成功生成且通過 deterministic validation 後才會嘗試執行。正常啟動時 `.candidate.auto_execution.started=true`，並包含 BT Engine `run_id`。生成成功但 Engine 拒絕或無法連線時，`bt_generation.succeeded` 仍可為 `true`，但 `auto_execution.status` 會是 `FAILED` 並附原因。

查詢結果：

```bash
MISSION_ID=replace-with-mission-id
curl -sS "http://127.0.0.1:8000/api/missions/${MISSION_ID}" | jq .
curl -sS "http://127.0.0.1:8000/api/missions/${MISSION_ID}/engine/status" | jq .
curl -sS "http://127.0.0.1:8000/api/missions/${MISSION_ID}/bt.xml"
```

即時事件 WebSocket：

```text
ws://MANTA_HOST:8000/ws/{session_id}
```

### 7.3 開新 session

```bash
curl -sS -X POST http://127.0.0.1:8000/api/sessions/reset \
  -H 'Content-Type: application/json' \
  -d '{"previous_session_id":"app-live-001"}' | jq .
```

response 會回傳新的 `session_id`，歷史資料保留供 audit，但不帶到新對話。

### 7.4 任務完成後提交 Experience feedback

只有 BT Engine 已到 terminal state 才接受 feedback；否則回傳 HTTP 409。

```bash
curl -sS -X POST \
  "http://127.0.0.1:8000/api/missions/${MISSION_ID}/feedback" \
  -H 'Content-Type: application/json' \
  -d '{
    "rating": 4,
    "comment": "夾取成功，但靠近物體時可以再慢一點。",
    "parameters": {
      "set_gripper_position": 55,
      "navigate_to_detected_object_speed": "slow",
      "navigate_to_point_speed": "normal"
    }
  }' | jq .
```

`rating` 為 1–5；夾爪為 0–100；導航速度只接受 `slow | normal | fast`。原始 JSON 會儲存在 `agent-runtime/state/experience-feedback/`，同時建立可被後續任務檢索的 Experience RAG document。

## 8. RAG 與地點資料

RAG source 目錄：

```text
agent-runtime/rag-knowledge/patterns/
agent-runtime/rag-knowledge/scene/
agent-runtime/rag-knowledge/locations/
agent-runtime/rag-knowledge/experiences/
```

修改知識檔後重建 index：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/rag/sync | jq .
curl -sS http://127.0.0.1:8000/api/rag/status | jq .
```

Location 與 Scene 的維護工具：

```bash
python3 docker/manage_rag_location.py --help
python3 docker/manage_rag_scene.py --help
```

正式機器人環境建議使用 `LOCATION_KNOWLEDGE_MODE=validated`，並為 location 填入已校正 frame、map version 與座標。`integration` 只適合 pipeline 串接期間；它會保留校正警告，但允許 configured location 進入規劃。

## 9. 從既有 persistent runtime 升級

不要刪除 `agent-runtime/`，其中包含模型 cache、token、環境知識、mission、feedback 與經驗。先更新 source code，再按來源版本依序執行 upgrade script。

從 v6.3 升到目前版本：

```bash
cd /mlsteam/workspace
bash docker/upgrade_v6_4_latest_nodes.sh
bash docker/restart_agent.sh
python3 docker/validate_v6_4_release.py
```

從 v5.6 或更早版本升級，依序執行：

```bash
bash docker/upgrade_v6_rag.sh
bash docker/upgrade_v6_1_node_sync.sh
bash docker/upgrade_v6_2_location_grounding.sh
bash docker/upgrade_v6_3_typed_rag.sh
bash docker/upgrade_v6_4_latest_nodes.sh
bash docker/restart_agent.sh
```

upgrade scripts 會更新其負責的 runtime 設定/知識並保留備份；仍應在升級前另外備份整個 `agent-runtime/`。全新 runtime 不需要執行 upgrade scripts。

## 10. 常見問題

### Compiler 無法啟動

確認 gated model 權限與登入位置：

```bash
export HF_HOME=/mlsteam/workspace/agent-runtime/models/huggingface
hf auth whoami
tail -n 120 agent-runtime/logs/btgenbot-vllm.log
```

### BT Engine 顯示 401

把正確 token 寫入 `agent-runtime/config/settings.env` 的 `BT_ENGINE_TOKEN`，然後：

```bash
bash docker/test_bt_engine_connection.sh
bash docker/restart_agent.sh
```

### `engine_node_count` 只有 7

這是預期值，代表 live engine 的 7 個自訂 leaf nodes。請看 `effective_node_count` 是否為 20，不要要求 Engine 匯出 BehaviorTree.CPP 原生 structural nodes。

### `semantic_complete=false`

代表 live Engine 出現了本機 overlay 尚未描述的新 Action/Condition。先查看：

```bash
curl -sS http://127.0.0.1:8000/api/bt-engine/sync-status | jq .
curl -sS http://127.0.0.1:8000/api/bt-engine/nodes | jq .
```

補齊 `agent-runtime/config/bt_engine_semantic_overlay.yaml` 與 policy 後重啟 Agent；不要為了通過 health 而直接讓未知 node 進入 Planner。

### BT 已生成但沒有執行

檢查 response 的 `candidate.bt_generation` 與 `candidate.auto_execution`，再確認：

```bash
curl -sS http://127.0.0.1:8000/api/info | jq '.bt_engine'
tail -n 160 agent-runtime/logs/agent.log
```

常見原因是 request 明確指定 `auto_execute=false`、使用 `compare` mode、BT Engine 未授權/離線，或 Engine validate/execute 拒絕 XML。`compare` mode 永遠不會執行兩棵候選樹。

### 地點任務被拒絕

確認目的地存在於 Location RAG、具備可接受的 calibration/map metadata，而且生成的 `NavigateToPoint` 完全使用當次 retrieval candidate 的座標。詳見 [docs/LOCATION_GROUNDING_V6_2.md](docs/LOCATION_GROUNDING_V6_2.md) 與 [docs/TYPED_RAG_V6_3.md](docs/TYPED_RAG_V6_3.md)。

## 11. 安全注意事項

- `BT_ENGINE_AUTO_EXECUTE=1` 是預設值；一般成功的 `/api/chat` 會讓真實機器人開始執行。
- 自動執行不是跳過驗證：XML 必須先通過 Agent deterministic validation，接著才送到 BT Engine。
- 開發、回歸與 UI 串接測試應送 `options.auto_execute=false`。
- `compare` mode 只產生候選結果，永遠不執行。
- 在真實場域啟用前，先驗證 emergency stop、導航範圍、Location RAG、BT Engine token 與現場人員安全。
- 新的自訂 BT node 在 semantic overlay 完整以前會被隔離，不應直接開放給 LLM。

## 12. 延伸文件

- [App API](docs/APP_API.md)
- [BT Engine interface](docs/BT_ENGINE_INTERFACE.md)
- [BT nodes v6.4](docs/BT_NODES_V6_4.md)
- [BT node synchronization](docs/BT_NODE_SYNC_V6_1.md)
- [Typed RAG](docs/TYPED_RAG_V6_3.md)
- [RAG architecture](docs/RAG_ARCHITECTURE.md)
- [Location grounding](docs/LOCATION_GROUNDING_V6_2.md)
- [SetGripper ABI](docs/GRIPPER_ABI_V6_3.md)
- [實際程式結構與 active/stale audit](structure.md)
