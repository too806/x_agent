import { EmptyState } from '../components/States'

export function Review() {
  return (
    <EmptyState
      title="Review workspace is not wired up yet"
      hint="Phase 4b-3 会在这里接入待审队列与 Approve / Edit / Reject。"
    />
  )
}
