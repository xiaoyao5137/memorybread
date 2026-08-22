import React from 'react'
import './StartupLoading.css'

const StartupLoading: React.FC = () => (
  <main
    className="startup-loading"
    data-testid="startup-loading"
    role="status"
    aria-live="polite"
    aria-label="记忆面包正在启动"
  >
    <div className="startup-loading__indicator" aria-hidden="true">
      <span className="startup-loading__icon-frame">
        <img
          className="startup-loading__icon"
          src="/brand/memorybread-bread-mark.png"
          alt=""
        />
      </span>
      <span className="startup-loading__label">烘焙中....</span>
    </div>
  </main>
)

export default StartupLoading
