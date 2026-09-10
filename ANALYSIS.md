# WinClipboard 项目分析报告

> 分析日期:2026-09-10 · 分析基线:`a04395f`(AI 工作流提交后的工作区)
> 结论均经实际运行验证;凡未运行时验证的推断,文中均标注为「静态追踪」。

---

## 一、项目概览

Windows 桌面剪贴板管理器。核心代码约 **5,300 行** + 测试/工具约 **640 行**。

| 文件 | 行数 | 角色 |
|---|---|---|
| `clipboard_manager.py` | ~2,850 | 共享后端 `ClipboardCore` + tkinter 前端 + Win32 / DPAPI / 持久化 |
| `winclipboard_qt.py` | ~2,590 | 默认前端(PySide6 + qfluentwidgets),复用上面作 `cm` |
| `_core_test.py` | ~195 | 核心逻辑单元测试(36 项,无需 GUI) |
| `_ai_test.py` | 261 | AI 工作流端到端测试(本地假 AI 服务器,27 项) |
| `_integration_test.py` | ~160 | Qt 前端集成自检(真实 app/核心/面板,16 项,本次新增) |
| `_ui_preview.py` | 170 | 离屏渲染预览(浅色/深色/基线) |
| `_gui_test.py` | 85 | 热键弹出实测(对已启动进程注入 Ctrl+Shift+V) |

### 启动链路

`启动剪贴板.vbs`(桌面快捷方式指向它)/ `.bat` 三级解析解释器:

1. 同目录 `python.txt`(一行,`pythonw.exe` 完整路径)—— 显式覆盖,优先级最高
2. hermes venv(本机已知可用)
3. PATH 上的 `pythonw.exe`

随后探测 `import PySide6, qfluentwidgets` 决定入口:成功跑 Qt 版,失败回退 tkinter 版
(只需 pywin32 + Pillow)。设计目标是**任何环境下都能启动**。

### 架构要点

- **后端完全独立于 UI**:两版前端共享同一个 `ClipboardCore` 实例。
- 前端通过 `@property` 把 `cfg / history / private / worker / signal_q` 代理到 `core`;
  后端承诺容器**原地修改、引用稳定**,两版前端一致遵守 —— 这是「一份后端两个前端」成立的前提。
- **线程模型**:`TrayWorker`(独立 Win32 消息循环线程)持有托盘图标、`RegisterHotKey`、
  `AddClipboardFormatListener`、80ms `SetTimer`;事件经 `signal_q` 抛给前端 30ms 轮询
  (Qt 用 `QTimer`,tk 用 `root.after`)。

---

## 二、验证结果(全部实测)

| 检查项 | 结果 |
|---|---|
| `_core_test.py` | ✅ PASS 36/36 |
| `_ai_test.py` | ✅ PASS 27/27 |
| 全部 `.py` 编译 | ✅ OK(Python 3.11.15 / PySide6 6.11.2 / qfluentwidgets 1.11.3) |
| UI 离屏渲染 | ✅ 浅色 + 深色均正常出图 |
| `single_instance()` 互斥量守卫 | ✅ 首次 `True`、二次 `False` |
| 真实数据目录一致性 | 91 条历史;图片 16 引用 vs 磁盘 16 文件,**孤儿 0 / 丢失 0 / 重复哈希 0** |
| 备份轮转 | ✅ `history.json.bak1~bak3` 三份齐全,时间戳连续 |
| `save_history()` 单次耗时 | ~6.7 ms(91 条 / 152 KB,含 3 次 `os.replace`) |
| `app.log` 健康度 | 388 行,**近期无异常**;历史崩溃仅已修复的 `QFont.setFont`、`QSize` 未定义 |
| 实际使用痕迹 | 一个月内约 30 次启动;用户已自建 2 个 AI 服务 + 5 条工作流 |

日志中可见 `私密区已自动升级为 DPAPI 加密`、`敏感内容已隔离`、`prewarm done 1661ms`,
说明明文→加密迁移、敏感隔离、缩略图预热等机制在真实运行中确实按设计生效。

---

## 三、设计亮点

- **持久化防御完整**:`save_history` 原子写 + 滚动备份;`load_history` 主文件损坏自动
  回退 `.bak1~.bak3`;启动时清理已丢失的图片/文件条目。
- **敏感识别是真实现**:`is_sensitive` 用 Luhn 校验银行卡、身份证加权校验位、
  API key / JWT 正则,不是关键词糊弄;私密区 DPAPI(`CurrentUser` 作用域)加密,
  **解密失败不覆盖原文件**,旧明文在启动时自动升级。
- **图片迁移逻辑**:`_migrate_images` 补齐 SHA-1、按内容合并重复、清理孤儿 PNG,
  且在后台线程启动**之前**完成,避免竞态。
- **Qt 性能取舍有据**:卡片池复用(`_pool` + `id(rec)`)让搜索/切分类零重建;
  明确拒绝 `QGraphicsDropShadowEffect`(会让滚动每帧全量离屏模糊,实测 14ms+/步),
  改为 `paintEvent` 手绘 12 层静态阴影;搜索 120ms / 视图 180ms 双防抖;
  缩略图用 `Image.draft()` 做 JPEG 快速低分辨率解码。
- **踩过的坑都有注释留痕**:`windowOpacity` 与 `WA_TranslucentBackground` 竞态会导致
  面板卡在 alpha=0(窗口"可见"但点击穿透)、`SetWindowRgn` 与分层表面冲突、
  `SegmentedWidget.addItem` 的 onClick 传的是 bool 而非 routeKey、
  `INPUT_UNION` 必须含最大成员否则 x64 下 `sizeof(INPUT)=32≠40` 导致 `SendInput` 报错 87。

---

## 四、发现的问题

### 中

1. **AI 密钥明文落盘**(`winclipboard_qt.py` → `cm.save_config`)。
   `api_key` 明文写进 `config.json`,而项目本身已有 DPAPI 能力。
   *状态:* ✅ 已修复(见第五节)。
2. **`on_change` 跨线程回调**(`clipboard_manager.py` `add_item` → `self.on_change()`)。
   回调由 `TrayWorker` 所在线程触发,Qt 版在回调里访问 `self.panel.isVisible()`
   (非 GUI 线程访问 QWidget,Qt 未定义行为)。静态追踪确认链路;
   当前只走 `QTimer.start()`(线程安全)故**未实际出错,但契约脆弱**。
   *状态:* ✅ 已修复。
3. **`save_config` 无原子写 / 无备份**。与 `save_history` 的保护等级不一致,
   配置写入期崩溃可能损坏文件。
   *状态:* ✅ 已修复。

### 低(未改动,建议知悉)

4. **图片编辑器写剪贴板绕过自写抑制**:`ImageEditor._on_copy` / `_on_compress`
   直接 `set_dib_to_clipboard`,未设 `_ignore_clip_until`,编辑结果会被监听重新捕获入历史。
   可能是有意为之(把编辑结果存回历史),但会让「复制」额外产生条目。
5. **`_ignore_clip_until` 时间窗本质无法区分自写/外部写**:粘贴后 0.5s 内用户立刻复制
   会被吞掉。实践中影响极小。
6. **`PrivateWindow.do_del`** 用 `pop(self.lst.row(item))` 无越界检查;
   列表被其他路径刷短时会 `IndexError`(触发概率低)。
7. **历史无总字节上限**:当前 152 KB / 91 条,每次复制同步写 ~6.7 ms 在 UI 线程,
   条目多或含长文本后耗时会线性增长(用 `time.perf_counter` 实测)。
8. **缓存无界**:`_measure_cache`(仅 >3000 清空)、`CardList._thumbs`、
   Qt `_thumbs`(>200 清空)在超大历史下会各自增长。

---

## 五、本次已实施的修复

全部改动集中在 `clipboard_manager.py`(后端),两版前端各改 3 行以适配新信号。

### 1. 密钥 DPAPI 加密 + 明文自动升级(且不泄漏进备份)

新增 `_enc_field` / `_dec_field`(封装/解封单个密文字段)、`_load_config_raw`
(读配置 + 返回是否需要回写)、`_migrate_config_file`(就地把明文密钥加密)与
`ClipboardCore._migrate_config_secrets`(启动迁移)。

**关键细节**:迁移必须**先就地加密主文件,再 `save_config` 轮转备份**。
否则旧的明文文件会被 `_rotate_config_backup` 推进 `config.json.bak1` ——
加密了主文件却把明文留在备份里,等于没加密。这一点由回归测试
`备份无明文残留` 守住(实测:`config.json` / `.bak1` / `.bak2` 三份均不含明文)。

- 旧版明文密钥:启动时自动升级为密文,内存中仍是明文供 `ai_chat` 使用。
- 手工编辑:用户可直接在 `config.json` 写明文,下次启动自动加密。
- `win32crypt` 不可用:退化为编码明文并记日志(与私密区策略一致)。
- 换用户/换机器导致解不开:该字段置空 + 记日志,**原文件保持原样便于排查**。
- 备份内容同为密文(回退一份备份后仍是加密态)。

### 2. `save_config` 原子写 + 滚动备份 + 损坏回退

与 `save_history` 对齐:`_rotate_config_backup`(旧 → `.bak1` → `.bak2` → `.bak3`)、
临时文件 + `os.replace`、写入失败清理 tmp 并记日志;`load_config` 主文件损坏时
依次回退 `.bak1~.bak3`。

### 3. 历史变化事件改走 `signal_q`

移除跨线程直调:`add_item` 现调用 `_notify_change()`,只投递 `("change",)` 队列信号;
两版前端各自在 `_handle_signal` 中处理(仍在自己的 UI 线程轮询里),
原回调语义(面板可见才刷新 / Qt 侧 180ms 防抖)完全保留。
`ClipboardCore.__init__` 的 `on_change` 参数一并移除,`_core_test.py` 中的桩同步清理。

### 回归测试(新增 14 项,`_core_test.py` 25 → 36 项)

密钥不落明文 / 标记 dpapi / 保留其它键 / 往返可解密 / 明文标记待升级 /
明文已升级加密 / **备份无明文残留** / 二次启动不再回写 / 坏密文置空且不回写 /
备份已轮转 / config 损坏回退备份 / add_item 投递 change 信号。
测试同时补齐了 `CONFIG_FILE` 的临时目录隔离(原先遗漏,会污染真实配置)。

### 集成自检(新增 `_integration_test.py`,16 项)

构建**真实的 `WinClipboardApp`**(真实核心 + 真实面板 + 真实定时器,仅渲染离屏),
覆盖端到端链路:真实启动路径下的密钥迁移 → 捕获经 `signal_q("change")` 走到
防抖刷新与卡片重建 → 缩略图/分类/搜索/置顶/删除 → 服务窗口与运行台 → 运行期
`app.log` 无异常。`_ai_test.py` 用的是 `FakeApp` 替身,覆盖不到这些真实链路。

### 文档

`README.md`:补充密钥加密与手工填写的说明、`config.json` 原子写与备份回退、
配置项示例补 `ai_services` / `ai_workflows`、文件清单补 `_gui_test.py`、新增 FAQ 两条。

---

## 六、总体评价

代码质量明显高于其「自动生成」的署名水平:

- 容错密集且多数是**有意为之**(为保证常驻稳定),而非无脑吞异常;
- 持久化可靠,真实运行一个月日志零近期异常;
- 两套测试全绿,覆盖核心状态机与 AI 工作流全链路;
- 设计权衡都有注释留痕,能看出真实的调试经验沉淀。

「抽出 `ClipboardCore` 共享后端、两个前端」的重构(commit `f93a6c4`)执行得干净,
本次的 AI 工作流大改在此基础上扩展也保持了分层纪律。

**架构上唯一值得长期关注的是 `ClipboardCore` 的状态访问纪律**:目前
`history` / `private` / `cfg` 会被 UI 线程(Tk 主线程 / Qt 主线程)与 `TrayWorker`
同时触及。当前之所以安全,是因为**所有变更都经由 `signal_q` 回到 UI 线程执行**
(`TrayWorker` 只投递 `("clip", item)`,不直接改容器)。本次修复把最后一处
「核心直调前端」的旁路也收敛到了队列,**这条纪律现在是完整闭合的,建议在后续
改动中继续保持** —— 一旦有人在 `TrayWorker` 里直接调 `add_item`,就会引入真正的竞态。
