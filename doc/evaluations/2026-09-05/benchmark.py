# -*- coding: utf-8 -*-
import argparse, json, time, urllib.request, fcntl, contextlib, hashlib
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--url', default='http://127.0.0.1:11436')
p.add_argument('--model', default='qwen3.5:4b')
p.add_argument('--label', required=True)
p.add_argument('--output-dir', type=Path, required=True)
p.add_argument('--smoke', action='store_true')
p.add_argument('--throughput', action='store_true')
p.add_argument('--repeats', type=int, default=2)
args = p.parse_args()
root = args.output_dir
root.mkdir(parents=True, exist_ok=True)

def request(path, body=None):
    req = urllib.request.Request(args.url + path, data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    return urllib.request.urlopen(req, timeout=240)

def generate(name, prompt, fmt=None, limit=8192):
    body = {'model': args.model, 'prompt': prompt, 'think': False, 'stream': True, 'keep_alive': '10m', 'options': {'num_ctx': 16384, 'num_predict': limit, 'temperature': 0.7, 'top_p': 0.8, 'top_k': 20, 'seed': 42}}
    if fmt:
        body['format'] = fmt
    start = time.monotonic()
    first = None
    text = ''
    thinking = ''
    d = {}
    try:
        with request('/api/generate', body) as r:
            for line in r:
                d = json.loads(line)
                if d.get('error'):
                    raise RuntimeError(d['error'])
                if d.get('response') and first is None:
                    first = time.monotonic() - start
                text += d.get('response', '')
                thinking += d.get('thinking', '')
                if d.get('done'):
                    break
        row = {k: d.get(k) for k in ['done', 'done_reason', 'total_duration', 'load_duration', 'prompt_eval_count', 'prompt_eval_duration', 'eval_count', 'eval_duration']}
        row.update(label=args.label, case=name, model=args.model, wall_s=round(time.monotonic() - start, 3), ttft_s=first, output_chars=len(text), thinking_chars=len(thinking), decode_tps=round(d.get('eval_count', 0) / max(d.get('eval_duration', 1) / 1000000000.0, 1e-09), 2), prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
        row['unattributed_s'] = (d.get('total_duration', 0) - sum((d.get(k, 0) for k in ['load_duration', 'prompt_eval_duration', 'eval_duration']))) / 1000000000.0
        if fmt:
            try:
                value = json.loads(text)
                row['json_valid'] = True
                row['required_fields_present'] = isinstance(value, dict) and all((k in value for k in ['summary', 'decisions', 'risks', 'actions', 'unknowns']))
            except ValueError:
                row['json_valid'] = False
        (root / (args.label + '-' + name + '.txt')).write_text(text)
    except Exception as exc:
        row = {'label': args.label, 'case': name, 'error': str(exc), 'wall_s': time.monotonic() - start}
    with open(root / (args.label + '.jsonl'), 'a') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return row
with open('/tmp/memory-bread-interactive-demand.lock', 'a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_SH)
    print('benchmark priority acquired', flush=True)
    if args.smoke:
        generate('smoke', '只回复：本地推理启动成功。', limit=32)
    else:
        fixed = '此材料完全虚构。项目代号青禾；负责人林岚。预算上限18万元，已支出6.4万元。试点为240人。上线日期尚未决定。测试截止日期2026年9月18日。质量门槛为事实准确率不低于97%、JSON合法率不低于99%。当前事实准确率95.8%，JSON合法率98.7%，均未达标。离线状态下核心功能必须可用。原始个人记录仅保存在本机。严禁把测试计划写成已完成事实。'
        topics = ['需求澄清', '离线采集', '事实提炼', '证据引用', '中文检索', '长文创作', '模型切换', '运行时安装', '性能测量', '数据迁移', '异常恢复', '用户反馈', '内存管理', '缓存复用', '多轮对话', '权限设置', '质量回归', '灰度发布', '旧版回退', '支持培训']
        records = []
        for i, t in enumerate(topics, 1):
            records.append(f'记录E{i:02d}：第{i}次讨论聚焦{t}。团队确认本阶段只进行内部验证，不承诺对外发布日期。此项负责人是林岚，执行人是周明，验收人是陈佳。当前已完成问题收集和现状梳理，方案评审、实现与回归测试尚未完成。评审需要分别记录可复现步骤、预期行为、实际行为、影响范围及回退条件；验收报告要保留测试数据与证据编号。对于{t}，建议先建立可配置的能力边界，避免按单一案例写死分支。遇到证据不足时保留未知，不推断用户身份或补写日期。风险包括旧版配置不兼容、跨进程状态不同步以及长任务排队。后续应先测量再决策，新旧版本并存，切换失败保留旧配置。任何新方案都需要验证中文内容、断网行为和数据保留；相关数量和进度不得与项目总览矛盾。')
        corpus = fixed + '\n' + '\n'.join(records)
        tasks = [('extraction', '请根据下列记录生成完整的结构化提炼结果，只输出JSON对象，键为summary、decisions、risks、actions、unknowns。summary是300字左右摘要；decisions和risks各包含至少8条；actions按20项讨论逐项列出负责人、执行人、验收人、当前状态、后续步骤、验收标准和证据编号；unknowns明确尚未决定或完成的事项。保留事实与建议的区别，总输出至少1800个汉字。材料：\n' + corpus, 'json'), ('qa', '根据材料回答：项目现在能否上线，为什么？请撰写1800至2400字的证据分析，覆盖预算、试点、质量门槛、当前结果、离线能力、隐私、回退和20项讨论的准备情况。每个主要判断引用[E编号]，区分已知事实、建议和未知事项，不重复灌水，不虚构完成情况。材料：\n' + corpus, None), ('creation', '请依据材料写一份1800至2400字的内部实施方案，包含现状、目标、20项工作的分工与依赖、质量门禁、分阶段验收、风险处理、回退与待决策事项。按准备、试点、验收的相对阶段安排，不杜撰具体上线日期。每个阶段写清楚进入条件与完成证据。保留材料中的准确数值，并区分计划与实际状态。材料：\n' + corpus, None)]
        generate('warmup', '请用一句中文说明先验证再切换模型的原因。', limit=64)
        if args.throughput:
            tasks = [('fixed-creation', tasks[-1][1] + '\n本轮目标篇幅至少4000字，请充分展开每个阶段和20项工作。', None)]
        for rep in range(args.repeats):
            for name, prompt, fmt in tasks:
                generate(name + '-' + str(rep + 1), prompt, fmt, limit=2048 if args.throughput else 8192)
