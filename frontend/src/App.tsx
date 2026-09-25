import { Navigate, Route, Routes } from 'react-router-dom'

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
 */
export function App() {
  return (
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
  )
}
