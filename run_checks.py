# -*- coding: utf-8 -*-
"""统一隔离检查入口(阶段 6.2)。

运行(默认 = 全部必需隔离检查):
    ./.venv/Scripts/python.exe -X utf8 run_checks.py

选项:
    --benchmark   追加性能基准(耗时长,单独运行更稳;同条件至少 3 轮取中位)
    --integration 追加 _integration_test.py(会接触真实系统剪贴板;需要
                  无其他实例占用时人工确认后运行)
    --integration-sandbox 追加界面/核心集成(系统剪贴板与工作线程使用替身)
    --only 名1,名2  只运行指定套件(按文件名子串匹配,如 --only core,ai)

纪律:
    - 全部子进程用 sys.executable 继承 UTF-8,模块全局状态互不污染;
    - 每套件独立超时;subprocess.run 终止测试主进程，派生进程需套件自行收尾;
    - 默认不安装依赖、不重建 EXE、不运行安装器、不注入桌面行为
      (真实热键/剪贴板/安装验收属独立的手工验收项,见 GLM_CHANGELOG);
    - 任一必需检查失败 → 总退出码非零。
"""
import os
import subprocess
import sys
import time
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# (名称, 脚本, 超时秒, 必需) —— 必需套件默认全跑
SUITES = [
    ("core", "_core_test.py", 600, True),
    ("storage", "_storage_regression_test.py", 900, True),
    ("ai", "_ai_test.py", 600, True),
    ("ai-actions", "_ai_actions_test.py", 600, True),
    ("pinboard", "_pinboard_test.py", 900, True),
    ("classifier", "_classifier_test.py", 600, True),
    ("privacy", "_privacy_regression_test.py", 600, True),
    ("clipboard-failure", "_clipboard_failure_test.py", 600, True),
    ("capacity", "_capacity_regression_test.py", 600, True),
    ("runlog", "_runlog_regression_test.py", 300, True),
    ("pagecount", "_pagecount_regression_test.py", 300, True),
    ("module-split", "_module_split_test.py", 600, True),
    ("perf-semantics", "_perf_regression_test.py", 600, True),
    ("ctxpack", "_ctxpack_test.py", 300, True),
    ("ctxpack-store", "_ctxpack_store_test.py", 300, True),
    ("ctxpack-ui", "_ctxpack_ui_test.py", 300, True),
    ("ctxpack-workflow", "_ctxpack_workflow_test.py", 300, True),
    ("quick-ai", "_quick_ai_test.py", 300, True),
]
OPTIONAL = {
    "benchmark": ("benchmark", "_benchmark_test.py", 1800),
    "integration": ("integration", "_integration_test.py", 1800),
    "integration-sandbox": ("integration-sandbox", "_integration_sandbox_test.py", 600),
}


def run_one(name, script, timeout):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    t0 = time.perf_counter()
    try:
        # Isolate before product imports: legacy fixtures may omit new paths.
        with tempfile.TemporaryDirectory(prefix="wclb_checks_") as sandbox:
            env["APPDATA"] = sandbox
            env["TEMP"] = sandbox
            env["TMP"] = sandbox
            env["SQLITE_TMPDIR"] = sandbox
            p = subprocess.run(
                [sys.executable, "-X", "utf8", os.path.join(HERE, script)],
                cwd=HERE, env=env, timeout=timeout, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        dt = time.perf_counter() - t0
        ok = p.returncode == 0
        tail = (p.stdout or "").strip().splitlines()[-3:]
        print("%-20s %-6s %6.1fs  %s" % (name, "PASS" if ok else "FAIL", dt,
                                         " | ".join(tail)))
        if not ok:
            print("---- %s 输出尾部 ----" % name)
            out = (p.stdout or "") + (p.stderr or "")
            print("\n".join(out.strip().splitlines()[-30:]))
        return ok
    except subprocess.TimeoutExpired:
        print("%-20s TIMEOUT (> %ss)" % (name, timeout))
        return False


def main():
    args = set(sys.argv[1:])
    only = None
    for a in list(args):
        if a.startswith("--only="):
            only = a.split("=", 1)[1].split(",")
            args.discard(a)
    todo = []
    for entry in SUITES:
        if only is None or any(k in entry[0] for k in only):
            todo.append(entry)
    for flag, entry in OPTIONAL.items():
        if ("--" + flag) in args:
            todo.append(entry + (True,))
    if not todo:
        print("没有匹配的套件")
        return 2
    print("WinClipboard 隔离检查(python %s)" % sys.version.split()[0])
    results = []
    for entry in todo:
        name, script, timeout = entry[0], entry[1], entry[2]
        if not os.path.exists(os.path.join(HERE, script)):
            print("%-20s MISSING %s" % (name, script))
            results.append((name, False))
            continue
        results.append((name, run_one(name, script, timeout)))
    failed = [n for n, ok in results if not ok]
    print()
    print("RESULT: %d/%d 通过%s" % (
        sum(1 for _n, ok in results if ok), len(results),
        "" if not failed else ";失败: " + ", ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
