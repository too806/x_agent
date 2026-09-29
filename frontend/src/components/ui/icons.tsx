/**
 * 图标。
 *
 * 手写的内联 SVG，**不装图标库**。理由不是省那几十 KB，而是这十几个图标全都可以
 * 用同一种线条语言描述（24 网格、2px 描边、圆头圆角），装一个几百个图标的包进来，
 * 反而要额外花力气约束自己只用其中一致的那一打。
 *
 * 约定：
 *   - 一律 `stroke="currentColor"`，颜色由**父元素的文字颜色**决定，
 *     这样"侧栏未选中是灰的、选中是白的"不用写第二套图标。
 *   - 尺寸默认 16（正文尺度）。侧栏用 16，按钮里用 14。
 *   - 图标只是**辅助**：凡是只靠图标表意的地方（纯图标按钮），必须配 aria-label；
 *     图标本身一律 aria-hidden，免得屏幕阅读器把路径读成乱码。
 */

interface IconProps {
  /** 边长，像素。 */
  size?: number
  className?: string
}

function Svg({
  size = 16,
  className,
  children,
}: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  )
}

/** 系统总览：四个格子。 */
export function IconOverview(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3" y="3" width="7" height="7" rx="1" />
      <rect x="14" y="3" width="7" height="7" rx="1" />
      <rect x="3" y="14" width="7" height="7" rx="1" />
      <rect x="14" y="14" width="7" height="7" rx="1" />
    </Svg>
  )
}

/** 运行记录：带折线的活动列表。 */
export function IconRuns(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M3 12h4l3-8 4 16 3-8h4" />
    </Svg>
  )
}

/** 内容审核：写字板 + 勾。 */
export function IconReview(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9 3h6v3H9z" />
      <path d="M15 4.5h2a2 2 0 0 1 2 2V19a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6.5a2 2 0 0 1 2-2h2" />
      <path d="m9 13 2 2 4-4" />
    </Svg>
  )
}

/** 发布记录：纸飞机。 */
export function IconPublished(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M21 3 3 10.5l7 2.5 2.5 7L21 3z" />
      <path d="M10 13l4-4" />
    </Svg>
  )
}

/** 数据分析：柱状图。 */
export function IconAnalytics(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M4 20V10" />
      <path d="M10 20V4" />
      <path d="M16 20v-7" />
      <path d="M22 20H2" />
    </Svg>
  )
}

export function IconRefresh(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M20 11a8 8 0 1 0-2.3 5.7" />
      <path d="M20 5v6h-6" />
    </Svg>
  )
}

export function IconClose(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M18 6 6 18" />
      <path d="m6 6 12 12" />
    </Svg>
  )
}

export function IconCopy(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="9" y="9" width="12" height="12" rx="2" />
      <path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1" />
    </Svg>
  )
}

export function IconCheck(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m20 6-11 11-5-5" />
    </Svg>
  )
}

export function IconExternal(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M14 4h6v6" />
      <path d="M20 4 10 14" />
      <path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5" />
    </Svg>
  )
}

/** 需要警惕但不致命（未定结果）。 */
export function IconAlert(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
      <path d="M12 9v4" />
      <path d="M12 17h.01" />
    </Svg>
  )
}

/** 明确的失败。 */
export function IconXCircle(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="m15 9-6 6" />
      <path d="m9 9 6 6" />
    </Svg>
  )
}

export function IconInfo(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 16v-4" />
      <path d="M12 8h.01" />
    </Svg>
  )
}

export function IconChevron(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="m6 9 6 6 6-6" />
    </Svg>
  )
}
