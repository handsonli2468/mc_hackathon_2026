# 調參指南：orb_tracker_node

參數檔：`src/object_tracker/config/params.yaml`（改完要重新 `colcon build`；也可以用 `params_file:=<路徑>` 指定別的檔案）

用 `orb_tracker_bringup.launch.py` 啟動時，參數優先順序（高 → 低）：
1. 命令列 `參數:=值`（params.yaml 裡的任何參數都可以，例如 `roi.enable:=false`）
2. params.yaml
3. launch file 的預設值（只有 `target_image_path`、`world_frame`、`init.enable`、`debug.enable`、`debug.img`）
4. 節點程式裡的預設值

用 `ros2 run` 啟動時，改用 `--ros-args -p 參數:=值`。

> 除了 `pose_filter.alpha`、`pose_filter.max_jump_m` 可以用 `ros2 param set /orb_tracker_node <參數> <值>` 即時調整，其他參數都只在啟動時讀取，改了要重啟 node。

---

## 1. 看懂 debug 輸出

需要 `debug.enable:=true`；要看畫面再加 `debug.img:=true`。

### 看 debug 畫面（debug image topic）
debug 畫面**不再開 OpenCV 視窗**，改成發布 topic，所以在沒有 GUI 的 Pi（server mode）上也能開 `debug.img`：

| 參數 | 預設 | 說明 |
|---|---|---|
| `debug.img` | true | 發布 debug 影像；**只有在有人訂閱時才會畫**，沒人看時幾乎不花 CPU |
| `debug.image_topic` | `/tracked_object/debug_image` | 用 image_transport 發布，所以同時有 `/tracked_object/debug_image/compressed`（JPEG，約 40～50 KB/張） |
| `debug.image_rate_hz` | 5.0 | 發布頻率上限；0 = 每一幀都發 |
| `debug.window` | false | 另外開 OpenCV 視窗，**只能在有螢幕的桌機用**，Pi 上不要開 |

從筆電看（和 Pi 同一個 `ROS_DOMAIN_ID`、同一個網段）：
```bash
ros2 run rqt_image_view rqt_image_view /tracked_object/debug_image/compressed
```
- **透過 WiFi 一律看 `/compressed`**。raw 影像一張約 1.2 MB，5 Hz 就要 6 MB/s。
- 兩個 tracker 發布到同一個 topic，不要同時開。
- 看不到 topic 時，先在筆電上執行 `ros2 topic list | grep debug_image`。DDS 在 WiFi 上的 discovery 不一定穩定（見 docs/vlm_transport.md 第 2 節）；真的不行時，在 Pi 上錄 bag，回來再看。

### debug 畫面內容
```
TRACKING OK KLT                                        ← 第 1 行：狀態 + 這一幀的結果 + 來源
m=140  i=138  r=0.99  e=0.40px  s=1.02  pts=138  t=5.7ms   ← 第 2 行：數值（位置固定）
```
| 欄位 | 意思 | 什麼時候有值 |
|---|---|---|
| `m` | ORB：通過 ratio test 的配對數；KLT：通過正反向檢查的追蹤點數 | 有場景特徵就有 |
| `i` | RANSAC inlier 數 | 算出 H 之後 |
| `r` | inlier 比例 = i / m | 算出 H 之後 |
| `e` | inlier 經 H 投影後的平均誤差（px） | 算出 H 之後 |
| `s` | 框的兩組對邊「長邊 / 短邊」取較大者 | 算出四角之後（否則顯示 0.00） |
| `pts` | 目前的 KLT 追蹤點數 | KLT 開啟且有追蹤點 |
| `t` | 從 cvtColor 到得出偵測結果的處理時間 | 這幀有跑偵測 |

來源（第 1 行最後）：`ORB_FULL` 全圖 ORB、`ORB_ROI` 只在上次的框附近跑 ORB、`KLT` optical flow 追蹤。

框的顏色：
- TRACKING 依來源：綠 = ORB_FULL、青 = ORB_ROI、藍 = KLT
- 黃 = HOLDING（最後可信的框）、橘 = CONFIRMING／等待確認的跳動、紅 = LOST 但這幀有框
- 青色細框 = 這幀搜尋的 ROI；藍色小點 = KLT 追蹤點

其他標示：
- 中心點旁的深度：紅字 `Z=0.312m` 是量到的深度；洋紅字 `Z=0.500m FB` 是**預設深度**（只有方向正確，距離是猜的）
- 第 1 行最後出現 `FULL_SEARCH`：HOLDING 已超過 `hold.full_search_after_s`，改跑全圖搜尋（仍在發布沿用的點）

### log
- `Track failed: <STATUS> (matches m, inliers i)`：每秒最多印一次失敗原因。
- `Track stats: N frames, success X% | 各 status 幀數 | state: 各狀態幀數 | source: 各來源幀數 | depth_fb: 使用預設深度的幀數 | time: avg / max`（`init.enable` 時最後再加 `init:` 事件計數和 handoff 平均延遲，見第 8 節）：每 2 秒統計一次，**調參主要看這行**。
- 成功時的 `Target inliers ...` 後面出現 `(depth fallback)`：這幀用的是預設深度。

### 狀態（TrackState）
| 狀態 | 意思 | 是否發布 |
|---|---|---|
| LOST | 沒有可信位置。`hold.timeout_s` ≤ 0（預設）時，只會出現在啟動後還沒追到過的時候；追到過之後只會在 TRACKING、HOLDING 之間切換 | 否 |
| CONFIRMING | 找到候選位置，連續確認中 | 否 |
| TRACKING | 這幀偵測成功，正常輸出 | 是 |
| HOLDING | 這幀失敗或跳動未確認，沿用最後可信位置 | `hold.publish` 為 true 時是 |

### 失敗原因（TrackStatus）→ 先看哪個參數
依處理流程排列；**前面的關卡沒過，後面的參數調了也沒用**。

| Status | 發生在 | 優先檢查 |
|---|---|---|
| `IMAGE_ERROR` | 影像轉換、depth 和 color 尺寸不同 | `align_depth.enable` 是否開啟 |
| `NO_TARGET` | 還沒有 target（`target_image_path` 為空、`init.enable` 開啟，mask 還沒 handoff 成功） | 見第 8 節，確認 mask 有送到 |
| `NO_FEATURES` | 場景抽不到特徵 | 曝光、`orb.fast_threshold` |
| `FEW_MATCHES` | 配對太少 | **target 本身**、距離、曝光、`orb.n_features`、`ratio_test` |
| `NO_HOMOGRAPHY` | 算不出 H | 同上 |
| `FEW_INLIERS` | inlier 太少 | `min_inliers`、`ransac_reproj_thresh` |
| `DEGENERATE_H` | H 鏡像或退化 | 通常是誤配，改善配對品質 |
| `NON_CONVEX` | 框不是凸四邊形 | 通常是誤配 |
| `SMALL_AREA` | 框太小 | `min_area_px` |
| `LOW_INLIER_RATIO` | r 太低 | `gate.min_inlier_ratio` |
| `HIGH_REPROJ_ERROR` | e 太大 | `gate.max_reproj_error_px` |
| `BAD_SHAPE` | s 太大 | `gate.max_side_ratio` |
| `KLT_FEW_POINTS` | KLT 追蹤點太少。KLT 失敗後一定會接著跑 ORB，最終顯示的是 ORB 的結果，所以**目前 log 和視窗都看不到這個值** | `klt.min_points`、`klt.fb_max_px`、`klt.win_size` |
| `CENTER_OUTSIDE` | 中心點在畫面外 | 物件只露出一部分 |
| `NO_DEPTH` | 中心附近沒有有效深度，而且不適用預設深度：窗口內「太近」的值比「太遠」多、`depth_fallback.enable` 關閉，或框面積超過 `depth_fallback.max_box_area_px` | `depth_min_m`／`depth_max_m`、`depth_window`、`depth_fallback.*`、透明或反光物件 |
| `TF_FAIL` | 轉不到 world_frame | 沒有 TF：桌面測試用 `world_frame:=camera_link` |
| `JUMP_UNCONFIRMED` | 位置跳動超過 `max_jump_m`，確認中 | `pose_filter.max_jump_m`、`hold.confirm_frames` |

---

## 2. 調參流程

### Step 0：準備可重播的測資
每次改參數都要重啟 node，用 rosbag 重播才能公平比較。錄影、重播步驟和要錄的情境見 [第 6 節](#6-rosbag-錄製與重播)。

最少先錄這三段：`S1` 靜止正面、`S4` 遮擋、`N1` 沒有目標。

### Step 1：先讓配對出現（目標：`m` 穩定幾十以上）
`m` 只有個位數時，後面的門檻都不用調，先處理這些：
1. 確認 target 和實際物件是**同一個**。
2. target 只框**平面、有紋理**的部分（標籤、印刷圖案）；不要有透明、反光、手、背景。
3. target 在**實際追蹤的距離**拍（ORB 容忍的尺度差距大約 3 倍以內）。
4. 畫面夠亮、不模糊；自動曝光被燈光帶偏時改用手動曝光：
   ```bash
   ros2 param set /camera_duck/camera depth_module.enable_auto_exposure false
   ros2 param set /camera_duck/camera depth_module.exposure 8000
   ```
5. 以上都做了還是不夠，再調第 3 節的「特徵與配對」參數。

### Step 2：全部放寬，建立 baseline
```
hold.enable:=false  min_inliers:=10  gate.min_inlier_ratio:=0.0
gate.max_reproj_error_px:=100.0  gate.max_side_ratio:=100.0
```
分別重播「有目標」和「沒有目標」的錄影，記下：
- **正確的框**：`i`、`r`、`e`、`s` 的範圍（特別是最差的情況）
- **誤判的框**（框在錯的東西上但顯示 OK）：`i`、`r`、`e`、`s` 的範圍

### Step 3：依序收緊門檻
每個門檻都設在「正確值」和「誤判值」之間，並讓正確值保留餘裕。
1. `min_inliers`（通常最能區分）
2. `gate.min_inlier_ratio`
3. `gate.max_reproj_error_px`
4. `gate.max_side_ratio`

每改一個就重播兩段錄影，確認：
- 有目標：success 沒有明顯下降
- 沒有目標：OK 的幀數減少

### Step 4：打開 hold，調輸出穩定度（要有真實的機器人 TF）
`hold.enable:=true`、`world_frame:=map`，調 `hold.confirm_frames` → `hold.timeout_s` → `pose_filter.max_jump_m` → `pose_filter.alpha`。

### 判斷調得好不好（看 `Track stats` 的 state）
- **有目標的錄影**：大部分是 `TRACKING`，少量 `HOLDING`；追到之後不應該出現 `LOST`（`hold.timeout_s` ≤ 0 時）
- **沒有目標的錄影**：從頭開始就沒有目標時，應該停在 `LOST`／`CONFIRMING`；追到後才把物件拿走，會一直是 `HOLDING`。兩種情況**出現 `TRACKING` 就是誤判被接受了**

> 統計以 2 秒為一個區間，不會剛好對齊情境切換，所以同時搭配 debug 畫面觀察。

---

## 3. 參數調整原則

### 3.0 相機與 target（影響最大）
| 項目 | 原則 |
|---|---|
| target 內容 | 平面、紋理豐富；不要透明、反光、手、背景 |
| 拍攝距離 | 和實際追蹤距離相近 |
| 曝光／模糊 | 夠亮、不糊；必要時手動曝光 |
| `camera_profile`（launch） | 目標在畫面裡太小時改 `1280,720,30`，細節更多但較耗 CPU |

### 3.1 特徵與配對（`NO_FEATURES`、`FEW_MATCHES`）
| 參數 | 調整效果 | 副作用 | 什麼時候動 |
|---|---|---|---|
| `orb.n_features` | 調大：target 和 ROI 搜尋保留更多特徵點 | 較慢 | ROI 裡也抓不到點 |
| `orb.n_features_full` | 調大：全圖搜尋（LOST、`FULL_SEARCH`、ROI 失敗後補找）保留更多特徵點 | 全圖那幾幀較慢 | **追丟後找不回來、`m` 個位數**：全圖只保留最強的 n 個點，低對比目標的名額會被雜亂背景搶光。0 = 和 `orb.n_features` 相同 |
| `orb.fast_threshold` | 調小（如 10）：低對比處也有點 | 雜訊點變多 | 畫面或 target 偏暗、對比低 |
| `detect_scale` | < 1：較快 | 小目標特徵變少 | 只在 CPU 不夠時用；FEW_MATCHES 時維持 1.0 |
| `ratio_test` | 調大（0.8～0.85）：配對變多 | 錯誤配對也變多 | 重複紋理被大量濾掉時 |
| `min_matches` | 只是門檻，**調低不會多出配對** | 太低時用很少的點算 H | 維持 ≥ `min_inliers` |

### 3.2 Homography（`NO_HOMOGRAPHY`、`FEW_INLIERS`、`DEGENERATE_H`、`NON_CONVEX`、`SMALL_AREA`）
| 參數 | 原則 | 副作用 |
|---|---|---|
| `ransac_reproj_thresh` | 模糊或輕微非平面時調大（5 → 8） | 錯的 H 也比較容易成立 |
| `min_inliers` | 設在「正確偵測時 `i` 的最小值」略低 | 太低誤判、太高常失敗 |
| `min_area_px` | 遠距離、目標小時調小 | 太小會讓雜訊小框通過 |

### 3.3 嚴格門檻（`LOW_INLIER_RATIO`、`HIGH_REPROJ_ERROR`、`BAD_SHAPE`）
| 參數 | 看哪個值 | 原則 |
|---|---|---|
| `gate.min_inlier_ratio` | `r` | 誤判通常 `r` 很低；背景雜亂時正確偵測的 `r` 也會偏低 |
| `gate.max_reproj_error_px` | `e` | **要小於 `ransac_reproj_thresh` 才有作用**；模糊、非平面、`detect_scale` < 1 都會讓 `e` 變大 |
| `gate.max_side_ratio` | `s` | 讓大角度的正確偵測通過並留餘裕；D405 距離近、透視強，`s` 會比預期大 |

### 3.4 深度（`CENTER_OUTSIDE`、`NO_DEPTH`）
| 參數 | 原則 |
|---|---|
| `depth_min_m`／`depth_max_m` | 涵蓋實際工作距離；D405 大約 0.07～0.5 m |
| `depth_window` | 中心點常落在深度破洞（透明、反光）時調大（如 11） |
| `depth_fallback.enable` | true：窗口內沒有有效深度、而且看起來是**太遠**（太遠的值 ≥ 太近的值，全部是 0／NaN 也算）時，用預設深度反投影，點會落在相機到物件的正確射線方向上 |
| `depth_fallback.value_m` | 預設深度，通常設在 D405 有效範圍的上限附近（0.5）。機器人靠近後會換成真實深度；兩者差距超過 `pose_filter.max_jump_m` 時要經過跳動確認 |
| `depth_fallback.max_box_area_px` | > 0 時，只有框面積 ≤ 這個值才用預設深度（遠的物件看起來小）。近距離的透明、反光物件也會沒有深度，用這個參數避免把它們放到 0.5 m。0 = 不檢查 |

### 3.5 輸出穩定度（hold 狀態機）
| 參數 | 調大 | 調小 |
|---|---|---|
| `hold.confirm_frames` | 誤判更難被確認；鎖定和跟上跳動較慢（30 fps 時 3 幀 ≈ 0.1 s） | 反應快，但短暫誤判可能被確認 |
| `hold.timeout_s` | **≤ 0（預設）= 永不逾時**，追到過就一直沿用最後可信點。> 0 時，超過這個時間進入 LOST 並停止發布（設回 0.5 = 舊行為） | 越小越容易掉到 LOST |
| `hold.full_search_after_s` | HOLDING 較久才放棄 ROI／KLT；物件移到別處時較晚找回 | 較早改跑全圖；較慢，但物件換位置時找得回來。輸出不受影響 |
| `pose_filter.max_jump_m`（可即時調） | 正常移動不會被當跳動；附近的誤判會直接被接受 | 正常移動被當跳動，輸出卡在 HOLDING |
| `pose_filter.alpha`（可即時調） | 跟得快但抖 | 穩但延遲 |
| `hold.publish` | true：HOLDING 時發布舊點 | false：只在 TRACKING 時發布 |

設定建議：
- `hold.confirm_frames`：大於 baseline 中「誤判連續出現」的最長幀數。
- `hold.timeout_s`：預設 0（永不逾時）。需要「追丟一段時間就停止發布」時才設成 > 0，並略大於「有目標時連續失敗」的一般長度。
- `hold.full_search_after_s`：略大於短暫遮擋、模糊的一般長度，讓短暫失敗時還能用 ROI／KLT 快速接回。
- `pose_filter.max_jump_m`：約為「物件每幀最大移動量 + 深度雜訊 + TF 誤差」的幾倍。
- HOLDING 發布的是 `map` 座標，機器人移動時舊點仍然有效，但**前提是物件本身沒有移動**。

### 3.6 ROI 搜尋與 KLT（速度、平滑度）
每幀的順序：狀態不是 LOST 時先跑 KLT → KLT 失敗、沒跑，或距離上次 ORB 成功已達 `klt.redetect_interval` 幀時跑 ORB（先 ROI，失敗再依 `roi.full_frame_fallback` 跑全圖）→ ORB 成功就採用 ORB 並重新播種追蹤點；ORB 失敗但 KLT 成功就採用 KLT。LOST，或 HOLDING 超過 `hold.full_search_after_s` 時，清空追蹤點和上次的框，一律全圖搜尋。

A/B 比較時把 `roi.enable:=false klt.enable:=false`，偵測流程等同加入 ROI/KLT 之前。

| 參數 | 調大 | 調小 |
|---|---|---|
| `roi.expand_ratio` | 物件移動快也在 ROI 內；速度優勢變小 | 更快、更少背景誤配；移動快時容易跑出 ROI |
| `roi.min_size_px` | 小目標也有足夠搜尋範圍 | 更快 |
| `roi.full_frame_fallback` | true：ROI 失敗同一幀就全圖補找（該幀較慢） | false：ROI 失敗就等下一幀 |
| `klt.redetect_interval` | ORB 跑得少、更快；KLT 漂移沒被發現的時間變長 | 更常用 ORB 校正；較慢 |
| `klt.min_points` | 點太少時就放棄 KLT，較保守 | 點少也繼續追，容易漂移 |
| `klt.max_points` | 點多、H 較穩；較慢 | 較快；遮擋時容易點數不足 |
| `klt.extra_corners` | true：框內補角點（假設平面，非平面物件會帶偏 H） | false：只用 ORB inlier |
| `klt.fb_max_px` | 保留更多點；錯誤的點也比較容易留下 | 只留很可靠的點；模糊時點數掉很快 |
| `klt.win_size`、`klt.max_level` | 可追更大的移動、更耐模糊；較慢、邊界較不精準 | 較快、較精準；快速移動會追丟 |

判斷方式（看 `Track stats`）：
- `time` 的 avg 有沒有下降：ROI 和 KLT 的主要目的就是變快。
- `source` 中 `KLT` 佔大部分、`ORB_ROI` 大約每 `klt.redetect_interval` 幀出現一次，是正常狀態。
- `ORB_FULL` 很多，但狀態不是 LOST，畫面也沒有 `FULL_SEARCH`：ROI 常找不到，檢查 `roi.expand_ratio` 是否太小。
- **沒有目標的錄影也出現 KLT 的 TRACKING**：KLT 黏在錯的東西上，調小 `klt.redetect_interval`（見第 7 節的限制）。

---

## 4. 常見症狀速查

| 症狀 | 原因與處理 |
|---|---|
| `FEW_MATCHES`，`m` 個位數 | target 用錯檔、物件不一致、target 太小或透明、距離差太多、太暗 → 回 Step 1 |
| `FEW_INLIERS`，`i` 在 `min_inliers` 附近跳動 | 門檻卡在邊緣 → 暫時調低 `min_inliers`，同時想辦法增加 `m` |
| `r` 很高、`e` 很小，但還是 `TF_FAIL` | 偵測其實成功，是 TF 問題 → 桌面測試用 `world_frame:=camera_link` |
| 大角度就 `BAD_SHAPE` | 調大 `gate.max_side_ratio` |
| 模糊時 `HIGH_REPROJ_ERROR` | 先改善曝光和模糊，再放寬 `gate.max_reproj_error_px` |
| 框得到但 `NO_DEPTH` | 距離太近、透明或反光物件，或預設深度沒有啟用／框太大 → 調 `depth_*`、`depth_window`、`depth_fallback.*` |
| 輸出的 z 固定在 0.5 附近、畫面顯示 `FB` | 物件太遠，正在使用預設深度；靠近到 50 cm 內就會換成真實深度 |
| 沒有目標也出現 `TRACKING` | 誤判被接受 → 收緊能區分真假的門檻，或調大 `hold.confirm_frames` |
| 輸出一直卡在 `HOLDING`、`JUMP_UNCONFIRMED` | 物件真的在移動 → 調大 `pose_filter.max_jump_m` |
| 常常掉到 `LOST` 又重新 `CONFIRMING` | 只會在 `hold.timeout_s` > 0 時發生 → 設回 0 或調大，並找出失敗的主因（看 Track stats） |
| 物件拿走了還一直發布同一個點 | `hold.timeout_s` ≤ 0 的預期行為：沒有新的可信偵測前會一直沿用 |

---

## 6. rosbag 錄製與重播

### 6.1 原則
- **只錄輸入，不錄輸出**：只錄影像、camera_info、TF。tracker 的結果在重播時重新計算，才能比較不同版本和參數。
- **錄影時不要開 `orb_tracker_node`**：`debug.enable:=true` 會把 `tracked_object` 發到 `/tf`，這筆資料會被錄進 bag。重播時就會和新開的 node 發出的 `tracked_object` 互相衝突。要看畫面改用 `frame_capture_node`，或錄影時不訂閱 debug 影像。
- **在接相機的那台電腦上錄**，不要透過 WiFi 錄，否則會掉幀。
- **解析度和 fps 要和實際執行時相同**：launch 預設 `camera_profile` 是 `848,480,30`。
- **每段 20～30 秒，一段只測一件事**，之後比較時比較好判斷是哪個情境出問題。

### 6.2 資料量估計
| 資料 | 每幀大小 |
|---|---|
| color：848×480×3（BGR） | 約 1.22 MB |
| aligned depth：848×480×2（16UC1） | 約 0.68 MB |
| **合計** | 約 1.9 MB/幀，30 fps 時約 **57 MB/s** |

換算下來，20 秒大約 1.1 GB，1 分鐘大約 3.4 GB。錄之前先用 `df -h` 確認磁碟空間。

錄影時**不開壓縮**，避免 CPU 跟不上而掉幀。壓縮率沒有實測過。

### 6.3 錄製步驟
bag 放在 workspace 根目錄的 `bags/`，不要放進 `src/`。container 內的路徑是 `/home/vision/vision_ws/bags`，host 端也看得到。

```bash
# T1：只開相機（參數和 orb_tracker_bringup.launch.py 相同）
ros2 launch realsense2_camera rs_launch.py \
  align_depth.enable:=true enable_sync:=true \
  depth_module.color_profile:=848,480,30 depth_module.depth_profile:=848,480,30

# T2（選用）：看畫面，也可以在同樣的光線下先擷取 target
ros2 run object_tracker frame_capture_node --ros-args -p save_dir:=src/object_tracker/targets

# T3：錄影，按 Ctrl+C 停止
mkdir -p bags
ros2 bag record -o bags/20260917_bottle_S4_occlusion \
  /camera_duck/camera/color/image_rect_raw \
  /camera_duck/camera/color/camera_info \
  /camera_duck/camera/aligned_depth_to_color/image_raw \
  /camera_duck/camera/aligned_depth_to_color/camera_info \
  /tf /tf_static
```

- 要測 `world_frame:=map` 時，錄影的同時機器人端必須在發布 TF。桌面測試沒有機器人也可以錄，重播時改用 `world_frame:=camera_link`。
- RealSense 預設的 `tf_publish_rate` 是 0，相機內部的 TF（`camera_link` → optical frame）只會出現在 `/tf_static`。

**錄完一定要檢查：**
```bash
ros2 bag info bags/20260917_bottle_S4_occlusion
```
| 項目 | 正常情況 |
|---|---|
| color、depth 的 Count | 約等於 30 × Duration（秒），明顯偏少就是掉幀 |
| `/tf_static` 的 Count | ≥ 1，沒有的話重播會出現 `TF_FAIL` |
| `/tf` 的 Count | 要用 `map` 時必須 > 0 |

掉幀時可以試：關掉預覽、縮短每段長度，或確認磁碟寫入速度夠快。

### 6.4 命名與紀錄
- **檔名**：`<日期>_<物件>_<情境編號>_<簡述>`，例如 `20260917_bottle_S4_occlusion`。
- **紀錄檔**：在 `bags/NOTES.md` 記下每段 bag 的資訊。**事件發生的時間點**就是評估時的標準答案，例如「8 秒手遮住、11 秒移開」，錄的時候順手記最準。

```markdown
| 檔名 | target | 距離 | 光線 | world_frame | 事件時間點 | 備註 |
|---|---|---|---|---|---|---|
| 20260917_bottle_S4_occlusion | bottle.png | ~25 cm | 室內燈 | camera_link | 8s 手半遮、11s 全遮、14s 移開 | |
```

### 6.5 情境清單
| 編號 | 情境 | 怎麼錄 | 驗證什麼 |
|---|---|---|---|
| **S1** | 靜止正面 | 相機和物件都不動 | 中心點抖動、baseline 成功率 |
| S2 | 距離變化 | 從約 10 cm 慢慢移到約 45 cm，再移回來 | 可追蹤的尺度範圍、深度範圍 |
| S3 | 角度變化 | 從正面慢慢轉到追不到為止，再轉回來 | 最大角度、`gate.max_side_ratio` |
| **S4** | 遮擋 | 手遮一半 2 秒 → 全遮 2 秒 → 移開 | `HOLDING`、`hold.full_search_after_s` |
| S5 | 快速移動 | 快速平移、晃動 | 模糊時的表現（之後拿來評估 KLT） |
| S6 | 出畫面再回來 | 物件移出畫面幾秒後再回來 | `HOLDING`（`FULL_SEARCH`）→ `TRACKING`；位置差超過 `max_jump_m` 時中間會經過 `JUMP_UNCONFIRMED` |
| S7 | 機器人移動、物件不動 | 需要機器人 TF | `map` 座標是否穩定、HOLDING 時舊點是否仍正確 |
| **N1** | 沒有目標 | 同一個場景，把物件拿走 | 誤判：不應該出現 `TRACKING` |
| N2 | 相似物件、雜亂背景 | 放外觀相近的物件，或換到雜亂的桌面 | 誤判 |

- 粗體的三段（S1、S4、N1）最優先。
- 每個 target 至少錄 S1、S3、S4、N1。

### 6.6 重播步驟
```bash
# T1：先開 node（使用 sim time）
ros2 run object_tracker orb_tracker_node --ros-args \
  --params-file install/object_tracker/share/object_tracker/config/params.yaml \
  -p use_sim_time:=true \
  -p target_image_path:=bottle.png -p world_frame:=camera_link \
  -p debug.enable:=true -p debug.img:=true

# T2：node 起來之後再重播
ros2 bag play bags/20260917_bottle_S4_occlusion --clock -r 0.5
```

各項設定的原因：

| 做法 | 原因 |
|---|---|
| 用 `ros2 run`，不用 launch | 目前 launch 檔沒有 `use_sim_time` 參數，而且會一起開相機 |
| `-p` 放在 `--params-file` 後面 | 後面的設定會覆蓋前面的 |
| `use_sim_time:=true` + `--clock` | TF 查詢會使用 bag 裡的時間 |
| **先開 node，再 play** | Humble 重播 `/tf_static` 時，晚訂閱的 node 可能收不到全部的 static TF（這是 Humble 已知的狀況，還沒在這台機器實測） |
| **不要用 `--loop`，每次重播都重啟 node** | hold 狀態機用影像 stamp 計時，bag 從頭開始時 stamp 會倒退，狀態會延續上一輪的軌跡。TF buffer 也可能因為時間倒退丟掉資料 |
| `-r 0.5` | 處理速度跟不上 30 fps 時，message_filters 會丟幀，每次丟的幀不一樣，比較就不公平。處理夠快的話可以拿掉 |

`Track stats` 是以實際經過的 2 秒為一個區間。用 `-r 0.5` 重播時，每個區間大約對應 bag 裡的 1 秒。

### 6.7 比較不同版本或參數
1. 同一段 bag，每種設定都重啟 node 各重播一次。
2. 把 log 存起來。ROS 2 log 預設輸出到 stderr，所以要加 `2>&1`：
   ```bash
   mkdir -p bags/results
   ros2 run object_tracker orb_tracker_node --ros-args ... 2>&1 | tee bags/results/20260917_bottle_S4_occlusion__min_inliers20.txt
   ```
3. 比較方式：
   - 對照 `NOTES.md` 裡的事件時間點，看各區間的 `Track stats`（`grep "Track stats"`）。
   - 有目標的區間：看 `TRACKING` 的比例。
   - N1、N2：看有沒有出現 `TRACKING`。

目前只能手動對照。等每幀 CSV 功能完成後，就可以直接算成功率和抖動。

---

## 7. 限制
- ORB + homography 假設物件是**平面、有紋理**。透明、圓柱、純色、反光的物件，調參數能改善的有限。
- `Track stats` 只有 2 秒的計數，沒有每幀數值的分佈；更精確的統計要等每幀 CSV 功能。
- KLT 的結果通過 gate 就會被採用，但 KLT 自己算出的 `r`、`e` 天生就好看（點是從上一幀追來的），**gate 很難擋住 KLT 漂移**。ORB 連續失敗時會一直沿用 KLT，沒有上限。
- `hold.timeout_s` ≤ 0（永不逾時）時，**物件被拿走或移走之後，仍會一直發布最後可信的舊點**，直到有新的可信偵測為止。下游不能用「有沒有收到點」判斷物件還在不在，要看 debug 的狀態。
- 預設深度（`depth_fallback`）**只有方向正確，距離不正確**：點落在相機到物件的射線上，但 z 固定是 `depth_fallback.value_m`。另外，近距離的透明、反光物件如果整個窗口都沒有深度，也會被當成「太遠」而使用預設深度，可以用 `depth_fallback.max_box_area_px` 排除。
- 用 mask 建 target 時，框的四角是 mask 的**外接矩形**，發布的點是 mask 的**質心**（不在 mask 內時改用最近的 mask 像素）。物件不是平面時，這兩者經 homography 投影後的位置會有偏差。
- 幀快取只保存 tracker **實際處理過**的幀。處理跟不上而掉幀時，mask 對應的那一幀可能不在快取裡，會出現 `MASK_FRAME_MISSING`。

---

## 8. 模擬上游 mask 輸入測試

### 8.1 用途
最終的流程是 server 跑 VLM 後回傳目標物的 mask，client 用 mask 建立 target。VLM 還沒完成之前，先用 `mask_init_tool` 手動框選來模擬 mask，測試「用 mask 建 target、處理時間差、切換 target」的流程。

和 file 模式（`target_image_path`）的差異：

| | file 模式 | mask 模式（`init.enable:=true`） |
|---|---|---|
| target 來源 | 啟動時讀取一張圖 | 收到 mask 後，從**過去那一幀**（快取）裁出物件 |
| 模型座標 | target 圖的像素 | mask 所屬那一幀的像素 |
| 發布的點 | target 圖的中心 | mask 的質心 |
| 切換 target | 要重啟 node | 收到新 mask，在目前這一幀找到後就切換（handoff） |
| 額外成本 | 無 | 幀快取（848×480 約 1.2 MB/幀）、待 handoff 期間每幀多跑一次全圖 ORB |

Mask 介面（之後的 VLM bridge 也用這個）：
- topic `mask_topic`（預設 `/tracked_object/init_mask`），`sensor_msgs/Image`，mono8，大小和 color 相同，非 0 = 物件
- `header.stamp` = **這個 mask 對應的 color 幀的 stamp**，不是發布時間
- QoS：reliable，depth 5

收到 mask 後的流程：
1. 依 stamp 從快取找到那一幀（誤差 ≤ `init.stamp_tolerance_s`）
2. 用深度清理 mask（和中位數差超過 `init.depth_gate_m` 的像素移除；mask 內沒有有效深度時略過），保留最大的連通區域
3. mask 內縮 `init.mask_erode_px` 後抽 ORB 特徵，建立候選模型
4. 待 handoff：每一幀用候選模型跑全圖 ORB，成功就切換，直接進入 TRACKING（跳過 CONFIRMING 和跳動確認）；超過 `init.handoff_timeout_s` 就放棄（詳見 8.6 節）

### 8.2 即時相機測試
```bash
ros2 launch object_tracker orb_tracker_bringup.launch.py \
  init.enable:=true launch_init_tool:=true target_image_path:="''" \
  world_frame:=camera_color_optical_frame debug.img:=true delay_s:=1.5
```
- `target_image_path` 要寫成 `"''"`（兩個引號字元）。`ros2 launch` 不接受 `target_image_path:=` 這種空值。
- `delay_s:=`、`cache_s:=`、`use_grabcut:=`、`grabcut_iters:=` 會轉給 `mask_init_tool`。
- 工具視窗按 `s`：凍結目前畫面並框選物件（Enter 確認），GrabCut 產生 mask，等到「那一幀的 stamp + `delay_s`」才發布。
- 單獨啟動工具：`ros2 run object_tracker mask_init_tool --ros-args -p delay_s:=1.5`

### 8.3 rosbag 重播測試
```bash
# T1：先開 tracker 和工具（都用 sim time，不開相機）
ros2 launch object_tracker orb_tracker_bringup.launch.py \
  launch_camera:=false use_sim_time:=true \
  init.enable:=true launch_init_tool:=true target_image_path:="''" \
  world_frame:=camera_color_optical_frame debug.img:=true

# T2：node 起來之後再播
ros2 bag play bags/<bag 名稱> --clock -r 0.5
```
- 工具的發布時機用 node clock（sim time）計算，所以延遲是以 bag 的時間為準。
- **框選時 bag 仍在播放**，框選本身花的時間也會算進延遲。實際延遲以工具印出的 `actual_delay` 為準；框選時間超過 `delay_s` 時，框完會立刻發布。
- 用 `ros2 run` 啟動 tracker 時，空字串要寫成 `-p "target_image_path:=''"`。寫成 `-p target_image_path:=` 會讓 rcl 無法解析參數，node 直接 abort。

### 8.4 測試情境

| 編號 | 情境 | 預期結果 |
|---|---|---|
| M1 | 啟動時沒有 target，物件靜止，`delay_s:=0` | 很快 `HANDOFF_OK`，然後 TRACKING |
| M2 | 物件靜止，`delay_s:=1.5` | `HANDOFF_OK`，延遲約 1.5 s |
| M3 | `delay_s:=1.5`，框選後立刻移動相機或物件 | 仍能 `HANDOFF_OK`；物件移出畫面時 `HANDOFF_TIMEOUT` |
| M4 | 追蹤 A 物件時，框選 B 物件 | 切換到 B，輸出點跳到 B，而且不經過 `JUMP_UNCONFIRMED` |
| M5 | 框選素面、沒有紋理的區域 | `MASK_FEW_FEATURES`，保留原本的模型 |
| M6 | `delay_s` 設得比 `init.cache_s` 大 | `MASK_FRAME_MISSING` |
| M7 | 物件在 50 cm 外 | mask 清理略過深度步驟（log：`depth_gate=skipped`），handoff 後使用 depth fallback |

### 8.5 要看的數值
- **事件 log**（每次都印）：`Init event <名稱>: mask_stamp=... <細節>`，另外有 `Mask cleaned`（像素數、深度清理結果）和 `Mask target candidate`（特徵數）。

  | 事件 | 意思 |
  |---|---|
  | `MASK_RECEIVED` | 收到 mask |
  | `MASK_FRAME_MISSING` | 快取裡找不到對應的幀（延遲太長、stamp 不對，或那一幀被 tracker 丟掉） |
  | `MASK_BAD_SIZE` | mask 大小和影像不同 |
  | `MASK_TOO_SMALL` | 清理後最大連通區域 < `init.min_mask_px` |
  | `MASK_FEW_FEATURES` | mask 內特徵 < `min_matches`，保留原本的模型 |
  | `HANDOFF_OK` | 在目前這一幀找到候選模型並切換，附延遲（這一幀 stamp − mask stamp） |
  | `HANDOFF_TIMEOUT` | 超過 `init.handoff_timeout_s` 都沒找到，丟棄候選模型 |
  | `HANDOFF_REPLACED` | 待 handoff 時收到新 mask，改用新的候選模型 |

- **`Track stats` 最後的 `init:`**：這 2 秒內的事件計數，以及 `handoff_latency_avg`。
- **處理時間**：比較待 handoff 期間（debug 畫面右上角顯示 `HANDOFF x.xs`）和平常的 `time: avg / max`，就是多跑一次全圖 ORB 的成本。
- **debug 畫面**：第 1 行最後是目前模型的來源（`file`／`mask`／`none`）；handoff 成功後 1 秒內右上角顯示 `NEW TARGET`。

### 8.6 什麼時候會進 HANDOFF
`HANDOFF` 不是 TrackState，是「**有一個候選模型，正在等它在目前這一幀被找到**」的等待期。debug 畫面右上角的 `HANDOFF x.xs` 就是等了多久。兩個 tracker（`orb_tracker_node` 的 `init.enable:=true`、`mask_tracker_node`）的規則相同。

**進入條件**：每收到一個 mask（`mask_init_tool` 或 VLM bridge 發的），依序檢查，**全部通過才會進 HANDOFF**：

| 步驟 | 通過條件 | 失敗時的事件 |
|---|---|---|
| 1. 找對應的幀 | 快取裡有 stamp 差 ≤ `init.stamp_tolerance_s` 的幀 | `MASK_FRAME_MISSING` |
| 2. 檢查大小 | mask 大小 = color 大小 | `MASK_BAD_SIZE` |
| 3. 深度清理 | 清理後最大連通區域 ≥ `init.min_mask_px` | `MASK_TOO_SMALL` |
| 4. 抽特徵 | 內縮 `init.mask_erode_px` 後的特徵數 ≥ `min_matches` | `MASK_FEW_FEATURES` |

任何一步失敗都不會進 HANDOFF，**目前的模型照常追蹤**。通過後 log 會印 `Mask target candidate`（orb）或 `Mask model candidate`（mask），接著就進入 HANDOFF。

- **和目前在不在追蹤無關**：TRACKING 中收到 mask 一樣會進 HANDOFF。接 VLM bridge 時，每次 `FOUND` 都會觸發一次，預設約每 `vlm.refresh_period_s`（8 秒）一次，**即使框的是同一個物件**。
- 已經在 HANDOFF 時又收到新 mask：舊候選直接丟掉，改等新的（`HANDOFF_REPLACED`），計時重新開始。

**HANDOFF 期間每一幀**：舊模型照常追蹤、照常發布點；另外用候選模型在**整張圖**跑一次 ORB，門檻和一般偵測相同。

**離開條件**（三選一）：

| 結果 | 條件 | 之後 |
|---|---|---|
| `HANDOFF_OK` | 候選模型在這一幀通過所有門檻 | 換成新模型，KLT 從這一幀重新開始。這一幀有 3D 點就直接進 TRACKING（跳過 CONFIRMING 和跳動確認）；沒有 3D 點（`NO_DEPTH`、`TF_FAIL`）就進 LOST，因為舊的 hold 點屬於舊物件 |
| `HANDOFF_TIMEOUT` | 超過 `init.handoff_timeout_s`（預設 2 秒）都沒找到 | 丟掉候選，**舊模型繼續用**；沒有舊模型就維持 `NO_TARGET`／`NO_MODEL` |
| `HANDOFF_REPLACED` | 等待中收到新的 mask | 改等新候選 |

- 逾時是用影像的 stamp 計算，從「收到 mask 當下快取裡最新那一幀」開始算，不是從 mask 的 stamp 開始算。所以 VLM 的推論時間（約 2～3.4 秒）不會吃掉這 2 秒。
- **常見的 `HANDOFF_TIMEOUT` 原因**：
  - 物件在 VLM 推論期間移出畫面，或被擋住。
  - 視角或距離和送出去那一幀差太多，配對不到。
  - mask 內特徵剛好超過 `min_matches`，但在新的一幀上通不過 inlier 門檻（看逾時訊息裡的 `last status`）。

**成本**：HANDOFF 期間每幀多跑一次全圖 ORB（`orb.n_features_full`），最多持續 `init.handoff_timeout_s`。Pi 5 上多出多少處理時間**還沒量過**：比較 `Track stats` 在 HANDOFF 期間和平常的 `time: avg / max` 就知道。

---

## 9. mask_tracker_node

### 9.1 用途和 orb_tracker_node 的差異
另一套**並存**的追蹤 node，針對**非平面**的目標（紙杯、小包餅乾）。VLM 回傳 mask 後建立模型，追住兩次 VLM 回應之間約 1～2 秒的空檔。

| | orb_tracker_node | mask_tracker_node |
|---|---|---|
| target 來源 | 圖片檔，或 mask（`init.enable`） | **只接受 mask**，啟動時沒有模型（`NO_MODEL`） |
| 幾何模型 | homography（假設平面） | similarity（平移、旋轉、等比縮放），**只用來算區域、ROI、剔除離群點** |
| 主要追蹤 | ORB，KLT 補空檔 | **KLT 為主**，點太少才在 ROI 跑 ORB |
| 輸出點 | 模型中心經 H 投影後取 3×3 深度 | **mask 幾何中心經 similarity 投影**，深度來自物件表面（見 9.7） |
| 深度剔除 | 無 | KLT 點的深度和物件參考深度 `d_obj` 差超過 `depth_gate_m` 就丟掉 |
| 沒有搜尋錨點時 | 每幀全圖 ORB | 每 `search.full_interval` 幀全圖 ORB，其他幀記 `SEARCH_IDLE` |
| 參數檔 | `config/params.yaml` | `config/mask_tracker_params.yaml` |
| debug 影像 | `/tracked_object/debug_image`（`debug.window` 時視窗名 `ORB Tracker`） | 同一個 topic（視窗名 `Mask Tracker`） |

**搜尋錨點**：state 是 TRACKING，或是 HOLDING 且距離最後可信 stamp ≤ `search.full_after_s`。沒有錨點時清空 KLT 點和上一次的物件區域（debug 第 1 行顯示 `FULL_SEARCH`），但**保留模型和輸出**。CONFIRMING 和 LOST 也沒有錨點。

**和 orb_tracker_node 狀態機的唯一差異**：`SEARCH_IDLE` 幀沒有搜尋，所以不會打斷 CONFIRMING 或跳動確認的連續計數。否則「每 N 幀才搜尋一次」時，永遠無法連續確認。

### 9.2 新的 status

| Status | 發生在 | 優先檢查 |
|---|---|---|
| `NO_MODEL` | 還沒收到 mask 或還沒 handoff 成功 | mask 有沒有送到、事件 log |
| `KLT_FEW_POINTS` | KLT 正反向檢查、深度剔除、區域剔除後剩下的點 < `klt.min_points`（之後會改跑 ORB，最終顯示 ORB 的結果） | `klt.fb_max_px`、`depth_gate_m`、`klt.region_margin_px` |
| `NO_TRANSFORM` | 算不出 similarity | 配對太少或全是雜訊 |
| `BAD_SCALE` | similarity 縮放超出 [1/`gate.max_scale_ratio`, `gate.max_scale_ratio`]，或 KLT 相鄰成功幀的縮放變化 > `gate.max_scale_change` | 相機快速靠近時放寬 `gate.max_scale_change` |
| `SEARCH_IDLE` | 沒有搜尋錨點，而且這幀不是 `search.full_interval` 的搜尋幀 | 屬於正常狀態，不算失敗；太多時縮小 `search.full_interval` |

沒有 `NO_HOMOGRAPHY`、`DEGENERATE_H`、`NON_CONVEX`、`SMALL_AREA`、`BAD_SHAPE`、`CENTER_OUTSIDE`。

### 9.3 和 orb_tracker_node 不同的參數

影像特徵相關的數值（ORB、配對、gate、ROI、KLT 點數與 LK 設定、`init.*`）和 `params.yaml` 保持一致，以已經在實機調過的 orb_tracker_node 為準。調整其中一邊時，記得兩個檔案一起改。

| 參數 | 預設 | 說明 |
|---|---|---|
| `orb.n_features_model` | 1000 | mask 內抽的特徵數上限（orb_tracker_node 用 `orb.n_features`） |
| `orb.n_features` / `orb.n_features_full` | 1000 / 5000 | ROI / 全圖搜尋（handoff、沒有錨點時） |
| `ratio_test` / `min_matches` / `min_inliers` | 0.75 / 15 / 10 | 和 orb_tracker_node 相同；`min_matches` 同時是 mask 內特徵數的下限 |
| `ransac_reproj_thresh` | 5.0 | similarity RANSAC 門檻（px） |
| `gate.max_scale_ratio` | 3.0 | similarity 縮放範圍 |
| `gate.max_scale_change` | 1.2 | KLT 相鄰成功幀的縮放變化上限 |
| `depth_gate_m` | 0.04 | mask 清理和 KLT 深度剔除共用 |
| `min_depth_points` | 5 | 有真實深度的 inlier 點少於這個數就判斷是否用預設深度 |
| `output.center_mode` | `mask_center` | 輸出點的位置：`mask_center` = mask 幾何中心；`features` = inlier 點的中位數（舊做法），見 9.7 |
| `output.center_depth_window` | 5 | 在中心點量深度的窗口大小（最大 7） |
| `depth_fallback.max_region_area_px` | 0.0 | 用物件區域面積（mask 多邊形投影）判斷，取代 `max_box_area_px` |
| `klt.max_points` / `klt.min_points` / `klt.refill_below` | 150 / 12 / 40 | 點數上限、低於下限改跑 ORB、低於補點門檻就補點 |
| `klt.region_margin_px` | 10 | KLT 點可以超出投影後 mask 多邊形的距離 |
| `search.full_after_s` | 0.5 | HOLDING 超過這個時間就沒有搜尋錨點 |
| `search.full_interval` | 5 | 沒有錨點時，每幾幀跑一次全圖 ORB |

沒有 `target_image_path`、`init.enable`、`roi.enable`、`klt.enable`、`klt.redetect_interval`、`hold.full_search_after_s`（改成 `search.full_after_s`）、`gate.max_side_ratio`、`min_area_px`、`detect_scale`。

debug 畫面：
- 物件區域是 mask 多邊形經 similarity 投影後的形狀；顏色規則和 orb_tracker_node 相同。
- KLT 點：綠 = inlier、紅 = 被深度剔除、橘 = 被區域剔除。
- 實心圓是 3D 輸出點投影回影像的位置（`mask_center` 模式下就是 mask 的幾何中心）；`Z=0.500m FB`（洋紅）表示預設深度。
- 第 2 行：`pts`（KLT 點數）、`inl`、`r`、`e`、`sc`（similarity 縮放）、`d`（`d_obj`）、`t`。

### 9.4 用 mask_init_tool 搭配 bag 重播測試
```bash
# T1：先開 tracker 和工具（sim time，不開相機）
ros2 launch object_tracker mask_tracker_bringup.launch.py \
  launch_camera:=false use_sim_time:=true launch_init_tool:=true \
  world_frame:=camera_color_optical_frame debug.img:=true delay_s:=1.5

# T2：node 起來之後再播
ros2 bag play bags/<bag 名稱> --clock -r 0.5
```
- 工具視窗按 `s` 框選物件，等 `delay_s` 後發布 mask。
- 框選時 bag 仍在播放，實際延遲以工具印出的 `actual_delay` 為準。
- 即時相機：拿掉 `launch_camera:=false use_sim_time:=true`，其他相同。
- 用 `ros2 run` 單獨開 tracker：`ros2 run object_tracker mask_tracker_node --ros-args --params-file install/object_tracker/share/object_tracker/config/mask_tracker_params.yaml -p use_sim_time:=true -p debug.img:=true`

### 9.5 測試情境
第 8.4 節的 M1～M7 都適用（M1 改成「啟動後第一次送 mask」，因為這個 node 本來就沒有初始 target）。另外加上非平面物件：

| 物件 | S1 靜止 | S3 角度（慢慢轉到追不到） | S4 遮擋（手遮一半 → 全遮 → 移開） |
|---|---|---|---|
| 紙杯（素面） | | | |
| 紙杯（印花） | | | |
| 小包餅乾 | | | |

每格記錄：success、HOLDING 比例、`depth_fb`、KLT 點數（`pts`）範圍、是否出現誤追（框在錯的東西上但 TRACKING）。

預期要特別注意的地方：
- **素面紙杯**：mask 內特徵很少，可能直接 `MASK_FEW_FEATURES`；即使 handoff 成功，ORB 重新偵測也可能一直 `FEW_MATCHES`，只能靠 KLT 撐。
- **S3 角度**：similarity 無法描述轉動後的形狀，區域剔除（`klt.region_margin_px`）可能丟掉太多點。
- **S4 遮擋**：手的深度和物件接近時，深度剔除擋不住手上的點。

### 9.6 和 orb_tracker_node 比較
1. 錄一段 bag（第 6 節），在 NOTES.md 記下要送 mask 的時間點。
2. 分別用兩個 launch 重播同一段 bag，**在同一個時間點框選同一個物件**（orb_tracker_node 要加 `init.enable:=true target_image_path:="''"`）。
   手動框選的時間點不會完全一樣；要精確比較時，把工具發布的 mask 錄進 bag（`/tracked_object/init_mask`），重播時就不需要開工具。
3. 兩次的 log 各自存檔，比較 handoff 之後各區間的 `Track stats`：
   - success（OK 的比例）
   - HOLDING 比例（越低代表越少靠沿用舊點）
   - `depth_fb`（使用預設深度的幀數）
   - `time: avg / max`

### 9.7 輸出點：幾何中心
以前的做法是把 inlier 特徵點各自反投影成 3D，再分別取 x/y/z 的中位數。**特徵集中在物件一側時**（例如紙杯只有 logo 那側有紋理），輸出點會偏到那一側，不在物件的中軸上。

現在預設 `output.center_mode: mask_center`：
- **位置**：建模型時算出 mask 的質心；質心不在 mask 內時（C 形物件），改用最近的 mask 像素。之後每一幀用 similarity 把它搬到目前的畫面，所以中心會跟著物件平移、旋轉和縮放，和紋理分布無關。
- **深度**：
  1. 先量中心點附近（`output.center_depth_window`）的深度。和 inlier 深度中位數的差距 ≤ `depth_gate_m` 就用它，量到的是物件正面中央的表面。
  2. 否則用 inlier 深度中位數。中心剛好落在深度破洞上，或者落到背景（例如杯口）時會走這條。
- **depth fallback** 也沿著中心點的方向。
- `d_obj`（KLT 深度剔除的參考深度）一樣用 inlier 深度中位數，和以前相同。

人工測資驗證：200×200 的方塊，只有左半邊有紋理，中心在 (400, 240)。

| 模式 | 輸出點換算回像素 |
|---|---|
| `mask_center` | (400, 240)，方塊中心 |
| `features` | (350, 182)，偏向有紋理的左半邊 |

**限制**：
- 中心是「VLM 那一幀看到的輪廓」的中心。物件轉到側面時，2D 輪廓本身就變了，similarity 無法反映，中心會有偏差，要等下一次 VLM 回應重建模型才會修正。
- Z 是物件**表面**的深度，不是物件體積中心的深度（和以前相同）。
- mask 本身不準時（例如 bbox 內的深度切割切到背景，見 10.6），中心也會跟著不準。

### 9.8 物體尺寸（給夾爪用）
每收到一次 VLM 結果，就用 bbox 估一次物體的寬高，發到 `size.topic`（預設 `/tracked_object/size`）。**不是每幀都發**：VLM 多久回一次，就發幾次。

- **型別**：`geometry_msgs/Vector3Stamped`，單位都是公尺：
  - `x`：寬，影像水平（u）方向。
  - `y`：高，影像垂直（v）方向。
  - `z`：估算用的深度 d0。
- **header**：`stamp` 是 VLM 算的那一幀的時間，`frame_id` 是 color 的 optical frame。
- **算法**：`寬 = bbox 寬(px) × d0 / fx`，`高 = bbox 高(px) × d0 / fy`。
  - bbox 是收到的 mask 的外接矩形。server 只回 bbox 時就是 bbox 本身，有回 mask 時是 mask 的外框。
  - d0 是 bbox 內落在 `depth_min_m`～`depth_max_m` 的深度中位數，和 mask 清理用的是同一個值。
- **QoS**：reliable + transient_local，depth 1。夾爪 node 晚啟動也拿得到最後一筆，訂閱端要用相同的 QoS：
  ```bash
  ros2 topic echo --qos-durability transient_local --qos-reliability reliable /tracked_object/size
  ```
- **不發的情況**：
  - bbox 內沒有有效深度（太遠或太近）。
  - 還沒收到 camera_info。
  - mask 在 `MASK_FRAME_MISSING`、`MASK_BAD_SIZE`、`MASK_TOO_SMALL` 這些步驟就被擋掉。
  - 被擋掉時 log 會出現 `Size not published ...`。
- **log**：`Object size: mask_stamp=... width=0.083m height=0.083m depth=0.250m bbox=200x200 px`。
- **關掉**：設 `size.enable: false`。
- 分開版（`mask_tracker_node`）和合併版（`mask_tracker_vlm_node`）都有這個功能，`orb_tracker_node` 沒有。

人工測資驗證（fx=600，200×200 px 的方塊，深度 0.25 m，理論值 200 × 0.25 / 600 = 0.0833 m）：兩個版本都輸出 `width=0.083m height=0.083m`。每 3 秒送一次 VLM 時，12 秒內發了 4 筆，同一段時間追蹤輸出約 150 幀。

**限制**（實機上還沒量過誤差）：
- **bbox 鬆，數值就偏大**：寬高直接取 VLM bbox，bbox 比物體大多少，估出來就大多少。
- **斜放的物體會估大**：bbox 和影像軸對齊，斜放的物體量到的是外接矩形，不是物體本身的寬度。
- **圓柱、球體會估小一點**：d0 是物體正面的表面深度，但輪廓邊緣在更遠的地方。例如半徑 3 cm 的杯子放在 20 cm 處，大約少估 13%。
- **bbox 碰到畫面邊緣**：物體可能有一部分在畫面外，數值只是下限，log 會加註 `bbox touches the image edge`。
- **只在 VLM 那一幀量**：之後物體靠近或遠離都不會更新，要等下一次 VLM 結果。

---

## 10. VLM bridge（送圖到上游 server）

### 10.1 架構
```
相機 color ──► vlm_bridge_node ──(ZeroMQ DEALER, JPEG)──► VLM server（Hackathon-gpu）
                     ▲                                        │
                     └──── bbox [x1,y1,x2,y2]（+ 選配 mask）◄──┘
                     │
                     └──► mask_topic（mono8，stamp = 送出去那一幀）──► tracker
                                                                  └─ 用深度在 bbox 內切出物件
```
- 協定細節見 [docs/vlm_transport.md](docs/vlm_transport.md)。
- **server 預設只回 bbox**（`has_mask: false`）。bridge 沒有深度影像，所以把**整個 bbox 範圍**當成 mask 發布；tracker 收到後用既有的 mask 清理流程在 bbox 內切出物件：
  1. 取 bbox 內有效深度的中位數 d0
  2. 去掉深度和 d0 差超過 `init.depth_gate_m` 的像素（背景）
  3. 保留最大的連通區域
  
  server 開了 `VLM_RETURN_MASK=1` 時（`has_mask: true`），改用 server 的 mask，一樣會再經過深度清理。
- **和 `mask_init_tool` 發同一個 topic**，兩者是替代關係：bridge 接真的 server，工具是手動框選。**不要同時開**。
- 網路收發在獨立執行緒，server 沒回應時不會影響追蹤。
- tracker 完全不用改，`orb_tracker_node`（`init.enable:=true`）和 `mask_tracker_node` 都能接。

### 10.2 接真的 server
```bash
# 1. 設定上級描述（HTTP，文件 4.5）
curl -X POST http://192.168.50.125:8080/api/query -H 'Content-Type: application/json' -d '{"text":"the red cup"}'

# 2. bridge（預設 endpoint 就是 tcp://192.168.50.125:5555）
ros2 launch object_tracker vlm_bridge.launch.py

# 3. tracker（不要開 launch_init_tool）
ros2 launch object_tracker mask_tracker_bringup.launch.py world_frame:=camera_color_optical_frame debug.img:=true
```
- server 用 `LA_MODE=slow` 或 `hybrid` 時，把逾時改成 `vlm.timeout_s:=12.0`（文件第 7、8 節：`fast` 約 3.4 秒，`slow`／`hybrid` 可能到 7.8 秒）。
- 描述改了（`query_version` 增加），bridge 會在下一次 ping 或回應時發現，並立刻重送。

### 10.3 用 mock server 測試
```bash
# T1：mock server（模擬 fast 模式約 3.4 秒；預設只回 bbox）
ros2 run object_tracker mock_vlm_server.py --delay-s 3.4

# T2：bridge 連本機
ros2 launch object_tracker vlm_bridge.launch.py vlm.endpoint:=tcp://127.0.0.1:5555

# T3：tracker
ros2 launch object_tracker mask_tracker_bringup.launch.py world_frame:=camera_color_optical_frame debug.img:=true
```
mock server 常用參數：

| 參數 | 用途 |
|---|---|
| `--delay-s` / `--jitter-s` | 模擬 VLM 推論時間 |
| `--bbox X1 Y1 X2 Y2` | 指定 bbox（上傳座標，`X2`、`Y2` 不含）；預設是畫面中央 |
| `--mask` / `--ellipse` | 附上 mask PNG（模擬 `VLM_RETURN_MASK=1`）；`--ellipse` 讓 mask 是橢圓 |
| `--drop-rate` | 不回應的機率，測 client 的逾時和重送 |
| `--not-found-rate` | 回 `NOT_FOUND` 的機率 |
| `--no-query` | 一律立刻回 `NO_QUERY`，測退避 |
| `--loading-requests N` | 前 N 筆立刻回 `ERROR "model loading"`，測錯誤退避 |
| `--query-version` | 改變版本，測「描述換了就重送」 |
| `--save-dir` | 存下收到的 JPEG，檢查畫質和大小 |

上游 server repo 另有自己的 mock（`python -m tools.mock_server`），協定相同，兩者都可以用。

### 10.4 log 怎麼看
- `Sent request N: 640x362, 10 KB, stamp=...`：送出去了，stamp 是那一幀的時間。
- `Mask published for request N: stamp=... bbox=[x1, y1, x2, y2] (bbox only) pixels=... candidates=1 score=-1.00`
  - stamp 和送出去那一幀相同，tracker 才找得到對應的幀。
  - `bbox` 已經換算回原始影像座標。
  - `bbox only`：server 沒附 mask，發布的是整個 bbox；`server mask`：用 server 的 mask。
  - `candidates`：VLM 找到幾個符合描述的物件（只回其中一個）。
  - `score`：沒有 mask 時固定 -1。
- tracker 端接著會出現 `Mask cleaned: ... depth_gate=d0=0.300m removed=...`：`removed` 就是 bbox 內被深度剔除的背景像素。
- `VLM stats: SENT=1 FOUND=1 | query_version=1 | encode 5 ms | rtt avg 3444 ms max 3714 ms | server 3408 ms | network 36 ms`
  - `encode`：JPEG 壓縮時間
  - `rtt`：送出到收到回應
  - `server`：server 自己回報的處理時間
  - `network = rtt − server`：純網路來回（文件實測 p50 約 36 ms，偶爾到 300 ms）
- `Not connected to tcp://...`：連不上 server（每 5 秒最多印一次），會自動重試。
- `No new frame on ... for X s (last stamp=...), not sending`：超過 `vlm.max_frame_age_s`（預設 1 秒）沒收到新的 color 影像，bridge 暫停送圖。以前會一直把同一張舊圖送出去，tracker 只會得到 `MASK_FRAME_MISSING`。原因可能是相機停了，也可能是 DDS 掉封包讓這個 node 收不到影像（見第 11 節）。
- `Request N timed out`：超過 `vlm.timeout_s` 沒回應；之後的舊回應會因為 `request_id` 對不上被丟棄。
- `Server error: model loading, retrying in 2.0 s`：server 模型還在載入，等 `vlm.error_backoff_s` 再送。

### 10.5 常見問題

| 症狀 | 原因與處理 |
|---|---|
| 一直 `Not connected` | server 沒開、IP 或 port 不對、防火牆沒開 5555/tcp |
| 一直 `NO_QUERY` | 還沒用 HTTP 設定描述（10.2 第 1 步）；server 重啟後描述會清空 |
| 常常 `timed out` | 推論時間超過 `vlm.timeout_s`；`slow`／`hybrid` 模式改 12.0 |
| `MASK_FRAME_MISSING`，而且每次的 `mask_stamp` 都一樣、`cache [...]` 的範圍也不變 | bridge 和 tracker 都收不到新影像。先跑 `ros2 topic hz /camera_duck/camera/color/image_rect_raw`：**hz 正常就是 DDS 掉封包（第 11 節）**；hz 也沒有才是相機停了，看 realsense node 的 log |
| 有 `FOUND` 但 tracker `MASK_FRAME_MISSING` | tracker 沒處理到那一幀（掉幀），或 `init.cache_s` 太短；推論約 3.4 秒時，`init.cache_s` 至少要大於 `vlm.timeout_s` |
| `Mask cleaned` 的 pixels 很小或 `MASK_TOO_SMALL` | bbox 內背景比物件多，深度中位數落在背景上，切到的是背景（見 10.6） |
| `NOT_FOUND` 一直出現 | VLM 找不到目標；先確認描述和畫面內容 |
| `Mask ... does not match bbox` | server 回的 PNG 大小和 bbox 不符，屬於協定錯誤 |
| tracker 收到 mask 但 handoff 不成功 | 看 tracker 的 `MASK_FEW_FEATURES` / `HANDOFF_TIMEOUT` 和逾時訊息的 `last status`（8.6 節） |

### 10.6 尚未實作與限制
- **「連續追丟 N 幀就送圖」（`vlm.lost_frames_trigger`）**：需要 tracker 的追蹤狀態，bridge 看不到。目前的送圖時機只有：還沒收到過 mask、每 `vlm.refresh_period_s`、逾時後重送、`query_version` 改變、`NO_QUERY` 或 `ERROR` 退避後。
- **bbox 內的深度切割很單純**：用 bbox 內深度的**中位數**當物件深度。bbox 很鬆、物件很細長或只占 bbox 一小部分時，中位數會落在背景上，切出來的是背景。之後可以改成「取最近的主要深度群」或「取 bbox 中心附近的深度」。
- `vlm.cache_size`：幀快取在 tracker 裡（`init.cache_s`），bridge 只保留最新一幀。
- 機器人上的 WiFi 延遲、Pi 5 上的 JPEG 壓縮成本都還沒量過；文件的延遲數據是兩台機器在同一個 AP 下量的。

### 10.7 合併版：mask_tracker_vlm_node
把 bridge 併進 mask_tracker 的版本。原本的 `mask_tracker_node` + `vlm_bridge_node` 保留，兩種擇一使用：

| | 分開（mask_tracker_node + vlm_bridge_node） | 合併（mask_tracker_vlm_node） |
|---|---|---|
| 訂閱 color | 2 份（tracker、bridge） | **1 份**：每幀經過 DDS 的影像從約 3.26 MB 降到約 2.04 MB（848×480 raw 的理論值，沒有實測） |
| 送出去的幀 | bridge 收到的最新一幀，tracker 不一定有 | **tracker 自己處理過、放進快取的那一幀**，不會因為兩個 node 收到的幀不同而 `MASK_FRAME_MISSING` |
| mask 來源 | `mask_topic`（bridge 或 `mask_init_tool`） | 內建 VLM client；**不訂閱 `mask_topic`**，不能搭配 `mask_init_tool` |
| 參數 | 各自的 yaml | `mask_tracker_params.yaml` + `vlm_bridge_params.yaml` 裡的 `vlm.*` |

```bash
ros2 launch object_tracker mask_tracker_vlm_bringup.launch.py world_frame:=camera_color_optical_frame debug.img:=true
# 例如改 server 位址：vlm.endpoint:=tcp://192.168.50.125:5555
```
- log 和分開版相同：`Sent request`、`Mask published`、`VLM stats` 都印在 `mask_tracker_vlm_node` 底下。`MASK_RECEIVED` 後面會標 `(built-in VLM client)`。
- `vlm.publish_mask:=true`：把收到的 mask 也發到 `mask_topic`，方便用 rqt 檢查。
- mask 在 network thread 收到後先排隊，**下一幀開始處理前**才交給 tracker，所以 tracker 不用加鎖。這會多等最多一幀的時間（約 33～45 ms）。
- 每一幀都會複製一份彩色影像給 VLM client（848×480 約 1.2 MB 的記憶體複製）。Pi 5 上的成本**還沒量過**。
- 已驗證（docker、假相機、mock server）：`HANDOFF_OK`、沒有 `MASK_FRAME_MISSING`、color 只有 1 個訂閱者、server 沒開時照常執行、Ctrl+C 能正常結束。**Pi 上的 DDS 掉封包有沒有改善，要在 Pi 上實測。**

---

## 11. Pi 部署：DDS 收不到影像

### 11.1 症狀（2026-09-18 在 Pi 5 上發生）
- node 還在跑，但影像 callback 停了，而且**不會自己恢復**：
  - tracker 的 `Track stats` 不再出現（沒有新的幀），但 mask 的 log 還在。
  - bridge 一直送同一個 `stamp`。
- 這時另外開 `ros2 topic hz` 看相機 topic，頻率是正常的（約 22 Hz）。**只有已經在跑的 node 收不到**。
- 兩個 node 停下來的時間不一樣：這次 bridge 比 tracker 早約 4 分鐘。

### 11.2 原因
- 同一台機器上的 node 之間，Cyclone DDS 也是走 UDP。一張 848×480 rgb8 影像約 1.2 MB，要切成約 19 個 UDP 封包；depth 約 0.8 MB。
- Pi 的 kernel 接收 buffer 是預設的 212 KB（`net.core.rmem_max`），封包一多就會被 kernel 丟掉。實測：
  - 全系統 `RcvbufErrors` 74928
  - tracker 的 socket 丟了 38092 個封包
  - bridge 的 socket 丟了 12857 個封包
- **已確認的**：封包因為 buffer 不夠被大量丟掉。
- **還沒確認的**：為什麼丟封包之後，這個 node 就「永久」收不到，而不是偶爾掉幀。可能和 Cyclone 0.10 重組大訊息的方式有關，但我查不到確切的根據。

### 11.3 解法
1. **repo 的 [config/cyclonedds.xml](config/cyclonedds.xml)** 已經加上 `SocketReceiveBufferSize max="16MB"`（`git pull` 就有）。這個值會被 kernel 限制在 `net.core.rmem_max` 以內，所以**第 2 步一定要做**。
2. **在 Pi 的 host 上**（不是 container 裡，因為 container 是 host 網路）調高上限：
   ```bash
   # 立刻生效（重開機後會失效）
   sudo sysctl -w net.core.rmem_max=16777216

   # 永久生效
   echo 'net.core.rmem_max=16777216' | sudo tee /etc/sysctl.d/60-ros-dds.conf
   sudo sysctl --system
   ```
   要還原的話，刪掉 `/etc/sysctl.d/60-ros-dds.conf` 然後重開機，就會回到 212 KB。
3. **重啟所有 ROS node**（包括 realsense），新的 buffer 大小只在 node 啟動時設定。

### 11.4 確認有沒有改善
```bash
cat /proc/sys/net/core/rmem_max        # 應該是 16777216
grep Udp: /proc/net/snmp               # RcvbufErrors（第 5 個數字）隔幾秒看一次，應該不再增加
```
- 跑一段時間後，tracker 的 `Track stats` 要一直有輸出，bridge 送出去的 `stamp` 要一直在變。
- 如果 buffer 調大之後還是會停，就要考慮其他做法：
  - 把 tracker 和相機放在同一個 process（composable node，走 intra-process，不經過 DDS）。
  - 降低解析度或 fps。
  - 讓 node 偵測到收不到影像時，自己重新訂閱。
  - 改用合併版 `mask_tracker_vlm_node`（10.7），color 只訂閱一份。

