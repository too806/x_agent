import assert from 'node:assert/strict'
import { test } from 'node:test'

import { traceKind, traceReason, traceSummary } from '../src/utils/trace.ts'

/**
 * Agent 决策轨迹的展示逻辑测试。
 *
 * 这里测的两件事都不是"算得对不对"，而是**遇到不认识的东西时会不会装作没事**：
 * 后端加了新动作、某个动作没写理由、步号不连续。这三种情况在真实运行里都会出现，
 * 而它们的错误表现都是"界面上少了一段或者空了一块"，没有异常、没有日志。
 */

test('十个已知动作各自归到它那一类', () => {
  // 取值来自 app/agent/schemas.py::AgentAction。前端不新增也不改名，
  // 这个表只是把动作归到视觉分组上。
  const expected: Record<string, string> = {
    discover_topics: 'plan',
    research_topic: 'research',
    deepen_research: 'research',
    extract_evidence: 'research',
    generate_angles: 'generate',
    select_angle: 'generate',
    generate_candidates: 'generate',
    criticize: 'critic',
    revise: 'revise',
    finish: 'finish',
  }
  for (const [action, kind] of Object.entries(expected)) {
    assert.equal(traceKind(action), kind, `${action} 应该归到 ${kind}`)
  }
})

test('认不出的动作落到中性类，而不是被丢掉', () => {
  // 后端加了新动作而前端还没跟上时，这一步必须**照样出现在时间线上**，
  // 只是没有专属配色。悄悄丢掉一步比配色不对严重得多。
  assert.equal(traceKind('some_future_action'), 'plan')
})

test('空动作不抛异常', () => {
  assert.equal(traceKind(null), 'plan')
  assert.equal(traceKind(undefined), 'plan')
  assert.equal(traceKind(''), 'plan')
})

test('没写理由时说成一句话，不留白', () => {
  // 留白看起来像渲染出错。而理由恰恰是整条轨迹里唯一由 Agent 自己写的字段，
  // 它空着本身就是一条信息。
  const fallback = traceReason(null)
  assert.equal(traceReason(undefined), fallback)
  assert.equal(traceReason(''), fallback)
  assert.equal(traceReason('   '), fallback)
  assert.equal(traceReason('\n\t '), fallback)
  assert.notEqual(fallback.trim(), '')
})

test('有理由时原样给出（只去首尾空白）', () => {
  assert.equal(traceReason('  因为素材太少  '), '因为素材太少')
})

test('空轨迹不给概览', () => {
  assert.equal(traceSummary([]), null)
})

test('单步说"1 步"', () => {
  assert.equal(traceSummary([{ step: 1 }]), '1 步')
})

test('连续的步号只报步数', () => {
  assert.equal(traceSummary([{ step: 1 }, { step: 2 }, { step: 3 }]), '3 步')
})

test('步号不连续时把区间说出来', () => {
  // 中间缺的那一步有信息量（它没有被记下来），不能只报一个"3 条"。
  const summary = traceSummary([{ step: 1 }, { step: 3 }])
  assert.ok(summary)
  assert.match(summary, /1–3/)
  assert.match(summary, /2 条/)
})
