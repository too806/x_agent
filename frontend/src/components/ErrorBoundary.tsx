import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/**
 * 渲染异常兜底。
 *
 * 之前任何一个组件抛错（后端返回了一个没预料到的形状、某个字段意外是
 * undefined），React 会卸载整棵树——用户看到的是一片白屏，没有任何提示，
 * 也没有可以点的东西。这是"演示版"和"真在用的控制台"之间最刺眼的一个差别：
 * 真在用的东西出错时会说"出错了，试试重试"，而不是消失。
 *
 * 刻意**不显示 error.stack**：它属于服务端和开发者的信息，对使用者没有帮助，
 * 还可能泄露构建路径。stack 打印到 console 给开发者看。
 *
 * 这里只兜渲染期异常，不兜事件处理和异步请求——那些各有各的错误路径
 * （`useAsync` / `useMutation` 的 `error`），并且能给出更具体的说法。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error('界面渲染出错：', error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="state state--error" role="alert">
        <div className="state__title">这个页面出错了</div>
        <div className="state__hint">
          <div>界面在渲染时遇到了未预期的错误，当前页面无法继续显示。</div>
          <div>后端接口本身可能仍然正常——可以重试，或者换到别的页面继续操作。</div>
          {/* 只给一行原始消息，便于对着控制台日志找到同一条。堆栈留在 console 里。 */}
          <div className="mono">错误信息：{error.message}</div>
        </div>
        <div className="state__actions">
          <button
            type="button"
            className="btn"
            onClick={() => this.setState({ error: null })}
          >
            重试
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => window.location.reload()}
          >
            重新加载页面
          </button>
        </div>
      </div>
    )
  }
}
