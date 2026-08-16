import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { BookOpenText, CheckCircle2, Download, FileCode2, PackageOpen, UserRound, X } from 'lucide-react'
import type {
  CreationSkillContent,
  CreationSkillMarketItem,
  CreationSkillPackageFile,
  LocalCreationSkill,
} from '../utils/creationSkills'
import {
  CREATION_SKILL_AGENT_OPTIONS,
  CREATION_SKILL_TOOL_OPTIONS,
  agentSkillZipBytes,
  codexSkillPackageFiles,
  isTextSkillFile,
  skillFileBytes,
  skillFileText,
} from '../utils/creationSkills'
import './CreationSkillDetail.css'

export interface CreationSkillDetailData extends CreationSkillContent {
  id: string
  title: string
  summary: string
  categoryPath: string[]
  author?: string
  isOfficial?: boolean
  statusLabel: string
  installed: boolean
  source: 'local' | 'market'
  packageFiles: CreationSkillPackageFile[]
}

interface CreationSkillDetailProps {
  skill: CreationSkillDetailData
  onClose: () => void
  focusFiles?: boolean
  primaryAction?: {
    label: string
    loadingLabel?: string
    loading?: boolean
    disabled?: boolean
    onClick: () => void
  }
}

export function localSkillDetail(
  skill: LocalCreationSkill,
  categoryPath: string[],
): CreationSkillDetailData {
  const detail = {
    ...skill,
    id: String(skill.id),
    categoryPath,
    statusLabel: skill.sourceKind === 'market'
      ? '来自市场'
      : skill.sourceKind === 'imported'
        ? '手工上传 · Codex / Claude Code 兼容'
        : skill.sourceKind === 'manual'
          ? '手工新建'
          : skill.published
          ? '已发布'
          : skill.status === 'draft'
            ? '草稿'
            : '已保存',
    source: skill.sourceKind === 'market' ? 'market' as const : 'local' as const,
  }
  return { ...detail, packageFiles: codexSkillPackageFiles(skill) }
}

export function marketSkillDetail(
  skill: CreationSkillMarketItem,
  installed: boolean,
): CreationSkillDetailData {
  const detail = {
    ...skill,
    categoryPath: skill.categoryPath.map(item => item.name),
    author: skill.author.nickname,
    statusLabel: skill.isOfficial ? 'MemoryBread 官方技能' : '市场技能',
    installed,
    source: 'market' as const,
  }
  return { ...detail, packageFiles: skill.packageFiles.map(file => ({ ...file })) }
}

export default function CreationSkillDetail({
  skill,
  onClose,
  focusFiles = false,
  primaryAction,
}: CreationSkillDetailProps) {
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const [selectedFilePath, setSelectedFilePath] = useState('SKILL.md')

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    closeButtonRef.current?.focus()
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  useEffect(() => {
    setSelectedFilePath(
      skill.packageFiles.some(file => file.path === 'SKILL.md')
        ? 'SKILL.md'
        : skill.packageFiles[0]?.path || '',
    )
  }, [skill.id, skill.packageFiles])

  const selectedFile = useMemo(
    () => skill.packageFiles.find(file => file.path === selectedFilePath) || skill.packageFiles[0],
    [selectedFilePath, skill.packageFiles],
  )

  if (focusFiles) {
    const totalBytes = skill.packageFiles.reduce((sum, file) => sum + file.sizeBytes, 0)
    return (
      <div className="creation-skill-modal" role="presentation" onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}>
        <section
          className="creation-skill-source"
          role="dialog"
          aria-modal="true"
          aria-labelledby={`creation-skill-source-title-${skill.id}`}
        >
          <header className="creation-skill-source__header">
            <div>
              <span>Skill 源文件</span>
              <h2 id={`creation-skill-source-title-${skill.id}`}>{skill.title}</h2>
              <p>{skill.packageFiles.length} 个文件 · {formatFileSize(totalBytes)} · Codex / Claude Code 兼容</p>
            </div>
            <button ref={closeButtonRef} type="button" onClick={onClose} aria-label="关闭源文件">
              <X size={18} />
            </button>
          </header>
          <div className="creation-skill-source__browser">
            <aside>
              <header><strong>源文件</strong><small>{skill.packageFiles.length} 个</small></header>
              {skill.packageFiles.map(file => (
                <button
                  type="button"
                  key={file.path}
                  className={file.path === selectedFile?.path ? 'is-active' : ''}
                  onClick={() => setSelectedFilePath(file.path)}
                  title={file.path}
                >
                  <FileCode2 size={14} />
                  <span>{file.path}<small>{formatFileSize(file.sizeBytes)}</small></span>
                </button>
              ))}
              {skill.packageFiles.length > 0 && (
                <button
                  className="creation-skill-source__download-all"
                  type="button"
                  onClick={() => downloadSkillPackage(skill.title, skill.packageFiles)}
                >
                  <Download size={14} />下载全部源文件 (.zip)
                </button>
              )}
            </aside>
            <section>
              <header>
                <div>
                  <strong>{selectedFile?.path || '选择文件'}</strong>
                  <small>{selectedFile ? `${selectedFile.mediaType} · ${formatFileSize(selectedFile.sizeBytes)}` : '没有可查看的源文件'}</small>
                </div>
                {selectedFile && (
                  <button type="button" onClick={() => downloadSkillFile(selectedFile)}>
                    <Download size={14} />下载文件
                  </button>
                )}
              </header>
              <div className="creation-skill-detail__file-preview creation-skill-source__preview">
                {selectedFile
                  ? <SkillFilePreview file={selectedFile} hideHeader />
                  : <p>这份技能还没有可查看的源文件。</p>}
              </div>
            </section>
          </div>
        </section>
      </div>
    )
  }

  const recipeSections = [
    {
      heading: skill.sectionHeadings.textStyle,
      content: skill.textStyle,
      examples: skill.fieldExamples.textStyle,
    },
    {
      heading: skill.sectionHeadings.diagramStyle,
      content: skill.diagramStyle,
      examples: skill.fieldExamples.diagramStyle,
    },
  ]
  const resourceLabels = new Map<string, string>([
    ...CREATION_SKILL_AGENT_OPTIONS.map(option => [option.id, option.label] as const),
    ...CREATION_SKILL_TOOL_OPTIONS.map(option => [option.id, option.label] as const),
  ])

  return (
    <div className="creation-skill-modal" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose()
    }}>
      <section
        className="creation-skill-detail"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`creation-skill-detail-title-${skill.id}`}
      >
        <header className="creation-skill-detail__header">
          <div>
            <span>{skill.statusLabel}</span>
            <h2 id={`creation-skill-detail-title-${skill.id}`}>{skill.title}</h2>
            <p>{skill.summary}</p>
            <div className="creation-skill-detail__meta">
              {skill.categoryPath.length > 0 && <span>{skill.categoryPath.join(' / ')}</span>}
              {skill.author && <span><UserRound size={14} /> {skill.author}</span>}
              {skill.installed && <span><CheckCircle2 size={14} /> 已安装</span>}
            </div>
          </div>
          <button ref={closeButtonRef} type="button" onClick={onClose} aria-label="关闭技能详情">
            <X size={18} />
          </button>
        </header>

        <div className="creation-skill-detail__body">
          <section className="creation-skill-detail__capability">
            <div>
              <span>Agent 触发依据</span>
              <h3>Skill 描述</h3>
              <p>{skill.skillDescription.purpose}</p>
            </div>
            <dl>
              <div><dt>适用文档</dt><dd>{skill.skillDescription.documentTypes.join('、')}</dd></div>
              <div><dt>解决问题</dt><dd>{skill.skillDescription.problems.join('；')}</dd></div>
              <div><dt>涉及领域</dt><dd>{skill.skillDescription.domains.join('、') || '不限特定领域'}</dd></div>
              <div><dt>目标产物</dt><dd>{skill.skillDescription.deliverables.join('；')}</dd></div>
            </dl>
            <div className="creation-skill-detail__workflow">
              {skill.executionSteps.map((step, index) => (
                <article key={`${step.id}-${index}`}>
                  <span>{String(index + 1).padStart(2, '0')}</span>
                  <div>
                    <h4>{step.title}</h4>
                    <p>{step.objective}</p>
                    {step.output.trim() && <small>产出：{step.output}</small>}
                    {step.tools.includes('data_search') && (
                      <small>网页采集：{step.retainWebpageScreenshot === true ? '保留证据截图' : '不保留证据截图（优先静默取数，必要时临时打开浏览器）'}</small>
                    )}
                    {(step.agents.length > 0 || step.skills.length > 0 || step.tools.length > 0) && (
                      <div className="creation-skill-detail__resources" aria-label={`${step.title} 可调用能力`}>
                        {step.agents.map(id => <span key={`agent-${id}`}><b>@{resourceLabels.get(id) || id}</b><i>Agent</i></span>)}
                        {step.tools.map(id => <span key={`tool-${id}`}><b>@{resourceLabels.get(id) || id}</b><i>Tool</i></span>)}
                        {step.skills.map(id => <span key={`skill-${id}`}><b>@{id}</b><i>Skill</i></span>)}
                      </div>
                    )}
                  </div>
                </article>
              ))}
            </div>
          </section>

          <section className="creation-skill-detail__files">
            <div className="creation-skill-detail__files-heading">
              <div>
                <span><PackageOpen size={15} /> Agent Skills 标准目录</span>
                <h3>源文件</h3>
                <p>{skill.packageFiles.length} 个文件 · {formatFileSize(skill.packageFiles.reduce((sum, file) => sum + file.sizeBytes, 0))}</p>
              </div>
              {skill.packageFiles.length > 0 && (
                <button type="button" onClick={() => downloadSkillPackage(skill.title, skill.packageFiles)}>
                  <PackageOpen size={14} /> 下载全部源文件 (.zip)
                </button>
              )}
              {selectedFile && (
                <button type="button" onClick={() => downloadSkillFile(selectedFile)}>
                  <Download size={14} /> 下载 {selectedFile.path.split('/').pop()}
                </button>
              )}
            </div>
            <div className="creation-skill-detail__file-browser">
              <nav aria-label="技能文件列表">
                {skill.packageFiles.map(file => (
                  <button
                    type="button"
                    key={file.path}
                    className={file.path === selectedFile?.path ? 'is-active' : ''}
                    onClick={() => setSelectedFilePath(file.path)}
                    title={file.path}
                  >
                    <FileCode2 size={14} />
                    <span>{file.path}</span>
                    <small>{formatFileSize(file.sizeBytes)}</small>
                  </button>
                ))}
              </nav>
              <div className="creation-skill-detail__file-preview">
                {selectedFile
                  ? <SkillFilePreview file={selectedFile} />
                  : <p>这份技能还没有可查看的文件。</p>}
              </div>
            </div>
          </section>

          <section className="creation-skill-detail__section">
            <span>标题写法</span>
            <h3>{skill.sectionHeadings.commonTitles}</h3>
            <ul>{skill.commonTitles.map(item => <li key={item}>{item}</li>)}</ul>
            <ExampleList items={skill.fieldExamples.commonTitles} />
          </section>

          {recipeSections.map(section => (
            <section className="creation-skill-detail__section" key={section.heading}>
              <span>创作配方</span>
              <h3>{section.heading}</h3>
              <p>{section.content}</p>
              <ExampleList items={section.examples} />
            </section>
          ))}

          <section className="creation-skill-detail__section">
            <span>作者话术</span>
            <h3>{skill.sectionHeadings.writingGuidelines}</h3>
            {skill.writingGuidelines.length > 0
              ? <ul>{skill.writingGuidelines.map(item => <li key={item}>{item}</li>)}</ul>
              : <p>这份技能没有提取到稳定的惯用话术。</p>}
            <ExampleList items={skill.fieldExamples.writingGuidelines} />
          </section>

          {(skill.distinctiveSections || []).map((section, index) => (
            <section className="creation-skill-detail__section creation-skill-detail__section--distinctive" key={`${section.title}-${index}`}>
              <span>特色亮点 {index + 1}</span>
              <h3>{section.title}</h3>
              <p>{section.description}</p>
              <div className="creation-skill-detail__guidance">
                <strong>复刻指引</strong>
                <p>{section.guidance}</p>
              </div>
              <ExampleList items={section.examples} />
            </section>
          ))}

          <section className="creation-skill-detail__document">
            <div>
              <BookOpenText size={17} />
              <h3>完整示例文档</h3>
            </div>
            <p>示例用于理解结构与表达方式，创作时不会照抄其中主题。</p>
            <article className="creation-skill-detail__markdown">
              <ReactMarkdown>{skill.exampleDocument}</ReactMarkdown>
            </article>
          </section>
        </div>

        <footer className="creation-skill-detail__footer">
          <button type="button" onClick={onClose}>关闭</button>
          {primaryAction && (
            <button
              type="button"
              className="is-primary"
              onClick={primaryAction.onClick}
              disabled={primaryAction.disabled || primaryAction.loading}
            >
              {primaryAction.loading && primaryAction.loadingLabel
                ? primaryAction.loadingLabel
                : primaryAction.label}
            </button>
          )}
        </footer>
      </section>
    </div>
  )
}

function SkillFilePreview({ file, hideHeader = false }: { file: CreationSkillPackageFile; hideHeader?: boolean }) {
  const text = skillFileText(file)
  if (text !== null && isTextSkillFile(file)) {
    return (
      <>
        {!hideHeader && <div><strong>{file.path}</strong><span>{file.mediaType}</span></div>}
        <pre>{text}</pre>
      </>
    )
  }
  if (file.mediaType.startsWith('image/')) {
    return (
      <>
        {!hideHeader && <div><strong>{file.path}</strong><span>{file.mediaType}</span></div>}
        <img src={`data:${file.mediaType};base64,${file.contentBase64}`} alt={`${file.path} 预览`} />
      </>
    )
  }
  return (
    <div className="creation-skill-detail__binary">
      <FileCode2 size={24} />
      <strong>{file.path}</strong>
      <span>该文件不支持直接预览，可下载后用对应应用打开。</span>
    </div>
  )
}

function downloadSkillFile(file: CreationSkillPackageFile) {
  const blob = new Blob([skillFileBytes(file)], { type: file.mediaType })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = file.path.split('/').pop() || 'skill-file'
  anchor.click()
  URL.revokeObjectURL(url)
}

function downloadSkillPackage(name: string, files: CreationSkillPackageFile[]) {
  const bytes = agentSkillZipBytes(name, files)
  const blob = new Blob([bytes], { type: 'application/zip' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${name.replace(/[^\p{L}\p{N}._-]+/gu, '-').replace(/^-|-$/g, '') || 'skill'}.zip`
  anchor.click()
  URL.revokeObjectURL(url)
}

function formatFileSize(bytes: number) {
  if (bytes < 1_024) return `${bytes} B`
  if (bytes < 1_024 * 1_024) return `${(bytes / 1_024).toFixed(bytes < 10_240 ? 1 : 0)} KB`
  return `${(bytes / (1_024 * 1_024)).toFixed(1)} MB`
}

function ExampleList({ items }: { items: string[] }) {
  if (items.length === 0) return null
  return (
    <div className="creation-skill-detail__examples">
      <strong>写法示例</strong>
      <ul>{items.map(item => <li key={item}>{item}</li>)}</ul>
    </div>
  )
}
