"""Read-only incident replay plus real local generation; never modifies history."""
import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from creation.document_integrity import integrity_problems, RISK_WRITING_POLICY
from creation.service import CreationService


async def main():
    report = {}
    db = Path.home() / '.memory-bread/memory-bread.db'
    with sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True) as connection:
        document = connection.execute('select generated_content from creation_history where id=131').fetchone()[0]
    report['incident_replay'] = {'history_id': 131, 'characters': len(document),
        'sha256': hashlib.sha256(document.encode()).hexdigest(),
        'risk_labels': document.count('【风险标注】'), 'problems': integrity_problems(document)}
    assert 'repeated_content' in report['incident_replay']['problems']
    service = CreationService(enable_vector_recall=False)
    system = '你是文档撰写者。只输出完整的简短正文，含三个二级章节：目标、执行、验收。总计300到500字。\n' + RISK_WRITING_POLICY
    prompt = ('为虚构的商家视频工具写试点方案。历史背景：2026年上半年试点有20家商家。'
        '此数字仅用于交代历史，不是目标周期实绩。现在的任务是设计下一阶段试点，'
        '不需要本周汇报或环比数据。目标是验证内容质量和商家使用意愿。不要编造结果。')
    chunks = []
    async for chunk in service.stream_agent_document(system_prompt=system, user_prompt=prompt):
        chunks.append(chunk)
    generated = ''.join(chunks).strip()
    report['live_generation'] = {'characters': len(generated), 'problems': integrity_problems(generated),
        'risk_labels': generated.count('【风险标注】'), 'headings': [l for l in generated.splitlines() if l.startswith('## ')]}
    assert generated and not report['live_generation']['problems']
    assert report['live_generation']['risk_labels'] <= 1
    assert len(report['live_generation']['headings']) == 3
    chunks = []
    async for chunk in service.stream_agent_document(system_prompt=system + '\n本轮只润色以下文档，保持全部二级标题和事实，精简表达。', user_prompt=generated):
        chunks.append(chunk)
    polished = ''.join(chunks).strip()
    problems = integrity_problems(polished, generated, preserve_sections=True)
    report['live_polish'] = {'characters': len(polished), 'problems': problems,
        'risk_labels': polished.count('【风险标注】')}
    assert not problems
    target = Path(__file__).resolve().parents[2] / 'doc/evaluations/creation-integrity-local-2026-09-05.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


asyncio.run(main())
