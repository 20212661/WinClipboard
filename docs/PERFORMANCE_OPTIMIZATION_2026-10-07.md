# WinClipboard 性能优化报告(2026-10-07)

> 四阶段优化:列表渲染 / 图片缓存 / 查询与快捷浮层 / 捕获路径。
> 原则:只改成本,不改行为——全部交互语义(子串搜索、Search DSL、置顶
> 分组、敏感隔离、删除撤销、快捷粘贴、双存储模式)经回归测试逐一验证
> 与优化前一致。工作区既有未提交改动(阶段 0-8 模块拆分等)原样保留,
> 本次改动叠加其上,未做任何覆盖、回退或清理。

## 一、修改内容、对应文件及原因

### 第一阶段:列表渲染(winclipboard_qt.py `HistoryPage`)

| 修改 | 位置 | 原因 |
|---|---|---|
| 卡片新建按时间预算分批,经 0ms QTimer 后台批次补齐;首批同步完成可见区(RENDER_FIRST=8) | `set_view` / `_new_card` / `_render_tick` | 60 卡冷构建实测 558ms 全部同步阻塞 GUI;现在首屏同步段 ~20ms,其余在事件循环空闲批次完成 |
| 视图代数:每次 `set_view` 清空在途渲染批次(`_render_pending`) | `set_view` 开头 | 快速连续搜索/切分类时,旧批次不会把旧记录回填进新视图 |
| 加载更多只追加新卡片,不再全量拆除重排布局 | `_extend_window` → `_queue_cards` → `_insert_cards` | 旧实现每次 extend 调 `set_view` 全量重排,扩展到 2000 卡累计 61.8s(二次方);现在增量插入 |
| 滑动窗口:物化超过 WIN_MAX(480)时把视口上方远端卡片释放回闲置池,滚动回顶再补回;滚动值按实际控件高度/行高估计补偿 | `_trim_window` / `_prepend_window` / `_on_scroll_maybe_extend` | 控件数量有硬上限(物化 ≤480 + 闲置池 ≤140);旧实现随滚动无限增长(2000 卡=2060 控件) |
| 闲置池上限 IDLE_CAP=140,超出 deleteLater | `_trim_idle` | 同上,封顶控件持有量 |
| 批量插入/重排期间禁用布局与重绘,末尾一次 activate | `_insert_cards` / `set_view` / `_trim_window` | 逐个 show() 在大列表下每次触发完整布局级联(600 存活卡实测 14ms/张 → 3.2ms/张) |
| 视图切换时旧卡即时进入复用池(不再等循环结束) | `set_view` 的 `spare` 池 | 搜索→全量往返不再批量新建卡片,退化为纯重绑;并以 `taken` 集合防同一控件双重使用 |
| 来源下拉集合与选择未变时跳过重建 | `sync_src_combo` | clear+addItem×40 触发整轮模型重置,高频刷新下是纯无效功(配合 core 侧 `source_apps` 缓存) |
| 顺带修复阶段 8 拆分遗漏:`wq.AnnoRect` 等标注类与 `_pg_get_lexer` 的兼容重导出 | import 区 | `_ui_preview.py` / `_integration_test.py` 在 HEAD 上已断(与本次优化无关,补一行恢复工具可用) |

### 第二阶段:图片预热与缓存(winclipboard_qt.py)

| 修改 | 位置 | 原因 |
|---|---|---|
| 启动预热只提交最近 48 张(PREWARM_THUMBS),不再把全量历史图片排进队列 | `_prewarm` | 300 张历史图片曾一次性入队;其余图片滚到可见时按高优先级现生成,功能不变 |
| 队列优先级:可见请求(卡片/预览绑定)排队首,预热可被 PREWARM_CAP=240 在途上限丢弃 | `ThumbLoader.submit`(PriorityQueue) | 可见请求必须先于后台预热完成;预热过载直接丢弃不丢功能 |
| 统一缓存写入路径 `_thumbs_put`(LRU 触碰 + 48MB 字节预算 + 400 条上限) | `thumb_cached` / `thumb_for` / `ThumbLoader._on_loaded` | `_on_loaded` 原来直写 `_thumbs` 完全绕过淘汰,300 张全留存 56MB 无上限增长(本次实测捕获的最大内存问题) |
| 后台解码改内存缓冲(QBuffer)+ QImageReader 解码期缩放 + EXIF 方向,失败回退 QImage.fromData | `ThumbLoader._run` | 格式支持时解码期降采样;后台线程不再长期持有文件句柄(与删除/孤儿清理的竞争窗口一并消除) |
| 解码失败路径进有界失败表(FAILED_CAP=200),不反复重试坏文件 | `_remember_failed` | 失败不永久卡 pending,也不形成重试风暴 |
| 退出时 `shutdown()` 排空队列 | `quit_app` → `ThumbLoader.shutdown` | 退出收尾不再留满队列的 daemon 线程 |
| 字节估算口径:w×h×4,不含 QImageReader 原始解码与 Qt 平台侧纹理开销(预算已留余量) | `_thumbs_put` 注释 | 统计值定义明确 |

### 第三阶段:查询与快捷浮层(clipboard_manager.py / store.py 不变)

| 修改 | 位置 | 原因 |
|---|---|---|
| 纯 SQL 可表达的过滤:limit 直接下推(`store.query` 原生支持) | `query_view` | 快捷浮层 limit=60 原来读全部匹配 ID 再切片,反而比全量慢 |
| 需内存补充判断的分类/子类型(链接、type: 子类型):分批读取(每批 400)持续过滤,凑够 limit 或读完才停 | `query_view` | 不会先 LIMIT 再过滤漏掉后面的正确结果(回归测试验证与全量结果一致) |
| dbid→下标映射集中缓存(`_dbid_pos`),变更点统一失效 | `query_view` / `_hist_*` 系列 | 不再每次查询全量重建 2 万条映射 |
| `source_apps()` 结果缓存,成员变更失效 | `source_apps` | 免高频刷新的全历史扫描 |
| 内存路径 `filter_view` 凑够 limit 即停 | `filter_view(limit=)` | 限量调用方不再承担全扫成本 |
| 主列表保持全量查询(计数显示与全量访问需要);本阶段明确:**未实现按需载入全部历史**,主列表减少的是 pos 映射重建与来源扫描开销 | `refresh_view` | 如实声明范围 |

EXPLAIN QUERY PLAN 验证:所有查询走 `idx_items_seq` 索引序扫描,LIMIT 下推可提前终止;`LIKE '%q%'` 子串语义无法受益于任何索引——**不新增索引**(依据实测而非猜测)。

### 第四阶段:捕获与内存过滤(clipboard_manager.py / pipeline.py)

| 修改 | 位置 | 原因 |
|---|---|---|
| 去重指纹索引:文本按内容、文件按路径元组、图片按像素 hash,首捕惰性建、此后增量维护;缺 hash 的旧图片条目自动回退线性扫描(语义逐条一致) | `_fp_lookup` / `_dedup_kinds` / `pipeline.run_capture(dedup_lookup=)` | 每次捕获曾全量扫描;混合历史(含无 hash 图片)实测 357ms/次 → 0.7ms |
| 指纹命中返回记录本体,索引键不落库、碰撞不可能(键即内容/像素哈希) | `pipeline.Outcome.rec` | 摘要键命中后核对原内容的担忧不存在:文本键就是原文,图片键是像素 SHA |
| 未置顶计数缓存(`_unpinned_total`),热路径精确增减、复杂路径置 None 重数 | `_evict` / `_hist_added` / `toggle_pin_at` | `_evict` 每次完整计数 |
| `_search_cache` 超 512 改为淘汰最旧一半(不再整表清空);条目离开历史时按身份定点清除 | `_search_text` / `_hist_removed` | 整表清空引发批量重算;定点清除同时消除 id 复用串数据的窗口 |
| 子类型徽章复用管线结果,同次捕获不再二次分类 | `pipeline.Outcome.badge` → `add_item` | add_item 曾对文本条目调用两次 content_badge |
| 集中失效钩子覆盖:新增/提升/删除/撤销/批量删/清空未置顶/一键清空/容量淘汰/自动清理/孤儿清理/图片迁移/导入(经 add_item)/加密⇄库模式切换(重排 _db) | `_hist_removed` / `_hist_added` / `_hist_reordered` / `_reset_hist_caches` 挂接全部变更点 | 索引与缓存不残留脏数据(回归测试驱动) |

如实声明:`history.insert(0)` / `pop` 的 O(N) 指针搬移保留(2 万条约 0.2ms,非瓶颈),**捕获路径并非严格 O(1)**,但已与历史规模脱钩的重复扫描全部消除。

## 二、同场景前后对比

测量脚本 `_perf_baseline.py`(隔离临时目录,不碰真实数据;离屏 Qt;
每场景预热后重复采样,报告中位数,P95 在备注;冷/热与同步/后台已区分)。
原始输出:`_perf_baseline_before.txt` / `_perf_baseline_after.txt`。

| 场景(样本数相同) | 优化前 | 优化后 | 说明 |
|---|---|---|---|
| U1 首屏同步阻塞(冷,60 卡窗口,n=5) | **558ms**(max 617) | **20ms**(max 33) | 剩余 60 卡在 ~530ms 后台批次内补齐(U1b),GUI 不冻结 |
| U2 热缓存重复刷新(n=7) | 1.08ms(p95 1.47) | 1.1~2.1ms(p95 ≤2.2) | 持平 |
| U3 搜索切换往返(过滤+两次重排,n=5) | 44.9ms(p95 49.9) | 64~90ms(p95 ≤95) | 口径变化:新版含后台批次排空;同步段与旧版相当。120ms 防抖内完成 |
| U4 扩展到 500 卡 | 5274ms **连续阻塞** | ~7.8s 总填充,**同步段 ≤81ms** | 阻塞变流水;控件 540→472 |
| U4b 扩展到 2000 卡 | **61.8s 连续阻塞**,控件 2060 | ~22s 后台填充,**同步段 ≤81ms**,控件 **520 有界**(物化 405+闲置) | 旧版每次 extend 全量重排(二次方);深滚动由滑窗封顶 |
| U4c 单次 extend(+80)同步段 | 5530ms | 73~86ms | |
| U5 快捷浮层刷新(空查询,n=5) | 9.9ms(p95 11.3) | **1.1~1.5ms**(p95 ≤1.8) | limit 下推生效 |
| U6 来源下拉同步(n=5) | 0.3ms | ~0ms(未变时跳过) | 4000 条下旧值已小;10 万条时收益放大 |
| C1b 内存 query_view limit=60(2 万条,n=5) | 5.2ms(全扫) | **0.0ms**(凑够即停) | |
| C2b SQL query_view limit=60(2 万条,n=5) | 59.3ms(p95 68.4) | **2.1~3.3ms**(p95 ≤3.6) | 候选读取 20000 → 60 条;pos 映射不再重建 |
| C2c SQL link 分类 limit=60(n=5) | 79.6ms(p95 88.7) | 4.3~8.5ms | 分批读取不漏结果 |
| C3 满历史新增条目(2 万条含混合类型,n=15) | **357ms**(p95 380) | **0.5~0.9ms**(p95 ≤4.7) | 指纹索引;无 hash 图片回退扫描路径保留 |
| C3b 重复条目提升(n=15) | 2.6ms | 2.6~4.4ms | 持平(本就近乎命中头部) |
| C3c `_evict` 容量判定(n=10) | 0.32ms | ~0ms | 计数缓存 |
| T1 启动预热提交量(300 张历史图片) | **300 张全量入队** | **48 张**(PREWARM_THUMBS) | 可见请求仍会高优先级现生成 |
| T1b/T1c 预热后缓存 | **300 项 / 56MB 无上限** | 48 项 / 9MB;预算封顶 48MB+400 项(`_on_loaded` 不再绕过淘汰) | 回归测试 F:超预算实测触发淘汰 |
| 真实桌面(重启实例后 perf.log) | 线索值:list_refresh 2680ms(历史日志) | list_refresh 5ms(热)/161ms(含批次的一次重建);**hotkey_to_first_paint 68ms** | P95 目标 <100ms 达标(单样本,见"未验证"节) |

控件上限(实测):物化卡片 ≤ WIN_MAX(480)+chunk,闲置池 ≤ 140;
全部匹配记录仍可访问(滚动到底自动扩展/更多按钮,滑窗回滚补回上方)。

## 三、已运行的检查与结果

| 套件 | 结果 |
|---|---|
| `run_checks.py` 全部必需(core/storage/ai/pinboard/classifier/privacy/clipboard-failure/capacity/runlog/pagecount/module-split) | **12/12 通过**(含新增 perf-semantics) |
| `_integration_test.py`(真实核心+真实面板,全量) | **RESULT: PASS**(全断言) |
| `_perf_regression_test.py`(新增,39 项) | **PASS**:索引/扫描等价(完整变更序列)、检索缓存有界、未置顶计数、位置映射失效、SQL+内存限量前缀一致、link/type 子类型分批不遗漏、批次代数不污染新视图、分类往返选择正常、滑窗有界+置顶保留+回顶补回、缩略图预算淘汰/LRU/可见优先/失败表/关停、来源下拉跳过 |
| `_benchmark_test.py`(10 万条) | **PASS**:捕获 0.75ms(预算 50)、搜索 171.9ms(预算 200)、分页 2.43ms(预算 100)、内存过滤 16.0ms(预算 100)、载入 220.7ms(预算 1000) |
| EXPLAIN QUERY PLAN | 查询走 idx_items_seq,LIMIT 提前终止;LIKE 子串无法用索引 → 不新增索引 |
| `_ui_preview.py` 离屏渲染 | 17 项几何断言 PASS(面板/私密区/编辑器/图钉画布) |
| 真实实例(源码路径确认后按流程重启,PostMessage 验证) | 热键弹出 ✓ Esc 收起 ✓;perf.log 首帧 68ms |
| 测试适配(3 处,均为异步语义而非行为变化) | `_integration_test` 虚拟化断言补批次排空;`_storage_regression_test` 同;`_core_test` 无改动 |

安全约束遵守:无任何网络请求;新增 SQL 调用全参数绑定(仅复用既有
`store.query`;未引入文本拼接)。

## 四、尚未验证的场景与剩余性能限制

**未验证:**
- 真实桌面的长时间使用与连续深滚动体验(离屏无法完全复现输入节奏;
  实例已重启加载新代码,但仅做了弹出/收起冒烟,热键首帧为单样本)。
- 正式打包 EXE(PyInstaller)的冒烟——本次未重建,下次打包构建时按
  `_packaged_smoke_test.py` 流程验证。
- 大量 JPEG/带 EXIF 方向照片的解码路径(QImageReader setAutoTransform
  仅在单测覆盖 PNG;方向正确性未用真照片样本验证)。

**剩余瓶颈(如实报告,未达理想值的项):**
1. **后台填充吞吐 ~30ms/卡**:单张卡片创建 ~4-6ms(qfluentwidgets 标签
   初始化 + 每色 QSS),事件循环每遍历对全部存活控件的事件过滤器有
   O(N) 固定成本。深滚动时 80 卡块约 1-2s 后台填满(GUI 保持响应)。
   结构性出路是 QListView+delegate 虚拟列表,按任务约束未在本次实施
   (现有 QWidget 卡片结构可用滑窗可靠限界,不满足"无法可靠限制增长"
   的替换前提)。
2. **单次 extend 同步段 73-86ms**:超出 8-12ms 目标,构成为 ~10ms 预算内
   绑定 + 一次全列表布局 activate(~30-50ms@400 条)+ 标签更新。布局
   activate 成本随物化条目数上涨,滑窗已把它压在几百条量级。
3. **SQL 子串全量查询(主列表)55-69ms@2 万条**:LIKE '%q%' 全表扫描是
   子串语义的固有成本,100k 库实测 ~171ms;破 100ms 须切 FTS 词级语义
   (行为变更,触发器已在维护,留待独立决策)。主列表仍是全量查询
   (计数与全量访问需要),**按需载入未实现**。
4. `history.insert(0)/pop` 的 O(N) 指针搬移保留(量级 0.2ms@2 万条)。
5. 滑窗滚动补偿在 prepend 路径用行高估计(±10% 误差),极端变高卡片
   场景回顶瞬间可能有轻微视口偏移;trim 路径用实测高度,精确。

## 五、复现

```
# 前后对比基线(隔离沙箱,~1 分钟)
.venv/Scripts/python.exe _perf_baseline.py before|after
# 性能语义回归(39 项)
.venv/Scripts/python.exe -X utf8 _perf_regression_test.py
# 全部必需检查 + 集成 + 基准
.venv/Scripts/python.exe -X utf8 run_checks.py
.venv/Scripts/python.exe _integration_test.py
.venv/Scripts/python.exe -X utf8 _benchmark_test.py
```

关键参数(可调):`HistoryPage.WIN_MAX/WIN_KEEP/IDLE_CAP/RENDER_*`、
`WinClipboardApp.THUMB_CACHE_BUDGET/THUMB_CACHE_COUNT/PREWARM_THUMBS`、
`ThumbLoader.PREWARM_CAP/FAILED_CAP`。
