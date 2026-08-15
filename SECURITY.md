# Security Policy

## Supported versions

项目仍处于 `0.1.x` 研究原型阶段，只维护仓库最新版本，不承诺历史版本的安全更新或长期支持。

## Reporting a vulnerability

仓库公开并启用该功能后，请优先通过 [GitHub Private Vulnerability Reporting](https://github.com/yile180304/embodied-robot-llm-agent/security/advisories/new) 私下报告安全问题。不要在公开 Issue 中粘贴 API Key、Broker 凭据、私有 Base URL、模型原始敏感输出、个人数据或可直接利用的漏洞细节。

报告中请包含：

- 受影响的版本或 commit；
- 最小复现步骤；
- 预期影响和攻击前提；
- 已知的临时缓解措施；
- 与 MQTT、模型 provider、文件系统或代码执行相关的日志时，请先移除凭据和个人信息。

## Project security boundaries

- 本项目禁止执行模型生成的任意代码。
- 模型只能调用 Tool Registry 中注册的高层 Tool。
- API Key 只能通过 `.env` 或系统环境变量提供，仓库只保留空值 `.env.example`。
- 默认 MQTT 示例面向受信任的本机 Broker，不提供面向公网的认证、TLS 或多租户隔离方案。
- 仿真急停和 Safety 不能替代真实机器人上的硬件急停、限位、制动或功能安全设计。

当前不公开维护者邮箱；若 GitHub Private Vulnerability Reporting 尚未启用，请先在不含漏洞细节的公开 Issue 中请求私密联系方式。
