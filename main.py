"""mem0-service — 局域网共享长期记忆服务。

对外暴露 mem0 官方 OSS REST 兼容的接口（/memories、/search），
供 Hermes（memory.provider=mem0）和 Windows 客户端共用。

组件：LLM=DeepSeek，Embedder=本地 Qwen3-Embedding（GPU），Vector=pgvector。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mem0-service")

app = FastAPI(title="mem0-service", version="0.1.0")

_memory = None
_memory_lock = threading.Lock()
_warm_state = {"loaded": False, "error": None}


def get_memory():
    """懒加载 mem0 Memory（首次调用会加载 embedder 模型，较慢）。"""
    global _memory
    if _memory is None:
        with _memory_lock:
            if _memory is None:
                from mem0 import Memory

                log.info("initialising mem0 (embedder=%s device=%s)", config.EMBED_MODEL, config.EMBED_DEVICE)
                _memory = Memory.from_config(config.mem0_config())
                log.info("mem0 ready")
    return _memory


def require_key(x_api_key: str = Header(default="")) -> None:
    """所有 /memories /search 端点要求 X-API-Key。"""
    expected = config.SERVICE_API_KEY
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="invalid api key")


# ----------------------------- 请求模型 -----------------------------

class Message(BaseModel):
    role: str
    content: str


class AddRequest(BaseModel):
    messages: Any = Field(..., description="消息数组 [{role,content}] 或纯字符串")
    user_id: str = config.DEFAULT_USER
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    metadata: Optional[dict] = None
    infer: bool = True


class SearchRequest(BaseModel):
    query: str
    user_id: str = config.DEFAULT_USER
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    limit: int = 10


class MemoryUpdate(BaseModel):
    text: Optional[str] = Field(default=None, description="新的记忆文本，会重新计算 embedding")
    metadata: Optional[dict] = Field(default=None, description="要合并进 metadata 的字段")


def _norm_messages(raw: Any) -> Any:
    if isinstance(raw, str):
        return [{"role": "user", "content": raw}]
    if isinstance(raw, list):
        out = []
        for m in raw:
            if isinstance(m, dict):
                out.append({"role": m.get("role", "user"), "content": m.get("content", "")})
            else:
                out.append({"role": "user", "content": str(m)})
        return out
    return [{"role": "user", "content": str(raw)}]


def _unwrap(result: Any) -> Any:
    """mem0 不同版本返回 {'results': [...]} 或裸 list，统一一下。"""
    if isinstance(result, dict):
        return result.get("results", result)
    return result


# ----------------------------- 端点 -----------------------------

@app.get("/health")
def health(x_api_key: str = Header(default="")):
    """公开探活只回状态；模型名/PG 地址等信息需要正确的 X-API-Key 才返回。"""
    body = {"status": "ok", "model_loaded": _warm_state["loaded"]}
    if not config.SERVICE_API_KEY or x_api_key == config.SERVICE_API_KEY:
        body.update(
            {
                "model_error": _warm_state["error"],
                "embed_model": config.EMBED_MODEL,
                "embed_dims": config.EMBED_DIMS,
                "llm_model": config.LLM_MODEL,
                "pg": f"{config.PG_HOST}:{config.PG_PORT}/{config.PG_DB}",
            }
        )
    return body


@app.post("/memories")
def add_memories(req: AddRequest, _: None = Depends(require_key)):
    mem = get_memory()
    kwargs = {"user_id": req.user_id, "infer": req.infer}
    if req.agent_id:
        kwargs["agent_id"] = req.agent_id
    if req.run_id:
        kwargs["run_id"] = req.run_id
    if req.metadata:
        kwargs["metadata"] = req.metadata
    try:
        result = mem.add(_norm_messages(req.messages), **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.exception("add failed")
        raise HTTPException(status_code=502, detail=f"mem0 add failed: {exc}") from exc
    return {"results": _unwrap(result)}


@app.post("/search")
def search_memories(req: SearchRequest, _: None = Depends(require_key)):
    mem = get_memory()
    filters: dict[str, Any] = {"user_id": req.user_id}
    if req.agent_id:
        filters["agent_id"] = req.agent_id
    if req.run_id:
        filters["run_id"] = req.run_id
    try:
        result = mem.search(req.query, top_k=req.limit, filters=filters)
    except Exception as exc:  # noqa: BLE001
        log.exception("search failed")
        raise HTTPException(status_code=502, detail=f"mem0 search failed: {exc}") from exc
    if isinstance(result, dict) and "results" not in result:
        return result
    return {"results": _unwrap(result)}


@app.get("/memories")
def list_memories(user_id: str = config.DEFAULT_USER, agent_id: Optional[str] = None,
                  limit: int = 100, _: None = Depends(require_key)):
    mem = get_memory()
    filters: dict[str, Any] = {"user_id": user_id}
    if agent_id:
        filters["agent_id"] = agent_id
    try:
        result = mem.get_all(filters=filters, top_k=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"mem0 get_all failed: {exc}") from exc
    return {"results": _unwrap(result)}


@app.get("/memories/{memory_id}")
def get_memory_item(memory_id: str, _: None = Depends(require_key)):
    mem = get_memory()
    try:
        result = mem.get(memory_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"not found: {exc}") from exc
    if not result:
        raise HTTPException(status_code=404, detail=f"memory {memory_id} not found")
    return result if isinstance(result, dict) else {"result": result}


@app.put("/memories/{memory_id}")
def update_memory_item(memory_id: str, req: MemoryUpdate, _: None = Depends(require_key)):
    """就地改写一条记忆（重新算 embedding）。mem0 官方 server 的 PUT 语义。"""
    if req.text is None and req.metadata is None:
        raise HTTPException(status_code=422, detail="at least one of 'text' or 'metadata' is required")
    if req.text is not None and not req.text.strip():
        raise HTTPException(status_code=422, detail="'text' must not be empty")
    mem = get_memory()
    try:
        existing = mem.get(memory_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"not found: {exc}") from exc
    if not existing:
        raise HTTPException(status_code=404, detail=f"memory {memory_id} not found")
    kwargs: dict[str, Any] = {}
    if req.text is not None:
        kwargs["text"] = req.text
    if req.metadata is not None:
        kwargs["metadata"] = req.metadata
    try:
        mem.update(memory_id, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.exception("update failed")
        raise HTTPException(status_code=502, detail=f"mem0 update failed: {exc}") from exc
    log.info("updated memory %s (%d chars)", memory_id, len(req.text or ""))
    return {"message": "Memory updated successfully!", "memory_id": memory_id}


@app.delete("/memories/{memory_id}")
def delete_memory_item(memory_id: str, _: None = Depends(require_key)):
    mem = get_memory()
    try:
        mem.delete(memory_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"delete failed: {exc}") from exc
    return {"deleted": memory_id}


@app.post("/warmup")
def warmup(_: None = Depends(require_key)):
    """手动触发 embedder 模型加载（首次约数十秒）。"""
    def _run():
        try:
            get_memory()
            _warm_state["loaded"] = True
        except Exception as exc:  # noqa: BLE001
            _warm_state["error"] = str(exc)
            log.exception("warmup failed")

    if not _warm_state["loaded"]:
        threading.Thread(target=_run, daemon=True).start()
    return {"warming": not _warm_state["loaded"], "loaded": _warm_state["loaded"]}


@app.on_event("startup")
def _startup():
    # 后台预热，避免第一个请求等模型加载
    threading.Thread(target=lambda: (_run_warmup()), daemon=True).start()


def _run_warmup():
    try:
        get_memory()
        _warm_state["loaded"] = True
        log.info("warmup complete")
    except Exception as exc:  # noqa: BLE001
        _warm_state["error"] = str(exc)
        log.exception("startup warmup failed")
