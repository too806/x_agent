/**
 * 时间范围的预设与解析。
 *
 * 运行记录页原来的写法是**永远摊开两个日期输入框**。它没错，但把"昨天到今天"
 * 这种最常见的看数动作变成了两次点开日历、四次点击。这里把它换成一个下拉：
 * 平时只需要选「最近 7 天」，真要卡某一段再切「自定义区间」。
 *
 * 两条刻意保留的规则：
 *
 * 1. **预设只算左端**。右端留空，交给后端按 `date_to` 缺省处理，不在这里假造一个
 *    "今天 23:59:59.999"。多算出来的那点尾巴在跨时区时正好会把边界上的记录吞掉，
 *    而这种少一条的错在审计界面上是不可接受的。
 * 2. **一律走本地日历日**。不用 `toISOString()`——它转成 UTC，UTC+8 的凌晨会被
 *    拨回前一天，"最近 7 天"就变成了 8 天。
 *
 * 全是纯函数：`node --test` 直接跑，不需要 DOM（见 `tests/date-range.test.ts`）。
 */

export interface TimeRangeOption {
  value: string
  label: string
  /** 预设覆盖多少个**自然日（含今天）**。`null` = 不设左端。 */
  days: number | null
}

export const TIME_RANGES: readonly TimeRangeOption[] = [
  { value: '', label: '全部时间', days: null },
  { value: '7', label: '最近 7 天', days: 7 },
  { value: '30', label: '最近 30 天', days: 30 },
  { value: '90', label: '最近 90 天', days: 90 },
  { value: 'custom', label: '自定义区间', days: null },
]

export type TimeRangeValue = (typeof TIME_RANGES)[number]['value']

/** 进「自定义区间」时若没有起点，给一个看得见的默认值——30 天前。
 *  不这么做的话两个空日期框看起来和「全部时间」一模一样，人会以为自己选错了。 */
export const CUSTOM_RANGE_SEED_DAYS = 30

/** `Date` → `<input type="date">` 认的本地日历日 `YYYY-MM-DD`。 */
export function localDateString(date: Date): string {
  const year = String(date.getFullYear()).padStart(4, '0')
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/**
 * N 天前的本地日历日。
 *
 * 用 `new Date(y, m, d - n)` 而不是 `date.setDate()`：前者在月末、年末、闰年上
 * 由 Date 构造器自己进位，不需要我们再判一次。也不会碰到夏令时——这里只有
 * 年月日，没有时分秒。
 */
export function daysAgoDateString(days: number, now: Date = new Date()): string {
  return localDateString(new Date(now.getFullYear(), now.getMonth(), now.getDate() - days))
}

/**
 * 预设 → 左端日历日。
 *
 * 「最近 7 天」= **含今天在内共 7 个自然日**，所以起点是 6 天前，不是 7 天前。
 * 差这一天的差别在界面上看不出来，在对账时看得出来。
 *
 * `''`（全部时间）与 `'custom'` 都返回空串：一个是不设左端，另一个的左端由
 * 使用者自己填。
 */
export function rangeStartDate(value: TimeRangeValue, now: Date = new Date()): string {
  const preset = TIME_RANGES.find((range) => range.value === value)
  if (!preset || preset.days === null) return ''
  return daysAgoDateString(preset.days - 1, now)
}
