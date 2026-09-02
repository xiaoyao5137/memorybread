# 时间证据长期建设：阶段 A 交付包

> 状态：方案已于 2026-08-29 放弃。不得据此启动阶段 A.1 或阶段 B-E；本目录仅保留阶段 A 的历史验收证据。

本目录是 `creation-temporal-evidence-rfc.md` 阶段 A 的可评审交付物。阶段 A 不接入运行链路、不迁移用户数据库、不改变现有创作。

## 交付物

| 文件/目录 | 内容 |
| --- | --- |
| `contract.md` | 三类时间、事项、状态、日期角色、用途判断的规范性契约 |
| `annotation-guide.md` | 金标语料的切分、标注、分歧处理和隐私规则 |
| `storage-api-design.md` | 阶段 B 候选数据库、索引、内部 API、迁移与回滚设计 |
| `evaluation-deterministic.json` | 文档级基线与事项级确定性门禁的冻结评测结果 |
| `evaluation-live-model.json` | 本地模型事项提取与门禁的评测结果，生成后冻结 |
| `manual-review-sample.csv` | 60 条不含答案的分层人工复核表，每个场景族两条 |
| `acceptance-report.md` | 阶段 A 逐项验收结论、限制和阶段 B 建议 |
| `acceptance-result.json` | 机器可读质量门结果和阶段 B 准入结论 |

配套代码和语料：

- `shared/temporal-evidence/temporal-evidence.schema.json`
- `ai-sidecar/temporal_evidence_stage_a.py`
- `ai-sidecar/scripts/generate_temporal_evidence_stage_a_corpus.py`
- `ai-sidecar/scripts/render_temporal_evidence_stage_a_report.py`
- `ai-sidecar/scripts/score_temporal_evidence_manual_review.py`
- `ai-sidecar/tests/fixtures/temporal_evidence_stage_a.jsonl`
- `ai-sidecar/tests/test_temporal_evidence_stage_a.py`

## 可复现命令

```bash
cd MemoryBread

python3.9 ai-sidecar/scripts/generate_temporal_evidence_stage_a_corpus.py \
  --output ai-sidecar/tests/fixtures/temporal_evidence_stage_a.jsonl \
  --review-output doc/temporal-evidence-stage-a/manual-review-sample.csv

python3.9 ai-sidecar/temporal_evidence_stage_a.py validate \
  --corpus ai-sidecar/tests/fixtures/temporal_evidence_stage_a.jsonl

python3.9 ai-sidecar/temporal_evidence_stage_a.py evaluate \
  --corpus ai-sidecar/tests/fixtures/temporal_evidence_stage_a.jsonl \
  --output doc/temporal-evidence-stage-a/evaluation-deterministic.json

python3.9 ai-sidecar/temporal_evidence_stage_a.py live-evaluate \
  --corpus ai-sidecar/tests/fixtures/temporal_evidence_stage_a.jsonl \
  --split holdout \
  --output doc/temporal-evidence-stage-a/evaluation-live-model.json

python3.9 ai-sidecar/scripts/render_temporal_evidence_stage_a_report.py \
  --deterministic doc/temporal-evidence-stage-a/evaluation-deterministic.json \
  --live doc/temporal-evidence-stage-a/evaluation-live-model.json \
  --output doc/temporal-evidence-stage-a/acceptance-report.md \
  --json-output doc/temporal-evidence-stage-a/acceptance-result.json
```

真实模型评测只访问 `127.0.0.1` 的本地模型服务。评测工具不读取用户数据库，也不上传语料。
