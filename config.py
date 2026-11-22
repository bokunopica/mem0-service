"""mem0-service 配置：所有密钥来自 .env，不硬编码。"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _req(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"missing required env var {name} in {BASE_DIR / '.env'}")
    return v


# --- 服务自身 ---
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "18810"))
# 客户端鉴权：Hermes 插件 / Windows 客户端都带 X-API-Key
SERVICE_API_KEY = _req("MEM0_SERVICE_KEY")

# --- LLM（记忆抽取）---
LLM_BASE_URL = os.environ.get("MEM0_LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("MEM0_LLM_MODEL", "deepseek-flash")
LLM_API_KEY = _req("MEM0_LLM_API_KEY")

# --- Embedder（本地 GPU 模型）---
EMBED_MODEL = os.environ.get("MEM0_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
EMBED_DIMS = int(os.environ.get("MEM0_EMBED_DIMS", "1024"))
EMBED_DEVICE = os.environ.get("MEM0_EMBED_DEVICE", "cuda")

# --- 向量库（pgvector）---
PG_HOST = os.environ.get("MEM0_PG_HOST", "127.0.0.1")
PG_PORT = int(os.environ.get("MEM0_PG_PORT", "5432"))
PG_DB = os.environ.get("MEM0_PG_DB", "mem0")
PG_USER = os.environ.get("MEM0_PG_USER", "mem0")
PG_PASSWORD = _req("MEM0_PG_PASSWORD")
COLLECTION = os.environ.get("MEM0_COLLECTION", "memories")

# --- 默认身份 ---
DEFAULT_USER = os.environ.get("MEM0_DEFAULT_USER", "bot")


def mem0_config() -> dict:
    """mem0 OSS 组件配置：LLM 走 DeepSeek，Embedder 本地 Qwen3，向量走 pgvector。"""
    return {
        "llm": {
            "provider": "openai",
            "config": {
                "model": LLM_MODEL,
                "openai_base_url": LLM_BASE_URL,
                "api_key": LLM_API_KEY,
                "temperature": 0.1,
            },
        },
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model": EMBED_MODEL,
                "embedding_dims": EMBED_DIMS,
                "model_kwargs": {"device": EMBED_DEVICE},
            },
        },
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "host": PG_HOST,
                "port": PG_PORT,
                "dbname": PG_DB,
                "user": PG_USER,
                "password": PG_PASSWORD,
                "collection_name": COLLECTION,
                "embedding_model_dims": EMBED_DIMS,
            },
        },
        "history_store": {"provider": "sqlite", "config": {"path": str(BASE_DIR / "history.db")}},
    }
