# Third-Party Notices

本文件用于公开发布审核，不替代各上游项目的完整许可证文本。安装依赖时应以实际解析出的版本、源码分发包和上游许可证为准。

## Python direct dependencies

| Package | Declared range | License | Upstream |
| --- | --- | --- | --- |
| Pydantic | `>=2.7,<3` | MIT | <https://github.com/pydantic/pydantic> |
| Eclipse Paho MQTT | `>=2.1,<3` | EPL-2.0 OR BSD-3-Clause | <https://github.com/eclipse-paho/paho.mqtt.python> |
| LangGraph | `>=1.2,<2` | MIT | <https://github.com/langchain-ai/langgraph> |
| langchain-openai | `>=1.4,<2` | MIT | <https://github.com/langchain-ai/langchain> |
| python-dotenv | `>=1.0,<2` | BSD-3-Clause | <https://github.com/theskumar/python-dotenv> |
| FastAPI | `>=0.116,<1` | MIT | <https://github.com/fastapi/fastapi> |
| Uvicorn | `>=0.35,<1` | BSD-3-Clause | <https://github.com/encode/uvicorn> |
| Shapely | `>=2.1,<3` | BSD-3-Clause | <https://github.com/shapely/shapely> |

开发依赖包括 pytest（MIT）和可选的 Zig toolchain Python package（MIT）。Python 当前没有锁文件，因此公开发布或制作可重现发行包前，应生成一次基于实际环境的完整依赖与许可证清单。

## Web direct dependencies

| Package | Locked version | License | Upstream |
| --- | --- | --- | --- |
| Three.js | `0.169.0` | MIT | <https://github.com/mrdoob/three.js> |
| Lucide | `0.468.0` | ISC | <https://github.com/lucide-icons/lucide> |
| Vite | `8.2.1` | MIT | <https://github.com/vitejs/vite> |
| TypeScript | `5.9.3` | Apache-2.0 | <https://github.com/microsoft/TypeScript> |
| `@types/three` | `0.169.0` | MIT | <https://github.com/DefinitelyTyped/DefinitelyTyped> |

完整 Web 依赖树、版本、完整性哈希和 SPDX license 字段记录在 `web/package-lock.json`。其中包含 MPL-2.0 等传递依赖；仓库不提交 `node_modules`。

## Firmware build dependencies

固件构建脚本只在被忽略的 `.deps/` 中获取并校验固定版本，不把第三方源码直接纳入项目源码交付：

| Dependency | Pinned source | License |
| --- | --- | --- |
| cJSON | `v1.7.19`, commit `c859b25da02955fef659d658b8f324b5cde87be3` | MIT |
| FreeRTOS-Kernel | `V11.2.0`, commit `0adc196d4bd52a2d91102b525b0aafc1e14a2386` | MIT |

如果分发包含这些依赖源码或其编译产物，应同时保留对应 checkout 中的 `LICENSE` / `LICENSE.md` 文本并重新核对合规要求。

## Unitree GO1 visual assets

仓库已包含 `web/public/assets/go1/` 下的五组 STL、`go1.xml`、`NOTICE.md` 和上游 `LICENSE`。来源为 [MuJoCo Menagerie `unitree_go1`](https://github.com/google-deepmind/mujoco_menagerie/tree/da76818e269b82289eba39808e2fb91d679d6994/unitree_go1)，固定 commit 为 `da76818e269b82289eba39808e2fb91d679d6994`。

资产版权声明为：

```text
Copyright (c) 2016-2022 HangZhou YuShu TECHNOLOGY CO.,LTD. ("Unitree Robotics")
All rights reserved.
```

分发约束已随资产保存：

- 上游 BSD 3-Clause 完整文本位于 `web/public/assets/go1/LICENSE`；
- `NOTICE.md` 记录来源、commit、版权和无官方背书说明；
- 资产只作为本地浏览器视觉 mesh，不参与碰撞、footprint、传感器、blocked 或任务结果；
- 项目不引入 MuJoCo runtime、ROS、Gazebo、Unitree SDK 或真实 GO1 控制。

## Project license

项目自身代码采用 MIT License，完整文本位于根目录 `LICENSE`，版权持有人为 `yile180304`。第三方依赖和 Unitree GO1 视觉资产继续适用各自许可证，本项目的 MIT License 不替代这些上游条款。
