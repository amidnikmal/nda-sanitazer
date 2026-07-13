"""code-tracer: local code-insight tracer.

Indexes a project directory with tree-sitter (static, not LLM) and answers
where to look. LLM is optional (annotations / rerank) via an OpenAI-compatible
llama-server on 127.0.0.1:8080; it degrades silently to fast mode when absent.
"""

__version__ = "0.1.0"
