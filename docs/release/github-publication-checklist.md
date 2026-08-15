---
doc_type: release-checklist
slug: github-publication
component: repository
status: draft
summary: LLM-Agent 首次公开到 GitHub 前的内容、许可证、安全、隐私和仓库设置审核清单。
tags: [github, release, security, licensing]
last_reviewed: 2026-08-17
---

# GitHub 公开发布审核清单

## 当前结论

代码、回放、固定验收矩阵、真实模型条件路径和主要演示已经具备公开展示基础，但现在还不应直接 push。仓库已初始化 Git，`origin/main` 指向 `yile180304/embodied-robot-llm-agent`；2026-08-17 通过 GitHub API 核对时该仓库仍为 `PRIVATE`，默认分支为 `main`，远端尚无 Actions workflow。候选公开范围、安全报告入口、CI 和第三方资产声明均已准备；当前唯一法律元数据阻塞是项目正式许可证及版权持有人尚未确认。`.codestable/`、`HANDOFF.md`、`AI_HANDOFF.md`、本机环境和构建产物已由 `.gitignore` 排除。

2026-08-17 已完成一次原工程复跑和一次干净公开副本验证。加入 README 演示 GIF 后，当前候选公开集合为 127 个文件、约 23.30 MiB，最大单文件为 11.98 MiB；未发现 API Key、Token、私人 Windows 工作区路径、超过 50 MiB 的文件或被禁止的内部目录。扫描命中的 `EMBODIED_AGENT_API_KEY=<local-secret>` 与测试用 `dotenv-key` 均为明确的占位/测试数据，不是有效凭据。

五个候选公开视觉资产已完成抽查。README 首图 `semantic-mission-demo.gif` 展示定位红色瓶子后前往蓝色目标区的 16 秒语义导航任务，不宣称视觉识别、抓取或搬运：公开版从 1920 x 1080、33.24 MiB 的本地原片压缩为 854 x 480、160 帧、11.98 MiB；开场、中段和完成状态均清晰，原片由 `*.gif` 规则保持忽略。原 `mission-control-desktop-scene.png` 曾显示模型替换前的盒状机器人，已用当前 GO1、标准 Task API、真实本机 Mosquitto 绕障 Mission 的成功终态重新生成；新图为 1425 x 891、87,130 bytes。所有公开视觉资产均未发现本机路径、用户名或凭证。

本次准备的公开材料：

- 根目录 `README.md`；
- `.gitignore`、`.gitattributes` 和 `.env.example`；
- `CONTRIBUTING.md`、`SECURITY.md`、`THIRD_PARTY_NOTICES.md`；
- `docs/dev/contributing.md` 和 `docs/user/local-simulation.md`；
- GitHub Issue/PR 模板和基础 CI；
- pip、npm 和 GitHub Actions 的每周 Dependabot 配置；
- Replay/Evidence 使用说明与精选桌面截图；
- 候选 MIT 许可证文本，但尚未激活为根目录 `LICENSE`。

## 待维护者拍板

| 决策 | 当前建议 | 状态 |
| --- | --- | --- |
| GitHub 仓库名 | `embodied-robot-llm-agent`；当前 origin 已使用该名称 | 候选已准备 |
| 仓库描述 | Safe LangGraph + MQTT + Python/Three.js quadruped simulation MVP | 远端已设置 |
| 仓库可见性 | 当前保持 private；首次完整 push 和 CI 通过后再切 public | 待执行 |
| 项目许可证 | MIT；第三方资产各自保留原许可证 | 待确认 |
| 版权持有人 | 使用公开身份或 GitHub 用户名 | 待填写 |
| 安全联系人 | 公开后启用 GitHub Private Vulnerability Reporting，不公开邮箱 | 待公开后启用 |
| `.codestable/` | 不公开 | 已由 `.gitignore` 排除 |
| `HANDOFF.md` / `AI_HANDOFF.md` | 不公开，包含本机路径和内部交接信息 | 已由 `.gitignore` 排除 |
| `reports/` | 只公开 README 引用的演示 GIF 和 4 张精选截图；Acceptance Pack 与 transcript 暂不公开 | 候选已准备 |
| 固件参考层 | 公开 C/FreeRTOS/Keil 参考层，并保留“非实机”边界 | 候选已准备 |

## 建议公开的文件

```text
.github/
.env.example
.gitattributes
.gitignore
CONTRIBUTING.md
README.md
SECURITY.md
THIRD_PARTY_NOTICES.md
docs/
firmware_keil/robot_firmware_runtime.uvprojx
firmware_keil/README.md
firmware_runtime/
firmware_sim/
pyproject.toml
reports/mission-control-desktop-scene.png
reports/go1-desktop.png
reports/go1-mobile.png
reports/replay-workbench-desktop.png
reports/semantic-mission-demo.gif
scripts/
src/
tests/
web/index.html
web/package.json
web/package-lock.json
web/public/favicon.svg
web/public/assets/go1/
web/src/
web/tests/
web/tsconfig.json
web/vite.config.ts
```

`reports/` 不建议整目录无条件加入；当前 `.gitignore` 只放行 README 使用的语义任务 GIF 与 Mission/GO1/Replay 精选截图。根目录的 33.24 MiB 原始 GIF、`reports/acceptance-pack/`、transcript、日志和固件构建产物继续忽略，除非维护者逐项确认后再放行。

## 不应上传

- `.env`、API Key、Token、密码和私有 provider URL；
- `.venv/`、`node_modules/`、`web/node_modules/`；
- `.deps/` 中的第三方源码 checkout；
- `web/dist/` 和 Python `dist/` 构建产物；
- `firmware_keil/Objects/`、`Listings/`、用户级 uVision 配置和本机 build log；
- `.pytest_cache/`、`__pycache__/`、coverage 和临时文件；
- 包含本机用户名、绝对路径或工具许可证信息的未脱敏日志；
- 任何简历、面试资料、相邻 `RAG` 工程或工作区其他文件；
- 未确认许可证的图片、字体、模型权重和复制来的源码；GO1 mesh 只能使用 `web/public/assets/go1/` 中已随附 NOTICE、BSD-3-Clause 和固定来源的版本。

当前 `.gitignore` 已默认排除内部 `.codestable/`、两份 handoff、Acceptance Pack、固件构建报告和常见生成物。正式 `git add` 前仍应检查 `git status --short --untracked-files=all`，不能只依赖 ignore 规则。

## 许可证与第三方资产

1. 先确认项目自身许可证和版权持有人。
2. 确认后，把 `docs/release/LICENSE-MIT.proposed.txt` 中的占位符替换为真实信息，并在根目录生成标准 `LICENSE`。
3. 核对 `THIRD_PARTY_NOTICES.md` 与实际依赖、lockfile 和分发物一致。
4. MuJoCo Menagerie GO1 资产已随 `web/public/assets/go1/NOTICE.md`、`LICENSE`、版权声明和来源 commit 保存；任何替换或压缩都必须更新这些记录。
5. 不要暗示 Unitree、Google DeepMind、OpenAI、Qwen、DeepSeek、LangChain 或其他上游对项目提供背书。

## 发布前验证

在干净环境或新虚拟环境中运行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Push-Location web
npm ci
npm test
npm run build
Pop-Location
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pip check
```

有本机 Broker 时额外运行 bridge 演示并检查截图、transcript 和急停：

```powershell
.venv\Scripts\python.exe -m embodied_agent.cli simulation `
  --bridge --host 127.0.0.1 --port 8001 `
  --mqtt-host 127.0.0.1 --mqtt-port 1883 --device-id dog01
```

最新原工程审核基线：Python `204 passed, 1 warning`；前端 `27 passed`；固件专项 `11 passed`；真实 Broker 集成 `19 passed, 1 warning`；显式真实模型 Acceptance Pack `10 passed / 0 failed / 0 skipped`；Vite production build 通过，主 bundle `574.18 kB`（gzip `147.66 kB`）；Keil/ArmClang 成功生成 245,304-byte Cortex-M4F 静态库。20 次本机 Mosquitto + Python DeviceSimulator RTT 为 P50 `0.532 ms`、P95 `0.764 ms`、max `1.237 ms`，只代表 localhost publish-to-Observation。真实模型路径保留显式 `--include-real-model` gate：默认不调用外网；未配置 Provider 时 skipped `provider_unconfigured`，已配置但未 opt-in 时 skipped `provider_opt_in_required`，只有源事件链核验通过才记为 passed。本机已用一个配置好的 OpenAI-compatible Provider 验证 `native_tool_calls_verified`，但公开副本不包含凭据或私有端点。

加入演示 GIF 前，干净公开副本已从 `.gitignore` 实际筛出的 126 个候选文件重新创建并验证：Python 新虚拟环境可完成 `pip install -e ".[dev]"`，测试为 `198 passed, 5 skipped`；5 个 skip 全部来自公开仓库故意不包含的 `.deps/` 与可选 Keil 构建依赖。`npm ci`、前端 `27 passed`、production build、`pip check` 和 `compileall` 均通过；启动该副本的 preview runtime 后，根页面、World API、Snapshot API 和 5.36 MiB GO1 trunk STL 均返回 HTTP 200，验证完成后服务已正常关闭。此后只增加压缩演示资产并调整 README、忽略规则和发布文档，没有修改运行时代码。

2026-08-17 在原工程最终复跑：Python `204 passed, 1 warning`；真实模型显式 Acceptance Pack `10 passed / 0 failed / 0 skipped`，real-model Bundle 为 `native_tool_calls_verified`；网页 Model Mission 还验证了 `move_robot(distance_m=-0.5)` 经本机 MQTT 成功后退并正常结束。前端 `27 passed`、production build、固件专项 `11 passed`、Keil 静态库构建、`pip check`、`compileall`、`npm audit` 和 `git diff --check` 的既有基线保持通过。

CI 已在 Python job 中先构建 `web/dist`、启动本机 Mosquitto，再执行完整 Python/MQTT 测试；独立 Web job 执行 `npm test` 与 production build；新增 Firmware job 会在 Ubuntu 上获取并验证 pinned cJSON/FreeRTOS，实际编译运行 7 个 portable host/source 检查节点，不依赖或冒充 Keil/ArmClang。Firmware job 的 7 个目标节点已在本机以相同 pytest 选择器验证通过，workflow 也由 checksum 校验后的 `actionlint 1.7.12` 检查通过。这样可以避免干净 checkout 因缺少前端生成物而失败、MQTT 用例被全部跳过，或公开固件层只靠 Windows 本机证据。该 workflow 当前仅存在本地工作树，远端尚无 workflow；必须在 push 后观察首轮 Ubuntu Actions 结果，再把仓库切换为 public。

## 隐私和敏感信息检查

正式 `git add` 前至少检查：

```powershell
rg -n --hidden `
  -g '!.git/**' -g '!.venv/**' -g '!node_modules/**' -g '!web/node_modules/**' `
  "API_KEY|SECRET|TOKEN|PASSWORD|BEGIN .* PRIVATE KEY|sk-[A-Za-z0-9]" .
rg -n "[A-Za-z]:\\\\|/home/|/Users/|AppData" .
```

第二条命令会宽泛命中 Windows、Linux 和 macOS 绝对路径；源代码中的可覆盖工具链默认值可以逐项解释，用户名、桌面目录、工作区路径和历史日志必须移除。公开 README、Issue、截图和 JSON 中不应泄露个人内容。

## GitHub 仓库设置

2026-08-17 只读核对结果：仓库为 private，默认分支 `main`，Actions 已启用且当前允许全部 Actions；`main` 尚无 branch protection，Private Vulnerability Reporting API 尚不可用，远端 workflow 数量为 0。这与“本地发布候选尚未 push”的状态一致，不应在 CI job 尚不存在时提前配置 required checks。

建议在创建仓库后配置：

- 默认分支 `main`；
- GitHub Actions 只读 `contents` 权限；
- 分支保护要求 Python 和 Web CI 通过；
- 开启 Private Vulnerability Reporting；
- 开启 Dependabot alerts，是否启用自动 PR 由维护者决定；
- Topics：`llm-agent`、`langgraph`、`mqtt`、`threejs`、`robotics`、`simulation`；
- About 中明确 `simulation-first research prototype`；
- 不启用 GitHub Pages，除非先设计静态部署方式，因为当前 runtime 依赖 Python API 和 WebSocket。

建议公开顺序：

1. 仓库保持 private，提交并 push 完整发布候选。
2. 等待首轮 Python 与 Web Actions 全部通过；失败时先在 private 状态修复。
3. 给 `main` 增加 branch protection，要求 Python tests and MQTT integration、Web tests and build、Firmware host and source checks 三项通过。
4. 启用 Private Vulnerability Reporting 和 Dependabot alerts。
5. 最后切换为 public，再从未登录窗口核对 README、图片、许可证和 Actions 可见性。

## 提交与推送前的建议步骤

审核通过后再执行，不要在本轮自动上传：

```powershell
git add .
git status --short
git diff --cached --stat
```

仓库已存在 `main` 和 `origin`。在人工确认 staged 文件没有内部资料、敏感信息或未授权资产后，再创建 scoped commit 并 push；本轮不自动提交或上传。

## 审核问题

1. 是否采用 MIT 作为项目代码许可证？
2. LICENSE 中使用真实姓名、GitHub 用户名还是组织名？
3. 是否公开 C/FreeRTOS/Keil 参考层，还是只发布 Python + Web 主项目？
4. 是否公开精选 `reports/` 证据？哪些截图和 JSON 可以长期保留？
5. `.codestable/`、`HANDOFF.md` 和 `AI_HANDOFF.md` 是否保持私有？
6. 安全联系人使用哪个邮箱，是否只启用 GitHub Private Vulnerability Reporting？
