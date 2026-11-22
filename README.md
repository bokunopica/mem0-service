# mem0-service — 局域网共享长期记忆服务

Hermes 与 Windows 开发机共用的长期记忆库。基于 **mem0**（事实抽取 + 去重 + 检索）+ **pgvector**，
embedder 用本地 **Qwen3-Embedding-0.6B**（RTX 4070），LLM 用 **DeepSeek**。

## 架构

```
Hermes (memory.provider=mem0) ─┐
                               ├─HTTP(X-API-Key)─▶ mem0-service 127.0.0.1:18810
Windows 客户端 ────────────────┘                     ├─ LLM: api.deepseek.com / deepseek-flash
                                                     ├─ Embedder: Qwen3-Embedding-0.6B (cuda:0)
                                                     └─ Vector: postgres:5432 / mem0 库
```

## 启停

```bash
systemctl --user {start|stop|restart|status} mem0-service
journalctl --user -u mem0-service -f      # 日志
```

首次启动会在后台加载 embedder 模型（数十秒），`GET /health` 的 `model_loaded` 变 true 即可用。

## 端点

全部要求请求头 `X-API-Key: <MEM0_SERVICE_KEY>`（见 `.env`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 + 模型加载状态 |
| POST | `/memories` | 写入记忆 `{messages:[{role,content}], user_id, agent_id?}` |
| POST | `/search` | 检索 `{query, user_id, agent_id?, limit?}` |
| GET | `/memories?user_id=` | 列出某用户全部记忆 |
| GET | `/memories/{id}` | 取单条（不存在返回 404） |
| PUT | `/memories/{id}` | 改写文本和/或 metadata `{text?, metadata?}`，重新算 embedding |
| DELETE | `/memories/{id}` | 删除 |
| POST | `/warmup` | 手动预热模型 |

## 身份约定

- `user_id`：默认 `bot`（可被 `.env` 的 `MEM0_DEFAULT_USER` 覆盖），人用的话自己指定
- `agent_id`：机器/客户端标识，建议 `linux-18`（本机）、`win`（Windows）

检索按 `user_id` 过滤；跨机共享就传同一个 `user_id`，需要机器隔离就把 `agent_id` 带上。

## 配置

全部在 `.env`（chmod 600）：`MEM0_SERVICE_KEY`、`MEM0_LLM_API_KEY`、`MEM0_PG_PASSWORD` 等。
改配置后 `systemctl --user restart mem0-service`。

模型首次下载走 `HF_ENDPOINT=https://hf-mirror.com`（国内镜像），缓存在 `~/.cache/huggingface/`。

**`.env` 永不入库**（见 `.gitignore`）。仓库里只有 `.env.example`，值全是占位符。

## 从零部署

```bash
git clone <this-repo> ~/mem0-service && cd ~/mem0-service
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # 国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple

# 1) 向量库：需要一个开了 pgvector 扩展的 Postgres
docker run -d --name mem0-pg -e POSTGRES_USER=mem0 -e POSTGRES_PASSWORD='<密码>' \
  -e POSTGRES_DB=mem0 -p 5432:5432 pgvector/pgvector:pg18
psql -h 127.0.0.1 -U mem0 -d mem0 -c 'CREATE EXTENSION IF NOT EXISTS vector;'

# 2) 配置密钥
cp .env.example .env && chmod 600 .env && $EDITOR .env

# 3) 起服务（用户级 systemd，或直接 uvicorn）
cp deploy/mem0-service.service ~/.config/systemd/user/   # unit 里用 %h，按你的家目录展开
systemctl --user daemon-reload && systemctl --user enable --now mem0-service
curl -s localhost:18810/health
```

首次启动后台加载 embedder（GPU 约 10 秒，CPU 数十秒），`/health` 的 `model_loaded` 变 true 即就绪。

## 客户端接入

Hermes（`memory.provider=mem0`）走的是插件里的 `SelfHostedBackend`：`X-API-Key` 鉴权，
端点就是本文档这套 `/memories`、`/search`。跨机共享时把服务绑到内网地址（或加一层端口转发），
客户端 `host` 指过来、`api_key` 填 `MEM0_SERVICE_KEY` 即可。

## 安全边界

- 除 `/health` 外所有端点都要 `X-API-Key`；`/health` 只在不带 key 时回 `{"status":"ok"}`，模型名/PG 地址要带 key 才返回。
- `PUT`/`DELETE` 会先查 `user_id` 归属（由客户端插件做），服务端只做 id 级操作——多租户场景需自行在网关层补齐。
- `PUT` 是原地覆盖：旧文本不保留历史。需要可回滚请自行接 `/history`。

