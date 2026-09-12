"""Synthetic existing-document Brainstorm update over the running local service.

No Core endpoint is called: events stay in the output file, never in user history.
Run with the sidecar Python environment and an explicit output path.
"""
import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    session_id = 'acceptance-intent-restatement-' + str(uuid4())
    root = '编写一份活动通知，说明时间和地点。只使用已确认信息，不检索，不增加称谓或活动内容。'
    confirmed = '活动地点改为二楼会议室，时间仍为周三下午。'
    document = '# 活动通知\n\n时间：周三下午。\n地点：一楼会议室。\n'
    instruction = ('先生成一版。请基于原始创作要求和当前创作简报中已提交的回答，'
                   '在当前文档基础上更新一版文档。'
                   '尚未回答的问题、未确认的选项和待补充事项不能当作用户决定；'
                   '缺失内容标注待确认，不编造事实。')
    payload = {
        'user_prompt': instruction, 'root_request': root,
        'session_id': session_id, 'current_document': document,
        'design_templates': [], 'selected_skills': [], 'available_skills': [],
        'conversation': [{'role': 'user', 'content': root},
                         {'role': 'assistant', 'content': document},
                         {'role': 'user', 'content': confirmed}],
        'creation_mode': 'brainstorm', 'model_mode': 'local',
        'enable_rag': False, 'enabled_tools': [],
        'creation_brief': {
            'session_id': session_id, 'root_request': root, 'phase': 'exploring',
            'revision': 2, 'answered_count': 1, 'depth': 1,
            'brief_markdown': '# 创作简报\n\n## 已确认\n' + confirmed,
            'decisions': [{'question_id': 'event.details', 'dimension': '活动时间与地点',
                           'summary': confirmed, 'user_inputs': [confirmed], 'source': 'user'}],
            'history': [], 'open_flags': [], 'current_question': None,
        },
    }
    events = []
    started = time.monotonic()
    result = {'scope': 'synthetic HTTP update; no Core/history writes', 'payload': payload}
    try:
        with httpx.Client(timeout=900, trust_env=False) as client:
            with client.stream('POST', 'http://127.0.0.1:8001/creation/agent/run', json=payload) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith('data: '):
                        continue
                    event = json.loads(line[6:])
                    events.append(event)
                    if event['type'] not in ('operation.checkpoint', 'agent.delta', 'document.delta'):
                        print(json.dumps({'event': event['type'], 'seconds': round(time.monotonic() - started, 1)}, ensure_ascii=False), flush=True)
    finally:
        result.update(events=events, seconds=round(time.monotonic() - started, 2))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    completed = next((event for event in reversed(events) if event['type'] == 'run.completed'), None)
    assert completed is not None, 'No completed event'
    output = completed['data']['document']
    assert '二楼会议室' in output and '周三下午' in output and '一楼会议室' not in output
    reviews = [event['data'].get('review', {}) for event in events if event['type'] in ('delivery.checked', 'delivery.rechecked')]
    assert any(review.get('status') == 'pass' for review in reviews), 'Delivery has not passed'
    assert any(event['type'] == 'inputs.assessed' for event in events)
    result.update(passed=True, final_document=output)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': True, 'seconds': result['seconds'], 'final_document': output}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
