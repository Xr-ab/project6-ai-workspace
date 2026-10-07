/**
 * Settings（Phase 9a，docs/07 §5.10 按 9a 裁定收窄成「只读为主 + 两个写口」）。
 *
 * 三条边界（都是 spec §0 裁定③ 的落地，不许顺手扩）：
 * 1. 模型名与单价**只读**：载体是 `core/config.py`，API 与 worker 各自启动时读一次，
 *    前端写了不生效；单价一改还会和历史 `agent_runs.cost` 并存两套口径。
 * 2. **API Key 管理永久不做**：密钥从前端下发 = 进浏览器网络面板与日志。数据面
 *    也已经堵死——`/stats/usage` 的 `pricing` 块是后端手写白名单，密钥字段不在里面。
 * 3. 审计自查**按角色条件渲染**：member 只看到说明文案，一个请求都不发
 *    （`GET /auth/audit-log` 是 admin 闸，发了必吃 403，纯噪声）。
 */
import { useEffect, useState } from 'react';

import { changePassword, listAuditLogs, updateProfile } from '../../api/auth';
import type { AuditLogItem } from '../../api/auth';
import { getUsage } from '../../api/stats';
import type { StatsGroupBy, StatsRange, StatsUsage } from '../../api/stats';
import { toReadableError } from '../../api/client';
import { useAuth } from '../../auth/AuthContext';
import { formatCost } from '../../lib/format';
import { RANGE_TABS } from '../../lib/ranges';
import styles from './SettingsPage.module.css';

const GROUPS: { key: StatsGroupBy; label: string }[] = [
  { key: 'task_type', label: '按任务类型' },
  { key: 'model', label: '按模型' },
  { key: 'day', label: '按天' },
];

export default function SettingsPage() {
  const { user, applyUser } = useAuth();
  const isAdmin = user?.role === 'admin';

  // ---- profile ----
  const [fullName, setFullName] = useState(user?.full_name ?? '');
  const [savingProfile, setSavingProfile] = useState(false);
  const [profileNotice, setProfileNotice] = useState<string | null>(null);
  // R126（终评 F5①）：成功/失败分成两个 state、两个类——原来一个 profileNotice 同时装
  // 「已保存」和失败文案，只能共用中性 `.notice`，失败看起来像成功。照抄同文件口令段
  // （passwordNotice `.notice` / passwordError `.errorText`）已验证的形状。
  const [profileError, setProfileError] = useState<string | null>(null);

  // ---- password ----
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [savingPassword, setSavingPassword] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordNotice, setPasswordNotice] = useState<string | null>(null);

  // ---- usage（只读展示的唯一数据来源：/stats/usage 一份响应里带着 pricing 摘要）----
  const [range, setRange] = useState<StatsRange>('week');
  const [groupBy, setGroupBy] = useState<StatsGroupBy>('task_type');
  const [usage, setUsage] = useState<StatsUsage | null>(null);
  const [usageLoading, setUsageLoading] = useState(true);
  const [usageError, setUsageError] = useState<string | null>(null);
  /** 刷新序号（R72）：刷新按钮只把它加 1，取数与写 state 全归下面这一个 effect。
   *  这样「响应回来还该不该写」只有一处守卫（effect 自己的 cancelled），不必再给点面
   *  补一份 ref 身份守卫。Task 6 需要 ref 是因为 `retry`/`create` 在 effect 之外还各自
   *  写 state（R69）；这里点面不写 state，所以不需要同样的守卫。
   *  不这么写的后果：原本 `loadUsage` 在函数里直接 `setUsage(await getUsage(...))`，
   *  它同时是 effect 体和刷新按钮的 onClick——切了 range 后旧周期的响应晚到，
   *  会把新 range 的表覆盖成旧 range 的数据，而标题 chip 停在新 range（永久错标，
   *  不是闪一下：这张表没有轮询，没有下一拍来自愈）。 */
  const [usageTick, setUsageTick] = useState(0);

  // ---- audit（admin only）----
  const [audits, setAudits] = useState<AuditLogItem[]>([]);
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditError, setAuditError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setUsageLoading(true);
    setUsageError(null);
    void (async () => {
      try {
        const data = await getUsage(range, groupBy);
        if (cancelled) return;
        setUsage(data);
      } catch (err) {
        if (!cancelled) setUsageError(toReadableError(err));
      } finally {
        if (!cancelled) setUsageLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [range, groupBy, usageTick]);

  // 非 admin 连请求都不发（针⑦ 的判据就是网络面板里没有这条 403）
  useEffect(() => {
    if (!isAdmin) return;
    let cancelled = false;
    void (async () => {
      try {
        const page = await listAuditLogs(1, 20);
        if (cancelled) return;
        setAudits(page.items);
        setAuditTotal(page.total);
      } catch (err) {
        if (!cancelled) setAuditError(toReadableError(err));
      }
    })();
    return () => { cancelled = true; };
  }, [isAdmin]);

  const saveProfile = async () => {
    setSavingProfile(true);
    setProfileNotice(null);
    setProfileError(null);
    try {
      const next = await updateProfile(fullName.trim());
      applyUser(next); // 顶栏/其它页下次读 user 就是新值（R11）
      setProfileNotice('已保存');
    } catch (err) {
      setProfileError(toReadableError(err));
    } finally {
      setSavingProfile(false);
    }
  };

  const savePassword = async () => {
    setSavingPassword(true);
    setPasswordError(null);
    setPasswordNotice(null);
    try {
      await changePassword(oldPassword, newPassword);
      setOldPassword('');
      setNewPassword('');
      // 旧会话不强杀是契约（spec §3.3）：文案如实说清，别让人以为改了口令就万事大吉
      setPasswordNotice('口令已更新。已登录的其它会话最长会保留到其自然过期。');
    } catch (err) {
      // 旧口令错 → 403 AUTH_403003；新口令太短 → 422；打满闸 → 429（client 已带 Retry-After 文案）
      setPasswordError(toReadableError(err));
    } finally {
      setSavingPassword(false);
    }
  };

  if (!user) return null; // RequireAuth 已保证登录态；这里只给 TS 收窄非空

  // R126（终评 F1）：载荷自证门——`/stats/usage` 响应里的 range/group_by 与当前选择一致才渲表。
  // 切档后旧周期的响应晚到时，屏幕上最多是「正在切换到…」的提示，绝不会把旧窗口的数挂在新档标题下
  // （这张表没有轮询，没有下一拍自愈——见 usageTick 注释里那条永久错标）。
  // 刻意不用 `setUsage(null)` 实现：那会让「同档刷新失败」把已有数字也清掉，
  // 与本页既有的「保留旧数 + 出横幅」姿态相反。不匹配时旧载荷留着但不出屏，等新响应到再换。
  const usageFresh = usage !== null && usage.range === range && usage.group_by === groupBy;
  const rangeLabel = RANGE_TABS.find((item) => item.key === range)?.label ?? range;
  const groupLabel = GROUPS.find((item) => item.key === groupBy)?.label ?? groupBy;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>Settings</h1>
      </header>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>个人资料</h2>
        <label className={styles.field}>
          <span className={styles.label}>姓名</span>
          <input
            className={styles.input}
            value={fullName}
            maxLength={100}
            onChange={(e) => setFullName(e.target.value)}
          />
        </label>
        <div className={styles.row}>
          <button
            type="button"
            className={styles.primaryButton}
            onClick={() => void saveProfile()}
            disabled={savingProfile || fullName.trim().length === 0}
          >
            {savingProfile ? '保存中…' : '保存'}
          </button>
          {profileNotice && <p className={styles.notice}>{profileNotice}</p>}
        </div>
        {profileError && <p className={styles.errorText}>{profileError}</p>}
        <dl className={styles.readonlyList}>
          <dt>邮箱</dt><dd>{user.email}</dd>
          <dt>角色</dt><dd>{user.role === 'admin' ? '组织管理员' : '成员'}</dd>
          <dt>组织</dt><dd>{user.organization_name ?? '—'}</dd>
        </dl>
        <p className={styles.hint}>邮箱与角色不可在本页修改。</p>
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>修改口令</h2>
        <label className={styles.field}>
          <span className={styles.label}>当前口令</span>
          <input
            className={styles.input}
            type="password"
            value={oldPassword}
            autoComplete="current-password"
            onChange={(e) => setOldPassword(e.target.value)}
          />
        </label>
        <label className={styles.field}>
          <span className={styles.label}>新口令（8~72 位）</span>
          <input
            className={styles.input}
            type="password"
            value={newPassword}
            autoComplete="new-password"
            onChange={(e) => setNewPassword(e.target.value)}
          />
        </label>
        <div className={styles.row}>
          <button
            type="button"
            className={styles.primaryButton}
            onClick={() => void savePassword()}
            disabled={savingPassword || oldPassword.length === 0 || newPassword.length < 8}
          >
            {savingPassword ? '提交中…' : '更新口令'}
          </button>
          {passwordNotice && <p className={styles.notice}>{passwordNotice}</p>}
        </div>
        {passwordError && <p className={styles.errorText}>{passwordError}</p>}
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>模型与单价（只读）</h2>
        {usageLoading && <p className={styles.hint}>加载中…</p>}
        {usageError && <p className={styles.errorText}>{usageError}</p>}
        {usage && (
          <dl className={styles.readonlyList}>
            <dt>对话模型</dt><dd>{usage.pricing.llm_model}</dd>
            <dt>向量模型</dt><dd>{usage.pricing.embedding_model}（{usage.pricing.embedding_dim} 维）</dd>
            <dt>币种</dt><dd>{usage.pricing.currency}</dd>
            <dt>输入单价</dt>
            <dd>
              {usage.pricing.input_price_per_1k === null
                ? '未配置'
                : formatCost(usage.pricing.input_price_per_1k, usage.pricing.currency)} / 1k
            </dd>
            <dt>输出单价</dt>
            <dd>
              {usage.pricing.output_price_per_1k === null
                ? '未配置'
                : formatCost(usage.pricing.output_price_per_1k, usage.pricing.currency)} / 1k
            </dd>
          </dl>
        )}
        <p className={styles.hint}>
          改这些要动配置并重启 API 与 worker，且历史成本会与新单价并存两套口径 —— 本产品刻意不提供前端写口。
        </p>
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>Token / Cost 用量</h2>
        <div className={styles.switchBar}>
          {RANGE_TABS.map((item) => (
            <button
              key={item.key}
              type="button"
              className={item.key === range ? `${styles.chip} ${styles.chipActive}` : styles.chip}
              onClick={() => setRange(item.key)}
            >
              {item.label}
            </button>
          ))}
          <span className={styles.divider} />
          {GROUPS.map((item) => (
            <button
              key={item.key}
              type="button"
              className={item.key === groupBy ? `${styles.chip} ${styles.chipActive}` : styles.chip}
              onClick={() => setGroupBy(item.key)}
            >
              {item.label}
            </button>
          ))}
          <button type="button" className={styles.chip} onClick={() => setUsageTick((t) => t + 1)}>刷新</button>
        </div>
        {usage && !usageFresh && (
          <p className={styles.hint}>正在切换到 {rangeLabel} · {groupLabel}…</p>
        )}
        {usageFresh && (
          <table className={styles.table}>
            <thead>
              <tr><th>分组</th><th>任务数</th><th>Token</th><th>成本</th></tr>
            </thead>
            <tbody>
              {usage.groups.map((group) => (
                <tr key={group.key}>
                  <td>{group.key}</td>
                  <td>{group.tasks}</td>
                  <td>{group.total_tokens.toLocaleString()}</td>
                  <td>{formatCost(group.total_cost, usage.pricing.currency)}</td>
                </tr>
              ))}
              {usage.groups.length === 0 && (
                <tr><td colSpan={4}>这区间没有用量。</td></tr>
              )}
              <tr className={styles.totalRow}>
                <td>合计</td>
                <td>{usage.total.tasks}</td>
                <td>{usage.total.total_tokens.toLocaleString()}</td>
                <td>{formatCost(usage.total.total_cost, usage.pricing.currency)}</td>
              </tr>
            </tbody>
          </table>
        )}
        {/* scope 说明与表同进退：表没过门时单独挂一句 scope 也是错标源 */}
        {usageFresh && (
          <p className={styles.hint}>
            {usage.scope === 'org' ? '统计范围：本组织' : '统计范围：仅本人'}；
            合计行按组求和，跨组出现的同一记录会在多组各计一次（口径见 06 §2.8）。
          </p>
        )}
      </section>

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>操作审计</h2>
        {isAdmin ? (
          <>
            {auditError && <p className={styles.errorText}>{auditError}</p>}
            <table className={styles.table}>
              <thead>
                <tr><th>时间</th><th>动作</th><th>对象</th></tr>
              </thead>
              <tbody>
                {audits.map((row) => (
                  <tr key={row.id}>
                    <td>{new Date(row.created_at).toLocaleString()}</td>
                    <td>{row.action}</td>
                    <td>{row.target_type ? `${row.target_type} ${row.target_id ?? ''}`.trim() : '—'}</td>
                  </tr>
                ))}
                {audits.length === 0 && <tr><td colSpan={3}>还没有审计记录。</td></tr>}
              </tbody>
            </table>
            <p className={styles.hint}>显示最近 20 条（共 {auditTotal} 条）。分页控件在 9b。</p>
          </>
        ) : (
          <p className={styles.hint}>操作审计仅组织管理员可见。</p>
        )}
      </section>
    </div>
  );
}
