---
name: ui-polish
description: 美化/迭代 WinClipboard(PySide6 + qfluentwidgets)桌面 UI 的固定流程。离屏渲染现状 → 几何断言+视觉审查 → 对照 Fluent 设计规范以库原生方式改进 → 前后对比 → 回归测试 → 重启实例验证。凡用户要求「美化 UI / 界面不好看 / 调样式 / 改布局 / 换主题色材质」,哪怕只说「效果不行」,都应使用本 skill。
---

# ui-polish — WinClipboard UI 美化流程

## 不可违背的原则(用户强偏好,违反即返工)

1. **库原生优先**:颜色、材质、组件一律走 qfluentwidgets 的 API
   (`setTheme` / `setThemeColor` / `setMicaEffectEnabled` / `CardWidget` /
   `SettingCard` / `InfoBar` …),**不自写配色、不魔改库组件样式**。派生色从
   `themeColor()` + `isDarkTheme()` 计算(见 `winclipboard_qt.py: init_palette`)。
2. **先调研后动手**:拿不准长什么样时,先查 qfluentwidgets 官方 Gallery
   (PySide6-Fluent-Widgets 仓库 examples / gallery 截图)的同类页面怎么做。
3. **拒绝割裂多窗口**:功能收进 FluentWindow 导航页,不做游离弹窗。

## 流程

### 1. 渲染现状(改前基线)

```
python .agents/skills/ui-polish/scripts/render_pages.py [light|dark] [页面名…]
```

- 数据全部隔离到 `%TEMP%`,不碰真实历史/配置。
- 输出 PNG 在 `%TEMP%\wclb_ui_polish\`。
- 深浅色都要看(主题派生色容易只在一边出错)。

### 2. 审查(顺序不能反:先断言,后目视)

- **几何断言是权威**:关键控件必须程序化验证,例如
  `btn.geometry().right() <= viewport.geometry().right()`、
  行显隐用 `isHidden()`(**对话框未 `show()` 时子控件 `isVisible()` 恒为 False**)。
- 图像分析/视觉模型只能当参考——本项目有真实前科:把几何完全正常的布局
  误报成「按钮被裁」,且其回答经常中途截断。断言与目视冲突时信断言。

### 3. 改进(对照 Fluent 设计语言)

- 材质:真实桌面(Win11 本地会话)启用 Mica;离屏/RDP 必须回退纯色
  (DWM 不合成 → 整窗透明,`_mica_supported()` 已封装)。
- 主题色:`setThemeColor` + `init_palette(isDarkTheme())` 重派生;
  持久化到 config.json 的 `theme_color`。
- 排版/间距/字号用库组件默认,不要手调像素级 margin 堆细节。

### 4. 前后对比

同一脚本、同一页面集再渲染一轮,文件名带 `_after`,给用户看前后对比。

### 5. 回归(全绿才算完)

```
python _core_test.py && python _ai_test.py && python _integration_test.py
```

`_gui_test.py` 需要运行中的实例;这台机器偶发吞掉注入式按键
(SendInput 收不到 WM_HOTKEY)时会环境性 FAIL,与代码无关,按
skill 内「重启实例」一节的方法用 PostMessage 验证代替。

### 6. 重启实例让用户看到效果

```
# 杀掉全部实例进程(注意:venv 的 pythonw 是转发壳,壳+uv 真身两个 PID
# 是同一实例,不是双实例,别只杀一个留着另一个)
for pid in $(powershell -NoProfile -Command '(Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like "*winclipboard_qt*" -and $_.Name -like "python*" }).ProcessId'); do
  taskkill //PID $pid //F; done
wscript "启动剪贴板.vbs"
```

验证用 PostMessage(不要依赖物理热键/注入按键):

```python
host = win32gui.FindWindow('WinClipboardHostWnd', 'WinClipboardTrayHost')
win32gui.PostMessage(host, 0x0312, 0, 0)      # WM_HOTKEY → 应弹出
main = win32gui.FindWindow(None, 'WinClipboard')
win32gui.PostMessage(main, 0x0100, 0x1B, 0)   # WM_KEYDOWN Esc → 应收起
```

启动后 30 秒内 PostMessage 可能因预热未就绪无响应,重试一次再判。

## 已知坑速查(都在本项目踩过)

| 坑 | 正确做法 |
|---|---|
| `EditableComboBox.setCurrentText` 只认下拉列表内文本 | 任意文本用 `setText`(LineEdit 语义,`textChanged` 会转 `currentTextChanged`) |
| 长文本(如 base_url)不换行 | `setWordWrap(True)`,否则卡片最小宽度撑过视口、右侧控件被整排裁掉 |
| qfluentwidgets 按钮初始化各解析一次 QSS(~1.4ms/个) | 批量场景懒创建(首悬停)或池复用 |
| offscreen 平台 | Mica 不渲染;`QApplication.clipboard()` 是本地桩,验真剪贴板走 win32 |
| `RoundMenu.addAction` 只收 QAction | 传 str 首次渲染即崩,用 `QAction(text, menu)` |
| 模态对话框离屏 `exec` 卡死 | 测试里拦截 `exec`(记录构建),只验构建与字段 |
| 视觉模型顺着引导幻觉/中途截断 | 见流程第 2 步:断言优先 |
