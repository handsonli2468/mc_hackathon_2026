# 桌緣外參校正 Debug 指南

`field_calib_node` 用桌子邊緣求相機外參，並且自己發布 `map → camera_link`。本文件說明使用流程、怎麼看 debug 畫面、怎麼判斷問題出在哪。
流程與原理見 [FLOW.md](FLOW.md) 第 6 節。

---

## 1. 使用流程

### 1.1 輸入桌子尺寸

編輯 `src/field_calib/config/field.yaml`（或用 `field_file:=` 指定其他檔案）：

```yaml
table:
  length: 1.8   # 單張桌子長邊（m），map X 方向
  depth: 0.6    # 單張桌子短邊（m）
  count: 2      # 沿短邊併排的桌子張數
```

- 節點**每次校正都會重新讀這個檔**，改完直接呼叫 `~/calibrate` service 即可，不用重啟。
- 之後的網頁只要寫入同樣內容，再觸發校正。

### 1.2 啟動

```bash
# container，~/vision_ws
colcon build --packages-select field_calib aruco_test realsense2_camera && source install/setup.bash
ros2 launch realsense2_camera rs_launch.py        # cam_tf.enable 預設已改為 false
ros2 launch field_calib field_calib.launch.py     # 負責發布 map → camera_link
```

debug 畫面一律用 image topic 發布（不開 OpenCV 視窗），用 `rqt_image_view` 或 RViz 的 Image display 看：

| topic | 內容 |
|---|---|
| `/field_calib_node/debug/image` | **主要看這個**：平常是 live overlay；校正時逐輪顯示，完成後停在結果 `calib.hold_s` 秒（預設 15）再回到 live |
| `/field_calib_node/live/overlay` | 只有 live overlay |
| `/field_calib_node/calib/{overlay,strips,residuals}` | 最近一次校正（每輪都會更新） |
| `/field_calib_node/{live,calib}/overlay/compressed` | `live.compact:=true` 時才有的 JPEG（`sensor_msgs/CompressedImage`，format `bgr8; jpeg compressed bgr8`） |
| `/pnp_duck_node/debug/image`、`/homography_duck_node/debug/image` | 定位節點的 debug 畫面（`debug.img:=true` 時才發布，topic 可用 `debug.img_topic` 改） |

沒有訂閱者的 topic 不會畫圖也不會編碼，省 CPU。

容器目前沒有 `rqt_image_view`，要用的話先安裝 `ros-humble-rqt-image-view`，或在 RViz 加 Image display。

啟動時：

| 情況 | 行為 |
|---|---|
| 有上次的結果檔（`tools/calib/out/ros/cam_tf.yaml`） | 直接發布該外參 |
| 沒有結果檔 | 自動校正一次，通過檢查才發布 |
| `map → camera_link` 已經有別人在發布 | terminal 警告（通常是 `rs_launch.py cam_tf.enable:=true`），兩者會互相覆蓋，請關掉一個 |

### 1.3 高頻串流（給 App / 瀏覽器）

未壓縮的完整版疊圖是 1740×820、每張 4.2 MB，實測用 **BEST_EFFORT 訂閱根本收不到**（UDP 片段被丟），只有 RELIABLE 收得到。要給 App 連續看畫面時開 compact：

```bash
ros2 launch field_calib field_calib.launch.py \
    field_file:=/home/vision/vision_ws/tools/calib/field_app.yaml \
    live.compact:=true live.period:=0.1
```

| | 預設 | `live.compact:=true` |
|---|---|---|
| `~/live/overlay`、`~/calib/overlay`、`~/debug/image` | 完整版 1740×820（含右側面板） | 精簡版，尺寸等於相機原圖（1280×720），左上角一行 `band / acc` |
| `~/{live,calib}/overlay/compressed` | 沒有這個 topic | JPEG，`live.jpeg_quality` 預設 80 |
| 寫進 `debug_dir` 的 PNG | 完整版 | **仍是完整版**（App 結果頁讀 `final_overlay.png`） |

實測（合成場景、`live.period:=0.1`）：compressed 10.0 Hz、每張 106–144 KB、BEST_EFFORT 訂閱收得到。

### 1.4 重新校正

- `ros2 service call /field_calib_node/calibrate std_srvs/srv/Trigger`，在 `/field_calib_node/debug/image` 看過程與結果。
- 相機被移動也可以直接校正：初值同時來自
  - **深度**：擬合桌面平面，再用長方形擬合桌面範圍（不需要舊外參）；
  - **目前的 TF**。
  兩者都跑一次，取結果較好的。
- 通過檢查（採用點 ≥ `calib.min_accept_ratio`，inlier RMS ≤ `calib.max_rms_px`）才會：
  - 重新發布 `map → camera_link`；
  - 寫入 `tools/calib/out/ros/cam_tf.yaml`（下次啟動會載入）。
- 沒通過：TF 不變，overlay 標題顯示 `calib FAIL: 原因`，service 回傳失敗。
- `pnp_duck` / `homography_duck` 每 `camera_pose_refresh_s`（預設 1 秒）重查 TF。外參一變，log 出現 `camera pose updated`，不用重啟。
- 每次校正的 debug 圖與結果存在 `tools/calib/out/ros/<時間>/`：`<depth|tf>_band_*`、`final_*`、`cam_tf.yaml`。
- 只想試算、不想動到 TF：`calib.apply:=false`（dry run）。

### 1.5 原點約定

- X 沿桌子長邊，Y 沿併排方向，Z 向上，z=0 是桌面。
- 長方形合法的原點角有兩個（對角），**取離相機正下方較遠的那個**。以目前場地來說，就是上方桌子的右上角。
- 相機若搬到桌子另一側，原點會跟著換到另一個對角，機器人真值要用同一個約定量。

## 2. 兩種畫面

| 畫面 | 何時 | 用的位姿 | 用途 |
|---|---|---|---|
| **live**（標題 `live`） | 平常，每 1 秒更新 | 目前 TF（本節點發布的外參） | 檢查**目前使用中的外參**對不對；相機被碰、桌子被推會立刻看出來 |
| **calib**（標題 `calib`） | 呼叫 `~/calibrate` 之後 | 每一輪優化前 / 後 | 看校正過程中每條邊抓到哪裡、哪些點被丟掉 |

live 模式對不上時，terminal 會出現：

```
table edges do not match the current extrinsic: inlier RMS 2.6 px, accepted 22% (...)
```

門檻：`live.warn_rms_px`（預設 1.5 px）、`live.warn_inlier_ratio`（預設 70%）。

---

## 3. overlay 畫面怎麼看

左邊是相機影像，右邊是資訊面板。

### 影像上的標記

| 標記 | 意義 |
|---|---|
| 半透明色帶 | 每條線段的搜尋範圍（±band px） |
| 彩色粗線 | 模型桌緣（本輪優化**後**的位姿） |
| 白線 | 完整桌子外框 + 接縫（含圓角區） |
| 灰線 | 模型桌緣（本輪優化**前**的位姿）；live 模式和白線重疊 |
| 紅／綠箭頭 | map 原點與 X（紅）、Y（綠）軸，各 0.2 m |
| ● 綠 | accepted：採用的邊緣點 |
| × 橘 | weak_gradient：梯度太弱（`edge.grad_thresh`） |
| × 紫 | low_contrast：桌面／地板亮度差不夠（`edge.contrast_thresh`），常見於白色物品壓在邊上 |
| × 藍 | at_band_edge：最強邊緣在搜尋範圍邊界上 → 真正的邊可能在範圍外，或抓到別的東西 |
| × 紅 | huber_outlier：有抓到邊，但離模型線太遠（≥ 3 × `lm.delta`），優化時被降權 |

### 右側面板

- **四個角落放大**（origin = map 原點、far left、near left、near right）：白線角落應該貼著桌子圓角的延長線交點。
- **pose moved this stage**：這一輪位姿改變多少。最後一輪應該只剩 1–2 mm、< 0.05°。
- **每條線段統計**：

| 欄位 | 意義 |
|---|---|
| acc/n | 採用點數 / 影像內的取樣點數 |
| weak / lowc / edge / outl | 各剔除原因的點數 |
| RMS | 採用點到模型線的距離 RMS（px） |
| mean | 平均偏移（px），**正值 = 偵測到的邊在模型外側（地板那側）** |

### 正常的樣子（最後一輪 band 12 px）

- 綠點連成一條線，貼在白桌與地板的交界上。
- 各線段 RMS 約 0.2–0.9 px，mean 在 ±0.7 px 內。
- 被遮住的地方（人、筆電、機器人、線材）是 × 或沒有點，這是正常的，不影響結果。

---

## 4. 常見症狀與處理

| 症狀 | 可能原因 | 怎麼確認 / 處理 |
|---|---|---|
| live 畫面整個模型框**歪斜、扭轉**，warning 一直出現 | 相機或桌子被移動過，外參已過時；或另有節點在發布 `map → camera_link`（例如手動開了 `cam_tf.enable:=true`） | 看啟動時有沒有「already published by another node」警告；呼叫 `~/calibrate` 重新校正 |
| live 突然從正常變成對不上 | 相機被碰到、桌子被推動 | 呼叫 `~/calibrate` 重新校正；通過後 TF 自動更新，定位節點約 1 秒內跟上 |
| 某條邊幾乎全是 × 或 acc 很少 | 被人或物品擋住 | 清開後再校正；長期擋住的邊用 `field.disabled_segments:=right_1` 關掉 |
| 某條邊有一段綠點整段偏 1–3 px | 抓到桌腳、線材、陰影、桌邊反光 | 看該輪的 `strips.png`（見第 5 節）確認抓到什麼；清開該區或關掉該邊 |
| 第一輪（80 px）大量藍 × | 初值離真值太遠，或搜尋範圍內有更強的邊 | 看 terminal 的 `depth init` 是否成功（深度初值不依賴舊外參）；或加大 `calib.bands` 第一個值 |
| `depth init failed: table region ... does not match the input` | 輸入的桌子尺寸錯，或桌面大半被擋住 | 檢查 `field.yaml`；清開桌面再校正 |
| `calib FAIL: accepted ...% < ...` | 太多邊被擋住 | 清開桌邊；長期擋住的邊用 `field.disabled_segments` 關掉 |
| 最後一輪 pose moved 還很大（> 5 mm） | 還沒收斂 | 多加一輪小 band，例如 `calib.bands: [80, 40, 20, 12, 8]` |
| 左右兩條邊 mean 都是負（或都是正） | 模型桌子比實際寬（或窄） | 量實際桌子尺寸，改 `field.yaml` 的 `table.length` / `table.depth` |
| 深度平面檢查的相機高度和桌緣解差 1–2% | D455 深度尺度誤差，**或桌子尺寸不對** | 用捲尺量桌子外框；尺寸對了高度差還在，才歸因於深度 |
| 每次校正 `cam_tf` 差幾 mm / 零點幾度 | x、roll、yaw 彼此會互相抵消，數字看起來差很多但對定位影響小 | 比較實際定位結果，不要只比 `cam_tf` 數字 |

---

## 5. 進一步 debug：strips / residuals（離線）

overlay 看不出來時，用每一輪存下的 PNG，或離線工具逐輪看：

```bash
python3 tools/calib/capture_frames.py --n 30 --out tools/calib/out/cap1.npz     # container，擷取
python3 tools/calib/field_edge_calib.py tools/calib/out/cap1.npz --show          # 逐輪顯示三種畫面
python3 tools/calib/field_edge_calib.py tools/calib/out/cap1.npz --disable right_1
python3 tools/calib/field_edge_calib.py --selftest     # 合成影像自我測試（含無舊外參的深度初值）
```

- **strips**：每條線段沿模型線「拉直」，橫軸 = 沿線位置、縱軸 = 法線方向放大 4×，**往下 = 往地板**。
  1 px 偏差會變成 4 px，可以看出綠點是抓在桌緣上，還是抓到桌腳、線材、陰影。
- **residuals**：每條線段「殘差 vs 沿線位置」：

| 形狀 | 意義 |
|---|---|
| 整條平移 | 尺寸或位置不對 |
| 一條斜線 | 旋轉不對，或那條桌邊本身不直 |
| 彎曲 | 畸變參數或桌邊彎曲 |
| 局部跳一段 | 誤抓（遮擋物、桌腳、線材） |

---

## 6. 可調參數（`src/field_calib/config/param.yaml`）

優先順序：command line > param.yaml > launch 預設 > 節點預設。

| 參數 | 預設 | 說明 |
|---|---|---|
| `field_file` | `''` | 桌子尺寸檔；空字串 = `share/field_calib/config/field.yaml` |
| `field.disabled_segments` | `''` | 不使用的線段，逗號分隔。2 張桌子時為 `far, right_0, left_0, right_1, left_1, near, seam_1`（`far` = 離相機遠的長邊，`near` = 近的長邊） |
| `edge.grad_thresh` | 6.0 | 最小梯度（灰階 / px） |
| `edge.contrast_thresh` | 20.0 | 桌面與地板最小亮度差 |
| `edge.blur` | 1.0 | 抓邊前的高斯模糊 sigma（px） |
| `lm.delta` | 1.5 | Huber 門檻（px）；≥ 3 倍視為 outlier |
| `calib.frames` | 30 | 校正時取 median 的幀數 |
| `calib.bands` | [80, 40, 20, 12] | 每一輪的搜尋範圍（px） |
| `calib.stage_delay` | 1.0 | 每一輪畫面停留秒數 |
| `calib.result_file` | `''` | 結果檔；空字串 = `<calib.output_dir>/cam_tf.yaml` |
| `calib.on_startup` | `if_missing` | 啟動時：`if_missing` 沒結果才校正、`always` 每次都校正、`never` 不校正 |
| `calib.min_accept_ratio` / `calib.max_rms_px` | 0.6 / 1.5 | 低於／高於門檻就不套用 |
| `calib.apply` | true | false = dry run，不發布 TF、不寫結果檔 |
| `live.band` / `live.period` | 12 / 1.0 | live 檢查的搜尋範圍與更新週期 |
| `live.compact` | false | true：疊圖改成相機原圖尺寸，並發布 JPEG compressed topic |
| `live.jpeg_quality` | 80 | compressed topic 的 JPEG 品質 |
| `calib.hold_s` | 15.0 | 校正完成後 `~/debug/image` 停在結果畫面的秒數 |

桌子尺寸、角落排除距離、接縫、各桌 x 偏移在 `src/field_calib/config/field.yaml`。
定位節點的 `camera_pose_refresh_s`（預設 1.0 秒，0 = 關閉）在 `src/aruco_test/config/param.yaml`。
