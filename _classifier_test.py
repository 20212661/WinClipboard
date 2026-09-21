# -*- coding: utf-8 -*-
"""分类器黄金语料(golden corpus,建议 23)—— detect_content 的回归地板。

语料:classifier_corpus/*.json,每条 {"text": …, "expect": <subtype>}。
改动 detect_content / 嗅探器(_sniff_jwt/_sniff_base64/csv_sniff/廉价代码
特征)之前与之后各跑一次:每类 precision / recall / 误报命中率的漂移
一眼可见,不再是"感觉没变坏"。

指标与阈值:
- recall(每类)   ≥ 0.90 —— 该认的要认得出(≥3 条才纳入阈值,样本太少只报告)
- precision(每类) ≥ 0.90 —— 认成该类的里真比率(预测集中的漏网即误报来源)
- false_positive 文件命中率 ≥ 0.90 —— 不该认的必须保持 plain

运行: python _classifier_test.py   退出码 0 = 全部达标
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clipboard_manager as cm

CORPUS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "classifier_corpus")
RECALL_MIN = 0.90
PRECISION_MIN = 0.90
FP_ACC_MIN = 0.90
MIN_CASES_FOR_GATE = 3

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail if not cond else "")
    if not cond:
        FAILS.append(name)


def load_cases():
    cases = []
    for fn in sorted(os.listdir(CORPUS_DIR)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(CORPUS_DIR, fn), "r", encoding="utf-8") as f:
            for c in json.load(f):
                if isinstance(c, dict) and c.get("text") is not None:
                    cases.append((fn, str(c["text"]),
                                  str(c.get("expect") or "plain")))
    return cases


def main():
    cases = load_cases()
    print("语料: %d 条(来自 classifier_corpus/)" % len(cases))
    # 逐条预测
    rows = []      # (file, text, expect, pred, note)
    for fn, text, expect in cases:
        info = cm.detect_content(text)
        rows.append((fn, text, expect, info.subtype, info.confidence))
    # 每类 recall
    by_expect = {}
    for fn, text, expect, pred, conf in rows:
        e = by_expect.setdefault(expect, {"n": 0, "ok": 0, "miss": []})
        e["n"] += 1
        if pred == expect:
            e["ok"] += 1
        else:
            e["miss"].append((fn, text[:40], pred))
    # 每类 precision(预测为该类且判对的 / 预测为该类总数)
    by_pred = {}
    for fn, text, expect, pred, conf in rows:
        p = by_pred.setdefault(pred, {"n": 0, "ok": 0})
        p["n"] += 1
        if pred == expect:
            p["ok"] += 1
    print()
    print("%-14s %5s %5s %8s %8s" % ("类别", "条数", "命中", "recall", "precision"))
    for cat in sorted(by_expect):
        e, p = by_expect[cat], by_pred.get(cat, {"n": 0, "ok": 0})
        rec = e["ok"] / e["n"] if e["n"] else 1.0
        prec = p["ok"] / p["n"] if p["n"] else 1.0
        print("%-14s %5d %5d %7.0f%% %7.0f%%" % (cat, e["n"], e["ok"],
                                                 rec * 100, prec * 100))
        for fn, head, got in e["miss"]:
            print("    MISS [%s] 期望 %s 实得 %s: %s…" % (fn, cat, got, head))
        if e["n"] >= MIN_CASES_FOR_GATE:
            check("recall·%s ≥ %.0f%%" % (cat, RECALL_MIN * 100),
                  rec >= RECALL_MIN, "%.0f%%" % (rec * 100))
            if p["n"]:
                check("precision·%s ≥ %.0f%%" % (cat, PRECISION_MIN * 100),
                      prec >= PRECISION_MIN, "%.0f%%" % (prec * 100))
    fp = by_expect.get("plain")
    if fp and fp["n"] >= MIN_CASES_FOR_GATE:
        acc = fp["ok"] / fp["n"]
        check("误报地板·false_positive 命中 ≥ %.0f%%" % (FP_ACC_MIN * 100),
              acc >= FP_ACC_MIN, "%.0f%%" % (acc * 100))
    # 无预测溢出到语料未覆盖的类别(可忽略的提示,不设阈值)
    extra = set(by_pred) - set(by_expect)
    if extra:
        print("提示: 预测溢出类别(语料未覆盖): %s" % ", ".join(sorted(extra)))
    print()
    print("RESULT:", "PASS" if not FAILS else "FAIL %d 项: %s"
          % (len(FAILS), ", ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
