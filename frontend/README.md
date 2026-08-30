# Lumon 前端

这是 Lumon 的 React + TypeScript + Vite 前端。完整的安装、配置和本地运行说明请看仓库根目录的 [README.md](../README.md)。

常用命令：

```bash
npm ci
npm run dev
npm run lint
npx tsc -b --noEmit
```

开发服务器默认监听 `127.0.0.1:5173`，并将 `/api` 请求代理到本地后端 `127.0.0.1:8001`。如需调整地址，请通过 `VITE_DEV_API_PORT` 或 `VITE_DEV_HOST` 显式配置。
