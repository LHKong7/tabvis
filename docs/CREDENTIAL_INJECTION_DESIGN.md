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

## 15. 分阶段实施（摘要与实现状态）

- **Phase 0 ✅**：安全契约和测试骨架。`tabvis/authentication/`（models、errors、policy、
  capabilities、profile_store、audit、secrets）、`tabvis/dlp/canary.py`、Agent 工具
  `BrowserAuthenticate`（仅 `credential_profile_id`）、`secret_store` 生产模式禁止明文后端、
  `resolve_credential()` 弃用。
- **Phase 1 ✅**：同进程功能原型（L0）。`totp.py`（RFC 6238）、`approval.py`、
  `policy_engine.py`、`adapters/`（受限 AuthenticationBrowser、generic_password、registry）。
- **Phase 2 ✅**：Credential Broker 进程隔离（L1）。`tabvis/credential_broker/`
  （secrets providers、executor、broker、protocol+server 的 Unix socket + SO_PEERCRED、
  hardening）、`authentication/broker_client.py`。
- **Phase 3 ✅**：Browser Host 和跨进程认证租约。`browser/auth_lease.py`、
  `browser/auth_browser.py`、`browser/host.py`，`policy_guard` 在认证期间拒绝普通浏览器工具
  （`browser_authentication_locked`）。L2 的独立 OS 身份/沙箱为部署配置。
- **Phase 4 ✅**：Session Vault 与任务隔离。`tabvis/session_vault/`（Envelope Encryption、
  AAD 绑定 user+task+profile+session、任务级隔离与跨任务复用门控、级联删除）。
- **Phase 5 ✅**：全链路 DLP 与外部 Provider。`tabvis/dlp/`（gateway、url、text、image）、
  Canary 命中即失败关闭、1Password/Vault Provider。DLP 接入现有各出口的落地为后续集成工作。
- **Phase 6 🚧**：Managed Authentication Runtime Integration。首批运行时集成已落地（可信上下文、
  Tool→Broker 调用链、Playwright Controller、租约心跳/取消、Vault 生命周期和主要 DLP 出口）；
  L2 部署与完整生产安全验收仍在进行。把 Phase 0–5 的独立组件接入
  Gateway、CLI、Run Orchestrator 和真实 Playwright Browser Runtime，形成可启用、可取消、
  可恢复并可通过生产安全验收的端到端认证链路。详细计划见 §16。

> 说明：Phase 0–5 的组件级安全逻辑已实现并有单元测试覆盖，但尚未形成生产端到端链路。
> L2 强隔离（独立 OS 用户/容器/远程 Worker）、真实 Playwright Browser Host，以及把 DLP
> Gateway 接入每一个出口，统一纳入 Phase 6；相关部署选择仍依赖完整规范中的 §18 决策项。

## 16. Phase 6 — Managed Authentication Runtime Integration

### 16.1 目标与完成定义

Phase 6 的目标不是新增另一套认证核心，而是把 Phase 0–5 已有的模型、Broker、Executor、
Browser Host、Session Vault 和 DLP 组件装配进默认运行时。完成后：

1. `TABVIS_AUTHENTICATION_ENABLED=1` 暴露的 `BrowserAuthenticate` 必须执行真实认证，不得再
   固定返回 `internal_authentication_error`。
2. Agent 仍然只能提交 `credential_profile_id`；`task_id`、`user_id`、`agent_id`、
   `browser_session_id`、Origin 和 Capability 必须来自可信运行时。
3. 开发模式可以使用 L0 便于调试；L1 使用独立 Broker 进程；生产模式必须使用 L2，并在无法满足
   隔离、审计或安全 Secret Provider 要求时失败关闭。
4. 认证成功、失败、超时、取消、进程崩溃和 Run 结束都必须有确定的清理路径。
5. 所有可能离开受信任域的数据必须经过统一 DLP Gateway。

Phase 6 未通过 §16.10 的验收门槛前，托管认证保持默认关闭，不得宣称生产可用。

### 16.2 Gateway / CLI 的 Broker 生命周期与配置装配

Gateway 与 CLI 必须复用同一个 composition root，负责构建并管理托管认证依赖：

- 根据 `TABVIS_CREDENTIAL_BROKER_MODE` 选择 L0、L1 或 L2；不允许生产配置静默降级到较低级别。
- 解析 `TABVIS_CREDENTIAL_BROKER_ENDPOINT`，校验 Unix socket 路径、父目录权限、所有者和平台
  长度限制；未显式配置时使用短且权限受控的运行时目录。
- 在 `--serve` 启动时启动或连接 Broker，执行 startup hardening 和健康检查；关闭时停止接收新
  请求、等待或取消在途认证、删除 socket，并回收过期 Capability 和租约。
- one-shot CLI 只为当前进程/Run 建立短生命周期 Broker；无论正常退出还是异常退出都执行清理。
- 装配 Profile Store、Secret Provider、Approval Service、Audit Sink、Capability Store、
  Browser Host client、Session Vault 和 DLP Gateway，不允许各入口自行创建不一致的单例。
- 配置缺失、Secret Provider 不安全、审计不可用或 Broker 不健康时，工具应返回稳定错误码并且
  不解析任何秘密。

### 16.3 Orchestrator 注入可信运行上下文

Run Orchestrator 是 Agent 请求与 Broker 内部请求之间唯一可信的上下文桥梁：

- Agent 工具输入继续由 `AgentAuthenticationRequest` 校验，字段只能是
  `credential_profile_id`，额外字段必须拒绝。
- Orchestrator 从当前 Durable Agent、Run、Browser Binding 和认证主体中读取
  `task_id`、`user_id`、`agent_id` 和 `browser_session_id`，并生成唯一 `request_id`。
- 不得信任模型消息、工具参数、URL 参数或客户端请求体中同名字段。
- 创建内部 `AuthenticationRequest` 前必须检查 Run 仍处于可执行状态、Browser Binding 属于
  当前 Agent、用户主体未变化且请求尚未取消。
- 取消状态必须持续传播到 Broker/Executor；不能只在发起请求时做一次快照。
- 可信上下文缺失、冲突或过期时失败关闭，并返回稳定错误码，不把内部标识或异常细节返回 Agent。

### 16.4 `BrowserAuthenticateTool → BrokerClient → Broker` 调用链

`BrowserAuthenticateTool.call()` 必须改为调用由运行时注入的认证服务，而不是直接读取
Secret Store 或 BrowserService：

1. 工具校验 `credential_profile_id` 并向 Orchestrator 提交 Agent-visible 请求。
2. Orchestrator 使用 §16.3 的可信字段调用 `enrich_request()`。
3. 根据运行模式选择 `InProcessBrokerClient` 或 `SocketBrokerClient`，设置总超时并绑定取消信号。
4. Broker 重新读取 Profile 所有权和实时浏览器上下文，执行 Policy、Approval、Capability 和
   Executor 流程。
5. 返回值必须重新通过严格的 `AuthenticationResult` schema，只允许
   `success`、`authenticated_origin`、`requires_human_interaction` 和 `error_code`。
6. IPC 断开、超时、Broker 重启或响应格式错误时，Capability、租约和敏感字段必须被清理，工具
   只返回稳定错误码。

Broker 的并发控制必须在一个原子临界区中完成“检查并占有”，同一 Browser Session 也不得同时
执行两个认证。`max_uses` 的检查与成功计数更新必须具备同等级的原子性或持久化事务保证。

### 16.5 真实 Playwright `PageController`

Browser Host 内实现 Playwright-backed `PageController`，并保持受限接口：

- Browser Host 独占 `Browser`、`BrowserContext`、`Page`、CDP endpoint 和 profile path；这些对象
  或地址不得返回 Agent、Broker 或 Executor。
- `current_context()` 从实时 Playwright frame tree 计算 top-level/frame/ancestor Origins、
  `page_id` 和单调递增的 `navigation_generation`，不能接受调用方声明的 Origin。
- `find_field()` 只返回 Browser Host 生成的短生命周期 opaque handle；handle 必须绑定
  page/frame/navigation generation，并在导航或租约结束时失效。
- `type_bytes()` 是秘密进入浏览器的唯一接口。每次输入前重新验证 Capability、页面、完整 frame
  链、HTTPS 和证书状态；实现不得把秘密写入普通日志、trace、异常参数或 Agent-visible tool input。
- `clear_fields()` 必须覆盖 username/password/TOTP 字段；无法确认清理成功时销毁整个
  BrowserContext，而不是把上下文归还普通浏览器工具。
- 认证期间禁止 screenshot、DOM snapshot、evaluate、download 和普通 browser RPC；认证后的第一
  次可见捕获应用 `post_auth_redaction_spec()`。

### 16.6 认证租约心跳、取消与崩溃恢复

- `begin_authentication()` 获取租约后必须自动启动心跳任务，心跳间隔不得超过 TTL 的三分之一；
  调用方不应依赖手工调用 `lease.heartbeat()`。
- 心跳续租、过期回收和释放必须使用跨进程原子机制，校验 `lease_id` 后再更新或删除，禁止旧持有者
  覆盖新租约。
- 用户取消、Run 取消、总超时、Broker/Executor 崩溃和 Browser Host 断连都必须触发同一清理
  状态机：停止输入、失效 Capability、清理字段、终止心跳、释放租约，并在不确定时销毁 Context。
- Browser Host 启动时回收过期租约；回收期间普通 Agent RPC 仍然失败关闭。
- 人工确认、push MFA 等长流程必须在显式总超时内保持租约，不得因固定 120 秒 TTL 静默解锁。

### 16.7 Session Vault 创建、恢复与清理

- 只有在强认证成功信号成立、最终 Origin 合法且敏感字段已清理后，Browser Host 才能导出
  `storage_state` 并交给 Session Vault 加密保存。
- 创建会话时使用 Profile 的 TTL、可复用策略和 allowed Origins；持久化内容只能是加密 envelope，
  不得回退到明文文件。
- 恢复前重新检查同一用户、任务绑定、Profile 状态、过期/撤销状态和 requested Origins；恢复操作
  必须发生在 Browser Host 内，原始 cookie/token/storage state 不得经过 Agent 或普通 Gateway API。
- Run/Task 结束时删除不可跨任务复用的会话；Profile 禁用、删除、用户撤销或密钥轮换失败时执行
  级联删除；启动和定时维护时清理过期记录。
- 解密失败、key id 不匹配或记录损坏时失败关闭并删除不可恢复记录，不得返回部分状态。

### 16.8 DLP 出口接入

运行时只创建一个统一 `DLPGateway`，以下出口在序列化或写入前必须调用它：

- 模型请求与工具结果；
- Session Transcript、Run 事件和 Context Pack；
- Browser Artifact、截图元数据、下载元数据和错误页面摘要；
- 普通日志、审计、遥测和 crash report；
- Gateway/legacy HTTP API 与 SSE 响应；
- 临时文件、调试 trace 和人工交接材料。

DLP 必须同时处理 header、URL、嵌套 mapping/list、Pydantic model 和其他实际使用的 payload
形态。Canary 或禁止对象命中时必须阻止整个出口，并触发统一响应：失效 Capability、结束认证租约、
把关联 Session 标记为不可复用、写入不含秘密的 `dlp.secret_blocked` 审计事件。DLP hook 自身失败
不能把阻止结果改成放行。

### 16.9 实施顺序

建议按以下可独立验收的切片交付：

1. **Composition**：Gateway/CLI 生命周期、配置校验、Broker health 与安全启动/停止。
2. **Trusted call path**：Orchestrator 上下文注入和
   `BrowserAuthenticateTool → BrokerClient → Broker`。
3. **Browser isolation**：真实 `PageController`、跨进程租约、心跳、取消和 Context 销毁。
4. **Session lifecycle**：Session Vault 保存、恢复、Run/Task 清理和撤销级联。
5. **Egress enforcement**：DLP 接入全部出口并删除现存旁路。
6. **Production gate**：L2 部署、跨平台测试、故障注入、安全评审和发布文档。

每个切片必须默认关闭或保持失败关闭，不能为了让后续切片可开发而临时把秘密暴露给 Agent。

### 16.10 测试与生产安全验收

Phase 6 至少需要以下自动化测试：

- 一条真实工具调用的端到端测试，证明启用后不再固定返回
  `internal_authentication_error`，并且 Agent 只看到 `AuthenticationResult`。
- Gateway daemon 与 one-shot CLI 两种 composition 的启动、健康、关闭和崩溃恢复测试。
- 并发认证、同 session 重入、`max_uses`、Capability 重放和租约过期/回收竞态测试。
- 导航、iframe 切换、HTTP 降级、证书错误、恶意 selector/handle 和页面替换的对抗测试。
- 用户取消、Run 取消、Broker/Browser Host/Executor 崩溃、IPC 截断和超时的故障注入测试。
- 长于默认租约 TTL 的人工 MFA 流程测试，证明心跳期间普通 Browser RPC 始终被拒绝。
- Session Vault 的同用户/跨任务/Origin 约束、过期、撤销、密钥错误和无明文回退测试。
- 对模型、Transcript、Artifact、日志、审计、遥测、API/SSE 和 crash report 的 DLP Canary
  测试，并覆盖混合 header/body、嵌套对象和模型对象。
- Linux/macOS 的 Unix socket、peer credential、权限和路径长度测试；生产支持 Windows 时补充
  等价 IPC 与 ACL 测试。
- 安装矩阵测试，证明基础安装或明确的 managed-auth extra 包含 Session Vault 所需加密依赖。

生产发布必须同时满足：

1. L2 隔离通过安全评审，Agent Runtime 无法读取 Secret Provider、Broker 内存、Browser profile、
   CDP endpoint、原始 cookie 或 storage state。
2. `TABVIS_AUTH_AUDIT_FAIL_CLOSED=1` 时，审计写入失败会阻止认证成功，而不是吞掉异常继续执行。
3. 任何失败路径都不会在模型上下文、工具参数、日志、异常、Artifact、Transcript 或 API 中出现
   测试秘密。
4. 完整测试、静态检查和跨平台 IPC 测试通过，且没有依靠跳过安全测试获得绿色结果。
5. 运维文档说明配置、启动、密钥轮换、撤销、审计、故障恢复和降级策略。

### 16.11 当前编码进度

已落地：

- `authentication/runtime.py` 成为 CLI/Gateway 共用 composition root。开发模式装配 L0
  `CredentialBroker`；`ipc`/`production` 使用经过路径、类型、所有者和 `0600` 权限校验的 Unix
  socket。生产模式缺少显式 L2 验证或安全 Secret Backend 时启动失败关闭。
- `ToolUseContext` 由 Orchestrator 注入 principal/run/session/browser binding；
  `BrowserAuthenticateTool` 只构造 `AgentAuthenticationRequest`，经 Runtime、BrokerClient 和
  Broker 执行，并重新校验严格的 `AuthenticationResult`。
- Browser Host 已接入真实 `PlaywrightPageController`、BrowserService 动作互斥、跨进程租约锁、
  自动心跳、取消/超时 Capability 失效、字段清理、必要时 Context 销毁和首次认证后截图遮罩。
- Broker 的并发占有改为原子临界区；Session Vault 已接入成功创建、强 Cookie 信号恢复、任务结束
  清理、Profile 禁用/删除级联撤销和启动过期清理。
- 模型工具结果、Transcript、Context Pack、Run preview、Browser Artifact、HTTP/SSE 和认证审计
  已接入统一 DLP Gateway；Canary 命中会使在途认证 Context 标记为销毁、清空 Capability 并撤销
  活跃 Profile 的 Vault session。
- 自动化测试覆盖 Tool→Runtime→Broker→Browser/Vault、可信上下文缺失、取消、租约心跳、Broker
  并发、审计失败关闭、DLP 混合对象和真实 Chromium PageController。

尚未作为“Phase 6 完成”验收：

- L2 Broker 的独立 OS 用户/容器/远程 Worker 启停与健康协议仍属于部署工作；当前 production gate
  会在未显式确认 L2 时拒绝启动。
- 仍需完成 Linux/macOS IPC 矩阵、Broker/Browser Host/Executor 崩溃故障注入、真实 MFA 长流程、
  全部遥测/crash-report/临时 trace 出口审计，以及正式安全评审和运维 runbook。
