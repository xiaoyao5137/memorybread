// 仅用于执行过程的动作标题与规划预览；能力选择器和展开明细仍展示执行者。
export const CREATION_AGENT_ACTIONS: Record<string, string> = {
  '创作主 Agent': '生成创作内容',
  '创作 Agent': '生成创作内容',
  '行业调研 Agent': '调研行业与市场',
  '数据分析 Agent': '分析数据快照',
  '数据查询规划 Agent': '编译并执行数据查询',
  '方案设计 Agent': '设计落地方案',
  '章节设计 Agent': '设计章节结构',
  '文档撰写 Agent': '生成文档内容',
  '去 AI 味 Agent': '润色行文风格',
  '细节润色 Agent': '完善内容细节',
  '表格润色 Agent': '优化表格结构',
  '字体润色 Agent': '优化排版与重点标识',
  '图片润色 Agent': '完善文档图示',
  '质量审校 Agent': '质量审校',
  '全文整合润色 Agent': '全文整合润色',
}

export const creationActionText = (text: string): string => {
  let result = text
  for (const [name, action] of Object.entries(CREATION_AGENT_ACTIONS)) {
    const pattern = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/ /g, '\\s*')
    result = result.replace(new RegExp(`${pattern}\\b`, 'gi'), action)
  }
  return result
}
