# -*- coding: utf-8 -*-
"""AI 结果出口统一执行核(PR3B-2A):复制 / 贴回原窗 / 存入历史 / 加入上下文包。

三个 AI UI(QuickAIWindow / EmbeddedAIPanel / AskContextDialog)此前各持
一份变体实现(签名与提示各异)。本模块只编排既有业务接口——
core.copy_text / core.paste_text(force, target_hwnd) / core.add_item /
ctxpack.ui_tray.add_text_to_pack——不另造剪贴板操作或存储实现,也不触碰
ChatSession:出口只处理「已经拿到的结果文本」,绝不发起第二次模型调用。

操作返回 (ok: bool, detail: str):detail ∈ {"", "empty", "target_gone",
"clipboard", "<经 sanitize_log 的错误摘录>"};调用方自行映射提示文案,
UI 行为与文案保持各调用方现状。

安全语义保持:存历史走捕获管线(敏感内容自动进私密区,见 cm);
贴回沿用核心粘贴事务(前台校验/失败回滚在 core);入包走 ctxpack
快照(add_text_to_pack 内部处理敏感确认)。贴回的目标失焦只报
target_gone、不写剪贴板;SetForegroundWindow 尽力而为,失败不回滚——
核心粘贴事务自身处理前台恢复校验。
"""
import clipboard_manager as cm

EMPTY = "empty"
TARGET_GONE = "target_gone"
CLIPBOARD_FAIL = "clipboard"


class ResultSink:
    """绑定一个 app 语境的结果出口执行核;操作幂等,可被用户重复触发。"""

    def __init__(self, app, *, pack_title):
        self.app = app
        self._pack_title = pack_title      # 入包标题(调用方语境,如「快速 AI 回答」)

    def copy(self, answer):
        """复制结果全文(core.copy_text)→ (ok, detail)。"""
        if not answer:
            return False, EMPTY
        if self.app.core.copy_text(answer):
            return True, ""
        return False, CLIPBOARD_FAIL

    def paste_back(self, answer, target_hwnd, exclude_hwnd=0):
        """贴回原窗口 → (ok, detail)。

        target 无效/已失焦/等于 exclude_hwnd → (False, TARGET_GONE),
        不写剪贴板;粘贴走核心事务,成功后尽力置前台。"""
        if not answer:
            return False, EMPTY
        if not target_hwnd or not cm.win32gui.IsWindow(target_hwnd) \
                or target_hwnd == exclude_hwnd:
            return False, TARGET_GONE
        if not self.app.core.paste_text(answer, force=True,
                                        target_hwnd=target_hwnd):
            return False, CLIPBOARD_FAIL
        try:
            cm.win32gui.SetForegroundWindow(target_hwnd)
        except Exception:
            pass  # 核心粘贴事务会校验前台;此处尽力而为
        return True, ""

    def save_history(self, answer):
        """存入历史(经捕获管线,敏感自动进私密区)→ (ok, 错误摘录|空)。"""
        if not answer:
            return False, EMPTY
        try:
            self.app.core.add_item(("text", answer))
            return True, ""
        except Exception as exc:
            return False, cm.sanitize_log(str(exc))

    def save_pack(self, answer):
        """加入当前上下文包(ctxpack 快照;敏感确认由 add_text_to_pack
        内部处理)→ (ok, 错误摘录|空)。与旧实现一致:成败提示由该接口
        内部的 toast 承担,此处不再二次提示。"""
        if not answer:
            return False, EMPTY
        try:
            from ctxpack.ui_tray import add_text_to_pack
            add_text_to_pack(self.app, answer, title=self._pack_title)
            return True, ""
        except Exception as exc:
            return False, cm.sanitize_log(str(exc))
