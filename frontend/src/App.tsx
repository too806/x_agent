import { Navigate, Route, Routes } from 'react-router-dom'

import { ErrorBoundary } from './components/ErrorBoundary'
import { ToastProvider } from './components/ui/Toast'
import { ConsoleProvider } from './hooks/useConsole'
import { AppLayout } from './layouts/AppLayout'
import { Analytics } from './pages/Analytics'
import { Overview } from './pages/Overview'
import { Published } from './pages/Published'
import { Review } from './pages/Review'
import { Runs } from './pages/Runs'

/**
 * 路由表。
 *
 * 五个页面就是全部导航，没有嵌套详情页：Run 的详情是**右侧抽屉**而不是新路由。
 * 这样在列表里翻找时不会丢掉筛选条件与浏览位置——运维查 Run 时经常要来回对照，
 * 每看一条就跳走一次会让这件事变得很难受。
 *
 * 三层包裹，从外到内各管一件事：
 *   ErrorBoundary —— 渲染异常时给一块可读的兜底，而不是白屏；
 *   ToastProvider —— 全局唯一的操作结果提示（位置、停留时间、播报方式统一）；
 *   ConsoleProvider —— 全局唯一的待办计数与总览数据。
 * 它们都是"整个界面只有一份"的东西，所以放在路由之外，切页面不重建。
 */
export function App() {
  return (
    <ErrorBoundary>
      <ToastProvider>
        <ConsoleProvider>
          <Routes>
            <Route element={<AppLayout />}>
              <Route index element={<Overview />} />
              <Route path="runs" element={<Runs />} />
              <Route path="review" element={<Review />} />
              <Route path="published" element={<Published />} />
              <Route path="analytics" element={<Analytics />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </ConsoleProvider>
      </ToastProvider>
    </ErrorBoundary>
  )
}
