import React, { useEffect, useMemo, useState } from 'react'
import type { ArticleTemplate } from '../../types'
import type { LocalCreationSkill } from '../../utils/creationSkills'
import { BakeButton, BakeCard, BakeMarkdown, BakeSectionHeader } from './BakeShared'

const formatTemplateTime = (timestamp?: number, fallback?: string) => {
  if (timestamp && timestamp > 0) {
    return new Date(timestamp).toLocaleString('zh-CN', { hour12: false })
  }
  return fallback || '—'
}

const BakeTemplatesTab: React.FC<{
  templates: ArticleTemplate[]
  total: number
  limit: number
  offset: number
  query: string
  from: string
  to: string
  draftQuery: string
  draftFrom: string
  draftTo: string
  selectedTemplateId: string | null
  onSelectTemplate: (id: string | null) => void
  onCreateTemplate: () => void
  onUpdateTemplate: (templateId: string, updater: (template: ArticleTemplate) => ArticleTemplate) => void
  onToggleTemplateStatus: (templateId: string) => void
  onDeleteTemplate: (templateId: string) => void
  onSettleSkill?: (template: ArticleTemplate) => void
  relatedSkills?: LocalCreationSkill[]
  onOpenSkill?: (skill: LocalCreationSkill) => void
  onViewSourceMemory: (memoryId?: string) => void
  memoryTitleById?: Map<string, string>
  onPageChange: (offset: number) => void
  onLimitChange: (limit: number) => void
  onDraftQueryChange: (query: string) => void
  onDraftFromChange: (value: string) => void
  onDraftToChange: (value: string) => void
  onSearch: () => void
  onClearFilters: () => void
  focusId?: string | null
}> = ({
  templates,
  total,
  limit,
  offset,
  query,
  from,
  to,
  draftQuery,
  draftFrom,
  draftTo,
  selectedTemplateId,
  onSelectTemplate,
  onCreateTemplate,
  onUpdateTemplate,
  onToggleTemplateStatus,
  onDeleteTemplate,
  onSettleSkill,
  relatedSkills = [],
  onOpenSkill,
  onViewSourceMemory,
  memoryTitleById = new Map(),
  onPageChange,
  onLimitChange,
  onDraftQueryChange,
  onDraftFromChange,
  onDraftToChange,
  onSearch,
  onClearFilters,
  focusId,
}) => {
  const selected = templates.find(item => item.id === selectedTemplateId) ?? templates[0]
  const [isEditing, setIsEditing] = useState(false)
  const [pageInput, setPageInput] = useState('')
  const hasActiveFilters = Boolean(query.trim() || from || to || focusId)
  const page = Math.floor(offset / limit) + 1
  const totalPages = Math.max(1, Math.ceil(total / limit))

  const editingValues = useMemo(() => ({
    name: selected?.title || '',
    category: selected?.docType || '',
    promptHint: selected?.promptHint || '',
    structureSections: selected?.sections.map(section => section.title).join('\n') || '',
    stylePhrases: selected?.stylePhrases.join('\n') || '',
    replacementRules: selected?.replacementRules.map(item => `${item.from} => ${item.to}`).join('\n') || '',
  }), [selected])

  const [draftName, setDraftName] = useState('')
  const [draftCategory, setDraftCategory] = useState('')
  const [draftPromptHint, setDraftPromptHint] = useState('')
  const [draftStructureSections, setDraftStructureSections] = useState('')
  const [draftStylePhrases, setDraftStylePhrases] = useState('')
  const [draftReplacementRules, setDraftReplacementRules] = useState('')

  useEffect(() => {
    setDraftName(editingValues.name)
    setDraftCategory(editingValues.category)
    setDraftPromptHint(editingValues.promptHint)
    setDraftStructureSections(editingValues.structureSections)
    setDraftStylePhrases(editingValues.stylePhrases)
    setDraftReplacementRules(editingValues.replacementRules)
    setIsEditing(false)
  }, [editingValues])

  const handleSave = () => {
    if (!selected) return
    onUpdateTemplate(selected.id, template => ({
      ...template,
      title: draftName.trim() || template.title,
      docType: draftCategory.trim() || template.docType,
      promptHint: draftPromptHint.trim(),
      sections: draftStructureSections
        .split('\n')
        .map(item => item.trim())
        .filter(Boolean)
        .map(title => ({ title, keywords: [] })),
      stylePhrases: draftStylePhrases
        .split('\n')
        .map(item => item.trim())
        .filter(Boolean),
      replacementRules: draftReplacementRules
        .split('\n')
        .map(item => item.trim())
        .filter(Boolean)
        .map(line => {
          const [from, to] = line.split('=>').map(item => item.trim())
          return { from: from || line, to: to || '' }
        }),
      updatedAt: new Date().toLocaleString('zh-CN', { hour12: false }),
      updatedAtMs: Date.now(),
    }))
    setIsEditing(false)
  }

  return (
    <>
      <BakeCard>
        <BakeSectionHeader
          title="文档"
          right={<BakeButton primary onClick={onCreateTemplate}>新建模板</BakeButton>}
        />
        <form
          className="bake-list-toolbar bake-list-toolbar--repository"
          onSubmit={(event) => {
            event.preventDefault()
            onSearch()
          }}
        >
          <div className="bake-list-toolbar__repository">
            <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--search">
              <label className="bake-form-field bake-filter-field bake-filter-field--search">
                <span className="bake-filter-label">关键词</span>
                <input
                  className="bake-input"
                  value={draftQuery}
                  onChange={(event) => onDraftQueryChange(event.target.value)}
                  placeholder="搜索模板名称、分类或提示词"
                />
              </label>
              <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--search">
                <BakeButton compact primary type="submit">搜索</BakeButton>
              </div>
            </div>
            <div className="bake-list-toolbar__repository-row bake-list-toolbar__repository-row--dates">
              <label className="bake-form-field bake-filter-field">
                <span className="bake-filter-label">新增开始日期</span>
                <input
                  className="bake-input"
                  type="date"
                  value={draftFrom}
                  onChange={(event) => onDraftFromChange(event.target.value)}
                />
              </label>
              <label className="bake-form-field bake-filter-field">
                <span className="bake-filter-label">新增结束日期</span>
                <input
                  className="bake-input"
                  type="date"
                  value={draftTo}
                  onChange={(event) => onDraftToChange(event.target.value)}
                />
              </label>
              {(draftQuery || query || draftFrom || from || draftTo || to || focusId) && (
                <div className="bake-list-toolbar__repository-actions bake-list-toolbar__repository-actions--secondary">
                  <BakeButton compact type="button" onClick={onClearFilters}>清除筛选</BakeButton>
                </div>
              )}
            </div>
          </div>
        </form>
      </BakeCard>
      <div className="bake-split-list-detail bake-split-list-detail--templates">
        <BakeCard className="bake-knowledge-list-card">
        <div className="bake-list bake-knowledge-list">
          {templates.length === 0 ? (
            <div className="bake-muted">{hasActiveFilters ? '当前筛选条件下没有文档。' : '当前还没有文档。'}</div>
          ) : templates.map(item => {
            const active = item.id === selected?.id
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => onSelectTemplate(item.id)}
                className={`bake-list-item bake-knowledge-list-item ${active ? 'bake-list-item--active' : ''}`.trim()}
              >
                <div className="bake-list-item__title bake-line-clamp-2">{item.title}</div>
                <div className="bake-muted bake-line-clamp-1">{item.docType}</div>
              </button>
            )
          })}
        </div>
        <div className="bake-pagination bake-pagination--extended">
          <div className="bake-pagination__controls">
            <BakeButton compact onClick={() => onPageChange(Math.max(0, offset - limit))}>上一页</BakeButton>
            <BakeButton compact onClick={() => onPageChange(offset + limit)}>{offset + limit >= total ? '已到底' : '下一页'}</BakeButton>
          </div>
          <div className="bake-pagination__summary bake-muted">模板共 {total} 条</div>
          <div className="bake-pagination__right">
            <label className="bake-pagination__field">
              <span className="bake-muted">每页</span>
              <select
                className="bake-input bake-pagination__select"
                value={String(limit)}
                aria-label="每页条数"
                onChange={(event) => onLimitChange(Number(event.target.value))}
              >
                {[10, 20, 50, 100].map(option => (
                  <option key={option} value={option}>{option} 条</option>
                ))}
              </select>
            </label>
            <div className="bake-pagination__jump">
              <span className="bake-muted">第</span>
              <input
                className="bake-input bake-pagination__input"
                type="number"
                min={1}
                max={totalPages}
                value={pageInput}
                onChange={(event) => setPageInput(event.target.value)}
                placeholder={String(page)}
                aria-label="跳转页码"
              />
              <span className="bake-muted">页</span>
              <BakeButton compact onClick={() => {
                const target = Number(pageInput)
                if (!Number.isFinite(target) || target < 1) return
                const nextPage = Math.min(totalPages, Math.floor(target))
                onPageChange((nextPage - 1) * limit)
                setPageInput('')
              }}>前往</BakeButton>
            </div>
          </div>
        </div>
      </BakeCard>

      <BakeCard className="bake-knowledge-detail-card">
        {selected ? (
          <div className="bake-kv bake-knowledge-detail">
            <div>
              <div className="bake-title" style={{ fontSize: 18 }}>{selected.title}</div>
              <div className="bake-muted" style={{ marginTop: 4 }}>
                {selected.docType} · ID: {selected.id} · 新增时间 {formatTemplateTime(selected.createdAtMs, selected.createdAt)} · 最近更新 {formatTemplateTime(selected.updatedAtMs, selected.updatedAt)}
              </div>
            </div>

            {selected.sourceUrl && (
              <div className="bake-knowledge-detail__section">
                <div className="bake-kv__title">来源网址</div>
                <a
                  href={selected.sourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="bake-source-url-link"
                >
                  {selected.sourceUrl}
                </a>
              </div>
            )}

            {isEditing ? (
              <div className="bake-grid-2">
                <label className="bake-form-field">
                  <span className="bake-kv__title">模板名称</span>
                  <input value={draftName} onChange={(event) => setDraftName(event.target.value)} className="bake-input" />
                </label>
                <label className="bake-form-field">
                  <span className="bake-kv__title">模板分类</span>
                  <input value={draftCategory} onChange={(event) => setDraftCategory(event.target.value)} className="bake-input" />
                </label>
                <label className="bake-form-field bake-form-field--full">
                  <span className="bake-kv__title">提示词说明</span>
                  <textarea value={draftPromptHint} onChange={(event) => setDraftPromptHint(event.target.value)} className="bake-textarea" rows={3} />
                </label>
                <label className="bake-form-field">
                  <span className="bake-kv__title">结构字段（每行一项）</span>
                  <textarea value={draftStructureSections} onChange={(event) => setDraftStructureSections(event.target.value)} className="bake-textarea" rows={6} />
                </label>
                <label className="bake-form-field">
                  <span className="bake-kv__title">风格短语（每行一项）</span>
                  <textarea value={draftStylePhrases} onChange={(event) => setDraftStylePhrases(event.target.value)} className="bake-textarea" rows={6} />
                </label>
                <label className="bake-form-field bake-form-field--full">
                  <span className="bake-kv__title">替换规则（格式：原词 =&gt; 替代词）</span>
                  <textarea value={draftReplacementRules} onChange={(event) => setDraftReplacementRules(event.target.value)} className="bake-textarea" rows={5} />
                </label>
              </div>
            ) : (
              <>
                <div className="bake-knowledge-detail__section">
                  <div className="bake-kv__title">结构骨架（决定输出结构）</div>
                  <div className="bake-list">
                    {selected.sections.length > 0 ? selected.sections.map(section => (
                      <div key={section.title} className="bake-list-item">
                        <div className="bake-list-item__title">{section.title}</div>
                        <div className="bake-muted">关键词：{section.keywords.join(' / ') || '未设置'}</div>
                      </div>
                    )) : <div className="bake-muted">暂无结构骨架</div>}
                  </div>
                </div>

                <div className="bake-knowledge-detail__section">
                  <div className="bake-kv__title">表达风格（决定措辞）</div>
                  <div className="bake-muted">常用短语：{selected.stylePhrases.join('、') || '—'}</div>
                  <div className="bake-muted" style={{ marginTop: 6 }}>
                    替换规则：{selected.replacementRules.map(item => `${item.from} → ${item.to}`).join('；') || '—'}
                  </div>
                  <div className="bake-muted" style={{ marginTop: 6 }}>写作提示：{selected.promptHint || '—'}</div>
                </div>

                <div className="bake-knowledge-detail__section">
                  <div className="bake-kv__title">详细描述</div>
                  <BakeMarkdown content={selected.fullContent} />
                </div>
              </>
            )}

            <div className="bake-knowledge-detail__section bake-related-skills">
              <div className="bake-kv__title">关联技能</div>
              {relatedSkills.length ? (
                <div className="bake-related-skills__list">
                  {relatedSkills.map(skill => (
                    <button type="button" key={skill.id} onClick={() => onOpenSkill?.(skill)}>
                      <span><strong>{skill.title}</strong><small>{skill.summary}</small></span>
                      <em>{skill.status === 'draft' ? '草稿' : skill.installed ? '已安装' : '已保存'}</em>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="bake-muted">这份文档还没有关联技能，可点击下方「沉淀技能」创建。</div>
              )}
            </div>

            <div className="bake-actions--primary">
              {isEditing ? (
                <>
                  <BakeButton primary onClick={handleSave}>保存模板</BakeButton>
                  <BakeButton onClick={() => setIsEditing(false)}>取消编辑</BakeButton>
                </>
              ) : (
                <>
                  <BakeButton primary onClick={() => setIsEditing(true)}>编辑模板</BakeButton>
                  {onSettleSkill ? (
                    <BakeButton onClick={() => onSettleSkill(selected)}>沉淀技能</BakeButton>
                  ) : null}
                  <BakeButton onClick={() => onToggleTemplateStatus(selected.id)}>{selected.status === 'enabled' ? '停用' : '启用'}</BakeButton>
                  <BakeButton danger onClick={() => onDeleteTemplate(selected.id)}>删除文档</BakeButton>
                  <BakeButton compact onClick={() => onViewSourceMemory(selected.sourceMemoryIds[0])}>查看来源时间线</BakeButton>
                </>
              )}
            </div>
            <div className="bake-related-summary">
              <div className="bake-related-row">
                <span className="bake-related-row__label">来源时间线</span>
                <span className="bake-related-row__value">
                  {selected.sourceMemoryIds.length > 0
                    ? selected.sourceMemoryIds.map(id => memoryTitleById.get(id) ?? `时间线 #${id}`).join('、')
                    : '暂无'}
                </span>
              </div>
            </div>
          </div>
        ) : (
          <div className="bake-muted">暂无模板</div>
        )}
      </BakeCard>
      </div>
    </>
  )
}

export default BakeTemplatesTab
