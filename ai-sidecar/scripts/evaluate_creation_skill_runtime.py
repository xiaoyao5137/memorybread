"""Live Core/sidecar/SQLite acceptance; creates named isolated test histories."""
import argparse
import hashlib
import gzip
import json
import time
from pathlib import Path

import httpx

ORIGINAL = '创作一篇如何对快手灵机产品大规模吸引非L0商家的方案，为商家制作爆款视频，进而极大提高GMV爆可品和可以使用SOTA视频生成模型的方案。'


def main(case, output, resume_session=None, history_id=None, prior_events=None):
    with httpx.Client(base_url='http://localhost:7070', timeout=1800) as client:
        raw = client.get('/api/creation/skills').raise_for_status().json()
        skills = [{**item, 'id': item['client_skill_key']} for item in raw if item['installed']]
        selected = next(item for item in skills if item['title'] == '技术架构方案评审文档模板')
        query = ORIGINAL if case == 'business' else '使用@技术架构方案评审文档模板 编写商家增长业务方案，文档标题为《灵机商家增长试点方案》，主目标是提高商家入驻与GMV。'
        session = resume_session or ('skill-governance-acceptance-' + case + '-' + str(int(time.time())))
        chat = [{'id': session + '-instruction', 'role': 'user', 'content': query}]
        history = {'id': history_id} if resume_session else client.post('/api/creation/history/start', json={'prompt': query, 'root_request': query,
            'session_id': session, 'conversation': chat}).raise_for_status().json()
        payload = {'user_prompt': query, 'root_request': query, 'session_id': session,
            'instruction_id': session + '-instruction', 'run_id': session + '-run',
            'design_templates': [], 'conversation': chat, 'available_skills': skills, 'selected_skills': [],
            'explicit_skill_ids': [] if case == 'business' else [selected['id']],
            'model_mode': 'local', 'enabled_tools': [], 'enable_rag': False,
            'enable_web_search': False, 'enable_image_generation': False}
        previous_text = (gzip.decompress(prior_events.read_bytes()).decode() if prior_events.suffix == '.gz' else prior_events.read_text()) if prior_events else ''
        events = [json.loads(line) for line in previous_text.splitlines()] if previous_text else []
        output.mkdir(parents=True, exist_ok=True)
        metadata = {'prior_events': str(prior_events) if prior_events else None, 'case': case, 'session_id': session, 'history': history,
                    'catalog': [{'id': s['id'], 'title': s['title']} for s in skills],
                    'tools': [], 'note': 'Local model; empty requested tool list. Strict workflows may resolve their own declared tools. Tests routing, assembly and persistence, not factual research quality.'}
        (output / (case + '-result.json')).write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        with (output / (case + '-events.jsonl')).open('w') as log:
            with client.stream('POST', '/api/creation/agent/run', json=payload) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith('data: '):
                        continue
                    event = json.loads(line[6:])
                    events.append(event)
                    audit_event = event
                    if event.get('type') in {'document.preview', 'document.delta', 'document.patch.delta'}:
                        content = event.get('data', {}).get('content', '')
                        audit_event = {key: event[key] for key in ('type', 'sequence', 'timestamp') if key in event}
                        audit_event['data'] = {'chars': len(content), 'sha256': hashlib.sha256(content.encode()).hexdigest(),
                            'first_line': content.splitlines()[0] if content else ''}
                    log.write(json.dumps(audit_event, ensure_ascii=False) + '\n')
                    log.flush()
                    if event.get('type') not in {'document.delta', 'document.preview', 'model.delta', 'agent.delta'}:
                        print(json.dumps({'type': event.get('type'), 'status': event.get('status'), 'message': event.get('message')}, ensure_ascii=False), flush=True)
        finished = next((e for e in reversed(events) if e.get('type') == 'run.completed'), {})
        doc = finished.get('data', {}).get('document', '')
        history_id = history['id']
        saved = client.get('/api/creation/history/' + str(history_id)).raise_for_status().json()
        stored = saved.get('generated_content', '')
        title = next((line[2:].strip() for line in doc.splitlines() if line.startswith('# ')), '')
        admission = [a for e in events if e.get('type') == 'skill.admission' for a in e.get('data', {}).get('assessments', [])]
        checks = {'completed': bool(finished), 'content_saved': bool(doc) and doc == stored,
                  'independent_title': bool(title) and title != selected['title'],
                  'expected_selection': (not any(a.get('admitted') for a in admission)) if case == 'business'
                      else any(a.get('admitted') and a.get('source') == 'user' for a in admission)}
        if case != 'business':
            checks['specified_title'] = title == '灵机商家增长试点方案'
        previews = [e.get('data', {}).get('content', '') for e in events if e.get('type') == 'document.preview']
        checks['preview_titles'] = bool(previews) and all(p.splitlines()[0] == '# ' + title for p in previews if p)
        metadata['actual_tools'] = sorted({e.get('actor', {}).get('id', '') for e in events if e.get('type') == 'tool.started'})
        metadata.update(title=title, chars=len(doc), checks=checks, passed=all(checks.values()), admission=admission)
        (output / (case + '.md')).write_text(doc)
        (output / (case + '-result.json')).write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
        if not metadata['passed']:
            raise SystemExit('Live runtime acceptance failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=['business', 'explicit'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument("--resume-session")
    parser.add_argument("--history-id", type=int)
    parser.add_argument("--prior-events", type=Path)
    args = parser.parse_args()
    if args.resume_session and not (args.history_id and args.prior_events):
        parser.error("Resume requires history ID and prior event evidence")
    main(args.case, args.output, args.resume_session, args.history_id, args.prior_events)
