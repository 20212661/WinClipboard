# -*- coding: utf-8 -*-
"""内置命令目录:全部功能一处注册(路线图 2.3「以后新增功能只注册」)。

register_builtin(registry, app) 在应用启动时调用一次;action 全部为
闭包,延迟取 app 状态(注册时 app.win 尚未建好也没关系)。需要选中
条目的命令用 when 谓词 gating,面板自动隐藏不适用项。
"""

from .command import Command

import clipboard_manager as cm
import ai as ai_pkg


def _sel(app):
    """当前选中记录(history/_view/selected;取不到返回 None)。"""
    try:
        vi = app.selected
        if vi is None or not 0 <= vi < len(app._view):
            return None
        return app.history[app._view[vi]]
    except Exception:
        return None


def _sel_text(app):
    r = _sel(app)
    return r if r is not None and r.get("type") == "text" else None


    def _is_url(rec):
        return bool(rec.get("type") == "text"
                    and cm.url_info(rec.get("content") or ""))


def register_builtin(registry, app):
    """登记内置命令;返回命令条数。"""
    reg = registry

    def sel():
        return _sel(app)

    def has_sel():
        return sel() is not None

    def has_text():
        return _sel_text(app) is not None

    def has_image():
        r = sel()
        return r is not None and r.get("type") == "image"

    def _ai(action_id, name, kw=""):
        def act():
            d = (getattr(app, "ai_actions", None)
                 or ai_pkg.get_registry()).get(action_id)
            app.run_ai_action(d, _sel(app))
        return act

    # ---- 剪贴板 ----
    reg.register(Command("clipboard.paste", "粘贴选中条目",
                         app.paste_selected, when=has_sel,
                         category="剪贴板", keywords="paste 回贴"))
    reg.register(Command("clipboard.paste_plain", "纯文本粘贴(不带格式)",
                         app.paste_plain, when=has_text,
                         category="剪贴板", keywords="plain 纯文本"))
    reg.register(Command("clipboard.paste_transform", "转换粘贴…",
                         app.open_transform, when=has_text,
                         category="剪贴板", keywords="transform json url"))
    def _copy_current():
        rec = sel()
        if rec is None or not app.core.copy_record(rec):
            raise RuntimeError("内容不可用或剪贴板被占用,请重试")
    reg.register(Command("clipboard.copy", "复制当前内容", _copy_current,
                         when=has_sel, category="剪贴板",
                         keywords="copy 复制选中"))
    reg.register(Command("clipboard.delete", "删除选中条目(可撤销)",
                         app.delete_selected, when=has_sel,
                         category="剪贴板", keywords="delete 删除"))
    reg.register(Command("clipboard.pin", "固定 / 取消固定选中条目",
                         app.toggle_pin, when=has_sel,
                         category="剪贴板", keywords="pin 置顶"))
    reg.register(Command("clipboard.clear", "清空历史(保留置顶)",
                         app.clear_all, category="剪贴板",
                         keywords="clear 清空"))
    def _focus_search():
        app.win.show()
        app.win.raise_()
        app.panel.search.setFocus()
        app.panel.search.selectAll()
    reg.register(Command("clipboard.search", "搜索历史", _focus_search,
                         category="剪贴板", keywords="search 查找 过滤"))

    # ---- 查看与对比 ----
    def _open_viewer():
        rec = sel()
        if rec is None:
            raise RuntimeError("没有选中条目")
        if rec.get("type") == "image":
            app.open_image_viewer(rec)
        elif rec.get("type") == "text" and _is_url(rec):
            app.open_link(rec)
        else:
            app.open_viewer(rec)
    reg.register(Command("view.open", "打开查看器(JSON 自动格式化)",
                         _open_viewer, when=has_sel,
                         category="查看与对比", keywords="viewer json 查看"))
    reg.register(Command("view.diff", "与基准对比 / 选为对比基准",
                         lambda: app.compare_pick(sel()), when=has_sel,
                         category="查看与对比", keywords="diff 对比 ctrl"))

    # ---- AI(复用 ai/ 动作注册表,零配置语义同卡片 ✨AI 菜单) ----
    for aid, name, kw in (
            ("code_explain", "AI:解释代码", "explain 代码"),
            ("code_optimize", "AI:优化代码", "optimize 重构"),
            ("code_comment", "AI:生成注释", "comment 注释"),
            ("summarize", "AI:总结", "summary 摘要"),
            ("translate", "AI:翻译(中英互译)", "translate"),
            ("to_markdown", "AI:生成 Markdown", "markdown 转换")):
        reg.register(Command("ai." + aid, name, _ai(aid, name),
                             when=has_text, category="AI", keywords=kw))
    reg.register(Command("ai.agent", "Agent 任务(LLM 规划并执行)",
                         lambda: app.open_agent(rec=_sel(app)),
                         when=has_text, category="AI",
                         keywords="agent planner 规划 计划 任务"))
    reg.register(Command("ai.workflows", "管理工作流",
                         app.show_ai_hub, category="AI",
                         keywords="workflow 工作流"))
    reg.register(Command("ai.services", "配置 AI 服务",
                         app.show_services, category="AI",
                         keywords="service api key 服务"))

    # ---- 图钉画布 ----
    reg.register(Command("pin.add", "固定到画布(选中图片)",
                         lambda: app.pin_image(sel()), when=has_image,
                         category="图钉画布", keywords="pin 画布 钉"))
    reg.register(Command("pin.clipboard", "直钉剪贴板图片",
                         lambda: app.pinboard.pin_clipboard_image(),
                         shortcut="Ctrl+Alt+P", category="图钉画布",
                         keywords="pin clipboard screenshot"))
    reg.register(Command("pin.board", "显示图钉画布",
                         lambda: app.pinboard.show_board(),
                         category="图钉画布", keywords="board canvas 画板"))
    reg.register(Command("pin.click_through", "画布鼠标穿透切换",
                         lambda: app.pinboard.toggle_click_through(None),
                         shortcut="Ctrl+Alt+T", category="图钉画布",
                         keywords="穿透 clickthrough"))

    # ---- 导航 ----
    def _nav(page_attr, also_show=True):
        def act():
            if also_show:
                app.win.show()
            app.win.switchTo(getattr(app.win, page_attr))
        return act
    for pid, page_attr, name in (
            ("nav.history", "history_page", "剪贴板页"),
            ("nav.workflow", "workflow_page", "工作流页"),
            ("nav.services", "services_page", "AI 服务页"),
            ("nav.private", "private_page", "私密区页"),
            ("nav.settings", "settings_page", "设置页")):
        reg.register(Command(pid, name, _nav(page_attr), category="导航",
                             keywords="goto " + name))

    # ---- 应用 ----
    reg.register(Command(
        "app.toggle", "显示 / 隐藏主窗口", app.win.toggle,
        shortcut=lambda: cm.hotkey_label(
            app.cfg.get("hotkey_mod", 0), app.cfg.get("hotkey_key", 0)),
        category="应用", keywords="panel 主窗口"))
    reg.register(Command(
        "app.quickpaste", "快捷粘贴浮层", app.quick.toggle,
        shortcut=lambda: cm.hotkey_label(
            app.cfg.get("quick_mod", 0), app.cfg.get("quick_key", 0)),
        category="应用", keywords="quickpaste 浮层 alt"))
    reg.register(Command("app.export", "导出历史数据…",
                         lambda: app.win.settings_page._export_history(),
                         category="应用", keywords="export 导出 备份"))
    reg.register(Command("app.pause", "暂停 / 恢复剪贴板监听",
                         app.toggle_monitor_pause, category="应用",
                         keywords="pause 暂停 监听"))
    reg.register(Command("app.update", "检查更新",
                         lambda: app.check_update(silent=False),
                         category="应用", keywords="update 版本"))
    def _help():
        app.win.show()
        app.show_shortcut_help()
    reg.register(Command("app.help", "快捷键与操作说明", _help,
                         category="应用", keywords="help 帮助 快捷键"))
    reg.register(Command("app.about", "关于 WinClipboard", app.about,
                         category="应用", keywords="about 关于"))
    reg.register(Command("app.quit", "退出程序", app.quit_app,
                         category="应用", keywords="quit 退出"))
    return len(reg)
