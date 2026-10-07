/// <reference types="vite/client" />

// 声明自定义环境变量，让 import.meta.env.VITE_API_BASE 有具体类型，
// 而不是 Vite 默认的 any —— 拼错变量名时能在编译期发现
interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
