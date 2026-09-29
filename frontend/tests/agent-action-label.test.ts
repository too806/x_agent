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
  // Phase 5B.1 加回来的动作。漏了这一项，这条测试就一直绿着——
  // 它断言的是"列在这里的都有中文"，列漏了当然不会失败。
  'select_topic',
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

test('每个动作的中文名字互不相同', () => {
  // 时间线上每一步只显示中文名。两个动作译成同一个词，轨迹里就会出现
  // "连续两步都叫 X"，人分不出 Agent 到底换没换动作——而动作正是这条轨迹的全部信息。
  const labels = ACTIONS.map((action) => agentActionLabel(action))
  assert.equal(new Set(labels).size, labels.length, `动作名有重复：${labels.join(' / ')}`)
})

test('没翻译的动作原样返回，而不是"未知"', () => {
  assert.equal(agentActionLabel('summarize_everything'), 'summarize_everything')
})

test('空值显示 —', () => {
  assert.equal(agentActionLabel(null), EMPTY)
  assert.equal(agentActionLabel(undefined), EMPTY)
})
