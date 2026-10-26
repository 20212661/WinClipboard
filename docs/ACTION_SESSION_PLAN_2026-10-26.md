# PR 3B 设计草案:统一 Action Session(2026-10-26,只读规划)

> **性质**:只读规划,本轮未修改任何源码。实施另开 PR。
> 前置:PR 3A 已落地 `ai/` 包(Registry + ActionRequest/ActionResult 契约,de03bb2)。
> 方向:Clipboard-first AI Workspace——轻量交互、AI 可选增强;不做完整 Agent 平台。

## 一、四条 AI 会话路径现状(以 main 工作区为准,行号为 de03bb2 时点)

| # | 路径 | 线程封装 | 隐私门 | 服务解析 | 上下文附加 | 结果出口 | 在途/取消 |
|---|------|---------|--------|---------|-----------|---------|----------|
| 1 | 快速 AI 轮盘 QuickAIWindow(ai_quick.py:185-508) | `spawn_chat` daemon Thread(ai_quick.py:86) | `_gate`(:373) | **下拉索引直取 dict**(:418) | `_assemble_context_or_none`(:382) | 四出口(:462-491) | `_serial/_active/_pending`(:196,434-460) |
| 2 | 右栏 AI 工作台 EmbeddedAIPanel(ai_quick.py:511-1030) | 同上共用 | `_gate`(:911,与 #1 逐行同文) | 下拉索引直取(:949) | 同名复刻(:899) | 同构四出口(:995-1026) | 同款字段再写一遍(:526,965-991) |
| 3 | 工作流 WorkflowRunner(workflow_ui.py:1233+) | `AIWorker` QThread(workflow_ui.py:71) | `_privacy_gate`(:1658) | `cm.ai_service_by_name` 空名=第一个(:1555) | `resolve_wf_context`(:1207,启动恒定) | runlog+结果卡,复制最新(:1705) | Step Contract `Job.cancel`(协作式) |
| 4 | 上下文篮提问 AskContextDialog(ctxpack/ui_ask.py:181-251) | 复用 AIWorker(:215) | `_gate`(:224) | services[0](:220) | assemble 内建 | 第三变体四出口(:268-288) | `_zombies` 弃引用(:239) |

**共性**:HTTP 全部单点 `cm.ai_chat`(clipboard_manager.py:3392)——会话层不需要也不应该再造出口。
**差异痛点**:隐私门 ×4、服务解析两套、上下文附加 ×3、结果出口 ×3(签名还不一致)、在途管理 ×2;#1/#2 两面板 90% 同构是最大去重收益。

## 二、统一 Action Session 设计(未来 `ai/session.py`)

**定位**:单次 AI 调用的执行核(授权→解析→快照→执行→出口),供四条路径共用;**不是**编排器——工作流的多步串联/人工步骤/runlog 仍归 WorkflowRunner(Step Contract 不动),session 只替换其 `_start_ai` 底层。

```python
class ActionSession:
    """一次 AI 会话:材料与上下文在 start 时冻结(快照语义)。"""

    def __init__(self, host, *, source, on_done, on_state=None):
        # source ∈ {"quick","embedded","workflow","ask"}——仅用于
        # 日志/错误文案区分,不改变行为
        ...

    # ---- 五个关注点,各一个注入口 ----
    def set_material(self, text, rec=None): ...       # 材料:64K 截断;rec 保留记录身份(工作流入口复用)
    def attach_context(self, pack_ref=None): ...      # 统一走 ctxpack 拼装(assemble_active/resolve_wf_context
                                                      # 汇成一个入口),start 时恒定快照
    def resolve_service(self, name=""): ...           # 统一 cm.ai_service_by_name(空名=第一个);废弃"下拉直取"
    def authorize(self, prompt): ...                  # 隐私授权:privacy_scan → 宿主注入的确认 UI
                                                      # (生产=PrivacyGateDialog 三选一;PR2.5 语义:
                                                      #  mask/raw/cancel,取消抛 SessionCancelled)
    def start(self, prompt, service=None): ...        # 执行:后台线程调 cm.ai_chat,请求序号防串台
    def cancel(self): ...                             # 丢弃迟到结果(HTTP 在途不可中断,维持现状)

class ResultSink:                                     # 结果出口单实现(替换 ×3 变体)
    def emit(self, answer, *, origin_hwnd=None): ...  # 复制/贴回原窗/入史/入包 四动作统一签名
```

**与 PR 3A 契约的衔接**:`ActionRequest(auto=True)` 目前直连 `host.run_workflow`;PR3B 落地后,`executor._execute` 可选经 session 执行(获得统一取消/状态回调),契约字段不变。

**状态模型**:`idle → authorizing → running → done|cancelled|failed`;on_state 回调供 UI 展示(替换各面板自己的状态标签逻辑)。迁移后事件顺序保持「先授权后发送」不变。

## 三、迁移顺序建议(实施 PR 切分)

| 步 | 内容 | 验收 |
|---|------|------|
| 3B-1 | 抽 `ai/session.py` + ResultSink;**先迁 ai_quick 两面板**(收益最大:同构 90% 直接删重) | quick-ai 套件全绿;两面板行为/快捷键不变 |
| 3B-2 | AskContextDialog 换 session(删 `_zombies`/自带 `_gate`) | ctxpack-ui/ctxpack-workflow 全绿 |
| 3B-3 | WorkflowRunner `_start_ai` 底层换 session(AIWorker 退役;Step Contract/Job.cancel/人工步骤不动) | ai/ai-actions/ctxpack-workflow 全绿,runlog 语义不变 |
| 3B-4 | `executor.run_request` 增加经 session 的执行路径;UI 入口(卡片菜单/轮盘)按 Registry 迁移(含图标白名单) | 三入口同 action_id 行为一致(验收标准沿用审计 PR3) |

## 四、风险与守则

> **实施状态(2026-10-26 更新)**:3B-1 ✅(017dff1/7430ffc,双面板迁 ChatSession+面板信号断链修复)→ 3B-2 ✅(0242b66/f8b394f ResultSink 三调用方统一;6320712 AskContextDialog 接入)→ **3B-3 ✅ 验收通过**(30708af AIStepBridge 适配器+8 单测 → 16bbb81 WorkflowRunner._start_ai 接线 → PingSession 替换 AIWorker/移除 _worker_finished,wq 兼容重导出同步;四条会话路径全部统一到 ai.session.ChatSession,cm.ai_chat 唯一出口;门禁 17/18,perf-semantics 仅 E 项环境抖动,双文件 A/B 于本轮起点代码复现同款失败=与本轮无关)。剩余 3B-4(UI 入口按 Registry 迁移+图标白名单)未启动。**3B-4 ✅ 验收通过(2026-10-26)**:d9ec9dc 入口动作定义接入 Registry(translate/summarize 同 id 统一指令源,build_prompt 增 cfg;explain/polish/ask 未覆盖走本地兼容路径;AI_ACTION_ICONS 白名单+安全回退)+双入口同 Prompt 逐字节一致测试;80a5347 六内置 E2E+定义同源+打包资源断言。阶段一真实验收 12/12(右栏回显/篮双材料提问/双步自动/中途停止;停止入口=主按钮 _on_next 非 btn_auto)。**Action Engine 线(3B-1..3B-4)全部完成;后续只剩审计 PR4 的残项盘点。**

1. **不动的**:cm.ai_chat、粘贴事务、剪贴板监听、加密、数据库、WorkflowRunner 编排层。
2. **线程模型切换风险**(daemon Thread → QThread 统一):quick-ai 现有"QApplication 已销毁时静默丢弃 RuntimeError"的纪律必须保留(session 内置,不依赖调用方)。
3. **隐私语义冻结**:取消=不发送不复制(PRB2.5 已固化);session.authorize 是唯一授权点,任何路径不得绕过(含 WebStep 的剪贴板路径)。
4. **取消语义如实**:HTTP 在途不可中断,取消=丢弃迟到结果——文档与 UI 文案不承诺"中断请求"。
5. 每步独立提交、独立回滚;快照语义(材料/上下文 start 时冻结)在任何一步不得放松。
