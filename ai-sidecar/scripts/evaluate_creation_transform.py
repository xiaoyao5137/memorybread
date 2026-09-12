"""Exercise real local routing and bounded rewriting against a synthetic document."""
import argparse
import asyncio
import json
from pathlib import Path

from creation.agent_loop import CreationAgentLoop
from creation.service import CreationOptions, CreationService


async def evaluate(output: Path, model: str) -> None:
    service = CreationService(model=model, enable_vector_recall=False)
    document = (
        '# 会议通知\n\n## 时间安排\n'
        '本次会议的具体时间已经确定，会议将于周一上午举行。'
        '请参会人员在周一上午按时参加本次会议。'
        '会议的主要内容为讨论当前的项目进展情况。'
        '\n\n## 联系方式\n请联系项目组。\n'
    )
    instruction = '精简时间安排中的重复措辞，保留时间和议题，其他部分原样保留。'
    events = [event async for event in CreationAgentLoop(service).run(
        user_message=instruction, root_request='市场研究与架构设计方案',
        current_document=document, conversation=[], selected_skills=[],
        options=CreationOptions(), session_id='acceptance-synthetic',
        run_id='acceptance-local-transform',
    )]
    result = events[-1].get('data', {}).get('document', '')
    checks = {
        'completed': events[-1]['type'] == 'run.completed',
        'shorter': 0 < len(result) < len(document),
        'preserved_outside': result.startswith('# 会议通知\n\n## 时间安排\n')
        and result.endswith('\n\n## 联系方式\n请联系项目组。\n'),
        'kept_facts': '周一上午' in result and '项目' in result,
        'no_resources': not any(event['actor']['id'] in {
            'memory_search', 'internet_search', 'solution_design_agent',
            'quality_review_agent', 'document_writer_agent',
        } for event in events),
    }
    report = {'model': model, 'instruction': instruction, 'input_document': document,
              'checks': checks, 'document': result,
              'events': [{'type': event['type'], 'actor': event['actor']} for event in events
                         if event['type'] != 'operation.checkpoint']}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if not all(checks.values()):
        raise SystemExit('Local transform acceptance failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='qwen3.5:4b')
    args = parser.parse_args()
    asyncio.run(evaluate(args.output, args.model))
