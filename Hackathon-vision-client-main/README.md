# Hackathon-vision-client

RealSense D405 的視覺物件追蹤 client。上游 VLM server 每隔幾秒回一個目標物的 bbox，這邊負責在兩次回應之間把物件追住，並持續輸出目標的 3D 座標給下游。

- 追蹤演算法和參數怎麼調：[DEBUG.md](DEBUG.md)
- 整體流程圖：[docs/tracking_flow.md](docs/tracking_flow.md)
- 和 server 的傳輸協定：[docs/vlm_transport.md](docs/vlm_transport.md)
- 待辦和已知問題：[TODO.md](TODO.md)

---

## 1. 這個專案做什麼

```
RealSense D405 ──> tracker ──> /object_goal_pose （目標的 3D 點）
                      │                /tracked_object/size （目標寬高，給夾爪）
                      └──> VLM server （ZeroMQ，約 2～3 秒一次回 bbox）
```

兩種追蹤方案，topic 介面相同：

| 執行檔 | 方法 | 適合的目標 |
|---|---|---|
| `mask_tracker_node` | VLM mask + KLT + similarity | 非平面物件（紙杯、小包裝）；**目前部署用這個** |
| `orb_tracker_node` | ORB + homography | 平面、有印刷紋理的目標 |

`mask_tracker_vlm_node` 是把 VLM client 直接內建進 tracker 的版本，color 只訂閱一次，Pi 上建議用這個（見 [DEBUG.md](DEBUG.md) 10.7、11）。

---

## 2. 環境需求

- Ubuntu + Docker（含 `docker compose`），或 Raspberry Pi 5（部署端，同一套 image）
- Intel RealSense D405，USB 3.x
- ROS 2 Humble、Cyclone DDS —— 都裝在 container 裡，host 不需要另外裝 ROS
- 上游 VLM server（沒有的話用附的 mock server）

container 內已經裝好的東西（`docker/Dockerfile`）：ROS Humble base、cv_bridge、image_transport_plugins、rmw_cyclonedds_cpp、realsense2 套件、libzmq + cppzmq header、nlohmann-json、python3-zmq。

---

## 3. 快速開始

### 3.1 建立並進入 container

```bash
git clone <repo> Hackathon-vision-client
cd Hackathon-vision-client/docker
docker compose build          # 第一次會比較久
docker compose up -d
docker exec -it hackathon-vision-client-ws bash
```

- 專案目錄會掛進 container 的 `/home/vision/vision_ws`，在 host 或 container 內改檔案都通。
- compose 設定的重點：`network_mode: host`、`privileged`（RealSense 需要）、`ROS_DOMAIN_ID=59`、`RMW_IMPLEMENTATION=rmw_cyclonedds_cpp`、`CYCLONEDDS_URI` 指到 `config/cyclonedds.xml`。
- 要用 OpenCV 視窗（`debug.window:=true`）的話，在 host 先跑 `xhost +local:docker`。Pi 上沒有螢幕，不要開。

### 3.2 編譯

在 container 裡：

```bash
cd /home/vision/vision_ws
colcon build --symlink-install          # 第一次；src/realsense_ros 也會一起編，比較久
source install/setup.bash
```

之後只改 tracker 的話：

```bash
colcon build --packages-select object_tracker
```

### 3.3 跑起來

**合併版（相機 + 追蹤 + VLM client 一起起來，部署用這個）**

```bash
ros2 launch object_tracker mask_tracker_vlm_bringup.launch.py \
  world_frame:=camera_color_optical_frame debug.img:=true
```

**分開版（tracker 和 VLM bridge 各跑一個 process）**

```bash
# terminal 1
ros2 launch object_tracker mask_tracker_bringup.launch.py \
  world_frame:=camera_color_optical_frame debug.img:=true
# terminal 2
ros2 launch object_tracker vlm_bridge.launch.py vlm.endpoint:=tcp://192.168.50.125:5555
```

**確認有在動**

```bash
ros2 topic hz /object_goal_pose
ros2 topic echo --qos-durability transient_local --qos-reliability reliable /tracked_object/size
ros2 run rqt_image_view rqt_image_view /tracked_object/debug_image/compressed
```

---

## 4. Topic 介面

**訂閱**（namespace 見第 5 節）

| Topic | 型別 | 說明 |
|---|---|---|
| `/camera_duck/camera/color/image_rect_raw` | `sensor_msgs/Image` | D405 沒有獨立 RGB module，color 由 depth module 提供 |
| `/camera_duck/camera/aligned_depth_to_color/image_raw` | `sensor_msgs/Image` | 需要 `align_depth.enable:=true`，launch 已經帶上 |
| `/camera_duck/camera/color/camera_info` | `sensor_msgs/CameraInfo` | 內參 |
| `/tracked_object/init_mask` | `sensor_msgs/Image` (mono8) | 分開版才用；合併版走 process 內部佇列 |

**發布**

| Topic | 型別 | 頻率 | 說明 |
|---|---|---|---|
| `/object_goal_pose` | `geometry_msgs/PointStamped` | 每幀 | 目標中心在 `world_frame` 的座標 |
| `/tracked_object/size` | `geometry_msgs/Vector3Stamped` | 每次 VLM 回應 | x=寬 y=高 z=深度（公尺），reliable + transient_local |
| `/tracked_object/debug_image` | `sensor_msgs/Image` | ≤ 5 Hz | `debug.img:=true` 才發；`.../compressed` 適合無線看 |

TF：`debug.enable` 開啟時會廣播 `tracked_object`。輸出座標需要機器人端提供 `world_frame` 到 `camera_link` 的 TF；桌面測試可以用 `cam_tf.enable:=true` 自己發一個 static TF。

---

## 5. 相機 namespace

topic 前綴是 `/<camera_namespace>/<camera_name>/`，預設 **`/camera_duck/camera/`**，和 `src/realsense_ros` 的 `rs_launch.py` 一致。三個 bringup launch 都會把這兩個值明確傳給 `rs_launch.py`，所以不會因為 realsense 套件版本不同而跑掉。

要改成別的 namespace：

```bash
ros2 launch object_tracker mask_tracker_vlm_bringup.launch.py \
  camera_namespace:=camera camera_name:=camera \
  color_topic:=/camera/camera/color/image_rect_raw \
  depth_topic:=/camera/camera/aligned_depth_to_color/image_raw \
  camera_info_topic:=/camera/camera/color/camera_info
```

launch 參數只換相機那邊的 namespace，tracker 訂閱哪個 topic 是另外三個參數，兩邊要一起改。長期要改的話直接改 `config/*.yaml`。

---

## 6. 參數

| 檔案 | 給誰用 |
|---|---|
| `config/mask_tracker_params.yaml` | `mask_tracker_node`、`mask_tracker_vlm_node` |
| `config/params.yaml` | `orb_tracker_node` |
| `config/vlm_bridge_params.yaml` | `vlm_bridge_node`；合併版只取其中的 `vlm.*` |
| `config/cyclonedds.xml` | DDS 設定，compose 用環境變數指過去 |

優先順序（高到低）：命令列 `name:=value` → params 檔 → launch 內的 `NODE_ARG_DEFAULTS` → 程式裡的 `declare_parameter` 預設值。params 檔裡列出的任何參數都可以直接在命令列覆蓋。

常用的幾個：`world_frame`、`debug.img`、`debug.enable`、`vlm.endpoint`、`vlm.refresh_period_s`、`output.center_mode`、`size.enable`。各參數的意義和調法見 [DEBUG.md](DEBUG.md)。

---

## 7. 沒有 server 或沒有相機時的測試

**mock VLM server**（固定回一個 bbox，可以模擬延遲、掉包、`NOT_FOUND`、`NO_QUERY`、`model loading`）

```bash
ros2 run object_tracker mock_vlm_server.py --delay-s 3.4
ros2 launch object_tracker mask_tracker_vlm_bringup.launch.py vlm.endpoint:=tcp://127.0.0.1:5555
```

**bag 重播**

```bash
ros2 bag play <bag> --clock
ros2 launch object_tracker mask_tracker_bringup.launch.py launch_camera:=false use_sim_time:=true
```

**手動框選目標**（模擬上游 mask，需要螢幕）

```bash
ros2 launch object_tracker mask_tracker_bringup.launch.py launch_init_tool:=true delay_s:=0.1
```

錄 bag、測試情境、各種 log 怎麼看，見 [DEBUG.md](DEBUG.md) 第 6、8、9 節。

---

## 8. 部署到 Raspberry Pi 5

Pi 上跑的是同一份 repo 和同一個 image，流程和第 3 節相同。額外要做的兩件事：

1. **調大 UDP 接收緩衝**，否則 848×480 的影像會被 kernel 丟掉，node 會整個收不到影像：
   ```bash
   sudo sysctl -w net.core.rmem_max=16777216     # 要永久生效就寫進 /etc/sysctl.d/
   ```
   `config/cyclonedds.xml` 已經要到 16 MB，但 kernel 會用 `rmem_max` 蓋掉。細節和判斷方法見 [DEBUG.md](DEBUG.md) 第 11 節。
2. **用合併版**（`mask_tracker_vlm_bringup.launch.py`），影像流量少約 37%，也不會再出現 `MASK_FRAME_MISSING`。

Pi 沒有螢幕，`debug.window` 一律關著，要看畫面用 `debug.img:=true` 加 `/tracked_object/debug_image/compressed`。

---

## 9. 常見問題

| 症狀 | 先查什麼 |
|---|---|
| 看不到對方的 topic | 兩台的 `ROS_DOMAIN_ID` 要一樣，而且要在**同一個網段**、能雙向 ping。DDS 的 discovery 不會跨 router |
| `ros2 node list` 出現不是自己的 node、topic 有兩個 publisher | 同網路上有別人用同一個 domain。換一個沒人用的 `ROS_DOMAIN_ID`，否則會收到別台相機的影像和內參 |
| 一直 `Waiting for camera_info...` | 相機 namespace 和 tracker 訂閱的 topic 對不上，見第 5 節 |
| `Depth ... != color ...` | `align_depth.enable` 沒開 |
| 收到影像但幾乎沒有幀被處理 | UDP 緩衝不夠（第 8 節），或影像被別的 node 佔住頻寬 |
| `MASK_FRAME_MISSING` 很多 | 掉幀，或 VLM 回應時間超過 `init.cache_s`；改用合併版 |
| 追蹤一直 `NO_DEPTH` | D405 有效距離約 7～50 cm，太近或太遠都量不到 |

更完整的對照表在 [DEBUG.md](DEBUG.md) 第 4 節。

---

## 10. 專案結構

```
src/object_tracker/
  src/        mask_tracker_node / mask_tracker_vlm_node / orb_tracker_node
              vlm_bridge_node / mask_init_tool / frame_capture_node
  include/    mask_tracker_node.hpp（追蹤主體）
              mask_tracker_algorithms.hpp（mask 清理、similarity 等）
              vlm_client.hpp（ZeroMQ + 協定）
  launch/     四個 launch：三個 bringup + vlm_bridge
  config/     參數檔
  scripts/    mock_vlm_server.py
src/realsense_ros/   realsense-ros 原始碼（camera_namespace 預設為 camera_duck）
docker/              Dockerfile、compose.yaml
config/cyclonedds.xml
```

> 注意：目前 `.gitignore` 把所有 `*.md` 都排除了，所以這份 README 和 DEBUG.md 等文件不在版控裡，換機器時要另外複製。
