# Robot Brain 專案實際結構盤點

> 盤點日期：2026-09-20  
> 目前程式內版本：`6.4.0`（雖然外層資料夾名稱仍是 `robot-bt-agent-manta-v6.3.0`）  
> 盤點依據：實際啟動腳本、Python import/call path、FastAPI 路由、LangGraph graph、runtime bootstrap、BT node sync、RAG 寫入/讀取路徑，而不是只依 README 或檔名推斷。

## 1. 標記定義

| 標記 | 意義 |
|---|---|
| **[ACTIVE]** | 會進入目前 6.4 production 啟動或請求執行路徑。 |
| **[OPS]** | 不會被服務自動 import，但仍是目前部署、維護、驗證會用到的工具。 |
| **[TEST]** | 不進 production；仍能驗證目前設計或核心通用邏輯。 |
| **[LEGACY]** | 舊版遷移、相容層、舊契約測試或歷史資料；有保存價值，但不是目前 runtime 的真實介面。 |
| **[STALE]** | 已與 6.4 實際契約衝突、內容損壞，或會產生錯誤驗收結果；不應再當成目前操作依據。 |
| **[GENERATED]** | Python/pytest 自動產物，可刪除且不應納入版本控管。 |
| **[RISK]** | 檔案仍有效，但直接使用可能覆蓋 persistent runtime 或把測試資料用到真實機器人。 |

「沒有在 production 執行」不等於一定要刪除。以下將 production、維護工具、測試資產、歷史相容和真正冗餘內容分開標示。

## 2. 核心結論

1. 真正服務入口是：

   ```text
   docker/start_all.sh
     -> docker/start_models.sh all
        -> Qwen planner vLLM :8101
        -> BTGenBot-2 vLLM   :8102
     -> docker/start_agent.sh
        -> python3 -m uvicorn agent.app.main:app :8000
   ```

2. `agent/app/main.py` 是 API、session、mission、BT Engine execution 與 feedback 的總入口；`agent/app/graph.py` 是規劃 pipeline 的總入口。`agent/app/` 下沒有一個完整模組能被確認為「完全沒用」；每個模組都直接或間接進入這兩條路徑。

3. 目前節點數的正確解讀是：

   - live BT Engine custom leaves：7 個。
   - Agent 本地登錄的 BehaviorTree.CPP native/structural nodes：13 個。
   - 規劃與驗證的 effective vocabulary：20 個。
   - 因此 `/health.bt_engine_contract.engine_node_count == 7` 是正常的；總數要看 `effective_node_count == 20`。

4. runtime 設定的權威順序是：

   ```text
   live BT Engine GET /nodes?builtin=0       形式契約：ID/kind/ports/type/default
                  +
   agent-runtime/config/
     bt_engine_semantic_overlay.yaml         本地語意
     bt_skill_policy.yaml                    planner 暴露政策
     builtin_bt_nodes.yaml                   13 個 native nodes
                  ↓
     bt_engine_registry.generated.yaml       live formal cache
     skill_registry.yaml                     合併後、實際給 Planner/RAG 的 registry
                  ↓
     agent-runtime/state/rag.db              實際檢索索引
   ```

   `agent/defaults/` 只是新 runtime 的種子與升級來源。`common.sh` 和 `bootstrap.py` 原則上只在檔案不存在時複製，不會在每次啟動覆蓋 Manta 上的 persistent runtime。例外是明確執行 upgrade/refresh scripts。

5. 此份本機目錄沒有 `agent-runtime/`。Manta 真正運行時的資料庫、最新同步契約、feedback JSON、log、PID、model cache 和 timing 結果不在這份 source snapshot 內，因此 source audit 無法取代 Manta runtime audit。

6. 已確認的明顯過時內容：

   - `agent/tests/regression_cases.yaml` 使用已淘汰的 `FindObject`、`NavigateToObject`、`PickObject`、`IsObjectHeld`，且任務文字已損壞。
   - `docker/test_system.sh` 把未登錄的 `AlwaysSuccess` 當成合法 built-in，與目前 13-node registry 衝突。
   - `docs/RAG_ARCHITECTURE.md` 仍宣稱沒有 location-navigation node，但目前已有 `NavigateToPoint`。
   - `docs/BT_ENGINE_INTERFACE.md` 仍混有 v6.3「SetGripper 尚未確認」文字，後段卻已描述 6.4 的 0–100 契約。
   - `agent/defaults/skill_registry.demo.yaml` 是舊的虛構/demo ABI，production 不讀取；很多舊單元測試仍用它測通用 compiler 行為。
   - `.pytest_cache/`、所有 `__pycache__/` 和 `*.pyc` 都是冗餘產物。目前也沒有 `.gitignore` 阻止它們再次出現。

## 3. 實際運行架構

### 3.1 Process 與網路邊界

```text
App / Web UI
    |
    | HTTP / WebSocket :8000
    v
FastAPI (agent.app.main)
    |
    +--> LangGraph planning pipeline (agent.app.graph)
    |      +--> Qwen OpenAI-compatible API :8101
    |      +--> SQLite FTS5 RAG
    |      +--> BTGenBot-2 OpenAI-compatible API :8102
    |      +--> deterministic normalizers / validators / compiler assembly
    |
    +--> persistent mission/session DB: agent-runtime/state/brain.db
    +--> RAG DB:                agent-runtime/state/rag.db
    +--> feedback JSON:         agent-runtime/state/experience-feedback/*.json
    +--> timing JSONL:          agent-runtime/evaluations/timing/traces.jsonl
    |
    +--> Team BT Engine HTTP API
           /health /nodes /validate /execute /status /cancel /runs
           |
           v
        local robot BT Engine / ROS-side execution
```

Agent 不直接使用 ROS。對機器人執行端的邊界是 `agent/app/bt_engine_client.py` 所實作的 HTTP API。

### 3.2 啟動流程

```text
start_agent.sh
  -> shell bootstrap_runtime（缺檔才由 defaults 複製）
  -> 載入 agent-runtime/config/settings.env
  -> uvicorn agent.app.main:app
  -> FastAPI startup
       1. Python bootstrap_runtime
       2. 建立 brain.db schema
       3. 建立 rag.db / FTS5 schema
       4. GET live BT Engine /nodes?builtin=0
       5. 驗證 live formal contract
       6. 合併 semantic overlay + planner policy + builtins
       7. 原子更新 runtime cached model/registry
       8. 重建 RAG index
       9. 若 engine 暫時失聯，保留 last-known-good registry，但 health 會顯示 sync failure
```

`BT_ENGINE_SKIP_STARTUP_NODE_SYNC=1` 只是離線開發的緊急 bypass。正常 Manta 啟動會同步。

### 3.3 `/api/chat` 規劃流程

```text
使用者訊息 + session history + world_state
  -> conversation pending-slot grounding
  -> requirement extraction + retrieval sketch（Qwen）
  -> adaptive RAG
       skill / pattern / scene / location / experience
  -> capability gate
       NEED_MORE_INFO / UNSUPPORTED / planning
  -> pipeline 分支
       hybrid:
         compact semantic planner（正常 fast path）
         -> deterministic TaskPlanIR enrichment
         -> skill ABI normalization
         -> visual/location grounding policy
         -> plan hardening
         -> IR validation + quality gate
         -> 必要時 full planner / critic / bounded IR repair
         -> BTGenBot phase-by-phase compile
         -> deterministic policy assembly
       direct:
         Qwen 直接產 XML
  -> XML normalization
  -> structural + semantic + RAG closed-set validation
  -> bounded BT repair
  -> SUCCESS / PLANNING_FAILURE
  -> 只有有效 SUCCESS tree 才存成 mission
```

目前物件搜尋的 deterministic 形狀是：

```xml
<Sequence>
  <VisualizeObject object_name="..."/>
  <Timeout msec="60000">
    <ReactiveFallback>
      <IsObjectFound object_name="..." poll_ms="500"/>
      <Patrol/>
    </ReactiveFallback>
  </Timeout>
  <!-- 成功後才會執行後續 action -->
</Sequence>
```

`RotateInPlace` 不再是預設搜尋策略；`Patrol` 的路線由 BT Engine 擁有，Agent/LLM 不設計內部 patrol 行為。

### 3.4 執行與 Experience RAG

```text
已儲存 mission
  -> /api/chat 自動 POST BT Engine /execute（不等待人工按鈕）
       僅限 Agent deterministic validation 通過的 XML
       compare mode 不執行任何候選樹
  -> background poll /status/{run_id}
  -> brain.db 保存 execution run/events
  -> WebSocket 推送 UI
  -> terminal outcome 自動寫 execution experience
  -> terminal 後才接受 POST /feedback
       rating 1..5
       comment
       SetGripper.position 0..100（可選）
       NavigateToDetectedObject.speed（可選）
       NavigateToPoint.speed（可選）
  -> JSON 保存 + 寫入 rag_experiences/FTS5
  -> 後續類似任務可檢索
```

## 4. 目錄與檔案逐項分類

### 4.1 根目錄

| 檔案 | 狀態 | 實際角色 / 判斷 |
|---|---|---|
| `README.md` | **[OPS]** | 目前主說明，已包含 6.4 節點與 API，但累積大量 v5/v6 升級歷史，閱讀時應以 6.4 段落為準。 |
| `MANTA_DEPLOYMENT.md` | **[OPS]** | Manta 部署與版本遷移手冊；最新 6.4 章節有效，前段舊版本章節只適用相應來源版本。 |
| `ARCHITECTURE_FUSION.md` | **[OPS]** | 高階架構概念仍大致符合；不是可執行設定，也不應凌駕程式碼。 |
| `CHANGELOG_V6.md` | **[LEGACY]** | v6 歷史紀錄。 |
| `CHANGELOG_V6_0_1.md` | **[LEGACY]** | v6.0.1 歷史紀錄。 |
| `CHANGELOG_V6_1.md` | **[LEGACY]** | v6.1 歷史紀錄。 |
| `CHANGELOG_V6_2.md` | **[LEGACY]** | v6.2 歷史紀錄。 |
| `CHANGELOG_V6_3.md` | **[LEGACY]** | v6.3 歷史紀錄。 |
| `CHANGELOG_V6_4.md` | **[OPS]** | 目前版本變更摘要。 |
| `V6_1_VERIFICATION.md` | **[LEGACY]** | 當時版本的驗證紀錄，不是 6.4 acceptance test。 |
| `V6_2_VERIFICATION.md` | **[LEGACY]** | 當時版本的驗證紀錄。 |
| `V6_3_VERIFICATION.md` | **[LEGACY]** | 當時版本的驗證紀錄。 |
| `.pytest_cache/**` | **[GENERATED]** | 可安全刪除，不影響程式。 |

目前 snapshot 沒有 `.git/`、`.gitignore`、`pyproject.toml`、`requirements.txt` 或 compose file。容器依賴目前只寫在 `docker/Dockerfile`，版本重現性較弱；且缺少 `.gitignore` 是 cache/pyc 被帶進發佈包的直接風險。

### 4.2 `agent/app/`：production Python

以下全部是 **[ACTIVE]**。部分檔案內仍有 legacy compatibility branch，但不能因而刪掉整個檔案。

| 檔案 | 實際角色 |
|---|---|
| `__init__.py` | Python package marker。 |
| `settings.py` | 所有 env/runtime path 與 6.4 版本設定；在 import 時建立 frozen settings。 |
| `bootstrap.py` | 建立 runtime 目錄、缺檔時複製 defaults、從 v6.0 registry 遷移 semantic overlay。 |
| `main.py` | FastAPI app、startup、health、chat/session/mission/RAG/engine/feedback API、WebSocket、frontend mount。 |
| `graph.py` | LangGraph 狀態、節點、routing、hybrid/direct 主 pipeline、bounded repair。 |
| `schemas.py` | Pydantic API model、requirement/compact-plan/TaskPlanIR model、feedback 驗證。 |
| `model_client.py` | 呼叫 Qwen/BTGenBot 的 OpenAI-compatible HTTP client、structured output fallback、model health。 |
| `prompts.py` | 從 runtime config 載入 prompt，組裝每個模型角色的 messages。 |
| `registry.py` | 載入 merged skills、builtins、overlay、policy、formal cache，建立 capability/port prompt view。 |
| `bt_engine_client.py` | Team BT Engine HTTP client 與 auth header。 |
| `bt_engine_contract.py` | 解析 `/nodes` TreeNodesModel、合併 formal+semantic、驗證 7+13 契約。 |
| `bt_node_sync.py` | live node sync、候選驗證、backup/rollback、原子更新、missing-overlay scaffold、sync report。 |
| `rag.py` | SQLite FTS5 knowledge sync/retrieval、typed scene/location、closed-set validation、execution/user experience。 |
| `location_grounding.py` | location alias/pose/frame/map/calibration gate 與精確 port binding。 |
| `location_policy.py` | 把可執行 location candidate deterministic 寫入 plan。 |
| `conversation_grounding.py` | 多輪 clarification，尤其 recipient/location 描述的延續。 |
| `mission_semantics.py` | fetch/deliver/acquire/release 等 task semantics 與 goal-effect closure。 |
| `compact_plan.py` | compact LLM output normalization 與完整 TaskPlanIR enrichment。 |
| `skill_contract_normalizer.py` | 把模型的舊 node/port alias 正規化成 runtime ABI；目前仍在 pipeline 中。 |
| `grounding_policy.py` | 保證需要 perception 的導航前已有 visual grounding。 |
| `plan_hardening.py` | deterministic 加入 bounded search/recovery；6.4 主路徑為 continuous Patrol。 |
| `ir_validator.py` | TaskPlanIR node/port/argument/termination validation。 |
| `quality_gate.py` | robustness、grounding、recovery 品質檢查並決定是否進 critic/full planner。 |
| `ir_normalizer.py` | 將 full IR 縮成 BTGenBot 真正能可靠編譯的契約。 |
| `compiler_adapter.py` | blackboard binding、phase invocation、timeout/retry/search policy 資料。 |
| `phase_compiler.py` | phase XML 清理/驗證、deterministic fallback、組合完整 BehaviorTree。 |
| `bt_normalizer.py` | 清掉/修正 BTGenBot 不應擁有的 wrapper、名稱與 node 形式。 |
| `bt_xml.py` | XML extract、normalize、結構/port/type/range/blackboard 驗證。 |
| `bt_semantic.py` | fail-stop ordering、action/verification、continuous search 等語意驗證。 |
| `metrics.py` | tree depth/node/retry/fallback 等指標。 |
| `execution_bridge.py` | 正規化 engine status、產生人類可讀結果與 replan feedback、保存 execution。 |
| `store.py` | `brain.db`：messages、missions、execution events/runs、pending session state。 |
| `telemetry.py` | stage/model timing，寫入 `evaluations/timing/traces.jsonl`。 |

#### `agent/app/` 內的 legacy compatibility（仍在用，不應直接刪）

- `bootstrap.py` 的 v6.0 semantic migration：只對舊 persistent runtime 生效。
- `skill_contract_normalizer.py` 的舊 node/port alias mapping：用來吸收 LLM 仍可能產生的舊名稱。
- `plan_hardening.py`、`compiler_adapter.py`、`quality_gate.py`、`ir_normalizer.py` 中的 viewpoint/rotation search 分支：目前 6.4 registry 會走 continuous Patrol 分支，但 demo/舊 registry 測試仍覆蓋舊邏輯。
- `bt_engine_contract.py` 對 `RecoveryNode` 等舊 built-in 的驗證分支：目前 13-node builtins 沒有暴露該節點，屬相容邏輯。

若未來要移除這些分支，應先同時移除 demo registry、舊測試 fixture 和舊 runtime upgrade 支援；不能只刪單一函式。

### 4.3 `agent/defaults/`：新 runtime 種子

| 檔案 | 狀態 | 實際角色 / 判斷 |
|---|---|---|
| `settings.env` | **[ACTIVE][RISK]** | 新 runtime 的設定模板，只在缺檔時複製。預設 `LOCATION_KNOWLEDGE_MODE=integration`，可讓未校準 demo 座標進入執行路徑；真機應改成 `validated` 並填 map version。 |
| `bt_engine_nodes.xml` | **[ACTIVE]** | shipped 7 custom-leaf snapshot 與離線 fallback；13 個 native nodes 另由 `builtin_bt_nodes.yaml` 提供。啟動成功同步後由 live `/nodes` cache 取代 runtime copy。 |
| `bt_engine_registry.generated.yaml` | **[ACTIVE]** | shipped 7 custom-leaf formal cache；runtime 中由 node sync 重建。語意不以此檔為權威。 |
| `bt_engine_semantic_overlay.yaml` | **[ACTIVE]** | 7 個 custom leaves 的本地語意、capability、failure/verification/search policy。 |
| `bt_skill_policy.yaml` | **[ACTIVE]** | 決定 live leaves 是否能進 Planner/RAG；目前要求正好 7 個 custom leaves。 |
| `builtin_bt_nodes.yaml` | **[ACTIVE]** | 13 個 BehaviorTree.CPP native/structural node 的本地規則、描述與 Agent 限制。 |
| `skill_registry.yaml` | **[ACTIVE]** | shipped merged registry；fresh/offline fallback 使用，正常 startup sync 會在 runtime 重建。 |
| `skill_registry.demo.yaml` | **[LEGACY]** | 舊 `FindObject/PickObject/IsObjectHeld/...` ABI，只被舊測試 fixture 使用；production loader 不讀它。bootstrap 把它複製進 runtime 沒有 production 效益。 |

#### Prompts

| 檔案 | 狀態 | 使用時機 |
|---|---|---|
| `prompts/requirement.md` | **[ACTIVE]** | requirement + RAG retrieval sketch。 |
| `prompts/compact_planner.md` | **[ACTIVE]** | hybrid 正常 fast path。 |
| `prompts/planner.md` | **[ACTIVE]** | complex/escalated full TaskPlanIR。 |
| `prompts/critic.md` | **[ACTIVE]** | quality gate 判定需要時才呼叫。 |
| `prompts/ir_repair.md` | **[ACTIVE]** | IR validation failure 的 bounded repair。 |
| `prompts/compiler.md` | **[ACTIVE]** | BTGenBot phase compile。 |
| `prompts/bt_repair.md` | **[ACTIVE]** | hybrid XML repair。 |
| `prompts/direct_bt.md` | **[ACTIVE]** | `pipeline_mode=direct` 或 compare 的 direct 分支。 |

這些 prompt 也是「缺檔才複製」；修改 repo defaults 不會自動改到已存在的 Manta runtime。6.4 升級腳本會明確覆蓋 runtime prompts 並先備份。

#### RAG knowledge

| 檔案 | 狀態 | 判斷 |
|---|---|---|
| `rag/locations/meeting_room.yaml` | **[ACTIVE][RISK]** | 4 個 demo location；皆標記 `calibrated: false`，但 integration mode 仍可能用於 pipeline 測試，不能直接視為真機安全座標。 |
| `rag/scene/meeting_room.yaml` | **[ACTIVE]** | meeting room scene priors，座標只以 `location_ids` 引用，沒有重複嵌入 pose。 |
| `rag/patterns/action_then_verification.md` | **[ACTIVE]** | action/verification 通用模式。 |
| `rag/patterns/bounded_visual_search.md` | **[ACTIVE]** | 現行 VisualizeObject + Timeout + ReactiveFallback + Patrol 標準模式。 |
| `rag/patterns/ensure_state_before_action.md` | **[ACTIVE]** | condition-first guard 通用模式；實際 condition 仍受 closed set 限制。 |
| `rag/patterns/fail_stop_sequence.md` | **[ACTIVE]** | mission fail-stop Sequence。 |
| `rag/patterns/ground_before_navigation.md` | **[ACTIVE]** | perception grounding 後才能 NavigateToDetectedObject。 |
| `rag/patterns/human_clarification.md` | **[ACTIVE]** | 缺必要欄位時詢問使用者。 |
| `rag/patterns/scene_prior_then_local_search.md` | **[ACTIVE]** | scene prior 是搜尋線索，不取代 perception。 |
| `rag/patterns/track_dynamic_target.md` | **[STALE]** | 提到「tracking refresh」，目前 7 個 custom nodes 沒有 tracking node。雖然 closed-set validator 可擋 hallucination，這份文件仍可能誤導規劃語意，應改寫成現有 continuous camera pose + `NavigateToDetectedObject`，或停用。 |

### 4.4 `agent/tests/`

#### 目前可作為 6.4 驗證的測試

| 檔案 | 狀態 | 判斷 |
|---|---|---|
| `conftest.py` | **[TEST]** | 在 temporary runtime 隔離測試，不污染 production。 |
| `test_v6_4_latest_nodes_feedback.py` | **[TEST]** | 最新 7+13 nodes、SetGripper、feedback/experience 的主要回歸。 |
| `test_v6_3_typed_rag.py` | **[TEST]** | 名稱是 v6.3，但 typed RAG 與最新 7-node assertions 仍屬現行功能。 |
| `test_v6_2_location_grounding.py` | **[TEST]** | 名稱是 v6.2，但已使用目前 registry，仍驗證現行 location pipeline。 |
| `test_v6_1_node_sync.py` | **[TEST]** | live node sync/overlay quarantine 仍是現行 startup 核心。 |
| `test_v6_0_1_rag_profile.py` | **[TEST]** | RAG/parameter bounds 等通用 regression；版本名舊但仍觸及現行程式。 |
| `test_v6_rag.py` | **[TEST]** | closed-set RAG 與目前 Visualize/Patrol skills。 |
| `test_v5_3_formal_bt_engine.py` | **[TEST]** | 檔名舊，但 fixture 讀目前 `skill_registry.yaml`，assertions 已更新到 7 custom leaves 與 continuous Patrol。 |
| `test_v5_4_execution_bridge.py` | **[TEST]** | execution bridge 現行邏輯，透過上述 current formal fixture。 |
| `test_v5_5_grounding_fastpath.py` | **[TEST]** | compact/grounding/quality path 仍在 production。 |
| `test_v5_6_engine_compact_ui.py` | **[TEST]** | compact plan、engine validation 和 UI contract 的現行回歸。 |
| `run_timing_profile.py` | **[OPS][TEST]** | 對 live `/api/chat` 做 end-to-end timing，輸出完整 JSON；不是 pytest。 |

#### 只驗證舊 demo ABI 或通用演算法的測試

| 檔案 | 狀態 | 判斷 |
|---|---|---|
| `test_bt_validator.py` | **[LEGACY][TEST]** | inline `FindObject/PickObject/AlwaysSuccess` fixture，只能證明 validator 通用行為。 |
| `test_capability.py` | **[LEGACY][TEST]** | 最小 `FindObject` capability fixture。 |
| `test_ir_validator.py` | **[LEGACY][TEST]** | `PickObject/IsObjectHeld` fixture。 |
| `test_compiler_v43.py` | **[LEGACY][TEST]** | 使用 `skill_registry.demo.yaml`。 |
| `test_compiler_v44.py` | **[LEGACY][TEST]** | 使用 demo ABI。 |
| `test_compiler_v45.py` | **[LEGACY][TEST]** | 使用 demo ABI。 |
| `test_compiler_v46.py` | **[LEGACY][TEST]** | 使用 demo ABI。 |
| `test_timing_v47.py` | **[LEGACY][TEST]** | 測 adapter/timing-era 邏輯，但 fixture 是 demo ABI。 |
| `test_v5_1_status_flow.py` | **[LEGACY][TEST]** | 舊 Find/Navigate/Pick status flow。 |
| `test_v5_2_optimization.py` | **[LEGACY][TEST]** | 舊 demo search hardening。 |
| `test_v5_quality_recovery.py` | **[LEGACY][TEST]** | 舊 rotation/viewpoint recovery。 |

這批測試仍可保留來防止通用 compiler/validator 退化，但「全部通過」不能證明 live 6.4 node contract 正確。後續最好將其移至 `agent/tests/legacy/`，或改寫成目前 7-node fixture。

#### 已失效的 regression harness

| 檔案 | 狀態 | 判斷 |
|---|---|---|
| `regression_cases.yaml` | **[STALE]** | 任務文字亂碼，required nodes 全是舊 demo ABI。 |
| `run_regression.py` | **[STALE]** | runner 框架本身可重用，但 property check 硬編碼 `PickObject`/`IsObjectHeld`，輸出檔名仍是 `regression-v5.2-*`；搭配目前 cases 無法驗收 6.4。 |
| `__pycache__/**`, `*.pyc` | **[GENERATED]** | 可安全刪除。 |

### 4.5 `docker/`

#### 正常啟停與部署

| 檔案 | 狀態 | 實際角色 |
|---|---|---|
| `Dockerfile` | **[OPS]** | ROCm vLLM image + FastAPI/LangGraph/YAML/XML/pytest 工具。Base image 使用 `latest`，不能保證重建結果固定。 |
| `common.sh` | **[OPS]** | runtime paths、shell bootstrap、env load、HTTP wait、PID helper。 |
| `start_all.sh` | **[OPS]** | 啟動 planner、compiler、Agent。 |
| `start_models.sh` | **[OPS]** | vLLM planner/compiler 啟動與 gated HF 檢查。 |
| `start_agent.sh` | **[OPS]** | 正式 uvicorn 入口與 startup sync status。 |
| `restart_agent.sh` | **[OPS]** | 只重啟 FastAPI Agent。 |
| `stop_agent.sh` | **[OPS]** | 停 Agent。 |
| `stop_all.sh` | **[OPS]** | 停 Agent 與兩個 vLLM。 |
| `status.sh` | **[OPS]** | process/health/model/disk/GPU 狀態。 |
| `start_ssh.sh` | **[OPS]** | 可選的容器 SSH 基礎設施，不屬 robot brain request path。 |

#### 目前有效的管理/驗證工具

| 檔案 | 狀態 | 實際角色 |
|---|---|---|
| `run_timing_profile.sh` | **[OPS]** | 包裝 `agent/tests/run_timing_profile.py`。 |
| `test_unit.sh` | **[OPS][TEST]** | 跑全部 pytest；會混合 current 與 legacy-fixture tests。 |
| `test_bt_engine_connection.sh` | **[OPS]** | engine health/auth/runs/live custom nodes。 |
| `test_continuous_search.py` | **[OPS][TEST]** | 現行搜尋 end-to-end；印 health、IR、XML、validation、timing 並保存 JSON/XML。 |
| `test_grounded_delivery.sh` | **[OPS][TEST]** | 多輪 recipient clarification 與 grounded delivery smoke scenario。 |
| `validate_v6_4_release.py` | **[OPS][TEST]** | dependency-light 7+13 nodes、ports、overlay、prompt、examples、API source acceptance。 |
| `validate_typed_rag.py` | **[OPS][TEST]** | location schema、alias collision、scene reference 驗證；仍適用現行 typed RAG。 |
| `inspect_gripper_contract.py` | **[OPS]** | 讀 live formal cache/產 overlay scaffold；目前也會提醒 SetGripper 沒有 object identity。 |
| `manage_rag_location.py` | **[OPS]** | location CRUD/校準管理。 |
| `manage_rag_scene.py` | **[OPS]** | scene knowledge CRUD。 |
| `set_scene_location.py` | **[LEGACY][OPS]** | v6.2 指令相容 wrapper，實際轉呼叫 `manage_rag_location.py`；可保留但新文件不應再推薦。 |
| `upgrade_v6_4_latest_nodes.sh` | **[OPS][RISK]** | 從既存 runtime 升到目前 6.4；會備份後覆蓋 node contract、overlay、policy、merged registry、全部 prompts 與兩個 search patterns。 |

#### 可執行但不應作為目前 6.4 驗收的工具

| 檔案 | 狀態 | 判斷 |
|---|---|---|
| `test_system.sh` | **[STALE]** | 第 7 步要求 `AlwaysSuccess` 合法；目前 builtin registry 沒有該 ID，預期會誤報失敗。 |
| `test_rag.sh` | **[STALE]** | API 本身可用，但 sample 還要求 `verify_object_held`；目前沒有 `IsObjectHeld`，不能把輸出當 current capability acceptance。 |
| `refresh_runtime_defaults.sh` | **[LEGACY][RISK]** | 手動 refresh，會覆蓋 runtime prompts/builtins/engine model/RAG knowledge；不是正常 startup，也不是完整 6.4 upgrade。 |
| `enable_demo_skills.sh` | **[LEGACY]** | 檔名誤導；內容其實複製正式 `skill_registry.yaml`，不會啟用 `skill_registry.demo.yaml`，且沒有同步 overlay/policy/prompts。應改名或移除。 |
| `enable_formal_bt_engine_skills.sh` | **[LEGACY]** | 舊版 formal registry installer，只複製三個檔案，對 6.4 不完整；改用 6.4 upgrade + live sync。 |

#### 歷史 upgrade scripts

以下都是 **[LEGACY][RISK]**：只在「persistent runtime 正好來自對應舊版本」時才有意義。不可在已是 6.4 的 runtime 依序重跑，因為其中部分會覆寫設定、prompts 或契約。

- `upgrade_v5_settings.sh`
- `upgrade_v5_2_settings.sh`
- `upgrade_v5_3_contract.sh`
- `upgrade_v5_4_engine.sh`
- `upgrade_v5_5_grounding_fastpath.sh`
- `upgrade_v5_6_engine_ui.sh`
- `upgrade_v6_rag.sh`
- `upgrade_v6_1_node_sync.sh`
- `upgrade_v6_2_location_grounding.sh`
- `upgrade_v6_3_typed_rag.sh`

`docker/__pycache__/**` 與 `*.pyc` 是 **[GENERATED]**，可安全刪除。

### 4.6 `docs/`

| 檔案 | 狀態 | 判斷 |
|---|---|---|
| `APP_API.md` | **[OPS]** | 目前 app 整合的主要 API 文件，包含 chat、session reset、feedback、mission/engine 與 WebSocket。 |
| `BT_NODES_V6_4.md` | **[OPS]** | 目前 node 與 continuous-search 最可信的人類可讀文件。 |
| `BT_NODE_SYNC_V6_1.md` | **[OPS]** | 版本名舊，但 live formal + local semantic trust split 仍是現行設計。 |
| `LOCATION_GROUNDING_V6_2.md` | **[OPS]** | 版本名舊，但 location gate/binding 仍是現行設計。 |
| `TYPED_RAG_V6_3.md` | **[OPS]** | typed Scene/Location RAG 仍是現行設計。 |
| `BT_ENGINE_INTERFACE.md` | **[STALE][OPS]** | engine HTTP protocol 多數仍有效，但混有「v6.3 尚未確認 SetGripper」與 6.4 已確認內容；需清理矛盾段落後才能作唯一介面文件。 |
| `RAG_ARCHITECTURE.md` | **[STALE]** | 誤稱目前沒有 location-navigation node；實際已有 `NavigateToPoint` 與 deterministic binding。 |
| `GRIPPER_ABI_V6_3.md` | **[LEGACY]** | 檔案自己已標 `superseded`；只作決策歷史，不能拿來設定 6.4。 |
| `SETGRIPPER_OVERLAY_TEMPLATE.yaml` | **[LEGACY]** | 人工 review scaffold/reference，production 不讀取；目前正式語意已在 defaults overlay。 |

### 4.7 `examples/`

| 檔案 | 狀態 | 判斷 |
|---|---|---|
| `formal_bottle_bt.xml` | **[TEST][OPS]** | 目前 6.4 continuous search + navigation + SetGripper 範例，release validator 會解析。 |
| `grounded_delivery_bt.xml` | **[TEST][OPS]** | 目前 grounded delivery tree 範例，release validator 會檢查不得出現未知 tag。 |

### 4.8 `frontend/`

三個檔案都是 **[ACTIVE]**，因為 `main.py` 最後以 `/` mount 整個 frontend 目錄。

| 檔案 | 實際角色 |
|---|---|
| `index.html` | Chat、pipeline 選擇、artifact tabs、engine control/status UI。 |
| `app.js` | `/api/chat`、session reset、mission engine API、node sync、WebSocket、tree/timing/RAG rendering。 |
| `styles.css` | Web UI 樣式。 |

目前 frontend 沒有使用者 feedback 表單；feedback API 已存在，預期由隊友的 App 或後續 UI 呼叫。這不影響 backend 功能。

## 5. 真正的冗餘、可歸檔與不可直接刪除項目

### 5.1 可安全清除的自動產物

- `.pytest_cache/**`
- 所有 `__pycache__/**`
- 所有 `*.pyc`

建議加入 `.gitignore`，至少包含：

```gitignore
__pycache__/
*.py[cod]
.pytest_cache/
agent-runtime/
```

### 5.2 應先修正或隔離，不能繼續當 6.4 驗收依據

- `agent/tests/regression_cases.yaml`
- `agent/tests/run_regression.py`
- `docker/test_system.sh`
- `docker/test_rag.sh` 的 sample capability
- `docs/RAG_ARCHITECTURE.md`
- `docs/BT_ENGINE_INTERFACE.md` 的 v6.3 gripper 段落
- `agent/defaults/rag/patterns/track_dynamic_target.md`

### 5.3 適合移到 `legacy/` 或 `archive/`，但不必立刻刪除

- `skill_registry.demo.yaml` 與依賴它的舊 compiler/validator tests。
- v5.x/v6.0–v6.3 upgrade scripts。
- `enable_demo_skills.sh`、`enable_formal_bt_engine_skills.sh`、`set_scene_location.py`。
- 舊 CHANGELOG、verification reports、`GRIPPER_ABI_V6_3.md`、overlay template。

### 5.4 目前不可刪除

- `agent/app/` 任一完整模組：都有 production import/call path。
- 7 個 custom node contract、13 個 builtin node contract、semantic overlay、policy、merged registry。
- 舊相容 branch：在 demo fixture、LLM normalization 或舊 runtime migration 尚未正式停止前仍有作用。
- `agent-runtime` 內的任何資料：那是 Manta 的 persistent operational state，不是 build cache。

## 6. 建議的 6.4 唯一驗收集合

為避免歷史測試造成誤判，現階段應以這組為主：

```bash
# 1. 靜態 release data
python3 docker/validate_v6_4_release.py

# 2. Python regression（注意其中仍含 legacy fixture tests）
bash docker/test_unit.sh

# 3. live engine contract/auth
bash docker/test_bt_engine_connection.sh

# 4. runtime health：custom=7, builtin=13, effective=20
curl -sS http://127.0.0.1:8000/health | jq '{
  ok,
  version,
  contract_valid: .bt_engine_contract.valid,
  semantic_complete: .bt_engine_contract.semantic_complete,
  custom: .bt_engine_contract.engine_node_count,
  builtin: .bt_engine_contract.builtin_node_count,
  effective: .bt_engine_contract.effective_node_count,
  node_sync_ok: .bt_node_sync.ok,
  rag_ok: .rag.ok
}'

# 5. 現行 continuous search：完整顯示並保存 health/IR/XML/validation/timing
python3 docker/test_continuous_search.py --engine-validate

# 6. typed RAG
python3 docker/validate_typed_rag.py

# 7. 多種輸入 timing profile
bash docker/run_timing_profile.sh 5
```

在修正前，不應用 `docker/test_system.sh` 或 `agent/tests/run_regression.py` 判定 6.4 release 是否成功。

## 7. 此次盤點限制

- 此目錄是 extracted source snapshot，沒有 Git metadata，無法利用 commit history 判斷某檔案最後由哪一版引入或是否曾被 revert；本文以實際 call path 與現有內容為準。
- 本機的 Windows Python launcher 無法執行，WSL 也被環境拒絕，因此此次沒有在本機重跑 pytest。這不改變靜態 import/call-path 判斷；正式動態結果應依上述指令在 Manta 取得。
- Manta 的 `/mlsteam/workspace/agent-runtime` 未包含在此 snapshot。若 Manta runtime 曾經跨版本升級，必須另外比較 `agent-runtime/config` 與本 repo defaults；只看 repo 不能保證實際 prompts/overlay/settings 已升到 6.4。
