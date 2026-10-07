# Phase 11a 前端：两段构建。API 基址是**构建期**烘焙的（client.ts:10 读 import.meta.env），
# 运行时注环境变量无效 ⇒ R-I 必须在 build 前给它。
FROM node:22-slim AS build
WORKDIR /web
# lock 先行 + 依赖单独一层：改一行源码不该重装 node_modules
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
ARG VITE_API_BASE=/api/v1
ENV VITE_API_BASE=${VITE_API_BASE}
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /web/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
