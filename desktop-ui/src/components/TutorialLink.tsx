import React from 'react'
import { CircleHelp } from 'lucide-react'
import { openExternalUrl } from '../utils/appMetadata'
import './TutorialLink.css'

export const TUTORIAL_URLS = {
  consult: 'https://my.feishu.cn/wiki/KQ0dwThCgiM31Ck5gFFcpgZvnFb',
  creation: 'https://my.feishu.cn/wiki/S9RfwxiJPicCUdkq9B5cGb8JnOd',
  tasks: 'https://my.feishu.cn/wiki/Q6uswAsG5i6Mk1kkKLbcUCk7nTg',
  diary: 'https://my.feishu.cn/wiki/QgHAwh0FPiHmn5kuR1rcqciwnvf',
  memory: 'https://my.feishu.cn/wiki/VI81whICIidpuHk1r1nc5OLinBF',
  documents: 'https://my.feishu.cn/wiki/O0BpwM1kriy5ndkb1DKccePnnUf',
  knowledge: 'https://my.feishu.cn/wiki/Bughwcm1hiwPDukWCzZchSmwne2',
  operations: 'https://my.feishu.cn/wiki/NzHfwbk9Xi2aXakCzB9cxoXwnGf',
  data: 'https://my.feishu.cn/wiki/B2hjwF4cCiw1uyk4B9cc5qAxncc',
  memoryGraph: 'https://my.feishu.cn/wiki/M2OXwOSumibN2fkLhgeckI8pnTM',
  backup: 'https://my.feishu.cn/wiki/S2V6wDZmhig0SZkebNccxiEznNc',
  browserIntegration: 'https://my.feishu.cn/wiki/QI6RwITYziz15fkY0z9c4Hh6nWh',
} as const

const TutorialLink: React.FC<{
  url: string
  label?: string
  className?: string
}> = ({ url, label = '教程', className = '' }) => (
  <button
    type="button"
    className={`tutorial-link ${className}`.trim()}
    onClick={() => { void openExternalUrl(url) }}
    aria-label={`查看${label}（在浏览器中打开）`}
    title="查看教程"
  >
    <CircleHelp size={16} aria-hidden="true" />
  </button>
)

export default TutorialLink
