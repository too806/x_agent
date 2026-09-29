import type { ReactNode } from 'react'

/**
 * 页面 / 详情里反复出现的三种排布。
 *
 * 抽出来是因为它们此前被抄了两遍以上（`RunDetailDrawer` 有一套 `Section` + `Field`，
 * `ReviewPane` 里手写同样的 `section__head` 结构，两边字段网格各写一遍）。
 * 抄出来的问题不是多几行，是**改一处不会同步**：4b.1 调整过元信息标签的字号，
 * 抽屉里改了，审核面板里漏了——那种不一致单看一屏是发现不了的。
 */

/**
 * 一个带标题的区块。
 *
 * `lead` 是页面第一层区块（系统健康、核心指标）：它更大、下面带一条分界线。
 * 全页面标题同重是"模板感"最主要的来源之一——读者看不出内容被分成了几段。
 */
export function Section({
  title,
  hint,
  actions,
  lead = false,
  children,
}: {
  title: string
  /** 标题右侧的口径 / 来源说明。 */
  hint?: ReactNode
  /** 标题右侧的操作（按钮、链接）。与 hint 二选一。 */
  actions?: ReactNode
  lead?: boolean
  children: ReactNode
}) {
  return (
    <section className={lead ? 'section section--lead' : 'section'}>
      <div className="section__head">
        <h2 className="section__title">{title}</h2>
        {actions ?? (hint ? <span className="section__hint">{hint}</span> : null)}
      </div>
      {children}
    </section>
  )
}

/** 抽屉 / 面板内部的区块：比 `Section` 小一号，上面用一条细线分隔。 */
export function DetailSection({
  title,
  hint,
  children,
}: {
  title: string
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="detail-section">
      <div className="section__head">
        <h3 className="section__title">{title}</h3>
        {hint ? <span className="section__hint">{hint}</span> : null}
      </div>
      {children}
    </section>
  )
}

/** 一个"标签 + 值"。空值不要传 `null` 进来——由调用方决定显示什么。 */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="meta">
      <div className="meta__label">{label}</div>
      <div className="meta__value">{children}</div>
    </div>
  )
}

/** 等宽的 ID 值：Run ID / X Post ID。 */
export function MonoField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="meta">
      <div className="meta__label">{label}</div>
      <div className="meta__value meta__value--mono">{children}</div>
    </div>
  )
}

/** 自动铺开的字段网格。列宽下限 140px，够放 17 位数字加标签。 */
export function FieldGrid({ children }: { children: ReactNode }) {
  return <div className="drawer__metagrid">{children}</div>
}
