"""Literal configuration for the self-contained attack lab."""

from __future__ import annotations


MODEL_PAIR = "openai/gpt-oss-20b:groq"
BASE_MODEL = "openai/gpt-oss-20b"
PROVIDER = "groq"
ROUTER_URL = "https://router.huggingface.co/v1"
GATEWAY_BASE_URL = "http://hf-gateway:8080/v1"
MCP_URL = "http://repo-rag:8000/mcp"


__all__ = [
    "BASE_MODEL",
    "GATEWAY_BASE_URL",
    "MCP_URL",
    "MODEL_PAIR",
    "PROVIDER",
    "ROUTER_URL",
]
