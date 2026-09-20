# Hackathon Vision Server — 俯瞰相機定位

用固定在場地上方的 RealSense 相機，定位貼著 AprilTag 的機器人，輸出到 `/pose/global`。

| 項目 | 內容 |
|---|---|
| 最終輸出 | `/pose/global`（`geometry_msgs/PoseWithCovarianceStamped`，map 座標系） |
| 相機外參 | 由 `field_calib_node` 用**桌子邊緣**自動校正，並發布 `map → camera_link` |
| 定位方式 | `solvePnP`（IPPE_SQUARE）→ 沿視線射線移到已知高度 → 3-DoF（x, y, yaw）LM |
| 實測精度 | 合成影像 150 幀：xy RMS 0.4 mm、yaw RMS 0.48°；實機靜止抖動約 0.1 mm |

架構圖：[docs/localization_flow.png](docs/localization_flow.png)

![定位架構](docs/localization_flow.png)

---

## 1. 前提

| | 需求 |
|---|---|
| 硬體 | Intel RealSense（實測 D455、D405）、貼在機器人上的 AprilTag **16h5**、兩張併排的白桌（校正靶就是桌緣） |
| 主機 | Linux、Docker + Docker Compose v2、USB 3.0 |
| 軟體 | 其餘都在容器內（ROS 2 Humble、OpenCV 4.5、librealsense），不需要在主機裝 ROS |

預設場地：兩張 180 × 60 cm 的白桌沿長邊併排（外框 1.8 × 1.2 m）。尺寸可改，見第 4 節。

---

## 2. 建置

```bash
git clone <this repo> Hackathon-vision-server-localization
cd Hackathon-vision-server-localization/docker
docker compose build            # 第一次約 10-20 分鐘
docker compose up -d
docker exec -it hackathon-vision-server-ws bash
```

容器內（`~/vision_ws` 就是 repo 根目錄，直接掛載進去）：

```bash
colcon build
source install/setup.bash
```

記憶體不足時編譯會被系統中止（`exit 137`），改成單執行緒：

```bash
MAKEFLAGS=-j1 colcon build --parallel-workers 1
```

驗證：

```bash
ros2 pkg list | grep -E "aruco_test|field_calib"   # 兩個都要出現
python3 -m pytest src/field_calib/test -q          # 7 passed
python3 tools/calib/field_edge_calib.py --selftest # 最後一行 selftest PASSED
```

`docker/compose.yaml` 的重點：`network_mode: host`、`ipc: host`（OpenCV 顯示需要）、`ROS_DOMAIN_ID=59`、CycloneDDS。
**同一個網段若有別台機器跑同樣的節點，請確認 `ROS_DOMAIN_ID` 不同**，否則兩邊的 topic 和 TF 會互相蓋掉。

---

## 3. 啟動（三個終端機，都在容器內）

```bash
# 1. 相機（1280x720 @30）
ros2 launch realsense2_camera rs_launch.py

# 2. 相機外參校正 + 發布 map -> camera_link
ros2 launch field_calib field_calib.launch.py

# 3. 機器人定位
ros2 launch aruco_test pnp_duck.launch.py
```

驗證：

```bash
ros2 topic hz /camera/camera/color/image_raw      # 約 30 Hz
ros2 run tf2_ros tf2_echo map camera_link         # 有數值
ros2 topic echo /pose/global --once               # tag 在畫面內時才會有
```

`field_calib_node` 啟動時：有上次的結果檔（`tools/calib/out/ros/cam_tf.yaml`）就直接發布，沒有就自動校正一次。
它是 `map → camera_link` 的**唯一發布者**，所以 `rs_launch.py` 的 `cam_tf.enable` 預設是 `false`。

---

## 4. 第一次使用：設定場地並校正

1. **填桌子尺寸** — `src/field_calib/config/field.yaml`：

   ```yaml
   table:
     length: 1.8   # 單張桌子長邊（m）
     depth: 0.6    # 單張桌子短邊（m）
     count: 2      # 併排張數
   ```

   每次校正都會重新讀這個檔，改完不用重啟。

2. **清開桌邊**（人、筆電、線材會擋住桌緣），然後校正：

   ```bash
   ros2 service call /field_calib_node/calibrate std_srvs/srv/Trigger
   ```

3. **看結果**：`/field_calib_node/debug/image`（RViz 的 Image display，或裝 `ros-humble-rqt-image-view`）。
   綠點應該貼在白桌與地板的交界上。判讀方式與常見問題見 [DEBUG.md](DEBUG.md)。

4. 通過檢查才會套用：重新發布 TF、寫入結果檔，定位節點約 1 秒內自動跟上（`camera_pose_refresh_s`）。
   沒通過時 TF 不變，畫面標題顯示 `calib FAIL: 原因`。

5. **座標系約定**：X 沿桌子長邊、Y 沿併排方向、Z 向上、z = 0 是桌面。
   map 原點是「離相機正下方較遠」的那個桌角。機器人真值要用同一個約定量。

6. **tag 安裝方向**：`/pose/global` 的 yaw 會加上 `final_pose_yaw_offset_deg`（預設 −90°）。
   如果實機轉出來剛好反向，改成 `+90` 即可。

---

## 5. 主要 topic 與參數

### Topic

| Topic | 型別 | 說明 |
|---|---|---|
| `/pose/global` | `PoseWithCovarianceStamped` | **最終定位**：LM 結果（含 tag 安裝偏移），LM 不可用時退回原始 PnP |
| `/pose/global/pnp` | `PoseStamped` | 原始 PnP，z 是估出來的（可當一致性檢查） |
| `/pose/global/pnp_plane_lm` | `PoseStamped` | 平面約束 LM，tag 本身朝向 |
| `/field_calib_node/debug/image` | `Image` | 校正的 live／過程畫面 |
| `/field_calib_node/{live,calib}/overlay/compressed` | `CompressedImage` | `live.compact:=true` 時才有的 JPEG |
| `/pnp_duck_node/debug/image` | `Image` | 定位節點的 debug 疊圖（`debug.img:=true`） |

### 常用參數

優先順序：**command line > param.yaml > launch 預設 > 節點預設**。

`src/aruco_test/config/param.yaml`：

| 參數 | 預設 | 說明 |
|---|---:|---|
| `robot.id` | 1 | AprilTag 16h5 的 id |
| `robot.marker_size` | 0.08 | tag 黑框邊長（m），白邊不算 |
| `target_height` | 0.2 | tag 表面高度（m，map z = 0 是桌面） |
| `final_pose_yaw_offset_deg` | −90 | tag 座標系 → 機器人座標系 |
| `final_pose_cov.sigma_*` | 5 mm / 1° | `/pose/global` 的固定 covariance |
| `camera_pose_refresh_s` | 1.0 | 重查外參的週期，0 = 關閉 |

`src/field_calib/config/param.yaml`：見 [DEBUG.md 第 6 節](DEBUG.md)（校正輪數、門檻、live 串流設定等）。

給網頁／App 用的高頻串流：

```bash
ros2 launch field_calib field_calib.launch.py live.compact:=true live.period:=0.1
```

未壓縮的完整版疊圖是 1740×820、每張 4.2 MB，實測 **BEST_EFFORT 訂閱者收不到**（UDP 片段被丟）；
compact 模式改成相機原圖尺寸並發 JPEG，實測 10 Hz、每張約 106–144 KB。

---

## 6. 沒有相機也能重現

| 目的 | 指令 |
|---|---|
| 校正演算法自我測試（含無舊外參的深度初值） | `python3 tools/calib/field_edge_calib.py --selftest` |
| 疊圖繪製測試 | `python3 -m pytest src/field_calib/test -q` |
| 端對端定位精度（合成 tag 影像，30 個位置 ×5 幀） | 見下方 |
| 離線重跑一次校正（用擷取檔） | `python3 tools/calib/capture_frames.py --n 30 --out tools/calib/out/cap1.npz` 之後 `python3 tools/calib/field_edge_calib.py tools/calib/out/cap1.npz` |
| 定位方法比較（Monte Carlo） | `python3 tools/sim/plane_constrained_sim.py` |

端對端合成測試：

```bash
# 終端機 1
ros2 run aruco_test pnp_duck_node --ros-args -r __node:=pnp_duck_sim \
  -p RGB_topic:=/sim/image -p camera_info_topic:=/sim/camera_info \
  -p camera_frame:=sim_color_optical_frame \
  -p pose_topic:=/sim/pose/pnp_raw -p plane_lm.pose_topic:=/sim/pose/plane_lm \
  -p target_height:=0.2 -p robot.marker_size:=0.1 -p camera_pose_refresh_s:=0.0
# 終端機 2
python3 tools/sim/synthetic_tag_check.py
```

預期輸出（最後一列表格）：`plane_lm` 欄 150/150 偵測、xy RMS 約 0.4 mm、yaw RMS 約 0.5°。

---

## 7. 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| `colcon build` 中途被殺（exit 137） | 記憶體不足，改 `MAKEFLAGS=-j1 colcon build --parallel-workers 1` |
| RViz 沒有 `map` frame | `field_calib_node` 沒起來，或還沒有校正結果（看它的 log） |
| 節點名稱重複、TF 被蓋掉 | 同網段另一台用了相同 `ROS_DOMAIN_ID`，或 `rs_launch.py` 開了 `cam_tf.enable:=true`（啟動時會警告） |
| 訂閱大張疊圖收不到影像 | 4 MB 影像 BEST_EFFORT 會被丟，改用 RELIABLE 或開 `live.compact:=true` 吃 JPEG |
| live 畫面模型框整個歪掉 | 相機或桌子被移動，外參過時 → 重新校正 |
| `depth init failed: table region ... does not match` | `field.yaml` 的尺寸和實際桌面對不上，或桌面被擋掉大半 |
| 相機 `xioctl(VIDIOC_QBUF) failed` | USB 或裝置狀態異常，重插相機、確認 USB 3.0 |
| OpenCV 視窗／X11 錯誤 | 需要 `ipc: host` 與 `DISPLAY`，並在主機執行 `xhost +local:docker` |

---

## 8. 文件與目錄

| 檔案 | 內容 |
|---|---|
| [FLOW.md](FLOW.md) | 兩個定位節點與校正流程的完整架構、實測數據 |
| [DEBUG.md](DEBUG.md) | 校正 debug 畫面怎麼看、症狀對照表、全部參數 |
| [CALIBRATION.md](CALIBRATION.md) | 外參校正的設計文件（IMU／深度／桌緣的產品化流程） |
| [docs/localization_flow.dot](docs/localization_flow.dot) | 架構圖原始檔（`dot -Tpng -Gdpi=130 ... -o ...png` 重畫，需要 graphviz 與中文字型） |

```
src/aruco_test/      定位節點（pnp_duck 為主、homography_duck 比較用）
src/field_calib/     桌緣外參校正（ROS 節點 + 演算法 core + 測試）
src/realsense_ros/   RealSense 驅動（含改過的 rs_launch.py：cam_tf.*）
tools/calib/         離線校正工具、擷取工具（out/ 不進版控）
tools/sim/           模擬與合成影像測試
docker/              Dockerfile 與 compose
```
