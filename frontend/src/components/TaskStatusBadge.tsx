import type { TaskStatus } from '../api/agent';
import styles from './TaskStatusBadge.module.css';

/** 七值文案表（docs/07 §5.5）。Record<TaskStatus, string> 是刻意的：
 *  后端加第八态时这里编译不过，逼着改的人先对齐渲染，而不是运行时渲成 undefined。
 *  导出给 AgentTasksPage 的状态 Tab 用（同一份中文表两处共用，不抄第二遍）。 */
export const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  pending: '待执行',
  queued: '已排队',
  running: '执行中',
  waiting_approval: '待审批',
  completed: '已完成',
  failed: '失败',
  rejected: '已驳回',
};

/** 颜色分组：queued/pending 中性（都"还没开跑"，不许借进行色说话，8b T11 口径）；
 *  running/waiting_approval 进行色；completed 成功色；failed/rejected 失败色。 */
const KIND: Record<TaskStatus, 'idle' | 'active' | 'done' | 'bad'> = {
  pending: 'idle',
  queued: 'idle',
  running: 'active',
  waiting_approval: 'active',
  completed: 'done',
  failed: 'bad',
  rejected: 'bad',
};

export default function TaskStatusBadge({ status }: { status: TaskStatus }) {
  return <span className={`${styles.badge} ${styles[KIND[status]]}`}>{TASK_STATUS_LABELS[status]}</span>;
}
