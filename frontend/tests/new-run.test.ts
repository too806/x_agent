import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  DEFAULT_RUN_MODE,
  RUN_MODES,
  RUN_MODE_LABELS,
  createRunFailureMessage,
  createdRunNotice,
  newRunView,
  toRunPayload,
} from '../src/utils/newRun.ts'

/**
 * 「新建 Run」的输入契约测试。
 *
 * 用 Node 自带的 `node:test`（见 `tests/queue-selection.test.ts` 的说明）——这个
 * 前端没有组件渲染测试：`node --test` 不能转换 `.tsx`，仓库里也没有 DOM 环境。
 * 所以被测的判断全部收在 `src/utils/newRun.ts`（无依赖纯函数），而**那份视图模型
 * 就是弹窗渲染的全部依据**：组件照着它渲染，自己不写一个 if。这与
 * `utils/publishAction.ts` 的处理是同一套。
 *
 * 「Test 1：打开新建 Run 能看到创建界面」在这里的表达是：**默认状态下这份视图
 * 模型是什么**（问法、例子、能不能提交、有哪两个选项）——也就是弹窗打开瞬间
 * 屏幕上会出现的东西。点击本身不在这里测，它由组件实现保证。
 *
 *   npm run test:frontend
 */

const TOPIC = '英伟达最近为什么上涨？'
const GOAL = '今天关注 AI，找一个最近最值得讨论的话题'

// ---------------------------------------------------------------------------
// Test 1：打开之后看到什么
// ---------------------------------------------------------------------------

test('默认打开：问的是「你想写什么」，例子是具体题目，且还不能提交', () => {
  const view = newRunView({ mode: DEFAULT_RUN_MODE, text: '' })

  assert.equal(DEFAULT_RUN_MODE, 'topic')
  assert.equal(view.question, '你想写什么？')
  assert.equal(view.placeholder, '例如：英伟达最近为什么上涨？')
  // 空输入不能提交（Test 4 会展开），所以按钮此刻是灰的。
  assert.equal(view.canSubmit, false)
  assert.equal(view.payload, null)
})

test('两种运行方式都在界面上有名字，且没有第三种', () => {
  assert.deepEqual([...RUN_MODES], ['topic', 'goal'])
  assert.equal(RUN_MODE_LABELS.topic, '指定一个题目')
  assert.equal(RUN_MODE_LABELS.goal, 'Agent 自动选题')
})

test('切到 Agent 自动选题：问法与例子都换成"给方向"的那种', () => {
  const view = newRunView({ mode: 'goal', text: '' })

  assert.equal(view.question, '告诉 Agent 今天想关注什么')
  assert.equal(view.placeholder, '例如：今天关注 AI，找一个最近最值得讨论的话题')
  // 两种方式的问法必须是两句不同的话——同一个问法配两个链路，
  // 用户就分不清自己给的是"题目"还是"方向"。
  assert.notEqual(view.question, newRunView({ mode: 'topic', text: '' }).question)
})

// ---------------------------------------------------------------------------
// Test 2 / Test 3：提交什么
// ---------------------------------------------------------------------------

test('指定题目：发 {topic}，而且对象里没有 goal 这个键', () => {
  const payload = toRunPayload({ mode: 'topic', text: TOPIC })

  assert.deepEqual(payload, { topic: TOPIC })
  // 关键的一条：不是"goal 是空串"，是**根本没有 goal**。两个都给（哪怕有一个是
  // 空串）会命中后端"必须且只能提供 topic 或 goal 其中之一"那条规则。
  assert.equal(payload !== null && 'goal' in payload, false)
})

test('Agent 自动选题：发 {goal}，而且对象里没有 topic 这个键', () => {
  const payload = toRunPayload({ mode: 'goal', text: GOAL })

  assert.deepEqual(payload, { goal: GOAL })
  assert.equal(payload !== null && 'topic' in payload, false)
  // 人给的这句话不是题目：题目要由 Agent 跑完 discovery 才知道。
  assert.equal(newRunView({ mode: 'goal', text: GOAL }).canSubmit, true)
})

test('提交前去掉首尾空白（空检索词/空目标不该被送出去）', () => {
  assert.deepEqual(toRunPayload({ mode: 'topic', text: `  ${TOPIC}  ` }), { topic: TOPIC })
  assert.deepEqual(toRunPayload({ mode: 'goal', text: `\t${GOAL}\n` }), { goal: GOAL })
})

// ---------------------------------------------------------------------------
// Test 4：空输入
// ---------------------------------------------------------------------------

test('空、纯空格、全角空格都不能提交', () => {
  const blanks = ['', ' ', '   ', '\t', '\n', ' \t\n ', '　', '　 ']

  for (const text of blanks) {
    for (const mode of RUN_MODES) {
      assert.equal(toRunPayload({ mode, text }), null, `mode=${mode} text=${JSON.stringify(text)}`)
      const view = newRunView({ mode, text })
      // 两个说法必须同时为假：按钮亮着却发不出去，是最糟的一种。
      assert.equal(view.canSubmit, false)
      assert.equal(view.payload, null)
    }
  }
})

// ---------------------------------------------------------------------------
// Test 5：创建失败的文案
// ---------------------------------------------------------------------------

test('运行跑失败（5xx + RunResponse 形状）：说清 Run 已建好，并带出后端给的原因', () => {
  const message = createRunFailureMessage({
    status: 502,
    // 通用兜底只说得出这一句，真正的原因在 raw.error 里。
    message: '请求失败：HTTP 502。',
    raw: { run_id: 'run-1', status: 'failed', error: 'LLMError: upstream timeout' },
  })

  assert.match(message, /LLMError: upstream timeout/)
  assert.match(message, /已经建好/)
  // 这一句是这件事的全部意义：Run 在库里，人可以去看、也可以再发起一次。
  assert.match(message, /运行记录/)
  // 绝不许读起来像成功。
  assert.doesNotMatch(message, /成功/)
})

test('请求根本没被接受：用客户端挑出来的那句话，不编原因', () => {
  const cases = [
    { status: 422, message: '请求不合法 —— 必须且只能提供 topic 或 goal 其中之一', raw: { detail: [] } },
    { status: 0, message: '连不上 API 服务，请确认后端是否在运行。', raw: null },
    { status: 500, message: '请求失败：HTTP 500。', raw: 'Internal Server Error' },
  ]

  for (const error of cases) {
    assert.equal(createRunFailureMessage(error), error.message)
  }
})

test('形状只认一半时不当成"Run 已建好"', () => {
  // 只有 run_id（没有 error）：说不出原因，就不能替后端编一个。
  assert.equal(
    createRunFailureMessage({ status: 500, message: '请求失败：HTTP 500。', raw: { run_id: 'run-1' } }),
    '请求失败：HTTP 500。',
  )
  // error 是空白：同样没有原因可说。
  assert.equal(
    createRunFailureMessage({
      status: 502,
      message: '请求失败：HTTP 502。',
      raw: { run_id: 'run-1', error: '   ' },
    }),
    '请求失败：HTTP 502。',
  )
})

// ---------------------------------------------------------------------------
// Test 6：创建成功的文案
// ---------------------------------------------------------------------------

test('指定题目：提示里说清建的是什么题', () => {
  const notice = createdRunNotice({ topic: TOPIC })

  assert.equal(notice.title, '已创建 Run')
  assert.match(notice.detail, /题目/)
  assert.match(notice.detail, new RegExp(TOPIC))
})

test('Agent 自动选题：不能说"题目就是这句话"', () => {
  const notice = createdRunNotice({ goal: GOAL })

  assert.match(notice.detail, /目标/)
  // 这句是必须的：题目此刻还不存在，要等 Agent 跑完 discovery。
  assert.match(notice.detail, /题目由 Agent 自己发现/)
})

test('很长的输入在提示里截断，但"题目由 Agent 自己发现"那句不会被截掉', () => {
  const long = '关注'.repeat(80)
  const notice = createdRunNotice({ goal: long })

  // 目标那一段 = 60 字 + 省略号，剩下的部分原样保留。
  const summary = notice.detail.replace(/^目标：/, '').replace(/。题目由 Agent 自己发现。$/, '')
  assert.equal(Array.from(summary).length, 61)
  assert.match(notice.detail, /题目由 Agent 自己发现。$/)
  assert.ok(Array.from(notice.detail).length < Array.from(long).length, '提示不该把整段目标铺出来')

  // 截断只发生在**提示**里：发出去的仍然是完整的那句话（见 Test 2/3）。
  assert.deepEqual(toRunPayload({ mode: 'goal', text: long }), { goal: long })
})
