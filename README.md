# MC Hackathon 機器人系統

本儲存庫整合機器人任務規劃、行為樹執行、導航定位、視覺辨識與使用者操作介面。各資料夾都是可獨立建置與部署的子系統；本頁提供整體導覽，實際安裝、設定、啟動與除錯方式請依各子系統的 README 操作。

## 系統概觀

```text
使用者瀏覽器
    │
    ▼
Hackathon App ───────► Robot Brain ───────► BT Engine / Navigation
  操作與狀態介面          任務規劃、RAG、BT 生成       │
                                                    ├─► VLM Server + Vision Client
                                                    │     目標辨識、追蹤與 3D 座標
                                                    ├─► Localization Server + robot_localization
                                                    │     全域定位與 EKF 里程計融合
                                                    └─► micro-ROS Agent
                                                          MCU、底盤與夾爪介面
```

## 文件導覽

| 子系統 | 簡介 | 文件 |
|---|---|---|
| Robot Brain | 部署於 Manta LAB 的任務規劃服務。將自然語言任務搭配 typed RAG 轉成 BehaviorTree.CPP v4 XML，驗證後交由 BT Engine 執行，並保存任務回饋作為 Experience RAG。 | [robot_brain/README.md](robot_brain/README.md) |
| Hackathon App | React PWA 與 FastAPI Gateway 組成的機器人任務控制台。提供任務對話、執行狀態、中止任務、相機校正及任務回饋頁面。 | [Hackathon-app-main/README.md](Hackathon-app-main/README.md) |
| BT Engine 與導航 | 接收、驗證並執行 LLM 產生的行為樹，透過 ROS 2 action/service 串接導航、視覺與夾爪；也包含模擬器、mock robot、地圖和部署工具。 | [mc_main_nav-main/README.md](mc_main_nav-main/README.md) |
| Vision Server（VLM） | 使用 LocateAnything-3B 從 client 傳來的影像中找出指定物件，透過 ZeroMQ 回傳 bbox，並可選擇使用 SAM2 產生 mask。 | [Hackathon-vision-server-vlm-main/README.md](Hackathon-vision-server-vlm-main/README.md) |
| Vision Client | 在 Raspberry Pi 5／RealSense D405 端接收 VLM 的 bbox 或 mask，在兩次推論之間持續追蹤物件，並輸出目標 3D 座標與尺寸。 | [Hackathon-vision-client-main/README.md](Hackathon-vision-client-main/README.md) |
| Vision Server（Localization） | 使用場地上方的 RealSense 與機器人 AprilTag 估算全域姿態，包含桌緣相機外參校正，最終發布 ROS 2 `/pose/global`。 | [Hackathon-vision-server-localization-main/README.md](Hackathon-vision-server-localization-main/README.md) |
| Robot Localization | Docker 化的 ROS 2 Humble 定位融合服務。替 MCU 里程計更新時間戳，並以兩組 EKF 產生 `/odometry/local` 與 `/odometry/global`。 | [robot_localization_ws/README.md](robot_localization_ws/README.md) |
| micro-ROS Agent | 透過 USB serial 將微控制器上的 micro-ROS client 接入 ROS 2 DDS 網路，供底盤、感測器或夾爪等 MCU 節點使用。 | [uros_agent/README.md](uros_agent/README.md) |

## 建議閱讀順序

若要先了解完整系統，建議依序閱讀：

1. [Robot Brain](robot_brain/README.md)：了解任務如何由自然語言轉成行為樹。
2. [BT Engine 與導航](mc_main_nav-main/README.md)：了解行為樹節點、HTTP API 與 ROS 2 執行介面。
3. [Hackathon App](Hackathon-app-main/README.md)：了解使用者如何下達、追蹤及中止任務。
4. [VLM Server](Hackathon-vision-server-vlm-main/README.md) 與 [Vision Client](Hackathon-vision-client-main/README.md)：了解物件搜尋與追蹤資料流。
5. [Localization Server](Hackathon-vision-server-localization-main/README.md) 與 [Robot Localization](robot_localization_ws/README.md)：了解全域姿態、輪式里程計及 EKF 融合。
6. [micro-ROS Agent](uros_agent/README.md)：了解 MCU 如何加入 ROS 2 網路。

## 整合時的重要設定

- 所有需要互相通訊的 ROS 2 主機與容器必須使用相同的 `ROS_DOMAIN_ID`。多數子系統預設為 `59`，但 `uros_agent` 的文件預設為 `0`，整合前請統一設定。
- 多個服務使用 host networking；啟動前請依各 README 確認連接埠、主機 IP、ROS topic、action/service 名稱及 API token。
- 相機、serial device、AMD GPU 等硬體需要額外的 Docker device 或 privileged 權限，請以對應子系統的部署說明為準。
- 真實 BT Engine 會驅動機器人。開發與聯測時，請先使用各專案提供的 mock、模擬器或停用自動執行選項。

## 從哪裡開始

- 只想操作機器人：從 [Hackathon App](Hackathon-app-main/README.md) 開始。
- 要開發任務規劃：閱讀 [Robot Brain](robot_brain/README.md)。
- 要開發行為樹、導航或 ROS 介面：閱讀 [BT Engine 與導航](mc_main_nav-main/README.md)。
- 要處理目標辨識或追蹤：同時閱讀 [VLM Server](Hackathon-vision-server-vlm-main/README.md) 與 [Vision Client](Hackathon-vision-client-main/README.md)。
- 要處理相機校正或機器人全域定位：閱讀 [Localization Server](Hackathon-vision-server-localization-main/README.md) 與 [Robot Localization](robot_localization_ws/README.md)。
- 要連接 MCU：閱讀 [micro-ROS Agent](uros_agent/README.md)。
