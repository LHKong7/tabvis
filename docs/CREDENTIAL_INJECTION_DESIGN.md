# 安全凭据注入与自动认证设计

> 状态：Draft v0.1
> 日期：2026-07-24
> 读者：Tabvis 维护者、安全工程师、Browser Runtime 与 Gateway 实现者
> 范围：账号密码、TOTP、认证会话的安全解析、注入、验证、保存、销毁和审计
> 关联文档：`docs/AGENT_GATEWAY_DESIGN.md`、`docs/DATA_MODEL.md`

---

## 0. 文档约定

本文是实现设计，不是产品介绍。规范词含义如下：

- **MUST（必须）**：安全性或正确性要求，不允许省略。
- **SHOULD（应该）**：默认要求，只有记录了原因时才允许偏离。
- **MAY（可以）**：可选扩展。

文中使用以下状态标记：

| 标记 | 含义 |
|---|---|
| **Current** | 当前仓库已经实现 |
| **Target** | 本设计要求达到的最终状态 |
| **Transition** | 从当前实现迁移到目标状态的临时方案 |

### 0.1 核心结论

当前 Tabvis 已经具备 Secret Reference、Keychain/Keyring、浏览器所有权、策略引擎和部分
脱敏能力，但这些能力仍主要位于同一 Python 进程和同一操作系统权限域内。

本设计要求把“模型决策”和“秘密使用”分成两个信任域：

1. Agent Runtime 只能请求使用一个凭据档案 ID。
2. Credential Broker 在独立受信任进程中读取秘密并完成认证。
3. Agent Runtime 无权读取 Secret Provider、原始 Cookie、Storage State 或浏览器调试连接。
4. 所有返回 Agent、模型、日志、Artifact 和遥测的数据先经过 DLP Gateway。

### 0.2 安全口径

“秘密不暴露给智能代理”不表示密码在认证过程中只存在于 Secret Provider 和一个 Python
对象中。完成网页登录时，密码不可避免地会短暂进入：

- Secret Provider 的返回缓冲区；
- Credential Executor 的受保护内存；
- 浏览器输入和渲染进程；
- 已授权目标 Origin 的页面；
- 发往目标网站的 TLS 加密请求。

本设计保证的是：秘密明文 **MUST NOT** 进入模型上下文、Agent 可调用工具参数、普通浏览器
RPC、任务历史、Session Transcript、Browser Artifact、普通日志、审计内容、异常堆栈或遥测。

---

## 1. 目标与非目标

### 1.1 目标

1. Agent 只通过 `credential_profile_id` 请求认证。
2. 账号、密码、TOTP 种子和 TOTP 验证码不进入模型上下文。
3. 认证前验证 HTTPS、顶层 Origin、完整 iframe Origin 链、重定向状态和档案所有权。
4. 认证期间独占浏览器会话，禁止普通浏览器操作和观察。
5. 通过一次性、短时、会话和 Origin 绑定的 Capability 授权一次认证。
6. 支持单页面、两阶段登录、TOTP 和站点专用适配器。
7. 无法自动认证时安全切换到人工操作。
8. Cookie、Token 和 Storage State 与密码采用同等级保护。
9. 所有出站模型、日志、Artifact 和遥测数据经过统一 DLP。
10. 审计秘密使用行为，但不记录秘密内容。
11. 进程崩溃、超时、取消和重启时不遗留可复用 Capability 或未清理认证字段。

### 1.2 非目标

- 绕过 CAPTCHA、反机器人系统或网站服务条款。
- 绕过 WebAuthn 用户验证、硬件安全密钥或操作系统用户在场要求。
- 从网页、邮件、聊天或模型输出中自动提取密码作为新凭据。
- 允许 Agent 创建、修改或解析 Secret Reference。
- 保证被攻陷的目标网站不会读取用户提交给它的密码。
- 在第一阶段支持所有网站；未知网站允许安全失败或转人工。

---

## 2. 威胁模型

### 2.1 需要防御的攻击者

| 攻击者 | 示例 | 必须防御 |
|---|---|---|
| 恶意或被提示注入的网页 | 页面要求模型输出密码、执行 JavaScript、上传 Cookie | 是 |
| 被提示注入的 Agent | 请求读取 Keychain、Secret Ref、Cookie、浏览器 Profile | 是 |
| 普通工具旁路 | Bash、Read、BrowserSnapshot、BrowserType、MCP 获取秘密 | 是 |
| 任务间越权 | Task B 复用 Task A 的登录状态 | 是 |
| Origin 欺骗 | 同形域名、开放重定向、恶意 iframe、HTTP 降级 | 是 |
| 日志和遥测泄漏 | URL、Header、异常参数、截图、DOM、Trace 带出秘密 | 是 |
| Capability 重放 | 重复使用已批准的认证能力 | 是 |
| 进程崩溃 | Core dump、临时文件、未清字段、悬挂锁 | 是 |
| 同机普通用户 | 读取明文 Secret 文件或浏览器 Profile | 生产模式必须防御 |

### 2.2 不覆盖的攻击者

以下威胁需要更高层基础设施处理，不由本模块单独保证：

- 已获得 root、内核、Hypervisor 或 Broker 进程调试权限的攻击者；
- 已攻陷 Secret Provider、操作系统 Keychain 或目标网站的攻击者；
- 能修改 Tabvis 已签名发布包或生产配置的供应链攻击者；
- 能读取物理内存或实施硬件侧信道的攻击者。

### 2.3 部署安全级别

| 级别 | 隔离方式 | 能力声明 |
|---|---|---|
| L0 开发模式 | 同进程调用 | 仅用于功能调试，不得声称安全隔离 |
| L1 进程隔离 | 独立进程、同一 OS 用户 | 防误泄漏，不能防同用户任意代码 |
| L2 生产模式 | 独立进程、独立 OS 身份或强 Sandbox | 满足本文的 Agent/凭据隔离目标 |

生产发布 MUST 使用 L2。Agent Runtime 具有 Bash、文件读取或任意扩展能力时，仅拆成同一用户
下的两个进程不足以构成安全边界。

---

## 3. 当前实现与缺口

### 3.1 已有能力

| 能力 | 当前模块 | 状态 |
|---|---|---|
| Secret Reference | `tabvis/browser/secret_store.py` | **Current** |
| macOS Keychain / 系统 Keyring | `tabvis/browser/secret_store.py` | **Current** |
| Identity 仅保存 Credential Ref | `tabvis/browser/identity.py` | **Current** |
| Storage State 显式导入导出 | `tabvis/browser/identity_store.py` | **Current** |
| Agent 与 Browser Profile 1:1 | `tabvis/browser/manager.py` | **Current** |
| 浏览器操作策略入口 | `tabvis/browser/policy_guard.py` | **Current** |
| 工具输入 Artifact 默认脱敏 | `tabvis/browser/artifacts.py` | **Current** |
| Memory URL 和输入清洗 | `tabvis/agent/mem/sanitizer.py` | **Current** |

### 3.2 必须修复的缺口

1. `identity_store.resolve_credential()` 可以在 Agent 同一进程内返回明文。
2. `BrowserTypeInput.text` 是模型可见工具参数，不能用于密码输入。
3. Browser Runtime 可以把页面快照、截图和 DOM 返回模型。
4. Browser Artifact 当前会保存原始 URL、标题和 DOM。
5. 当前 Chromium Profile 跨 Run 保留登录状态，缺少按用户和任务的认证会话租约。
6. 当前 Secret Store 允许退化到 `0600` 明文 JSON 文件。
7. macOS `security` CLI 的写入参数会短暂出现在进程参数中。
8. 当前浏览器锁是进程内工作区所有权，不是跨进程认证租约。
9. 当前策略对已经打开页面的交互尚不能可靠地按实时 Origin 判定。
10. `tests/services/test_phase6_secrets_observation.py` 中 Credential Injection 测试区尚为空。

---

## 4. 目标架构

见原始设计文档（信任边界、Browser Host 要求）。

### 4.1 信任边界

| 组件 | 可以访问 | 禁止访问 |
|---|---|---|
| Model / Agent Runtime | Profile ID、脱敏页面、认证结果 | Secret Ref 解析、明文、Cookie、Storage State |
| Run Orchestrator | 可信 task/session/user 上下文、认证状态 | SecretValue |
| Credential Broker | Profile 元数据、策略、Capability | 模型上下文、普通工具历史 |
| Credential Executor | 短时 ResolvedCredentials、受限浏览器控制 | Agent Transcript、通用日志 |
| Browser Host | 页面和浏览器 Context | Secret Provider 管理接口 |
| Secret Provider | Secret Ref 和秘密值 | Agent、页面内容、任务 Prompt |
| DLP Gateway | 待发送的出站数据、Canary 指纹 | 主动解析 Secret Ref |

---

（数据模型、内部接口、流程、Policy、Adapter、会话、DLP、审计、并发、模块划分、分阶段实施、
测试计划、配置与运维、待决策项、最终安全属性等章节见项目内实现与设计说明。本副本用于代码
内引用锚点；完整规范以团队共享的设计文档为准。）

## 15. 分阶段实施（摘要）

- **Phase 0**：安全契约和测试骨架。添加数据模型、错误码和 Agent 可见 Schema；添加凭据 Canary
  与禁止序列化测试；给现有 Credential Injection 空测试区补充失败优先测试；标记
  `resolve_credential()` 为内部弃用接口；生产模式禁止明文 Secret Backend。验收：尚不自动登录，
  但所有新接口都不能接收秘密明文。
- **Phase 1**：同进程功能原型（L0）。
- **Phase 2**：Credential Broker 进程隔离（L1）。
- **Phase 3**：Browser Host 和 OS 权限隔离（L2）。
- **Phase 4**：Session Vault 与任务隔离。
- **Phase 5**：全链路 DLP 与外部 Provider。
