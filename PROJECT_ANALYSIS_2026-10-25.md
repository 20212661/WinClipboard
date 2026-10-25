# WinClipboard 项目分析 + 前端美化(2026-10-25)

> 范围:全项目结构盘点 → 视觉体系审计 → 本轮落地的改动 → 验证结果 → 遗留清单。
> 截图:`docs/ui/2026-10-25/`(改版后全套)、`docs/ui/2026-10-25/before_after.png`(前后对比)。

## 0. 一句话结论

功能层已经相当完整(剪贴板/私密区/查看器/工作流/AI 工作台/上下文包/图钉画布都通),
问题集中在**视觉语言不统一**:同一次会话里能同时看到「暖纸卡片流」「全大写衬线刊头」
「裸 QListWidget 文字行」三套设计。本轮把这些统一到一套 Warm Paper 语言上,
并修掉三个真实缺陷(卡片文字被按钮遮住、白卡里套白框、深浅色开关会进入不可读状态)。

## 1. 项目结构与规模

| 层 | 文件 | 行数 | 职责 |
|---|---|---:|---|
| 后端核心 | `clipboard_manager.py` | 3604 | 监听/热键/托盘/隐私识别/持久化/加密/AI 调用 |
| 主 UI | `winclipboard_qt.py` | 4407 | MainWindow(导航壳)、HistoryPage、PrivatePage、SettingsPage、Card |
| 工作流 UI | `workflow_ui.py` | 1899 | 服务管理、工作流编辑/运行、运行记录 |
| 内容查看器 | `viewer.py` | 1783 | 九视图(代码/JSON/MD/CSV/JWT/Base64/URL/图片/diff) |
| 图片编辑 | `image_editor.py` | 1152 | 裁剪/标注(非破坏矢量层) |
| 快速 AI | `ai_quick.py` | 1030 | 轮盘 + 结果窗 + 右栏内嵌工作台 |
| 快捷浮层 | `quickpaste.py` | 551 | Alt+` 的 Win+V 式列表 |
| 图钉画布 | `pinboard/` | 1633 | 自由画布 + 标注层 |
| 上下文包 | `ctxpack/` | 606+ | 收集/预算/拼装/托盘 |
| 设计层 | `ui_base.py` / `ui/theme.py` / `ui/delegates.py` / `ui/kit.py` | 约 1100 | 调色板、字体、列表委托、页面套件 |

- 22 个 `_*test*.py` 回归脚本,覆盖核心、集成、容量、隐私、性能、上下文包、图钉。
- 打包:`winclipboard.spec` + `打包构建.bat`(PyInstaller onedir,117MB)。

架构上做得好的地方:**调色板单一来源**(`ui_base.init_palette` 把派生色写回所有
`register()` 过的模块)、**卡片固定复用池 + 窗口化渲染**(60~80 张卡上限)、
**离屏截图/断言工具链**(`_ui_doc_shots.py`、`_ui_preview.py`、`.agents/skills/ui-polish/`)。
这些是这次能安全改样式的前提。

## 2. 视觉审计:改版前到底哪里不对

逐页看改版前截图(`.ui_shots/base/*.png`,已并进 `before_after.png`):

| # | 位置 | 现象 | 性质 |
|---|---|---|---|
| 1 | 工作流页 | `QListWidget` 塞字符串 `"名称\n3 步:GLM → DeepSeek → 人工"`,无图标、无卡片、无空态,整页一大块空白 | 设计缺失(最刺眼) |
| 2 | 私密区 | 全大写衬线刊头 `PRIVATE ARCHIVE` + 通栏黑线 + 直角黑框按钮 | 与其它页语言冲突 |
| 3 | 设置页 | 刊头 `INDEX / COLOPHON` + 1px 纯黑通栏线 | 同上 |
| 4 | 剪贴板卡片 | 操作按钮是浮层,窗口略窄时**压在标题上**(1000px 窗口已能复现) | 真 bug |
| 5 | 卡片宽度 | 标题不省略时会把卡片撑得比滚动视口还宽 → 右侧按钮被裁 | 真 bug(修 #4 时暴露) |
| 6 | 预览面板 | 白卡片里再套一块纯白 `QPlainTextEdit`,层次全靠一条上边框 | 层次不清 |
| 7 | 右栏 AI 工作台 | 未选中内容时是一整块空白画布,只有左上角一句浅灰提示 | 设计缺失 |
| 8 | 快捷浮层 | emoji 图标(🧾🔗🖼📁)+ 默认黑滚动条 + 无卡片容器 | 视觉降级 |
| 9 | 设置页「界面主题」 | 可选 跟随系统/浅色/深色,但 `apply_atelier_zero` 启动即强制浅色;选深色后 qfw 组件转深、页面自绘色仍浅 → 浅底白字 | 功能撒谎 |
| 10 | 设置页「主题色」 | 色板是 Windows 蓝/青/绿…,而卡片选中态写死 `rgba(217,119,87,…)` | 功能空转 |

## 3. 本轮改动

### 3.1 新增 `ui/kit.py` —— 页面级套件

统一「页头 / 空态 / 卡片容器 / 药丸徽章」,避免每个页面各写一套手搓 QSS:

- `PageHeader(title, subtitle, actions)`:中文标题(`TitleLabel` 原生)+ 一行说明 + 右侧动作区,不画通栏横线;
- `EmptyState(icon, title, hint, action_text, on_action)`:图标瓦片 + 主副文案 + 可选按钮;
- `Chip(text, tone)` / `tone(name)`:低饱和四色(accent/sand/green/neutral),深浅色各一套;
- `HairLine`、`CardPanel`、`SectionLabel`、`action_button`。

### 3.2 工作流页改成卡片流(`workflow_ui.py`)

新增 `WorkflowDelegate`:图标瓦片 + 名称 + **步骤药丸链**(`AI 步骤显示服务名`、`网页`、`人工`、
`变换·xxx`、`剪贴板`、`条件`,超出宽度自动 `…`)+ 右侧「N 步」。列表改透明底 + 2px 行距,
顶部动作区(运行记录 / 新建工作流),底部保留 编辑/删除/填入示例/AI 服务,
空态给「用示例模板开始」。双击卡片=编辑。

### 3.3 私密区 / 设置页头像统一(`winclipboard_qt.py`)

- 私密区:`PRIVATE ARCHIVE` + 黑线 → `PageHeader("私密区", …)`,「显示明文 / 复制并粘贴」
  收到页头右侧,列表改暖卡片行,按钮改柔和描边,新增空态。
- 设置页:`INDEX / COLOPHON` + 黑线 → `PageHeader("设置", …)`。SettingCard 组本身是库原生,未动。

### 3.4 列表委托重写(`ui/delegates.py`)

`EditorialDelegate` 从「黑描边 + 选中反相成黑底白字 + 1px 黑底线」改成暖卡片行:
白卡 + 1px 暖灰边 + 10px 圆角,选中是**淡陶土底 + 左侧 3px 指示条**(文字保持深色),
序号弱化到 TER 灰,右侧类型/时间/来源/置顶右对齐。类名与数据契约保持不变,调用方零改动。

### 3.5 卡片文字不再被按钮遮住(`winclipboard_qt.py: Card`)

- 文本省略宽度改为**动态计算**:悬停/选中时给按钮组让位,空闲时用满整行;
- 宽度基准取 `min(卡片宽, 滚动视口宽 - 12)`,掐掉「标签最大宽跟随卡片宽 → 卡片越撑越宽」的正反馈;
- 重排走 `QTimer` 单发延迟到下一拍 —— **直接在 `resizeEvent` 里改 `maximumWidth` 会让 Qt 布局
  来回振荡直到栈溢出**(本轮踩过,1000×680 主窗首帧必崩,已用 `_request_relayout` 修掉并加注释);
- 按钮组从 184px 收到 158px(单字按钮 24px、入包 46px、间距 4),同宽度下标题多出约 26px。

### 3.6 右栏 AI 工作台空态(`ai_quick.py`)

阅读区改成 `QStackedWidget`:无内容时是**居中引导页**(图标瓦片 + 「选一张卡片,或直接提问」+
两行说明),生成中/有回答时切白底阅读卡。不再把一句灰字丢在空白画布左上角。

### 3.7 快捷浮层重绘(`quickpaste.py`)

行委托改用 Fluent 矢量图标瓦片(有缩略图时贴缩略图)、白卡 + 圆角选中态 + 左侧指示条、
序号与元信息分层;窗口加 1px 暖灰描边、6px 细滚动条(去掉默认黑条)、行高 46→50。

### 3.8 主题色的「说到做到」(`ui_base.py` + 三处调用)

新增 `accent_rgba(alpha)`,把 11 处写死的 `rgba(217,119,87,…)` 全部替换掉:
卡片选中态、`+入包` 胶囊、AI 胶囊按钮、`Chip(accent)`。设置页换主题色后这些地方真的会跟着变。

### 3.9 主题下拉不再撒谎

启动期 `apply_atelier_zero` 强制浅色,深色下拉会产出不可读界面。现在只保留
`浅色(当前固定)` 并置灰,`_integration_test.py` 对应断言同步更新(附原因注释)。
深色要恢复,需要先把各页 QSS 全部改为调色板派生(见 §5)。

## 4. 验证

离屏渲染 + 全套回归(命令均在项目根,`QT_QPA_PLATFORM=offscreen`):

| 脚本 | 结果 |
|---|---|
| `_module_split_test.py` | PASS(含 `workflowui.list-refresh`、`palette-sync`) |
| `_ui_preview.py light` | PASS(17 项几何/绘制断言) |
| `_ctxpack_ui_test.py` | PASS(14 tests) |
| `_core_test.py` | PASS |
| `_ai_test.py` | PASS |
| `_integration_test.py` | PASS(1317 行全量;含卡片未溢出、浮层、虚拟化、撤销) |

截图:light 全套 14 张 + dark 3 张(用于确认深色未被我改得更糟)、`before_after.png`。
`.agents/skills/ui-polish/scripts/render_pages.py` 补了 `setThemeColor("#D97757")`——
此前它不设主题色,截出来的开关是库默认青色,与真实程序不符。

## 5. 遗留清单(按优先级)

1. **深色主题**:`apply_atelier_zero` 强制浅色;要恢复需把 `winclipboard_qt.py` / `ai_quick.py` /
   `ctxpack/ui_tray.py` 里写死的 `#F4F3EE / #E5E3DC / #FFFFFF / #141413` 换成调色板常量,
   并把 `ui/theme.py::_atelier_qss` 改成按主题生成。工作量约 1~2 天,建议单独一轮做。
2. **中性色仍是「暖纸专色」**:主题色色板切到蓝/绿时,强调色会变而白卡/暖灰边不变,观感会偏。
   若要真正支持任意主题色,需把中性色也做两套(暖/冷)。
3. **`QuickAIWindow`(Ctrl+Alt+空格 的结果窗)未纳入本轮**:还是旧的直角/纯白样式,
   与内嵌工作台不一致,建议下轮复刻 `_build_empty_canvas` + `Chip`。
4. **上下文篮折叠头**看起来像一个输入框(combo 外观),建议改成「箭头 + 标题 + 计数 chip」,
   把「包切换」留到展开态。
5. **卡片操作按钮仍是浮层**:宽度 < 420px 时标题会被省略得比较狠。可考虑把 钉/删/更多 收进
   一个 `⋯` 菜单(与快捷浮层一致),腾出约 80px 给标题。
6. **viewer.py 未动**:头部/工具栏本来就和暖纸语言一致(实测通过),但字号层级可以再收一档。

## 6. 附:本轮文件改动

```
 ui/kit.py               | 新增  页面套件(PageHeader/EmptyState/Chip/…)
 ui/delegates.py         | 重写  编辑风列表 → 暖卡片行
 ui_base.py              | +15   SURFACE_HOVER / ACCENT_TEXT / accent_rgba()
 workflow_ui.py          | +247  WorkflowDelegate + 卡片流 + 空态 + 页头动作
 winclipboard_qt.py      | +224  卡片动态省略/私密区/设置页/预览面板
 quickpaste.py           | +161  行委托重绘 + 浮层描边 + 细滚动条
 ai_quick.py             |  +79  右栏空态画布 + accent_rgba
 _integration_test.py    |  +10  主题下拉断言随行为更新
 .agents/.../render_pages.py | +5  补 setThemeColor
```
