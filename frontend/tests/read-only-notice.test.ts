import assert from 'node:assert/strict'
import { test } from 'node:test'

import { readOnlyNotice } from '../src/utils/format.ts'

/**
 * 只读详情面板那句说明的测试。
 *
 * 这里守的是一句话：**文案跟着状态走，不跟着设计走**。原来的实现是一句写死的
 * "这个 Run 已经决定了" —— 在"右栏只渲染刚决定完的那条"的设计下它是对的，
 * 而现在 /review 可以深链到任何一条 Run（运行记录抽屉的「去审核 →」），那句话
 * 就会对着一条运行中、甚至失败的 Run 说谎。
 *
 * 所以下面的用例不是"随机挑几个状态看看输出"，而是把**每一条能出现的入口**
 * 都钉住：运行中、失败、跑完但没停到审核、以及已决定的那一族。
 *
 *   npm run test:frontend
 */

test('已决定的那一族说"已经决定了"', () => {
  // 审核决定本身（无人决定时 review_status 才是 null，那时 run_status 是 waiting_for_review）。
  for (const status of ['approved', 'edited', 'rejected']) {
    assert.match(readOnlyNotice(status), /已经决定了/)
  }
})

test('发布中的三个状态也算已决定（发布的前置就是已批准 / 已编辑）', () => {
  // 依据是 utils/publishAction.ts 的 PUBLISH_ELIGIBLE_RUN_STATUSES，不是猜的：
  // 进不到发布阶段就意味着没人批准过。
  for (const status of ['publishing', 'publish_unknown', 'published']) {
    assert.match(readOnlyNotice(status), /已经决定了/)
  }
})

test('运行中的 Run 不能说"已经决定了"', () => {
  // 这是这条修改要修的那个谎：深链可以落到一条还在跑的 Run 上。
  const notice = readOnlyNotice('running')
  assert.doesNotMatch(notice, /已经决定/)
  assert.match(notice, /还在运行中/)
})

test('失败的 Run 不能说"已经决定了"，也不该提待审队列', () => {
  const notice = readOnlyNotice('failed')
  assert.doesNotMatch(notice, /已经决定/)
  assert.doesNotMatch(notice, /待审队列/)
  assert.match(notice, /运行失败/)
})

test('completed 说"跑完了，但没停到人工审核"', () => {
  // completed 的确切含义：app/main.py 只在终态里没有 __interrupt__ 时才标它。
  // 所以"没有等待决定的草稿"是能确定的，不是推测。
  const notice = readOnlyNotice('completed')
  assert.match(notice, /没有停到人工审核/)
  assert.doesNotMatch(notice, /已经决定/)
})

test('文案带上当前状态的中文标签', () => {
  // 状态是这一句里最有用的一半信息：同一句"已经决定了"配上"已批准"和"已发布"
  // 含义不同，配错就是把两种状态糊成一种。
  assert.match(readOnlyNotice('approved'), /当前状态：已批准/)
  assert.match(readOnlyNotice('published'), /当前状态：已发布/)
})

test('审核中的 Run 不会被说成已经决定了（防御分支）', () => {
  // 正常路径走不到（等待审核的那一支渲染的是决定界面）。留着是为了状态在两个
  // 请求之间被别处改掉时，文案不会开始胡说。
  const notice = readOnlyNotice('waiting_for_review')
  assert.doesNotMatch(notice, /已经决定/)
  assert.match(notice, /等待人工审核/)
})

test('认不出的状态不编造，原样带出真实值', () => {
  // 与 runStatusLabel 同一条立场：没翻译的英文枚举是看得见的缺口，
  // 一句"未知"会把缺口藏起来。
  const notice = readOnlyNotice('some_new_status')
  assert.match(notice, /SOME_NEW_STATUS/)
})

test('status 为 null 时不崩，也不假称已决定', () => {
  const notice = readOnlyNotice(null)
  assert.doesNotMatch(notice, /已经决定/)
  assert.match(notice, /不在等待审核/)
})
