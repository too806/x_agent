import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { App } from './App'
import './styles/tokens.css'
import './styles/app.css'

const container = document.getElementById('root')
if (!container) {
  // 没有挂载点就没什么可做的了。抛出而不是静默失败——空白页面最难排查。
  throw new Error('Root container #root is missing from index.html')
}

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
