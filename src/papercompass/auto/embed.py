"""Embedding similarity channel for v3 fusion.

Encodes the topic's `search_keyword_text` and each candidate's title+abstract
to vectors, returns a cosine-similarity score (0-100). The fusion stage
combines this with the LLM judge score and metadata score.

Default model: BAAI/bge-small-en-v1.5 (~33M params, ~130MB). Loading is
local-cache-first so normal scoring never surprises the user with a download;
set PAPERCOMPASS_EMBED_ALLOW_DOWNLOAD=1 when intentionally bootstrapping the
model cache. Single process can embed ~500 papers/sec.

Caching: query and candidate embeddings are kept in memory per process (no
disk cache yet — the embedding stage runs once per build, fast enough that
re-encoding on resume is acceptable). Add disk cache later if needed.

If `sentence-transformers` is not installed, or the configured model cannot be
loaded, scoring returns ``None`` for the embedding channel and emits a one-time
warning. The fusion stage redistributes the embedding weight in that case.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Any

_MODEL = None
_MODEL_LOCK = threading.Lock()
_MODEL_UNAVAILABLE = False
_MODEL_UNAVAILABLE_REASON = ""
_WARNED_UNAVAILABLE = False


def _warn_unavailable(reason: str) -> None:
    global _WARNED_UNAVAILABLE
    if _WARNED_UNAVAILABLE:
        return
    sys.stderr.write(
        "papercompass: embedding channel disabled: "
        f"{reason}. Install or repair with `uv sync --extra embed`.\n"
    )
    _WARNED_UNAVAILABLE = True


def _embed_download_allowed() -> bool:
    return (os.getenv("PAPERCOMPASS_EMBED_ALLOW_DOWNLOAD") or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_model(model_name: str = "BAAI/bge-small-en-v1.5"):
    global _MODEL, _MODEL_UNAVAILABLE, _MODEL_UNAVAILABLE_REASON
    if _MODEL is not None:
        return _MODEL
    if _MODEL_UNAVAILABLE:
        return None
    with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        if _MODEL_UNAVAILABLE:
            return None
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            _MODEL_UNAVAILABLE = True
            _MODEL_UNAVAILABLE_REASON = "sentence-transformers is not installed"
            _warn_unavailable(_MODEL_UNAVAILABLE_REASON)
            return None
        try:
            _MODEL = SentenceTransformer(model_name, local_files_only=True)
        except TypeError:
            try:
                _MODEL = SentenceTransformer(model_name)
            except Exception as exc:  # noqa: BLE001
                _MODEL_UNAVAILABLE = True
                _MODEL_UNAVAILABLE_REASON = f"failed to load {model_name}: {exc}"
                _warn_unavailable(_MODEL_UNAVAILABLE_REASON)
                return None
        except Exception as local_exc:  # noqa: BLE001
            if _embed_download_allowed():
                try:
                    _MODEL = SentenceTransformer(model_name)
                    return _MODEL
                except Exception as exc:  # noqa: BLE001
                    local_exc = exc
            _MODEL_UNAVAILABLE = True
            _MODEL_UNAVAILABLE_REASON = (
                f"failed to load local {model_name}: {local_exc}. "
                "Set PAPERCOMPASS_EMBED_ALLOW_DOWNLOAD=1 to download it"
            )
            _warn_unavailable(_MODEL_UNAVAILABLE_REASON)
            return None
        return _MODEL


def is_available() -> bool:
    return _load_model() is not None


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Encode a list of strings into normalized embeddings (L2 norm = 1).
    Returns None if the embedding library is unavailable, otherwise a list
    of float vectors in the same order as inputs."""
    model = _load_model()
    if model is None:
        return None
    if not texts:
        return []
    cleaned = [t if isinstance(t, str) else "" for t in texts]
    vecs = model.encode(cleaned, normalize_embeddings=True, show_progress_bar=False)
    return [list(map(float, v)) for v in vecs]


def cosine_similarity_normalized(a: list[float], b: list[float]) -> float:
    """Cosine similarity for L2-normalized vectors == dot product."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def candidate_text(paper: dict[str, Any]) -> str:
    """Concatenate title + abstract (capped) for embedding the candidate."""
    title = (paper.get("title") or "").strip()
    abstract = (paper.get("abstract") or paper.get("summary") or "").strip()
    if len(abstract) > 1500:
        abstract = abstract[:1500]
    return (title + ". " + abstract).strip(". ")


def score_candidates_against_topic(
    candidates: list[dict[str, Any]],
    topic_target_text: str,
) -> list[float | None]:
    """Return a 0-100 similarity score for each candidate (in input order).

    Returns `None` for entries when embedding cannot be computed — embedding
    library is missing, target text is empty, or the model failed to encode.
    Callers MUST forward `None` (not `0.0`) into fuse_scores so the channel
    weight is redistributed; treating a missing signal as 0.0 silently pushes
    every candidate's fused score down and rerank-by-emb becomes meaningless.
    """
    if not candidates:
        return []
    target = (topic_target_text or "").strip()
    if not target:
        return [None] * len(candidates)

    cand_texts = [candidate_text(c) for c in candidates]
    cand_vecs = embed_texts(cand_texts)
    if cand_vecs is None:
        return [None] * len(candidates)
    target_vec_list = embed_texts([target])
    if not target_vec_list:
        return [None] * len(candidates)
    target_vec = target_vec_list[0]

    scores: list[float | None] = []
    for vec in cand_vecs:
        sim = cosine_similarity_normalized(vec, target_vec)
        # bge models give similarities roughly in [-0.2, 1.0]; map to 0-100
        # via clamp + linear scale on the practical range [0.2, 0.8].
        clamped = max(0.0, min(1.0, sim))
        score_0_100 = max(0.0, min(100.0, (clamped - 0.2) / 0.6 * 100.0))
        scores.append(round(score_0_100, 1))
    return scores
