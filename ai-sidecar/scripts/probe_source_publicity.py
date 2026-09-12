#!/usr/bin/env python3
"""只读探针：把「出处性质」单独问一次，量这个轴在当前模型上的分辨力。

生产提示词里 source_publicity 只是七个字段之一；本脚本把它单独问，是为了把
「轴本身没用」和「轴有用但模型在长提示里填不对」这两种解释分开。只读、不写库。

    python3 ai-sidecar/scripts/probe_source_publicity.py --scope seeds --text-source predicate
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434") + "/api/chat"
MODEL = os.environ.get("MB_PROBE_MODEL", "qwen3.5:4b")

SYSTEM = (
    "你在判断一条已归档知识结论的出处性质。只输出一个英文取值，不要解释、不要引号、不要 JSON。\n"
    "own_work_only：结论的内容出自用户自己的产出（会议、聊天、排查过程、代码提交、文档草稿），"
    "公开世界里没有一份资料原样写着这句话。\n"
    "publicly_documented：结论的内容本身已经写在公开教科书、公开论文、公开标准或产品官方文档里，"
    "换任何人去查都能查到同一句话，用户只是把它复述了一遍。\n"
    "判断只看内容是否已被公开资料原样写过，不看它对用户有没有用。"
)


def ask(text):
    body = {
        "model": MODEL,
        "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "知识结论：\n" + text + "\n\n只回答 own_work_only 或 publicly_documented。"},
        ],
    }
    req = urllib.request.Request(
        OLLAMA_URL, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        payload = json.load(resp)
    return (payload.get("message", {}).get("content") or "").strip()


def classify(raw):
    low = raw.lower()
    if "publicly_documented" in low:
        return "publicly_documented"
    if "own_work_only" in low:
        return "own_work_only"
    return "unparsed:" + raw[:40]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--scope",
        choices=("veto-eligible", "seeds", "all"),
        default="veto-eligible",
        help="veto-eligible=今天受众为同领域任何人的已发布行；seeds=用户点名的 7 条",
    )
    ap.add_argument(
        "--text-source",
        choices=("summary", "predicate"),
        default="summary",
        help="summary=拿标题+摘要（以用户为主语）；predicate=只拿结论句 subject_key/predicate_key",
    )
    args = ap.parse_args()

    db = os.path.expanduser("~/.memory-bread/memory-bread.db")
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    conn.row_factory = sqlite3.Row
    import datetime

    local = datetime.datetime.now().astimezone()
    midnight = int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    if args.scope == "seeds":
        rows = conn.execute(
            "SELECT id, title, summary, content FROM bake_knowledge"
            " WHERE id IN (1206, 3804, 4196, 4270, 4273, 4274, 4275) ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, summary, content FROM bake_knowledge WHERE created_at_ms>=? ORDER BY id",
            (midnight,),
        ).fetchall()

    picked = []
    for row in rows:
        try:
            payload = json.loads(row["content"] or "{}")
        except json.JSONDecodeError:
            continue
        if args.scope == "all":
            picked.append(row)
        elif args.scope == "veto-eligible":
            if (
                payload.get("irreplaceability") == "partially_recoverable"
                and payload.get("reuse_audience") == "anyone_same_domain"
            ):
                picked.append(row)
        else:
            if row["id"] in (1206, 3804, 4196, 4270, 4273, 4274, 4275):
                picked.append(row)
    if args.limit:
        picked = picked[: args.limit]

    tally = {}
    for row in picked:
        try:
            content = json.loads(row["content"] or "{}")
        except json.JSONDecodeError:
            content = {}
        if args.text_source == "predicate":
            text = "；".join(
                v for v in (content.get("subject_key"), content.get("predicate_key")) if v
            )
        else:
            text = " / ".join([v for v in (row["title"], row["summary"]) if v])
        text = text[:600] or (row["title"] or "")[:600]
        raw = ask(text)
        verdict = classify(raw)
        tally[verdict] = tally.get(verdict, 0) + 1
        print(
            "%s %s  %s" % (row["id"], verdict, text[:70]), file=sys.stderr, flush=True
        )
    print(
        json.dumps(
            {
                "scope": args.scope,
                "text_source": args.text_source,
                "asked": len(picked),
                "tally": tally,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
