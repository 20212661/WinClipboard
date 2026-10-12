# WinClipboard · AI 上下文管理(Context Pack)实施记录

> 开发指导文档见会话输入版《AI 上下文管理(Context Pack)开发指导》(2026-10-12)。
> 本文件是它在仓库内的**进度表与执行日志**,每阶段结束更新。
> 分支:`feature/ctx-pack`(自 main `f80165e` 切出)。

## 接入点核实记录(§12.2,与指导文档不符处以代码为准)

| 指导文档说法 | 核实结果 |
| --- | --- |
| 记录 kind: text/code/json/markdown/csv/url/file_list/image_ref | 历史 `type` 实为 **text/files/image** 三种 + 文本子类型 `TYPE_OVERRIDES`(text/json/jwt/base64/url/csv/xml/markdown/code);ContextItem.kind 按子类型映射,jwt/base64/xml 归 `text` |
| 来源指纹 | `pipeline.fingerprint_of_rec(rec, sha1_fallback)`(文本=内容,文件=路径元组,图片=像素哈希) |
| run_checks 由 12 套变 13 套 | 核实:main 已有 12 套(含 perf-semantics);ctxpack 为第 13 套 |
| P0 按 13 组建议提交 | 阶段 8 拆分后实现文件与各阶段修复**同文件纠缠**,hunk 级分离需已删工具且有中间提交损坏风险 → 按文件粒度 11 个提交完成,纠缠处并入两个重构提交并在提交说明标注 |

## 进度表

| 阶段 | 状态 | 提交 | 备注 |
| --- | --- | --- | --- |
| P0 前置 | ✅ 完成 | main 3f7af00..f80165e(11 提交) | 8 新模块入库;`feature/ctx-pack` 切出;15 模块导入冒烟 OK |
| P1 模型/预算/拼装 | ✅ 完成 | 见 git log | `_ctxpack_test` 29 项 OK(估算区间/确定性/金样模板/截断/转义/性能 200KB≤10ms);CJK 计数用正则删串(translate 实测 17.5ms→2.5ms) |
| P2 存储/服务 | ✅ 完成 | 见 git log | `_ctxpack_store_test` 20 项 OK(原子写/轮转/损坏留档/DPAPI 故障注入/双向迁移/清空/备份往返/集成钩子);cm 四处薄钩子:CTXPACK_FILE 常量、set_history_encryption 同步迁移(开启失败中止切换)、wipe_all_data 第 5 步、portable 备份含包 |
| P3 UI 最小闭环 | 待开始 | | |
| P4 工作流/提问 | 待开始 | | |
| P5 轻量会话 | 可选(不做) | | P4 后再评估 |
| P6 AI 压缩 | 可选(不做) | | |

## 关键实现决策(偏离/细化指导文档处)

- **敏感条目语义**:`sensitive=True` = 入包时命中敏感探测且用户已显式放行;
  `assemble(include_sensitive=False)`(默认)安全兜底——敏感条目不拼入并给
  `sensitive_blocked` 丢弃原因;只有经隐私闸确认的发送路径传 `True`,
  且此时 warnings 仍列出敏感条目标题。
- **呈现顺序 vs 分配顺序**:预算分配按 (priority desc, captured_at desc)
  决定**谁入选**;入选条目按**包内顺序**呈现(尊重拖拽排序)。
- **单条目开销估算**按最坏情况(40 字全 CJK 标题)保守估算,宁高估。
- **估算器实现**:CJK 区段正则删串计数(C 级扫描),不引入 tiktoken;
  系数 CJK=1.0 token/字、其余 3.5 字符/token、代码/JSON 3.0;安全边际 0.9。
- **金样模板**:`time=""` 当 captured_at≤0;有值时输出本地时区
  `%Y-%m-%d %H:%M`(测试固定 captured_at=0 避免时区依赖)。
