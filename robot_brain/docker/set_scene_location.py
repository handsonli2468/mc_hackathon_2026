#!/usr/bin/env python3
"""Backward-compatible wrapper for v6.2 operator commands.

v6.3 stores executable places under rag-knowledge/locations rather than inside Scene
RAG. New deployments should use docker/manage_rag_location.py directly.
"""
from manage_rag_location import main

if __name__ == '__main__':
    raise SystemExit(main())
