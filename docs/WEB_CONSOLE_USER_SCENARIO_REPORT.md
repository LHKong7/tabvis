# Tabvis Web 控制台真实用户场景测试报告

- 测试日期：2026-07-25
- 测试地址：`http://localhost:8765/`
- 测试方式：通过浏览器实际点击、筛选、导航、刷新、复制和提交无变更设置
- 桌面环境：默认浏览器视口
- 移动环境：`390 × 844`
- 测试数据：服务中已有 4 个历史会话（3 个 completed、1 个 cancelled）

## 结论摘要

本轮共执行 10 个真实用户场景：

- 5 个通过
- 4 个部分通过
- 1 个失败

仪表盘、会话筛选、浏览器状态、设置读取/无变更保存、命令复制、SPA 深链刷新等基础能力正常。
但核心的 **New run / Continue 流程会白屏**，导致当前数据状态下无法从 Web 控制台创建或继续任务，
应作为最高优先级问题处理。

## 场景结果

| # | 用户场景 | 实际操作 | 结果 | 反馈 |
|---|---|---|---|---|
| 1 | 打开控制台查看系统概况 | 打开首页，等待健康轮询完成 | 通过 | 正确显示 running、capacity、sessions、open browsers 和当前浏览器引擎；从 `connecting…` 更新为实际状态约需一次轮询周期。 |
| 2 | 查看并筛选历史会话 | 进入 Sessions，点击 `completed 3` | 通过 | 列表从 4 条正确过滤为 3 条，筛选状态清晰，状态计数正确。 |
| 3 | 查看已完成会话详情 | 打开一条 completed 会话 | 部分通过 | 能显示状态、agent/session id、turn/tool 数和耗时；但当前历史记录没有显示 prompt/result，Live stream 也为空，用户无法回看任务和最终答案。 |
| 4 | 创建新任务或继续历史会话 | 分别点击 New run，以及在详情页点击 Continue | **失败** | 两个入口都会导航到 `/run` 后白屏。控制台报错：`TypeError: Cannot read properties of undefined (reading 'slice')`。 |
| 5 | 查看浏览器驱动和运行状态 | 进入 Browser 页面，检查活动引擎、驱动状态和打开的浏览器 | 通过 | 活动引擎、kernel、连接模式、安装状态和 Open browsers 均能正确展示。页面信息完整，但 21 个驱动与高级选项较长。 |
| 6 | 查看设置并保存 | 进入 Settings，确认配置加载；不修改任何值点击 Save & apply | 通过 | 设置正确加载；无变更保存不会改写配置，并显示 `applied live — no restart needed`。页面非常长，缺少分组导航、搜索或折叠。 |
| 7 | 按安装说明复制命令 | 进入 Setup，点击 Install 区域的 copy | 部分通过 | copy 正确变为 copied；启动命令已包含 `uv run tabvis` 和 `--serve` 两种方式。但安全说明仍称服务“没有认证”，与当前非 loopback 强制 Token 的实现不一致。 |
| 8 | 直接打开并刷新深层会话链接 | 打开 `/sessions/<id>`，然后刷新页面 | 通过 | 刷新前后都能恢复同一会话详情，说明生产 SPA fallback 工作正常。 |
| 9 | 访问不存在的前端路径 | 打开 `/not-a-real-page` | 部分通过 | 页面静默显示 Dashboard，但地址栏仍保留错误路径；没有 404、重定向或“页面不存在”提示，容易让用户误以为链接有效。 |
| 10 | 在手机宽度下使用控制台 | 将视口设为 `390 × 844`，检查首页和导航 | 部分通过 | 页面没有横向溢出，内容宽度和顶部导航适配正常；但导航文字被 CSS 隐藏后，可访问名称只剩 `◧`、`＋`、`≣` 等符号，对读屏和语音控制不可理解。 |

## 主要问题与优先级

### P0：New run / Continue 白屏，核心流程不可用

复现步骤：

1. 服务中存在至少一条 `prompt` 缺失的历史 Agent 记录。
2. 点击侧边栏 New run，或在历史会话详情中点击 Continue。
3. 页面进入 `/run` 后变成空白。

浏览器错误：

```text
TypeError: Cannot read properties of undefined (reading 'slice')
```

直接原因位于 `web/src/components/NewRun.tsx`：

```tsx
{agents.map((a) => (
  <option key={a.agent_id} value={a.agent_id}>
    Continue {a.agent_id} · {a.status} · {a.prompt.slice(0, 40)}
  </option>
))}
```

当前 `/agents` 返回的数据中至少有记录缺少 `prompt`，列表页也能看到对应的空白摘要。前端却把
`prompt` 当作必填字符串处理。

建议：

1. 前端立即做容错：`(a.prompt || 'No prompt').slice(0, 40)`。
2. API/兼容投影层保证 `prompt` 和 `result` 始终为字符串，而不是缺失或 `null`。
3. 给 `/run` 增加 Error Boundary，单条脏数据不应使整个页面白屏。
4. 增加“历史记录缺少 prompt/result”的回归测试。

验收标准：

- 任意历史记录字段缺失时 `/run` 仍可打开。
- New agent 可以提交。
- Continue 可以选择目标 Agent 并提交。
- 页面不出现未捕获异常或空白屏。

### P1：已完成会话无法有效回看结果

详情页只对当前内存中的 live run 显示 `frames`。刷新或打开历史会话时，Live stream 固定为空；
若兼容记录中也没有 `result`，页面只剩元数据。对真实用户而言，“完成了但看不到答案”等于任务结果丢失。

相关实现：

- `web/src/pages/SessionDetailPage.tsx`：历史会话传给 Stream 的是空数组。
- `web/src/components/Detail.tsx`：只有 `agent.result` 存在时才显示 result。

建议服务端提供持久化事件/最终结果读取接口，详情页加载历史事件或至少始终展示最终 answer、prompt 和错误。

### P1：Setup 的安全说明与实际认证策略不一致

Setup 当前写着“没有认证，非本机部署需要认证代理”。但服务端已经实现：

- 非 loopback 地址自动要求认证。
- 未配置 `TABVIS_SERVER_ADMIN_TOKEN` 时拒绝启动。
- 管理请求支持 Bearer Token。

相关实现位于 `tabvis/browser/server_auth.py`。过时说明可能让用户误判部署风险或重复搭建认证层。

建议根据 bind host 和认证状态动态展示：

- loopback：仅本机可达，默认无需 Token。
- 非 loopback：必须设置 `TABVIS_SERVER_ADMIN_TOKEN`，并给出请求头示例。
- 反向代理：作为额外加固方案，而不是唯一认证方式。

### P2：移动导航缺少可访问名称

`web/src/index.css` 在小屏下隐藏 `.nav-item` 中除图标外的所有文字，但 `NavLink` 没有
`aria-label`。实测可访问名称变成单个符号。

建议为每个导航链接添加 `aria-label={n.label}`，并为当前页面保留 `aria-current="page"`。

### P2：未知前端路由静默显示 Dashboard

`web/src/App.tsx` 的通配路由直接渲染 `<Dashboard />`，既不重定向也不提示错误。

建议二选一：

- 渲染明确的 Not Found 页面，并提供返回 Dashboard 的按钮。
- 使用 `<Navigate to="/" replace />`，确保地址栏同步恢复为 `/`。

### P2：Browser / Settings 信息密度过高

Browser 页面一次展示 21 个驱动和大量高级 stealth 设置；Settings 页面一次展示所有 Model、
Browser、Stealth、Server、OCR、Artifacts 和 Project 字段。功能完整，但新用户难以定位目标。

建议：

- 增加设置搜索。
- 分组折叠或左侧锚点目录。
- 默认收起与当前引擎不相关的字段。
- 为多个 `Download` 按钮加入具体名称，例如 `Download CloakBrowser`。

## 正向反馈

- 首页健康状态与容量信息简洁，轮询后状态更新正确。
- Sessions 的状态筛选和计数直观、响应及时。
- 生产构建支持深层链接刷新，SPA fallback 行为可靠。
- Browser 页面能明确区分 active engine、kernel、connection 和安装状态。
- Secret 字段采用 write-only 设计，没有把完整凭据回传到页面。
- Settings 只提交变化字段；无变更保存不会意外固化默认值。
- Setup 的复制按钮在本地 HTTP 页面也能工作。
- `390 × 844` 下没有横向滚动，基础响应式布局成立。

## 建议修复顺序

1. 修复 `/run` 白屏并补 Error Boundary。
2. 补齐历史会话的 prompt、result 和事件回放。
3. 更新 Setup 认证说明。
4. 修复移动导航可访问名称。
5. 增加 Not Found 行为。
6. 优化 Browser / Settings 的信息架构。

## 测试影响说明

本轮没有安装驱动、切换浏览器引擎、取消/退出 Agent，也没有修改用户设置。唯一提交动作是
Settings 的“无变更保存”，前端检测到没有差异后直接返回；另执行了一次本地剪贴板复制。
由于 New run 页面白屏，本轮没有创建新的 Agent 任务。
