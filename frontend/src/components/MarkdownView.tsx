import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import styles from './MarkdownView.module.css';

/**
 * 自定义渲染器放在模块作用域：它不依赖组件 props，放外面可以避免
 * 每次渲染都创建新对象导致 react-markdown 白跑一遍。
 *
 * 注意：react-markdown 传给自定义组件的 props 里带一个 `node` 字段
 * （AST 节点），直接 {...props} 展开到 DOM 上会触发 React 警告，
 * 所以这里只显式取需要的属性。
 */
const COMPONENTS: Components = {
  // 外链一律新窗口打开，避免把工作台页面顶掉
  a({ href, children }) {
    return (
      <a href={href} target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  },
};

/**
 * 统一的 Markdown 渲染（文档 §6.2）。
 * remark-gfm 补上表格 / 删除线 / 任务列表 / 自动链接 —— 模型输出里很常见，
 * 不加的话表格会渲染成一堆竖线。
 */
export default function MarkdownView({ content }: { content: string }) {
  return (
    <div className={styles.markdown}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
