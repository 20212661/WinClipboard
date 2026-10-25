# WinClipboard 架构审计与重构规划(2026-10-26)

> **性质**:只读架构审计,本轮未修改任何源码(仅新增本文档)。
> **基线**:main @ `2da5e9d` + 工作区未提交改动(Warm Paper 改版,9 文件 910 行插入,含今晨补修的 5 处缺口)。
> **方法**:全部结论以工作区实际源码为依据,四路并行深读(UI 结构 / AI 调用链 / 上下文与隐私 / 平台设施),关键结论均带 `file:line`。
> **未合并分支**:feature/ai-action-bar、feature/command-palette、feature/agent-planner、feature/cas-transactions-privacy、feature/ctx-pack(已合入 main 的是 ctx-pack;其余四条仍在分支上,fork 点 2ddad71,2026-09-22)。

---

## 0. 结论速览

1. **HTTP 层已经收敛**:`cm.ai_chat`(clipboard_manager.py:3392)是全项目唯一供应商调用出口,四条 AI 链全走它,无旁路。要建的不是 HTTP 引擎,而是它上面的**会话层**(线程封装 ×3、隐私门 ×4、服务解析 ×2 种、上下文附加 ×3、结果出口 ×3)。
2. **上下文篮已经是快照语义**(ctxpack/model.py:2-7):入包即固化内容,删历史/容量淘汰/换加密模式都不影响包内条目——外部建议担心的"删历史导致工作流读不到材料"在 main 上**不存在**;工作流 {context} 启动时一次性拼装恒定(workflow_ui.py:1243-1254),运行中改篮只影响下一次。
3. **隐私门是"两级模型"且每个 AI 出口都有最后一道**(quick AI ai_quick.py:373/911、工作流 workflow_ui.py:1658、ctxpack 提问 ui_ask.py:224);真实缺口只有两处:WebStep 复制提示词不过扫描(workflow_steps.py:239-261)、入包 file_list 存路径快照无存在性检查。
4. **Action Engine 不必从零建**:feature/ai-action-bar 的 `ai/` 包(声明式动作 JSON + Registry + executor 复用 WorkflowRunner)正是所需粒度,且已被 command/(发现层)与 agent/(高阶运行时)两个分支验证过依赖方向;`ai/` 纯 Python 无 Qt,可近乎原样移植,但需适配 main 的 `run_workflow(wf, rec, auto=)` 新签名。
5. **右栏化迁移已经在工作区做了一半**:Warm Paper 改版已建「预览/处理」双页签右栏(winclipboard_qt.py:850-903,detail_stack 挂 preview+ai_panel)——外部建议的"底部预览合并至右侧"方向与现状一致,只差收尾。
6. **工作区有一个现管断链 bug**:`set_detail_mode` 被 connect(881-882)但全工程无定义,点右栏「预览/处理」页签即崩;`toggle_preview`(1613)成孤儿方法。与今晨已修的 `_show_page_menu` 等 4 处属同类(并行改版遗留)。
7. **QuickAIWindow 与 EmbeddedAIPanel 90% 同构**(ai_quick.py:185-508 vs 511-1030),是全项目最大的一块可去重代码。
8. **入口重复度**:查看内容 7 入口、AI 操作 9 入口、加入上下文篮 8 入口、粘贴 7+ 入口——但底层数据/动作各自单一(复制走 copy_record、粘贴走 paste_record 事务、入包走 add_records_to_pack),重复主要在 **UI 表层**,收敛成本可控。
9. **测试门禁成熟**:run_checks.py 17 套必需套件(分钟级),隔离沙箱+离屏,已有 core/ai/ctxpack×4/quick-ai/privacy/storage 等覆盖;缺的是"粘贴焦点恢复/富文本粘贴/热键冲突"三类端到端(已有 _integration_test 可选套件触碰真实剪贴板)。
10. **建议拆 6 个 PR(0-5)**:在原建议的 5 个 PR 前必须加 **PR 0 = 落地工作区**(提交 Warm Paper + 修 set_detail_mode),否则后续每个 PR 都压在带活性 bug 的脏树上。

---

## 1. 前置风险:工作区现状

| 项 | 状态 | 处置 |
|---|---|---|
| Warm Paper 改版(9 文件,ui/kit.py 新增) | 未提交,可运行(今晨已补 5 处缺口) | PR 0 提交 |
| `set_detail_mode` 断链(winclipboard_qt.py:881-882) | **活性 bug**:点右栏页签即 AttributeError | PR 0 修复(实现方法或回退页签) |
| `toggle_preview` 孤儿(1613,仅测试调用) | 无 connect;`_integration_test.py` 仍调它 | PR 0/PR 2 一并处理 |
| 预览面板提示「双击可全屏查看」(1523) | 无对应事件处理器,文案失真 | PR 2 顺手修 |
| 底部预览面板(747-838)与右栏预览页(887)并存 | 迁移中间态,职责重叠 | PR 2 收口 |

**运行实例注意**:当前运行的实例若点击右栏「预览/处理」页签会弹错误框(不毁数据,重启即恢复)。

---

## 2. 交付物一:目录与核心类职责表

| 层 | 文件 | 行数 | 核心类/职责 |
|---|---|---:|---|
| 后端核心 | clipboard_manager.py | 3604 | `ClipboardCore`(973-2683):捕获门控/图片 CAS 入库/清理淘汰/查询过滤/粘贴事务/软删撤销/blob GC/导入导出/加密切换;非 UI 区:配置与备份(226-380)、敏感识别(610-710)、可迁移加密备份(569-608)、AI 服务配置与调用(3349-3500)、预设与种子(3502-3591) |
| Win32 宿主 | win32_host.py | 587 | TrayWorker 后台循环:托盘/剪贴板监听/5 组热键/粘贴双校验事务(477-507)/热键冲突检测;单实例互斥 |
| 存储底层 | store.py | 638 | SQLite clipboard_items(含 FTS5 外容表+3 触发器,29-76;UI 搜索实际走 LIKE 543-560);软删/GC/引用计数;schema v3 |
| 存储变体 | history_store.py / privacy_store.py | 156/241 | 历史 JSON+DPAPI 加密模式;私密区 DPAPI 状态机/备份链/api_key 字段加密 |
| 捕获管线 | pipeline.py | — | run_capture(122):敏感先于去重(138-149);make_record(82-98)记录模型 |
| 主 UI | winclipboard_qt.py | 4509 | `WinClipboardApp`(3266,QObject 枢纽)/`MainWindow`(1633,FluentWindow)/`HistoryPage`(524)/`Card`(109)/`PrivatePage`(1739)/`SettingsPage`(2129);CLIP_ACTIONS 注册表(3219-3248);card_menu(3629) |
| 工作流 | workflow_ui.py + workflow_steps.py + workflow_runlog.py | 1899/447/~160 | ServicesPage(430)/ServiceEditor(140)/PrivacyGateDialog(86)/WorkflowPage(1042)/WorkflowEditor(623)/WorkflowRunner(1233)/AIWorker(71);六类步骤契约+注册表;运行记录 |
| 快速 AI | ai_quick.py | 1030 | AIActionWheel(103)/QuickAIWindow(185)/**EmbeddedAIPanel(511,右栏常驻)**;build_prompt(38)/spawn_chat(86) |
| 快捷浮层 | quickpaste.py | 550 | QuickPasteWindow(180),Alt+`,纯键盘,共享 app.* 无自有数据层 |
| 查看器 | viewer.py | 1783 | ViewerWindow(345)/ImageViewerWindow(1484)/DiffWindow(1313),九视图 |
| 图片工作区 | image_editor.py + pinboard/ | 1152/10 文件 | 非破坏标注层;PinBoardManager/Window/自由画布 |
| 上下文篮 | ctxpack/ | 8 文件 | model(快照条目)/store(DPAPI)/service/add_snapshot(129)/budget(分配算法)/assemble(纯函数拼装)/ui_tray(托盘)/ui_ask(提问) |
| 设计层 | ui_base.py + ui/{theme,delegates,kit,settings,animations}.py | ~1100+kit | 调色板单一来源(init_palette+register);kit=PageHeader/EmptyState/CardPanel/Chip |

**包间依赖要点**:winclipboard_qt.py 是唯一"知道一切"的组装点(34 个 import);cm 被 UI 层直接 `import clipboard_manager as cm` 深达内部函数(sanitize_log/_get_foreground/_fmt_size…),是最大的隐性耦合面(拆它在 PR 5 之后另议)。

---

## 3. 交付物二:UI 组件树与主要调用链

### 3.1 组件树(工作区现状)

```
WinClipboardApp (QObject, winclipboard_qt.py:3266)
├─ core = cm.ClipboardCore ── worker = win32_host.TrayWorker(托盘/热键/监听)
├─ win = MainWindow(FluentWindow, 1633)
│   ├─ history_page = HistoryPage(524)  ← alias app.panel
│   │   ├─ 品牌栏(536-560): btn_brand→_show_page_menu / btn_pinboard / btn_settings
│   │   ├─ 搜索行(570-612): search / cmb_src / cmb_time / 安全模式徽章
│   │   ├─ 左栏: 分类按钮(621-641) + 卡片流(666-745, 池化+窗口化渲染) + 空态(724)
│   │   ├─ 底部预览面板 pv_*(747-838): badge/stats/json切换/复制/粘贴/加入上下文   ← 待并入右栏
│   │   ├─ 右栏 right_dock #aiRightDock(850-903):
│   │   │   ├─ 页签 btn_preview「预览」/ btn_process「处理」(865-882) ← connect 断链!
│   │   │   └─ detail_stack = preview(887) + ai_panel=EmbeddedAIPanel(889)
│   │   └─ ContextTray 上下文篮(841-842, 窗口最底部)
│   ├─ workflow_page(WorkflowPage, workflow_ui.py:1042)
│   ├─ private_page(PrivatePage, 1739)
│   ├─ settings_page(SettingsPage, 2129) ← 内嵌 services_page(ServicesPage, embedded)
│   └─ 左侧导航: 剪贴板/工作流/私密区/设置(1690-1694)
├─ pinboard = PinBoardManager(3342) → PinBoardWindow
├─ quick = QuickPasteWindow(3346, Alt+` 浮层)
└─ _quick_ai = QuickAIWindow(懒建 3903) → AIActionWheel(轮盘)

独立窗口: ViewerWindow / ImageViewerWindow / DiffWindow(viewer.py)
         WorkflowRunner 运行台 / WorkflowEditorWindow / WorkflowRunInspector(workflow_ui.py)
         TransformDialog(3154) / HotkeyRecorderDialog(1967) / 各功能对话框
```

### 3.2 五条关键信号链

1. **选中→预览→AI 联动**:`Card.mousePressEvent`(499)→ `select_card` → `select_index`(3516)→ `update_preview`(1436)→ `_sync_ai_material`(1445)→ `ai_panel.set_material`(ai_quick.py:712,生成中不抢占)。
2. **搜索/筛选**:`search.textChanged`(578)→ 120ms 防抖 → `refresh_view`(3461);分类(638)/来源(596)/时间(606)→ `set_*_filter` → `refresh_view`。
3. **粘贴回车族**(966-986):Enter→`paste_selected` / Shift+Enter 纯文本 / Ctrl+Shift+Enter 转换对话框 / Delete 批量删;粘贴走 `core.paste_record`(2068,前台窗口+剪贴板序号双校验,win32_host.py:477-507)。
4. **全局热键**(win32_host.py:439-450 → qt:4216-4247):Ctrl+Shift+V 主窗 toggle;Alt+` 快捷浮层;Ctrl+Alt+Space 快速 AI;Ctrl+Shift+P 直钉图片;Ctrl+Alt+T 画布穿透。热键全部可录制回滚(508-556)。
5. **卡片右键菜单** `card_menu`(3629-3707):快速AI → 批量删 → 上下文包子菜单 → 粘贴为/转换 → 类型纠正 → 工作流子菜单 → CLIP_ACTIONS 注册表逐项。

---

## 4. 交付物三:功能重复、耦合与回归风险清单

### 4.1 AI 会话层重复(重构主靶)

| 关注点 | 重复份数 | 位置 |
|---|---|---|
| 后台线程封装 | 3 | spawn_chat daemon Thread(ai_quick.py:86,轮盘+右栏共用)/ AIWorker QThread(workflow_ui.py:71,工作流+ctxpack 提问共用)/ ModelWorker(workflow_ui.py:127) |
| 隐私门 | 4 | ai_quick.py:373 与 911(逐行同文)/ workflow_ui.py:1658 / ctxpack/ui_ask.py:224 |
| 服务选择 | 2 种 | 下拉索引直取 dict(ai_quick.py:418/949,**不经** ai_service_by_name)vs cm.ai_service_by_name(3349,空名=第一个) |
| 上下文篮附加 | 3 | `_assemble_context_or_none` ×2(ai_quick.py:382/899 完全同文)+ resolve_wf_context(workflow_ui.py:1207) |
| 结果四出口(复制/贴回/入史/入包) | 3 变体 | ai_quick.py:462-491 / 995-1026 / ui_ask.py:268-288(签名还不一致) |
| 在途管理(_serial/_active/_pending/cancel) | 2 | ai_quick.py:196-199+434-460 / 526-534+965-991 |

### 4.2 界面层重复

- **QuickAIWindow vs EmbeddedAIPanel 90% 同构**(ai_quick.py:185-508 vs 511-1030),仅共用 build_prompt/spawn_chat。
- **双导航**:品牌菜单 `_show_page_menu`(1581)与 FluentWindow 左导航(1690)完全等价;设置入口 ×2。
- **入口面**:查看 7 / AI 9 / 入包 8 / 粘贴 7+ / 删除 5(详见代理审计;底层数据动作各自单一,属表层收敛)。
- **Esc 三层兜底**(1728/967/961)语义一致但路径重复。

### 4.3 数据与隐私(健康项与缺口)

✅ 已解决:上下文篮快照语义(model.py:2-7);删历史不影响包(全删除路径只动 clipboard_items,store.py:385-531);工作流启动即固化 {context}(wui:1243);每个 AI 出口有发送前隐私门;runlog/日志脱敏;敏感捕获入口闸先于去重(pipeline.py:138)。
⚠️ 缺口:① WebStep 提示词复制不过 privacy_scan(wsteps:239-261);② file_list 入包存路径文本快照,源文件删除后无存在性检查(svc:248);③ QuickAI/右栏入口无敏感闸(设计为发送时后置门,属两级模型,列为已知非 bug);④ 无流式输出(所有链路一次性返回,SDK max_retries=1 是唯一重试)。

### 4.4 回归高危区(改动时重点盯)

粘贴事务双校验(win32_host:477-507)/ 富文本 latin-1 字节往返(cm:1144-1174, 2133-2143)/ 加密切换先落后清(cm:2568-2641)/ blob 引用计数含 deleted 态(store:431)/ 卡片池+窗口化渲染参数(HistoryPage:684-707)/ 单实例互斥+boot_state 安全模式(qt:4423-4482)/ 预览 64K 截断与语言缓存。

---

## 5. 交付物四:目标架构映射(五模块)

| 目标模块 | 现有组成 | 动作 |
|---|---|---|
| **Clipboard Core** | HistoryPage/Card/搜索/筛选 + cm 查询/粘贴/变换 + quickpaste + viewer(文本侧) | 基本不动;PR2 收口预览呈现 |
| **AI Action Engine** | cm.ai_chat(HTTP,唯一出口,保留)→ **移植 feature/ai-action-bar 的 ai/ 包为底座**(Registry/声明式动作/executor 复用 WorkflowRunner);新建 ai/session.py 统一会话层(门/服务/在途/出口) | PR3 核心;command/ 包作为后续发现层选合入,agent/ 观望 |
| **Context & Workflow** | ctxpack/(成熟,快照+预算+拼装纯函数)+ workflow_steps.py 契约 + WorkflowRunner 运行台 | PR4 只做出口统一与 {context} 并轨,引擎不动 |
| **Image Workspace** | pinboard/ + image_editor.py + viewer 图片侧 | **冻结**(本轮不碰) |
| **Platform Infra** | cm 下半部 + store.py + history_store/privacy_store + win32_host | **冻结**(仅 PR3 给 ai_chat 加薄入口,不搬家) |

依赖方向规约:`ai/ → workflow_steps/ctxpack(读) → cm.ai_chat`;UI 层只调 ai/ 的 action_id,不再各自拼 prompt。

---

## 6. 交付物五+六:PR 实施计划(0-5)

> 通用纪律:每 PR 独立分支+worktree;跑 `run_checks.py`(17 必需套件,分钟级)全绿才提交;单 PR ≤ ~600 行 diff;回滚 = `git revert`(无 schema/配置格式变更,均可逆)。PR 0-2 只动 winclipboard_qt.py(+测试);PR 3 动 ai_quick.py/workflow_ui.py + 新增 ai/;PR 4 动 ctxpack 出口 + workflow_steps。

### PR 0 —— 落地工作区(前置,必须最先)
- **文件**:现工作区全部改动 + winclipboard_qt.py 两处(`set_detail_mode` 实现/页签回退;`toggle_preview` 保留为别名供 _integration_test)。
- **范围**:提交 Warm Paper;修 881-882 断链(实现 set_detail_mode:切 detail_stack 页 + 写回 preview_visible 配置,与 toggle_preview 合一);修 1523 失真文案。
- **测试**:run_checks 全套 + 手工点右栏两个页签 + _integration_test(可选套件,真实剪贴板)。
- **回滚**:整个 PR 0 = 一个或两个 commit,revert 即回到 2da5e9d。

### PR 1 —— 梳理(本文档即交付)
- **文件**:docs/ARCHITECTURE_AUDIT_2026-10-26.md(本文)+ PROJECT_ANALYSIS 索引行。
- **验收**:文档入仓,不改行为。

### PR 2 —— 界面收口:完成"底部预览 → 右栏预览页"迁移
- **文件**:winclipboard_qt.py(HistoryPage 布局区 747-905)、_integration_test.py(预览相关断言)。
- **范围**:① pv_* 面板内容并入右栏 preview 页(或删除底部面板,预览只在右栏);② ContextTray 保留底部但默认折叠为药丸(展开抽屉);③ 修「双击全屏」文案或补双击事件;④ 只动布局与信号绑定,不碰 cm/store/win32_host。
- **测试**:_integration_test 预览断言改指右栏;module-split/quick-ai 套件;离屏截图对比(_ui_doc_shots.py 重拍)。
- **回滚**:revert 单 commit;preview_visible 配置键语义保持。

### PR 3 —— Action Engine:移植 ai/ 包 + 会话层统一
- **文件**:新增 ai/(自 feature/ai-action-bar 摘取 registry/executor/actions JSON,适配 `run_workflow(wf, rec, auto=)` 签名与 {context});ai_quick.py(两面板的 send/_gate/_assemble_context_or_none/结果出口改调 ai.session);winclipboard_qt.py(card_menu 的 AI 子菜单改 registry 驱动);移植 _ai_actions_test.py 并适配。
- **范围**:一个 action_id 三入口(右键菜单/右栏胶囊/轮盘扇区)同源;隐私门/服务解析/在途管理收敛为 ai/session.py 单份;cm.ai_chat 不动。
- **测试**:run_checks ai 套件 + 新 _ai_actions_test + 手工三入口跑同一动作行为一致。
- **回滚**:revert 后回到两面板重复实现(功能无损);ai/ 为新增目录无残留。

### PR 4 —— 数据流收口:结果出口统一 + {context} 并轨 + 隐私缺口
- **文件**:ctxpack/ui_tray.py+ui_ask.py、ai_quick.py(出口改 ResultSink)、workflow_steps.py(WebStep 复制前过 privacy_scan;file_list 拼装时存在性标注)。
- **范围**:① 结果四出口(复制/贴回/入史/入包)统一为单实现;② QuickAI 的"附加上下文包"与工作流 {context} 走同一 assemble 路径(仅触发时机不同);③ 补 WebStep 扫描与 file_list 过期路径警告。
- **测试**:ctxpack 四套件 + ai 套件;新增"WebStep 复制内容经扫描"断言。
- **回滚**:三个子项各自独立 commit,可分别 revert。

### PR 5 —— 收尾:去重 + 文档 + 回归补强
- **文件**:ai_quick.py(QuickAIWindow/EmbeddedAIPanel 抽公共基类或组合共享件)、README、CHANGELOG;新增回归:粘贴焦点恢复(真集成)、Word/Excel 富文本往返、热键冲突回滚、工作流中途取消不写半截 runlog(后两项已有部分覆盖,补断言)。
- **验收**:run_checks 全绿;重复度表(4.1)中"×2/×3/×4"降为 ×1;README 与实际入口一致。

### 冻结清单(本轮不碰)
pinboard/ 全包、image_editor.py、加密与备份链(privacy_store/history_store/portable_backup)、剪贴板监听与粘贴事务(win32_host TrayWorker 双校验段)、store.py schema、单实例/安全模式机制。

---

## 7. 与原建议的差异(以源码纠偏)

| 原建议假设 | 源码事实 |
|---|---|
| "上下文篮引用还是快照?删历史是否影响?" | 已是快照,无影响(model.py:2-7);PR4 不需大改 |
| "建立 Action Registry/Runner" | feature/ai-action-bar 已有现成 ai/ 包且经两个分支验证,移植优于重建 |
| "AI 三个执行入口" | 实为四套会话层(轮盘/右栏/工作流/ctxpack 提问),HTTP 层早已单点 |
| "合并底部预览至右侧预览页" | 工作区改版已建右栏双页签,只差收尾与断链修复 |
| "敏感内容未经授权发送"风险 | 每个 AI 出口已有最后一道门;真缺口仅 WebStep 与 file_list 两处 |
| ActionRequest/ActionResult 草案 | 落地时以 ai/ 包既有 defn+WorkflowRunner 结果模型为基,补图片/取消/异步时再扩,不另起字符串模型 |

## 8. 待决策:产品方向

代码成熟度已经"押注平台":ctxpack(预算/拼装/快照)与 workflow_steps(契约/注册表/取消)是全项目工程质量最高的两块;走"轻量工具"路线意味着这些资产降级为可选件,走"AI 工作平台"路线则是补完 Action Engine 与结果流。**本审计按平台方向规划**(PR3/PR4 为主体);若选轻量方向,PR3/PR4 可缩为"PR3--lite:仅去重会话层,不引入 ai/ 包",PR 计划其余不变。此取舍影响 PR3 深度,开工前定即可。
