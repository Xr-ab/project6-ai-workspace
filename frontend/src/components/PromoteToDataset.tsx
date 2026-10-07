/**
 * 「加入评测集」一键回放沉淀（D9 / docs/08 §3.3 主渠道）。
 *
 * 触发面是**生产 run 详情的终态**（Agent 任务详情 / Workflow 执行详情共用本组件）；
 * 目标数据集由用户在下拉里选（服务端要求 dataset_id，且数据集是 org + user 双过滤的，
 * 别人的集根本选不到 —— 与裁定 D13 同一套归属闸）。
 *
 * 反推规则全在服务端（input / category / references），前端一个字段都不自报；
 * 同一条 run 重复加入是幂等的（服务端返回已存在的那条，本组件显示同一个 code）。
 * 成功后不折叠成"搞定了"就完事：明写 expected 待补 —— §3.3 自己就写了
 * "后续人工补 expected"，这里是"加入"不是"标好了"。
 */
import { useState } from 'react';

import { listDatasets, promoteCaseFromTaskRun } from '../api/evaluation';
import type { Dataset, EvalCase } from '../api/evaluation';
import { toReadableError } from '../api/client';
import styles from './PromoteToDataset.module.css';

interface Props {
  /** 要沉淀的 task_run id（生产 run；服务端再验 run_type / 终态 / 类别映射） */
  runId: string;
}

export default function PromoteToDataset({ runId }: Props) {
  const [open, setOpen] = useState(false);
  const [datasets, setDatasets] = useState<Dataset[] | null>(null);
  const [datasetId, setDatasetId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<EvalCase | null>(null);

  /** 首次展开才拉数据集（不在任务详情的加载路径上多加一次请求） */
  const openPanel = async () => {
    setOpen(true);
    setError(null);
    if (datasets !== null) return;
    try {
      const rows = await listDatasets();
      setDatasets(rows);
      setDatasetId((current) => current || rows[0]?.id || '');
    } catch (err) {
      setError(toReadableError(err));
    }
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await promoteCaseFromTaskRun(datasetId, runId));
    } catch (err) {
      setError(toReadableError(err));
    } finally {
      setBusy(false);
    }
  };

  if (result) {
    return (
      <p className={styles.done}>
        已加入评测集：{result.code}（expected 待人工补，去评测页用例明细里写要点）
      </p>
    );
  }

  if (!open) {
    return (
      <button type="button" className={styles.trigger} onClick={() => void openPanel()}>
        加入评测集
      </button>
    );
  }

  return (
    <span className={styles.panel}>
      {datasets !== null && datasets.length === 0 ? (
        <span className={styles.hint}>还没有数据集，先去评测页创建一个。</span>
      ) : (
        <>
          <select
            className={styles.select}
            value={datasetId}
            disabled={busy || datasets === null}
            onChange={(e) => setDatasetId(e.target.value)}
          >
            {(datasets ?? []).map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
          <button
            type="button"
            className={styles.confirm}
            disabled={busy || !datasetId}
            onClick={() => void submit()}
          >
            加入
          </button>
        </>
      )}
      <button type="button" className={styles.cancel} disabled={busy} onClick={() => setOpen(false)}>
        收起
      </button>
      {error && <span className={styles.error}>{error}</span>}
    </span>
  );
}