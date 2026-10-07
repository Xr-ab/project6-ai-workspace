/**
 * 登录页（Phase 8a T7，07 §65：全屏无框架 —— 不进 AppLayout，独立路由）。
 *
 * 双 Tab「登录 / 注册」共用一份 email+password：注册多收组织名与姓名
 * （后端 register 建组织并把首个用户定为 admin，见 auth_service）。
 * 成功后 AuthContext 已写好 token 与 user，这里只 navigate('/dashboard')；
 * RequireAuth 弹过来的场景也不特地把 from 塞回去 —— 登录落地统一进 Dashboard，
 * 行为可预期（07 §65 口径）。
 */
import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';

import { toReadableError } from '../../api/client';
import { useAuth } from '../../auth/AuthContext';
import styles from './LoginPage.module.css';

type Tab = 'login' | 'register';

export default function LoginPage() {
  const { login, register } = useAuth();
  const navigate = useNavigate();

  const [tab, setTab] = useState<Tab>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  // 注册专属字段（切回登录不清空，方便来回改）
  const [fullName, setFullName] = useState('');
  const [orgName, setOrgName] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isRegister = tab === 'register';
  // 前端只做"必填 + 密码下限"的最小闸，其余交给后端 422/业务码统一回话：
  // 校验规则真相在后端（register password min_length=8），这里复刻一份只会漂
  const passwordMinOk = password.length >= 8;
  const ready =
    !submitting &&
    email.trim() !== '' &&
    password !== '' &&
    (!isRegister || (passwordMinOk && fullName.trim() !== '' && orgName.trim() !== ''));

  const switchTab = (next: Tab) => {
    setTab(next);
    setError(null);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!ready) return;
    setSubmitting(true);
    setError(null);
    try {
      if (isRegister) {
        await register({
          email: email.trim(),
          password,
          full_name: fullName.trim(),
          organization_name: orgName.trim(),
        });
      } else {
        await login(email.trim(), password);
      }
      navigate('/dashboard', { replace: true });
    } catch (err) {
      setError(toReadableError(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className={styles.page}>
      <form className={styles.card} onSubmit={(e) => void submit(e)}>
        <h1 className={styles.brand}>企业 AI 智能工作台</h1>
        <p className={styles.subtitle}>登录后开始使用 Chat、知识库与 Workflow</p>

        <div className={styles.tabs} role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={!isRegister}
            className={`${styles.tab} ${!isRegister ? styles.tabActive : ''}`}
            onClick={() => switchTab('login')}
          >
            登录
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={isRegister}
            className={`${styles.tab} ${isRegister ? styles.tabActive : ''}`}
            onClick={() => switchTab('register')}
          >
            注册
          </button>
        </div>

        {error && <div className={styles.errorBanner}>{error}</div>}

        <label className={styles.field}>
          <span className={styles.label}>邮箱</span>
          <input
            className={styles.input}
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
          />
        </label>

        <label className={styles.field}>
          <span className={styles.label}>密码</span>
          <input
            className={styles.input}
            type="password"
            autoComplete={isRegister ? 'new-password' : 'current-password'}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={isRegister ? '至少 8 位' : '请输入密码'}
          />
        </label>

        {isRegister && (
          <>
            <label className={styles.field}>
              <span className={styles.label}>姓名</span>
              <input
                className={styles.input}
                type="text"
                autoComplete="name"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                placeholder="你的名字"
              />
            </label>
            <label className={styles.field}>
              <span className={styles.label}>组织名</span>
              <input
                className={styles.input}
                type="text"
                autoComplete="organization"
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
                placeholder="新建一个组织，你将作为其管理员"
              />
            </label>
          </>
        )}

        <button type="submit" className={styles.submit} disabled={!ready}>
          {submitting ? '处理中…' : isRegister ? '注册并进入' : '登录'}
        </button>
      </form>
    </div>
  );
}
