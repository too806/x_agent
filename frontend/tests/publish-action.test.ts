import assert from 'node:assert/strict'
import { test } from 'node:test'

import { PUBLISH_STATUSES, RUN_STATUSES } from '../src/types/api.ts'
import {
  FIRST_PUBLISH_LABEL,
  PUBLISH_ACTIONS,
  REPUBLISH_LABEL,
  RESOLVE_LABEL,
  UNRESOLVED_PUBLISHED_PATH,
  effectivePublishStatus,
  getPublishAction,
  isRunPublishEligible,
  publishActionView,
} from '../src/utils/publishAction.ts'

/**
 * 发布入口可见性契约。**这是安全测试，不是界面测试。**
 *
 * 这个矩阵每错一格，代价不是"界面不统一"，是多一条发到真实 X 账号上的推文。
 * 所以这里分两层测：
 *
 *   1. 一张**显式**的用例表（下面对照 §七 逐条写出来）—— 说清楚"我们约定的行为
 *      是这些"，改矩阵时 diff 就是审查对象。
 *   2. 一条**穷举**的不变式 —— 遍历所有 Run 状态 × 所有发布状态（含缺失与认不出
 *      的取值），断言两种按钮在任何一格都不会出现在不该出现的地方。上面那张表
 *      只能证明"我想到的组合是对的"，这一条才证明"我没有漏掉一种"。
 *
 *   npm run test:frontend
 */

/** Run 状态全集，外加三种"读不到 / 认不出"。 */
const RUN_INPUTS: readonly (string | null | undefined)[] = [
  ...RUN_STATUSES,
  null,
  undefined,
  'bogus_status',
]

const PUBLISH_INPUTS: readonly (string | null | undefined)[] = [
  ...PUBLISH_STATUSES,
  null,
  undefined,
  'no_such_status',
]

// ---------------------------------------------------------------------------
// 1. 显式矩阵（§七 的转移表）
// ---------------------------------------------------------------------------

/** [runStatus, publishStatus, 期望动作] */
const MATRIX: readonly [string | null | undefined, string | null | undefined, string][] = [
  // 首次发布
  ['approved', null, 'FIRST_PUBLISH'],
  ['edited', null, 'FIRST_PUBLISH'],

  // 重发：只有"确定没发出去"才允许
  ['approved', 'failed', 'REPUBLISH'],
  ['edited', 'failed', 'REPUBLISH'],

  // 在飞 / 已发出：没有任何发布入口
  ['approved', 'publishing', 'HIDDEN'],
  ['edited', 'publishing', 'HIDDEN'],
  ['approved', 'published', 'HIDDEN'],
  ['edited', 'published', 'HIDDEN'],

  // 结果未知：不留发布入口，只留去裁决
  ['approved', 'publish_unknown', 'RESOLVE'],
  ['edited', 'publish_unknown', 'RESOLVE'],

  // 没有发布资格：发布记录不能凌驾于人工审核之上
  ['waiting_for_review', null, 'HIDDEN'],
  ['waiting_for_review', 'failed', 'HIDDEN'],
  ['rejected', null, 'HIDDEN'],
  ['rejected', 'failed', 'HIDDEN'],
  ['running', null, 'HIDDEN'],
  ['running', 'failed', 'HIDDEN'],
  ['failed', null, 'HIDDEN'],
  ['failed', 'failed', 'HIDDEN'],
  ['completed', null, 'HIDDEN'],
  ['completed', 'failed', 'HIDDEN'],

  // 发布资格之后再要求一次：`publishing` 是 Run 在发布期间被镜像推进的状态，
  // **不是**一个新的发布资格。用它当资格就等于"一次在飞就能再发一次"。
  ['publishing', 'failed', 'HIDDEN'],
  ['publishing', null, 'HIDDEN'],
  ['publish_unknown', null, 'HIDDEN'],
  ['published', null, 'HIDDEN'],

  // 真实库里最常见的一种形状：发布期间 `agent_run.status` 会被镜像推进成与
  // `published_post.status` 同名。实测生产数据里两条 `published` 记录的 Run 状态
  // 就是 `published`（不是 `approved`）—— 所以"资格"不能只认 approved/edited 之后
  // 就完事，这几个镜像组合必须逐条钉住。
  ['publish_unknown', 'publish_unknown', 'RESOLVE'],
  ['publishing', 'publishing', 'HIDDEN'],
  ['published', 'published', 'HIDDEN'],
  // 镜像落后于记录（记录已经是未知，Run 还停在 publishing）：以记录为准。
  ['publishing', 'publish_unknown', 'RESOLVE'],
  ['published', 'publish_unknown', 'RESOLVE'],

  // publish_unknown 优先于资格判断（§三）：无论 Run 什么状态都要有裁决入口
  ['waiting_for_review', 'publish_unknown', 'RESOLVE'],
  ['rejected', 'publish_unknown', 'RESOLVE'],
  ['running', 'publish_unknown', 'RESOLVE'],
  ['failed', 'publish_unknown', 'RESOLVE'],

  // 读不到 / 认不出 —— 一律安全默认
  ['approved', undefined, 'HIDDEN'],
  ['edited', undefined, 'HIDDEN'],
  ['approved', 'no_such_status', 'HIDDEN'],
  ['edited', 'no_such_status', 'HIDDEN'],
  [null, null, 'HIDDEN'],
  [undefined, null, 'HIDDEN'],
  [null, 'failed', 'HIDDEN'],
  ['bogus_status', null, 'HIDDEN'],
  ['bogus_status', 'failed', 'HIDDEN'],
]

for (const [runStatus, publishStatus, expected] of MATRIX) {
  test(`矩阵：${String(runStatus)} + ${String(publishStatus)} → ${expected}`, () => {
    assert.equal(getPublishAction(runStatus, publishStatus), expected)
  })
}

// ---------------------------------------------------------------------------
// 2. 穷举不变式：两种按钮永远不会出现在不该出现的地方
// ---------------------------------------------------------------------------

test('穷举全部组合：publish_unknown / publishing 下绝不会出现发布或重发', () => {
  // 这就是"当前是否已经不存在任何 publish_unknown → 直接 Publish/Republish 的
  // 前端入口"这个问题的答案，而且它能被机器复验：把这条不变式打破的任何一次改动，
  // 都会在这里红掉。
  for (const runStatus of RUN_INPUTS) {
    for (const publishStatus of ['publish_unknown', 'publishing']) {
      const action = getPublishAction(runStatus, publishStatus)
      assert.ok(
        action !== 'FIRST_PUBLISH' && action !== 'REPUBLISH',
        `${String(runStatus)} + ${publishStatus} 不该拿到 ${action}`,
      )
    }
  }
})

test('穷举全部组合：没有发布资格的 Run 绝不会出现发布或重发', () => {
  for (const runStatus of RUN_INPUTS) {
    if (isRunPublishEligible(runStatus)) continue
    for (const publishStatus of PUBLISH_INPUTS) {
      const action = getPublishAction(runStatus, publishStatus)
      assert.ok(
        action !== 'FIRST_PUBLISH' && action !== 'REPUBLISH',
        `${String(runStatus)} + ${String(publishStatus)} 不该拿到 ${action}`,
      )
    }
  }
})

test('穷举全部组合：读不到的状态一律 HIDDEN', () => {
  for (const runStatus of RUN_INPUTS) {
    // undefined = 字段缺失；认不出的取值 = 后端加了枚举而前端还没跟上。
    for (const publishStatus of [undefined, 'no_such_status', 'another_unknown']) {
      assert.equal(
        getPublishAction(runStatus, publishStatus),
        'HIDDEN',
        `${String(runStatus)} + ${String(publishStatus)} 必须 HIDDEN`,
      )
    }
    // Run 状态读不到时也一样。
    assert.equal(getPublishAction(undefined, null), 'HIDDEN')
    assert.equal(getPublishAction(undefined, 'failed'), 'HIDDEN')
  }
})

test('穷举全部组合：返回值永远落在四种动作之内', () => {
  for (const runStatus of RUN_INPUTS) {
    for (const publishStatus of PUBLISH_INPUTS) {
      const action = getPublishAction(runStatus, publishStatus)
      assert.ok(
        (PUBLISH_ACTIONS as readonly string[]).includes(action),
        `${String(runStatus)} + ${String(publishStatus)} 返回了集合外的 ${action}`,
      )
    }
  }
})

test('只有 approved / edited 具备发布资格，其余一律没有', () => {
  const eligible = RUN_STATUSES.filter(isRunPublishEligible)
  assert.deepEqual([...eligible].sort(), ['approved', 'edited'])
  // RunStatus 里那三个"发布期间"的状态不能被当成新的资格。
  assert.equal(isRunPublishEligible('publishing'), false)
  assert.equal(isRunPublishEligible('publish_unknown'), false)
  assert.equal(isRunPublishEligible('published'), false)
  assert.equal(isRunPublishEligible(null), false)
  assert.equal(isRunPublishEligible(undefined), false)
  assert.equal(isRunPublishEligible(''), false)
})

test('publish_unknown 与 publishing 是两种不同的处境', () => {
  // 这是最容易顺手写错的一处：`isUnresolved()`（types/api.ts）把两者一起算"未决"，
  // 那是给发布记录页决定"要不要开裁决入口"用的。契约回答的是另一个问题 ——
  // "要不要放一个会调 X 的按钮" —— 所以对 publishing 必须是 HIDDEN。
  //
  // publishing 时第一个请求可能还在飞；此时给一个"去裁决"按钮也会把人引到
  // 一个还没落定的记录上。裁决入口留在发布记录页（唯一裁决台）。
  assert.equal(getPublishAction('approved', 'publish_unknown'), 'RESOLVE')
  assert.equal(getPublishAction('approved', 'publishing'), 'HIDDEN')
})

// ---------------------------------------------------------------------------
// 3. null 与 undefined 是两件事（§八）
// ---------------------------------------------------------------------------

test('null 表示"明确没有记录"，可以首次发布', () => {
  assert.equal(getPublishAction('approved', null), 'FIRST_PUBLISH')
})

test('undefined 表示"读不到"，绝不能当成"没有记录"', () => {
  // 把两者当成一回事，就会在接口少给一个字段的时候递上一个发布按钮。
  assert.equal(getPublishAction('approved', undefined), 'HIDDEN')
  assert.notEqual(getPublishAction('approved', undefined), getPublishAction('approved', null))
})

// ---------------------------------------------------------------------------
// 4. effectivePublishStatus：两个可能矛盾的字段
// ---------------------------------------------------------------------------

test('两个字段一致时就是它', () => {
  assert.equal(effectivePublishStatus({ status: 'failed' }, 'failed'), 'failed')
  assert.equal(
    effectivePublishStatus({ status: 'publish_unknown' }, 'publish_unknown'),
    'publish_unknown',
  )
})

test('两边都说没有记录时是 null（不是 undefined）', () => {
  assert.equal(effectivePublishStatus(null, null), null)
  assert.equal(effectivePublishStatus(undefined, null), null)
  assert.equal(effectivePublishStatus(null, undefined), null)
})

test('两个字段矛盾时返回 undefined —— 不猜，落进 HIDDEN', () => {
  // 有记录但镜像字段说"没有"：说不清。
  assert.equal(effectivePublishStatus({ status: 'failed' }, null), undefined)
  assert.equal(effectivePublishStatus({ status: 'failed' }, undefined), undefined)
  // 两边都非空但不一致：同样说不清。
  assert.equal(effectivePublishStatus({ status: 'failed' }, 'published'), undefined)
  assert.equal(effectivePublishStatus({ status: 'publishing' }, 'publish_unknown'), undefined)
  // 镜像字段有值但没有记录对象：也说不清。
  assert.equal(effectivePublishStatus(null, 'failed'), undefined)
})

test('矛盾的两个字段会一路传导成 HIDDEN', () => {
  // 端到端：连接口漂移都走同一个安全出口，不需要在组件里再补一层判断。
  const status = effectivePublishStatus({ status: 'failed' }, 'published')
  assert.equal(getPublishAction('approved', status), 'HIDDEN')
})

// ---------------------------------------------------------------------------
// 5. 界面行为（§十五）—— 以视图模型为准
// ---------------------------------------------------------------------------

/**
 * 为什么不直接渲染组件：
 *
 * `node --test` **无法转换 `.tsx`**（实测报 `ERR_UNKNOWN_FILE_EXTENSION: .tsx`），
 * 要渲染组件就必须引入 vitest / esbuild 这类新工具链，而这一轮不该顺手加依赖。
 *
 * 所以界面测试打在这里：`PublishActionSlot` **只**照着 `publishActionView` 返回的
 * 对象渲染，自己一个 if 都不写（没有按钮时它连 `<PublishControl>` 都不挂载）。
 * 断言这个对象，等价于断言屏幕上出现的东西，而且每个分支都可穷举。
 */

/** [runStatus, publishStatus, 该不该有发布按钮] */
const BUTTON_CASES: readonly [string | null | undefined, string | null | undefined, boolean][] = [
  ['approved', null, true], // ① 首次发布
  ['edited', null, true], // ② 首次发布
  ['approved', 'failed', true], // ③ 重发
  ['approved', 'publish_unknown', false], // ④ 去裁决
  ['approved', 'publishing', false], // ⑤ 发布中
  ['approved', 'published', false], // ⑥ 已发布
  ['waiting_for_review', 'failed', false], // ⑦ 没资格
  ['rejected', 'failed', false], // ⑧ 没资格
]

for (const [runStatus, publishStatus, expectButton] of BUTTON_CASES) {
  test(`界面：${String(runStatus)} + ${String(publishStatus)} ${
    expectButton ? '有' : '没有'
  }发布按钮`, () => {
    const view = publishActionView(runStatus, publishStatus)
    assert.equal(view.showPublish, expectButton)
    // "没有"也必须是**不渲染**，而不是渲染一个禁用按钮。
    assert.equal(view.showPublish, view.publishLabel !== null)
  })
}

test('界面：首次发布与重发用的是两句不同的文案', () => {
  assert.equal(publishActionView('approved', null).publishLabel, FIRST_PUBLISH_LABEL)
  assert.equal(publishActionView('edited', null).publishLabel, FIRST_PUBLISH_LABEL)
  assert.equal(publishActionView('approved', null).isRetry, false)

  assert.equal(publishActionView('approved', 'failed').publishLabel, REPUBLISH_LABEL)
  assert.equal(publishActionView('edited', 'failed').publishLabel, REPUBLISH_LABEL)
  assert.equal(publishActionView('approved', 'failed').isRetry, true)
})

test('界面：publish_unknown 给的是「去裁决」，而且它旁边那句话必须在', () => {
  const view = publishActionView('approved', 'publish_unknown')
  assert.equal(view.action, 'RESOLVE')
  assert.equal(view.showPublish, false)
  assert.equal(view.showResolve, true)
  assert.equal(view.resolveLabel, RESOLVE_LABEL)
  // 只给一个按钮是不够的：人得知道自己在被要求"先去 X 上看一眼"。
  assert.ok(view.note !== null)
  assert.match(view.note, /重复发布/)
  // 橙色块只属于 publish_unknown。
  assert.equal(view.noteTone, 'unknown')
})

test('界面：publishing 只解释，既没有发布按钮也没有裁决按钮', () => {
  const view = publishActionView('approved', 'publishing')
  assert.equal(view.action, 'HIDDEN')
  assert.equal(view.showPublish, false)
  assert.equal(view.showResolve, false)
  assert.ok(view.note !== null)
  // 不许借用橙色：借用会让它和 publish_unknown 长得一样。
  assert.equal(view.noteTone, 'neutral')
})

test('界面：已发布没有任何入口，也不必再解释一句', () => {
  const view = publishActionView('approved', 'published')
  assert.equal(view.showPublish, false)
  assert.equal(view.showResolve, false)
  // 上面那块发布记录（X Post ID + 发布时间）本身就是全部答案。
  assert.equal(view.note, null)
})

test('界面：failed 但没有资格时，要说明为什么没有重发按钮', () => {
  // 一块写着「发布失败 —— 这一条可以重试」的记录下面空空如也，会让人以为界面坏了。
  for (const runStatus of ['waiting_for_review', 'rejected', 'running', 'failed']) {
    const view = publishActionView(runStatus, 'failed')
    assert.equal(view.showPublish, false)
    assert.ok(view.note !== null, `${runStatus} + failed 应该有一句解释`)
    assert.match(view.note, /资格/)
  }
})

test('界面：读不到状态时直说是读不到，不是没有', () => {
  for (const publishStatus of [undefined, 'no_such_status']) {
    const view = publishActionView('approved', publishStatus)
    assert.equal(view.showPublish, false)
    assert.ok(view.note !== null)
    assert.match(view.note, /读不到/)
  }
})

test('界面：resolved 的地址与裁决台的筛选值是同一条', () => {
  // 两边各写一遍字面量迟早漂移，漂移的表现是"点了去裁决，落到一个没有筛选的
  // 列表页"——人以为自己看的是未决队列，其实是全部记录。
  assert.equal(UNRESOLVED_PUBLISHED_PATH, '/published?status=__unresolved__')
})

test('界面：任何动作都不会同时给出发布按钮和裁决按钮', () => {
  for (const runStatus of RUN_INPUTS) {
    for (const publishStatus of PUBLISH_INPUTS) {
      const view = publishActionView(runStatus, publishStatus)
      assert.ok(
        !(view.showPublish && view.showResolve),
        `${String(runStatus)} + ${String(publishStatus)} 同时出现了两个入口`,
      )
    }
  }
})
