import assert from 'node:assert/strict'
import { test } from 'node:test'

import { EMPTY, agentActionLabel } from '../src/utils/format.ts'

/**
 * Agent 动作名的中文化。
 *
 * 只锁一条约定：**没翻译的动作必须原样显示英文枚举**，不能塌成"未知"之类的
 * 占位符。后端加了新动作而这里忘了加中文时，时间线上会出现一个看得见缺口 ——
 * 这是刻意的（见 `format.ts` 里同一段注释）。
 *
 *   npm run test:frontend
 */

// 与后端 `app/agent/schemas.py::AgentAction` 的取值一致。
const ACTIONS = [
  'discover_topics',
  'research_topic',
  'deepen_research',
  'extract_evidence',
  'generate_angles',
  'select_angle',
  'generate_candidates',
  'criticize',
  'revise',
  'finish',
]

test('每个已知动作都有中文，且不残留英文下划线', () => {
  for (const action of ACTIONS) {
    const label = agentActionLabel(action)
    assert.notEqual(label, action, `${action} 没有中文文案`)
    assert.ok(label.length > 0)
  }
})

test('没翻译的动作原样返回，而不是"未知"', () => {
  assert.equal(agentActionLabel('summarize_everything'), 'summarize_everything')
})

test('空值显示 —', () => {
  assert.equal(agentActionLabel(null), EMPTY)
  assert.equal(agentActionLabel(undefined), EMPTY)
})
