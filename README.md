# T Maker

本项目是本地 A 股做 T 盯盘助手首版。它只做行情分析和买卖候选提示，不自动下单，不连接券商账户，不构成投资建议。

## 目录

- `backend/`：Python FastAPI 后端。
- `frontend/`：Vite React 仪表盘。
- `docs/superpowers/`：规格与实施计划。

## 启动

后端：

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m uvicorn tmaker.api.app:app --reload --host 127.0.0.1 --port 8000
```

前端：

```powershell
cd D:\it\t-maker\frontend
pnpm dev --host 127.0.0.1 --port 5174
```

然后打开 `http://127.0.0.1:5174`。

## 配置

复制 `.env.example` 为 `.env` 后填写 OpenAI-compatible API 配置。没有 API Key 也可以启动行情与规则演示。

后端 `/api/snapshot` 会通过腾讯分时接口拉取中际旭创、新易盛、亨通光电的 1 分钟真实行情。`/api/replay/recent?days=5&review=false` 会用腾讯最近 5 个交易日分钟数据做快速回放，并输出初步命中率；`review=true` 时才会对回放点逐个调用大模型复核。

```env
OPENAI_BASE_URL=https://api.openai.com
OPENAI_API_KEY=
OPENAI_MODEL=
OPENAI_WIRE_API=responses
OPENAI_REASONING_EFFORT=
OPENAI_DISABLE_RESPONSE_STORAGE=true
```

## 验证

```powershell
cd D:\it\t-maker\backend
.\.venv\Scripts\python.exe -m pytest -q

cd D:\it\t-maker\frontend
pnpm build
```
