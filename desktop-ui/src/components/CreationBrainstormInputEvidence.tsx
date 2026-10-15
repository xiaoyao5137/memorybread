import type {
  CreationBrainstormInputNeed,
  CreationBrainstormInputPreflight,
  CreationBrainstormInputReceipt,
  CreationBrainstormQuestion,
} from '../store/useAppStore'

const preflightLabels: Record<CreationBrainstormInputPreflight['status'], string> = {
  matched: '已找到相关本地资料',
  partial: '部分资料仍需核对',
  empty: '未找到匹配资料',
  failed: '本地资料检索失败',
  skipped: '尚未检索本地资料',
}

const needLabels: Record<CreationBrainstormInputNeed['status'], string> = {
  matched: '已有匹配资料',
  candidate: '候选资料待核对',
  partial: '资料内容不完整',
  missing: '未找到匹配资料',
  failed: '检索失败',
  skipped: '尚未检索',
}

const periodLabels: Record<CreationBrainstormInputReceipt['period_status'], string> = {
  matched: '与目标周期相符',
  mismatch: '与目标周期不符',
  unknown: '与目标周期关系待确认',
}

const completenessLabels: Record<CreationBrainstormInputReceipt['completeness'], string> = {
  complete: '内容完整',
  partial: '仅有部分内容',
  unknown: '完整性待核对',
}

function sourceDate(value: string | number | null | undefined): string | null {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value
  const numeric = typeof value === 'number' ? value : /^\d{10,13}$/.test(value) ? Number(value) : null
  const date = new Date(numeric === null ? value : numeric < 1e11 ? numeric * 1000 : numeric)
  return Number.isNaN(date.getTime()) ? null : date.toLocaleDateString('zh-CN')
}

function receiptDate(receipt: CreationBrainstormInputReceipt) {
  const observed = sourceDate(receipt.observed_at)
  const updated = sourceDate(receipt.updated_at)
  if (observed && updated && observed !== updated) return `采集 ${observed} · 更新 ${updated}`
  if (observed || updated) return `采集或更新 ${observed || updated}`
  return '采集日期未标明'
}

function receiptPeriod(receipt: CreationBrainstormInputReceipt) {
  const period = receipt.source_period
  return period?.start && period.end ? `统计期 ${period.start} 至 ${period.end}` : '统计期未标明'
}

export default function CreationBrainstormInputEvidence({ question, preflight }: {
  question: CreationBrainstormQuestion
  preflight?: CreationBrainstormInputPreflight
}) {
  if (!question.input_clarification || !preflight) return null
  const ids = new Set(question.input_clarification.need_ids)
  const needs = preflight.needs.filter(need => ids.has(need.id))
  const period = preflight.period
  const periodText = period.status === 'confirmed' && period.start && period.end
    ? `已确认周期 ${period.start} 至 ${period.end}`
    : period.status === 'not_required' ? '本次无需指定报告周期' : '报告周期待你确认'
  return <section className="creation-brainstorm-input-evidence" aria-label="本地资料核查">
    <div className="creation-brainstorm-input-evidence__heading">
      <strong>本地资料核查</strong>
      <span>{preflightLabels[preflight.status]}{preflight.as_of_local_date ? ` · 核查日期 ${preflight.as_of_local_date}` : ''}</span>
    </div>
    <p>{periodText}。统计期与采集时间分别核对。</p>
    {needs.length > 0 && <ul>
      {needs.map(need => {
        const first = need.receipts[0]
        return <li key={need.id} className={`is-${need.status}`}>
          <details>
            <summary>
              <span>{need.label}</span>
              <small>{needLabels[need.status]}{first ? ` · ${receiptPeriod(first)} · ${receiptDate(first)}` : ''}</small>
            </summary>
            {need.receipts.length > 0 ? <ul>
              {need.receipts.map((receipt, index) => <li key={`${receipt.source_type}:${receipt.source_id}:${index}`}>
                <strong>{receipt.title}</strong>
                <span>来源 {receipt.source_type} #{receipt.source_id}</span>
                <span>{receiptPeriod(receipt)} · {receiptDate(receipt)} · {periodLabels[receipt.period_status]} · {completenessLabels[receipt.completeness]}</span>
                {!receipt.url_redacted && /^https?:\/\//i.test(receipt.source_url || '') && <a
                  href={receipt.source_url} target="_blank" rel="noopener noreferrer">打开来源链接</a>}
              </li>)}
            </ul> : <p>{need.status === 'failed' ? '检索未完成，不能据此认定资料不存在。'
              : need.status === 'skipped' ? '本轮尚未检索，不能据此认定资料不存在。'
                : '没有可核对的来源记录。'}</p>}
          </details>
        </li>
      })}
    </ul>}
    {question.input_clarification.kind === 'source_confirmation' && <p>可选择已找到的来源，或在自定义答案中补充资料链接与定位信息。</p>}
  </section>
}
