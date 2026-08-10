import React from 'react'
import {
  LockKeyhole,
  PackageCheck,
  PackagePlus,
  Power,
  PowerOff,
  Trash2,
} from 'lucide-react'
import {
  CREATION_TOOL_DEFINITIONS,
  type CreationToolId,
  type CreationToolState,
} from '../utils/creationTools'
import './CreationToolsPanel.css'

interface CreationToolsPanelProps {
  tools: CreationToolState[]
  onInstall: (id: CreationToolId) => void
  onUninstall: (id: CreationToolId) => void
  onToggle: (id: CreationToolId, enabled: boolean) => void
  onResultLimitChange: (id: CreationToolId, resultLimit: number) => void
}

const CreationToolsPanel: React.FC<CreationToolsPanelProps> = ({
  tools,
  onInstall,
  onUninstall,
  onToggle,
  onResultLimitChange,
}) => {
  const stateById = new Map(tools.map(tool => [tool.id, tool]))

  return (
    <main className="creation-tools-page">
      <header>
        <h2>工具</h2>
      </header>

      <section className="creation-tools-section" aria-labelledby="required-tools-title">
        <header>
          <h3 id="required-tools-title">必备工具</h3>
        </header>
        <div className="creation-tools-grid">
          {CREATION_TOOL_DEFINITIONS.filter(tool => tool.required).map((definition) => {
            const state = stateById.get(definition.id)
            return (
              <article className="creation-tool-card is-required" key={definition.id}>
                <div className="creation-tool-card__status-row">
                  <span className="creation-tool-card__status is-official">官方工具</span>
                  <span className="is-enabled"><LockKeyhole size={12} /> 始终开启</span>
                </div>
                <h4>{definition.name}</h4>
                <p>{definition.summary}</p>
                <div className="creation-tool-card__meta">{definition.capability}</div>
                {definition.resultLimit && (
                  <div className="creation-tool-card__setting">
                    <label htmlFor={`creation-tool-result-limit-${definition.id}`}>
                      默认召回条数
                    </label>
                    <span>
                      <input
                        id={`creation-tool-result-limit-${definition.id}`}
                        type="number"
                        min={definition.resultLimit.min}
                        max={definition.resultLimit.max}
                        step={1}
                        value={state?.resultLimit ?? definition.resultLimit.defaultValue}
                        onChange={event => onResultLimitChange(definition.id, Number(event.target.value))}
                        aria-label={`${definition.name}默认召回条数`}
                      />
                      条
                    </span>
                  </div>
                )}
                <footer>
                  <button type="button" className="is-installed" disabled aria-label={`${definition.name}已安装`}>
                    <PackageCheck size={14} /> 已安装
                  </button>
                  <button type="button" className="is-enabled" disabled aria-label={`${definition.name}已开启`}>
                    <Power size={14} /> 已开启
                  </button>
                </footer>
              </article>
            )
          })}
        </div>
      </section>

      <section className="creation-tools-section" aria-labelledby="optional-tools-title">
        <header>
          <h3 id="optional-tools-title">可选工具</h3>
        </header>
        <div className="creation-tools-grid">
          {CREATION_TOOL_DEFINITIONS.filter(tool => !tool.required).map((definition) => {
            const state = stateById.get(definition.id) || {
              id: definition.id,
              installed: false,
              enabled: false,
            }
            return (
              <article
                className={`creation-tool-card${state.installed ? ' is-installed' : ''}${state.enabled ? ' is-enabled' : ''}`}
                key={definition.id}
              >
                <div className="creation-tool-card__status-row">
                  <span className="creation-tool-card__status">可选工具</span>
                  <span className={state.enabled ? 'is-enabled' : state.installed ? 'is-installed' : ''}>
                    {state.enabled ? '已开启' : state.installed ? '已安装' : '未安装'}
                  </span>
                </div>
                <h4>{definition.name}</h4>
                <p>{definition.summary}</p>
                <div className="creation-tool-card__meta">{definition.capability}</div>
                <footer>
                  <button
                    type="button"
                    className={state.installed ? 'is-installed' : ''}
                    onClick={() => state.installed
                      ? onUninstall(definition.id)
                      : onInstall(definition.id)}
                    aria-label={`${state.installed ? '卸载' : '安装'}${definition.name}`}
                  >
                    {state.installed
                      ? <Trash2 size={14} />
                      : <PackagePlus size={14} />}
                    {state.installed ? '卸载' : '安装'}
                  </button>
                  <button
                    type="button"
                    className={state.enabled ? 'is-enabled' : ''}
                    onClick={() => onToggle(definition.id, !state.enabled)}
                    disabled={!state.installed}
                    aria-label={`${state.enabled ? '关闭' : '开启'}${definition.name}`}
                  >
                    {state.enabled
                      ? <PowerOff size={14} />
                      : <Power size={14} />}
                    {state.enabled ? '关闭' : '开启'}
                  </button>
                </footer>
              </article>
            )
          })}
        </div>
      </section>
    </main>
  )
}

export default CreationToolsPanel
