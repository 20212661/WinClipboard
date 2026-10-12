# GLM 执行记录(GLM_CHANGELOG_2026-10-06)

按 `GLM_IMPLEMENTATION_PLAN_2026-10-06.md` 执行。工作区:主目录
`C:\Users\haoshangyang\Desktop\03_我的项目\WinClipboard`(main 分支)。
本文件区分:**原有工作区改动**(执行前已存在,未提交,属并行会话)与
**本轮新增/修改**(按阶段列出)。

## 阶段 0:基线(2026-10-06)

### 工作区状态(执行前,只读记录)

- HEAD:`0d7f4e4 feat(qt): 清空全部数据(两步确认 + 核心层 wipe_all_data)`
- 原有未提交改动(保留,不 reset/clean/checkout):
  - 已修改:`.agents/skills/ui-polish/scripts/render_pages.py`、`README.md`、
    `_core_test.py`、`_ui_preview.py`、`clipboard_manager.py`(+220 行,
    app_meta 抽取与存储修复)、`installer.ps1`、`store.py`、
    `winclipboard.spec`、`winclipboard_qt.py`、`安装.bat`
    (合计 `git diff --stat`:10 文件 +231/-85)
  - 未跟踪:`app_meta.py`、`_storage_regression_test.py`、
    `_packaged_smoke_test.py`、`PROJECT_STATUS_2026-10-03.md`、
    `PROJECT_ANALYSIS_2026-10-06.md`、`GLM_IMPLEMENTATION_PLAN_2026-10-06.md`、
    soak 三件套(soak_check.py / soak_report.md / soak_state.json)
- 运行环境:Python 3.12.10(main/.venv);本机另有 worktree
  WinClipboard-ai(feature/agent-planner 分支)的运行实例,与主目录互不影响。

### 基线测试(阶段 0 命令,`./.venv/Scripts/python.exe -X utf8`)

| 套件 | 退出码 | 结果 |
| --- | --- | --- |
| `_core_test.py` | 0 | PASS |
| `_storage_regression_test.py` | 0 | 17 项 OK |
| `_ai_test.py` | 0 | PASS |
| `_pinboard_test.py` | 0 | 全部通过 |
| `_classifier_test.py` | 0 | PASS |

基线失败:无。本轮引入失败:见各阶段记录。
性能基线(引自 PROJECT_ANALYSIS 单次样本,阶段 6 复测):10 万条搜索中位
197.20ms / 200 条分页中位 111.74ms(超 100ms 预算,阶段 6 处理)。

---

(以下各阶段按执行顺序追加)

## 阶段 1:私密区读取失败与保存失败保护

### 修改文件(本轮)

- `clipboard_manager.py`:
  - 模块级私密区存取重写:`load_private_status()`(状态
    ok/missing/recovered/unreadable + 模式 dpapi/plaintext + 恢复来源 +
    脱敏错误)、`PrivateSaveResult` 返回值的 `save_private()`(DPAPI 保护
    失败/临时文件失败/备份轮转失败/替换失败一律保留原文件,可重试;
    **不再降级写明文**)、备份链 bak1~3(损坏主文件与旧明文不进链;
    损坏主文件恢复时改名 `.corrupt` 留档一份)、兼容包装 `load_private()`。
  - 核心:`_private_load/_private_dirty/private_save_error` 状态;
    `_private_save()`(unreadable 禁止自动覆写)、`delete_private()`/
    `clear_private()`(失败恢复原列表;破坏性清空允许覆写不可读原件并
    清除备份与留档)、`_add_private`/`import_portable_payload`(失败只在
    内存+待重试)、`save_all()`(仅成功加载且有变更才写)、
    `_migrate_private()`(明文不进备份链;失败保留明文原件)、
    `wipe_all_data` 私密步(允许覆写不可读原件 + 清备份/留档,失败中止)。
- `winclipboard_qt.py`:PrivatePage 状态行(不可读/备份恢复/明文/
  未保存);do_del/do_clear 走核心方法(失败如实提示);quit_app 私密区
  保存失败弹确认(可取消退出);「清空全部数据」文案注明会删除无法读取
  的内容。
- `_core_test.py`:坏密文断言对齐新语义(先清备份链再测不可读路径;
    新增状态断言)。
- 新增 `_privacy_regression_test.py`:21 项(9 组场景,含"不可读→退出→
  字节不变"对比)。

### 结果

- `_privacy_regression_test.py`:21 项 OK
- `_core_test.py` PASS;`_storage_regression_test`(OK)/`_ai_test`(PASS)/
  `_pinboard_test`(全过)/`_classifier_test`(PASS)保持通过
- 策略记录:删除/清空失败=恢复原列表并提示(与磁盘一致);自动捕获
  失败=内存保留+待重试;备份只救援损坏主文件,不救援被删除主文件
  (删除视为用户清除意图)。

## 阶段 2:剪贴板写入结果贯穿复制/粘贴流程

### 修改文件(本轮)

- `clipboard_manager.py`:
  - `_write_clip_guarded()`:统一写入出口——成功才武装自捕获抑制窗,
    失败还原(不留多余抑制窗);`_set_clipboard` 抛异常也按失败不扩散。
  - `paste_record()`:写入失败短路(不派发 Ctrl+V、不提升历史、不更新
    库 seq、返回 False);文件过滤用临时副本(失败不修改原记录,成功才落
    过滤结果);force/auto_paste 仅在写入成功后派发。
  - `paste_text()`/`copy_text()`/`copy_record()` 统一返回真实 bool。
- `winclipboard_qt.py`:paste_rec/_paste_text_now 失败拉回主窗+如实提示
  (文件缺失保留原 MessageBox 前置判断);PrivatePage.do_copy、诊断复制、
  查看器主复制、Diff 复制、工作流运行台复制、变换对话框复制全部失败即
  错误提示、不再显示"已复制";新增 `_copy_or_warn()` 并接入
  INSPECTOR_ACTIONS 与树/查找字段复制。
- `workflow_steps.py`:ClipboardStep/WebStep 对 `ctx.copy_text` 返回值
  严格判定(None 也不当成功);失败 Done(halt)+「复制失败」说明;网页
  步骤失败不打开依赖复制的网页、不宣称"提示词已复制"。
- `quickpaste.py`:act_paste 先写后收——失败浮层留在原地(页脚提示
  可重试),成功才收起还原焦点。
- `_core_test.py`:`_set_clipboard` 成功桩 None→True(失败桩归
  `_clipboard_failure_test.py`)。
- 新增 `_clipboard_failure_test.py`:12 项(计划语义表 7 行 + 文件全缺/
  部分缺失两态/写入异常/抑制窗还原/库 seq 不提升/工作流三态契约/
  快捷浮层失败不收起)。

### 结果

- `_clipboard_failure_test.py` 12 项 OK
- `_core_test` PASS / `_ai_test` PASS / `_storage_regression` OK /
  `_pinboard_test` 全过 / `_classifier_test` PASS /
  `_privacy_regression_test` 21 项 OK
- 测试全程不发送真实按键、不碰系统剪贴板。

## 阶段 3:安装/卸载目录保护

### 修改文件(本轮)

- 新增 `installer_safety.ps1`(纯函数层,可被测试 dot-source):
  `Resolve-CanoPath`(规范化绝对路径)/`Test-PathIsWithin`(边界感知,
  同名前缀邻居不算子目录)/`Get-UnsafeTargetReason`(拒绝盘根/个人目录根/
  Windows/Program Files 内部/项目目录与其祖先/构建源三向关系)/
  安装标记(创建/校验:app+规范路径+实例ID,更新保稳定实例ID)/
  `Find-ReparsePoints`/`Get-TargetProcessIds`(按可执行路径匹配,
  接受进程对象替身)/`Test-ShortcutPointsTo`。
- `installer.ps1` 重写主流程,**校验先于一切变更**:目标安全校验 →
  非空目录必须有有效标记 → reparse 扫描 → dist 构建元数据版本核对 →
  仅停**本安装**实例(先优雅关闭,仍在运行则中止并提示,-ForceStop 才强杀;
  不再扫杀所有同名进程)→ /MIR /XJ /R:1(仅限已确认专属目录)→ 写标记 →
  快捷方式/注册项(复制成功之后才做);robocopy 2..7 如实警告;
  卸载命令带 -RegSuffix。
- 生成的 `uninstall.ps1`:删除前重新校验自身目录与标记(app/规范路径),
  标记无效即中止不删;reparse 拒绝递归;仅停自身实例(同优雅策略);
  只删除指向本安装 exe 的快捷方式;全部 `Remove-Item -LiteralPath`,
  目录最后删。
- 新增 `_installer_safety_test.ps1`:47 项检查(临时树 + 测试注册后缀,
  不碰真实快捷方式/默认目录/用户进程)。

### 结果

- `_installer_safety_test.ps1`:47 checks PASS(含真实安装→更新→卸载全流程、
  版本不匹配中止、无标记目录拒绝(哨兵完整)、锁定文件复制失败无注册项、
  junction 检出、无标记卸载中止)。
- 实证记录:robocopy 对"锁定的多余文件"仅 exit=2(警告级,已完成安装),
  对"锁定的待覆盖文件"exit≥8(中止);测试据此构造故障。
- `安装.bat` 入口兼容未变(直接调用 installer.ps1)。

## 阶段 4:实际加密状态与降级策略

### 修改文件(本轮)

- `clipboard_manager.py`:
  - `_enc_field()`:DPAPI 保护失败返回 None,**不再降级编码明文**。
  - `save_config()`:任一密钥加密失败 → 在轮转备份与临时文件之前整体
    中止(原文件与备份不动,已有有效配置可恢复),返回 False。
  - `_migrate_config_file()`:任一明文密钥加密失败 → 放弃迁移,明文原件
    保留;正常环境重跑可升级。
  - `_migrate_config_secrets()`:save_config 失败如实记日志(配置保留在
    内存待重试);删除"仍为明文"的成功文案分支(该路径已不可能成功)。
- `winclipboard_qt.py`:PrivatePage 顶部说明改为机制描述(不再以
  `dpapi_available()` 断言"已加密"),状态行按实际状态显示
  「DPAPI 已加密/旧明文未加密/无法读取/尚未保存」;服务编辑器保存失败
  弹"配置未保存(密钥在本会话可用,重启后需重填)"。
- `_privacy_regression_test.py` 新增 `ConfigEncryptionRegression` 5 项。

### 结果

- `_privacy_regression_test.py` 26 项 OK(私密区 21 + 配置密钥 5)
- `_core_test` PASS / `_ai_test` PASS / `_storage_regression_test` OK
- README 的降级说明在阶段 5 文档批次一并更新。

## 阶段 5:容量语义、运行记录与文档

### 修改文件(本轮)

- 5.1 容量:`clipboard_manager.py` `_evict()` 超限按**未置顶条数**计算
  (README 承诺语义:置顶不占 max_items 预算),仍从最旧普通记录淘汰,
  库删除与 blob 引用保护路径不变;`toggle_pin_at()` 取消置顶后超限立即
  淘汰。`_core_test.py` "置顶豁免淘汰"断言对齐新语义(置顶后 n4 不再被挤掉)。
- 5.2 运行记录:`workflow_runlog.py` `save_run/clear_runs` 临时文件+原子
  替换,失败保留原文件(save_run 返回 None / clear_runs 返回 False);
  既有文件损坏时留档 `.corrupt` 后从空白重建,不隐式覆写;`winclipboard_qt.py`
  运行台收尾与查看器清空按返回值如实提示(保存失败不打断工作流)。
- 5.3 文档:README「大列表虚拟化」改为「首屏分批加载」并注明全量仍在内存、
  删除绝对化承诺;新增「可靠性与失败语义」一节(私密区失败保护/真实加密
  状态/复制失败反馈/安装目录限制/运行记录原子化)与密钥"加密失败不降级"
  说明;`ANALYSIS.md` 标注为历史报告并链接当前状态文档。
- 新增 `_capacity_regression_test.py`(8 项,SQLite+加密 JSON 双模式:
  恰达上限/超限淘汰最旧/置顶不占预算/全置顶不淘汰/去重不虚增/取消置顶
  即时淘汰/置顶图片资产保护/最后引用释放)与 `_runlog_regression_test.py`
  (6 项:往返与 keep 截断/脱敏/损坏留档不覆写/写失败保留/清空失败保留/清空原子)。

### 结果

- `_capacity_regression_test.py` 8 项 OK;`_runlog_regression_test.py` 6 项 OK
- `_core_test` PASS / `_storage_regression_test` OK / `_ai_test` PASS
- 教训记录:两种事实源的测试必须各自独立子目录(SQLite 腿持连接时
  加密腿无法 unlink 同一 DB 文件)。

## 阶段 6:分页性能定位与统一测试入口

### 6.1 性能(证据驱动)

- 基线 3 轮(优化前,10 万条):搜索中位 124.13/126.07/124.79ms(p95 ≤130)
  全部 ≤200ms;分页中位 69.70/69.76/71.49ms(p95 最高 84.98)全部 ≤100ms。
  **分析报告中的 111.74ms 失败样本本轮未复现(单机波动),但余量薄。**
- 分段探针(10 万条,临时库):guarded COUNT **54.70ms** / page rows 0.84ms /
  行转换 0.46ms;EXPLAIN 显示无过滤 COUNT 也是 `SCAN clipboard_items`
  (表上无 state 可用索引)——**固定 SQL 快速路径无效**(fast COUNT 51.34ms
  与 guarded 几乎相同),瓶颈是 COUNT 本身而非守卫表达式。
- 修复:`store.py` 活跃计数缓存——`_active_count` 惰性计数,
  insert/upsert_promote/mark_deleted/restore/delete/delete_many/
  collect_garbage/reset/migrate_from_list 全部写失效;`query_page`
  **无任何可选过滤时**总数走缓存(O(1)),有过滤仍走 guarded COUNT
  (语义逐项一致,子串 LIKE/来源/时间/置顶/DSL 后过滤均未改动);
  无索引变更、无迁移。
- 优化后 3 轮:分页中位 **1.23/1.27/1.72ms**(p95 ≤2.0ms),约 50×;
  搜索 134~136ms(p95 ≤147)保持 LIKE 语义,预算内。
- 新增 `_pagecount_regression_test.py` 3 项(缓存与九类写操作失效、
  过滤总数与暴力 SQL 对照、count() 一致性)。

### 6.2 统一入口与发布依赖

- 新增 `run_checks.py`:默认顺序执行 core/storage/ai/pinboard/classifier +
  本轮新增 privacy/clipboard-failure/capacity/runlog/pagecount 十套,
  `sys.executable -X utf8` 子进程隔离,逐套超时与输出尾部,任一失败
  总退出码非零;`--benchmark` 显式启用;`--integration` 显式且注明会接触
  真实剪贴板;不安装依赖/不重建 EXE/不跑安装器。
  实测:`run_checks.py` → **10/10 通过**(合计 ~36s)。
- 新增 `requirements-release.txt`:实际验证版本基线
  (PySide6 6.11.2 / qfw 1.11.3 / pywin32 312 / Pillow 12.3.0 /
  cryptography 50.0.1 / Pygments 2.21.0 / openai 3.16.2 /
  PyInstaller 6.22.3 / Python 3.12.10),构建工具与运行依赖分开列;
  `requirements.txt` 兼容范围保持不变。

## 阶段 7:构建与交付验收

### 修改文件(本轮)

- `打包构建.bat`:图标生成子进程把 APPDATA 重定向到一次性临时目录
  (构建不再在真实用户数据目录创建任何文件;`ensure_dirs()` 调用移除)。
- `clipboard_manager.py` `single_instance()`:互斥名由数据目录派生
  (`Global\WinClipboard_Mutex_7hasj2k_<sha256(APP_DIR)[:8]>`)——同一
  数据目录仍单实例;隔离 APPDATA 的打包冒烟/开发实例与正式安装互不
  冲突(计划 7.6)。
- `_packaged_smoke_test.py`:源码/字节码核对清单 4→9 个模块
  (+workflow_steps/workflow_runlog/quickpaste/diagnostics/perf_probe);
  报告新增构建来源元数据 `build`(HEAD、脏标记、10 个源文件 SHA-256、
  依赖版本、Python 版本、构建时间)+ `exe_built_at` + EXE SHA-256。

### 构建与冒烟结果(本次执行)

- 重建:`python -m PyInstaller winclipboard.spec --noconfirm --clean`
  退出码 0;图标生成走临时 APPDATA。
- `dist/WinClipboard/build-verification.json`(重建后):
  version 1.0.0-rc1;git_head `0d7f4e4d2864…` + **dirty=True**(未提交
  改动如实入档,源文件逐个 SHA-256);10 模块/字节码与源码一致;
  普通模式 2 次会话 + 加密模式 2 次会话全部 启动/渲染/干净退出,
  各保留 405 条种子历史;加密模式无明文残留(库与 JSON 副本均校验);
  EXE SHA-256 `4de9aaf8ea185a3e…`。
- `_installer_safety_test.ps1` 对新产物复验:47 checks PASS(安装器
  版本核对用新 build-verification.json,一致)。
- 最终全量隔离检查:`run_checks.py` 10/10 通过(见阶段 6 记录 + 收尾
  复跑,结果在下方"总验收")。

### 总验收对照

- 阶段 1—5 必需故障回归:全部通过(privacy 26 / clipboard-failure 12 /
  capacity 8 / runlog 6 / config 加密 5 项含于 privacy 套件)。
- 既有回归保持:core PASS / storage 17 OK / ai PASS / pinboard 全过 /
  classifier PASS(run_checks 10/10)。
- 私密区不可读与保存失败不覆写可恢复原件:字节级断言通过
  (test_unreadable_save_all_preserves_bytes 等)。
- 失败粘贴无 Ctrl+V 派发:语义表 7 行断言通过(cmd_q 队列为空)。
- 安装目标/卸载归属验证:47 项通过,哨兵文件完整。
- 文案与磁盘实际加密状态一致:PrivatePage 状态行按 load/save 实况;
  日志/备份无新增明文(回归断言扫描文件与日志正文)。
- 性能:分页 1.2~1.7ms 中位(预算 100ms);搜索 124~136ms(预算 200ms);
  全部达标并有前后对照(阶段 6)。
- 构建模块与源码一致;隔离启动/重启验证记录完整。
- 原有工作区改动保留(未 reset/clean/checkout);真实用户数据与正式
  安装实例未被替换(安装器测试只用临时目录与 TEST 注册后缀)。

### 未执行(如实"未验证")

- 真实桌面验收:全局热键实弹、向真实应用粘贴(普通/纯文本/富文本/
  文件/图片)、焦点变化取消粘贴、暂停监听、真实数据上的加密切换重启、
  图片撤销、工作流复制失败的真实 UI 表现——均未执行(需要用户桌面配合,
  属独立手工验收项)。
- `_integration_test.py`(接触真实系统剪贴板)与 `_gui_test.py`(注入
  全局热键)本轮未运行;`--integration` 选项已预留为显式入口。
- 正式安装/卸载(默认目录、真实开始菜单/桌面快捷方式、控制面板条目)
  未执行;安装器行为经临时树 + 独立注册后缀验证。
- 连续运行(soak)巡检未安排;上一次 soak 记录属旧构建。
- 未提交任何 commit(计划默认交付可审查工作区;建议分组见下)。

### 建议提交分组(未执行,供审查后采用)

1. `fix: preserve unreadable private storage and report save failures`
   (阶段 1 + 4:私密区/配置密钥状态与失败保留;_privacy_regression_test)
2. `fix: stop paste when clipboard writes fail`
   (阶段 2:写入结果贯穿;_clipboard_failure_test)
3. `fix: validate installation ownership before mirror and uninstall`
   (阶段 3:installer_safety.ps1 + installer.ps1 + _installer_safety_test)
4. `fix: report actual encryption state and preserve secrets on failure`
   (阶段 4 UI 部分,可与 1 合并)
5. `feat(capacity): pinned items excluded from max_items budget`
   (阶段 5.1 + _capacity_regression_test)
6. `fix(runlog): atomic run-history persistence`
   (阶段 5.2 + _runlog_regression_test)
7. `docs: align README with actual reliability semantics`
   (阶段 5.3 + ANALYSIS.md 标注)
8. `perf(store): O(1) active-count fast path for unfiltered paging`
   (阶段 6.1 + _pagecount_regression_test)
9. `test: add isolated check runner and release dependency baseline`
   (run_checks.py + requirements-release.txt)
10. `build: isolated build-time APPDATA + per-data-dir mutex + provenance`
    (阶段 7:打包构建.bat / single_instance / _packaged_smoke_test)

注:app_meta.py、_storage_regression_test.py、_packaged_smoke_test.py 的
**原有未提交内容**(并行会话工作)与上述文件的本轮新增改动在工作区中
叠加,提交前需按文件逐 hunk 审查区分(本文件各阶段"修改文件"小节已
逐项标注本轮改动点)。

## 阶段 8:后续架构拆分(2026-10-07 执行)

进入条件核验:阶段 1—7 已完成,run_checks 10/10、构建验收与打包冒烟
完整(见上)。按"每次只迁一个子系统、不改数据格式与行为、保留旧导入
兼容"执行;每步跑隔离回归,最后统一重建 + 字节码核对。

### 拆分结果(原 winclipboard_qt.py 8066 行 → 8 模块)

| 模块 | 内容 | 行数 |
| --- | --- | --- |
| `ui_base.py` | 主题调色板(多模块同步 init_palette/register)、字体、图标/文本/窗口助手、Badge | 327 |
| `viewer.py` | 染色管线/_CodeEdit/ViewerWindow/DiffWindow/ImageViewerWindow + INSPECTOR_ACTIONS + _copy_or_warn | 1761 |
| `image_editor.py` | Compress/Scale 对话框 + 标注图元 + ImageEditor | 1152 |
| `workflow_ui.py` | AIWorker/ModelWorker/PrivacyGate/服务页/工作流编辑·运行·运行记录 | 1641 |
| (winclipboard_qt.py) | 主窗/卡片/历史页/设置/应用编排 | **8066→3536** |

### 拆分结果(原 clipboard_manager.py 4199 行)

| 模块 | 内容 |
| --- | --- |
| `win32_host.py` | 消息/热键/托盘常量与 ctypes 原型、hotkey_label、单实例互斥(APP_DIR 派生)、前台进程、CF_DIB/CF_HDROP 构造、SendInput 族、_get_clip_seq、**TrayWorker 整体** |
| `privacy_store.py` | DPAPI 原语、_enc_field/_dec_field、私密区状态机(load_private_status/save_private/备份链/损坏留档) |
| `history_store.py` | _history_from_container/load_history/save_history/备份轮转、加密残留清理、旧库移除 |
| (clipboard_manager.py) | ClipboardCore 状态机与捕获管线(**4199→3374 行**) |

### 关键设计:兼容与测试语义保持

- **旧导入兼容**:winclipboard_qt / clipboard_manager 顶部重导出全部被搬
  公开名(含 INSPECTOR_ACTIONS、save_private、TrayWorker 等);预览脚本
  与既有测试的 `wq.*` / `cm.*` 引用零改动(除下述一处测试桩位修正)。
- **调色板多模块同步**:ui_base.init_palette 把派生色写入所有已注册
  模块(各 UI 模块 import 时 register(sys.modules[__name__])),拆分前后
  深浅色切换语义一致(拆分回归专门断言四模块同步)。
- **打桩语义保持**:win32_host/privacy_store/history_store 不在导入期
  依赖 cm;TrayWorker 的粘贴事务原语(send_ctrl_v/_get_foreground/
  _get_clip_seq/time)与 ICON_FILE/APP_DIR/ensure_dirs,私密区的
  PRIVATE_FILE/_dpapi_protect/_dpapi_unprotect/_private_write_payload/
  _private_rotate_backups,历史的 HISTORY_FILE/DB_FILE 全部经 `_cm()`
  运行期回查 —— 测试对 cm.* 的 monkeypatch(路径重定向/故障注入)继续
  原样生效(核心证据:_core_test 事务四场景、_privacy_regression 26 项
  全绿)。
- **测试桩位修正(一处,属"同步更新调用")**:_ai_test 的
  PrivacyGateDialog 桩改打 workflow_ui 命名空间(运行台搬过去后从自身
  命名空间解析类)。

### 第 6 项:记录身份与列表加载评估(不实施)

产出 `docs/RECORD_IDENTITY_EVALUATION.md`:梳理 history 下标/_view 下标/
id(rec) 身份/_db 四套坐标的混用点与脆弱点;建议先落稳定 `_rid` 标识
(行为不变前置批次),真实分页(QAbstractItemModel)属独立架构任务
(进入条件与依赖已列),本轮不实施。

### 验证(全部实际执行)

- 新增 `_module_split_test.py`(run_checks 第 11 套):调色板四模块
  浅/深同步、ImageEditor/查看器九类内容/Diff/图片查看器构造与渲染像素、
  workflow_ui 同一性与构造、28 个兼容重导出名 —— **56 项 PASS**。
- `run_checks.py`:**11/11 通过**(core/ai/storage/pinboard/classifier/
  privacy/clipboard-failure/capacity/runlog/pagecount/module-split)。
- pyflakes 名字审计:八个拆分文件无未定义名。
- 重建(PyInstaller --clean,退出 0)+ `_packaged_smoke_test.py`:
  核对清单 9→15 模块与源码字节码一致;普通/加密模式各两次启动·渲染·
  干净退出,405 条种子保留;无明文残留。
- `_installer_safety_test.ps1` 对新产物:47 checks PASS。
- 过程缺陷(已修,如实记录):
  (1) 拆 4 把 DEFAULT_CFG 误卷入 win32_host 常量段,且重导出清单含
      main 分支不存在的 ID_HOTKEY_PALETTE(该热键只在 worktree 特性
      分支)——两处修正后 import 恢复;
  (2) ui_base 的下划线常量 _PV_TEXT_CAP 不随 star-import 导出,
      HistoryPage 预览在打包版抛 NameError——离屏套件未触发该路径,
      **由打包冒烟捕获**,补显式导入后重建复验通过(打包冒烟的价值实证);
  (3) 拆 5 的 save_private 内 _dpapi_protect 调用漏加路由规则,
      _privacy_regression 三用例当场拦截,补 _cm() 回查后全绿;
  (4) 段边界行号漂移由工具的锚点自检拦截一次(3505/3506),修正后通过。
- 拆分工具 `_stage8_split*.py` ×4 用后即删(方法论:锚点边界自检 +
  字节级搬移 + 定向路由计数校验 + 原子落盘,失败不写盘)。

### 未验证(如实)

- 真实桌面验收与正式安装/卸载仍沿用阶段 7 的"未验证"清单(本轮未做
  任何桌面行为);EXE 冒烟为隔离 APPDATA + offscreen。
- 每步拆分跑全量 run_checks,统一在末尾重建 + 字节码核对一次
  (计划允许"保留旧导入兼容入口或同步更新所有调用"后统一回归)。

### 建议提交分组追加(未执行)

11. `refactor(ui): extract viewer/editor/workflow-ui modules with palette base`
    (拆 0—3 + _module_split_test + 兼容重导出)
12. `refactor(core): extract win32 adapter and storage services`
    (拆 4—5 + _cm() 打桩路由 + pyflakes 清零)
13. `docs: record identity evaluation for future pagination`
    (第 6 项评估)








