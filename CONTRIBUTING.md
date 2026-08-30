# 贡献指南

## 开始之前

请先阅读根目录 README 和 `SECURITY.md`。贡献者应使用自己的 API Key、自己的 Reddit/SensorTower 账号和合成测试数据，不要提交非公开配置、私有部署域名、真实用户内容或第三方凭据。

## 本地开发

```bash
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
cd frontend && npm ci
```

后端使用 `./scripts/start-local-dev.sh`，前端使用 `npm run dev`。默认服务只监听本机。

## 提交前检查

```bash
cd frontend
npm run check
```

Python 修改至少应通过 AST 解析；涉及 API、Session、外部 URL、Markdown 或凭据的改动需要补充回归测试。

演示数据必须通过 `python scripts/generate-demo-data.py` 生成。不要把真实社区原文、评论或用户链接替换进 `data/demo/`。

## 提交范围

- 不提交 `.env`、运行数据、日志、CLI 凭据或非公开文档。
- 新增外部数据源时，说明安装方式、许可证和再分发限制。
- 不在公共代码中加入默认共享 Key 或默认公网部署脚本。
- 提交信息和 Pull Request 应说明行为变化、测试结果及未验证事项。
