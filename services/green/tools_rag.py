"""RAG tools: let agents retrieve similar past incidents, and record a new
memory once one is resolved.

Backed by Firestore's native vector search (`find_nearest`), not a
separate vector DB -- consistent with the rest of this service already
using Firestore for everything else. Needs a vector index on the
`incident_memory` collection's `embedding` field before
`search_similar_incidents` will work; Firestore vector search requires a
composite index like any other Firestore query, the same way any other
Firestore query needing a composite index does. See README.md's "RAG"
section for the exact `gcloud firestore indexes` command -- this file
can't create the index itself, and `search_similar_incidents` will return
a clear error (not a silent empty result) until it exists.
"""
from __future__ import annotations

import os
import time

from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector
from google.genai import Client

import state

EMBEDDING_MODEL = os.environ.get("GREEN_TEAM_EMBEDDING_MODEL", "text-multilingual-embedding-002")
MEMORY_COLLECTION = "incident_memory"

_genai_client: Client | None = None


def _embed(text: str) -> list[float]:
    global _genai_client
    if _genai_client is None:
        _genai_client = Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
    response = _genai_client.models.embed_content(model=EMBEDDING_MODEL, contents=text)
    return list(response.embeddings[0].values)


def record_incident_memory(incident_id: str, summary: str) -> str:
    """Save a short summary of a resolved incident for future retrieval.

    Call this once, from reporter_agent, after the post-mortem is
    written -- not earlier, so half-finished investigations don't
    pollute future retrieval with wrong or incomplete conclusions.

    Args:
        incident_id: the incident this memory is about.
        summary: 2-4 sentences covering the symptom, root cause, the fix,
            and the outcome. This is both what gets embedded for search
            AND what a future search will show verbatim -- write it to be
            useful standalone, not as a pointer back to this incident.
            Keep it short: a long summary embeds less precisely than a
            focused one, and the whole point is fast pattern-matching
            against a new incident, not a full re-read of the old one.

    Returns:
        Confirmation text.
    """
    vector = _embed(summary)
    state.db().collection(MEMORY_COLLECTION).document(incident_id).set(
        {
            "incident_id": incident_id,
            "summary": summary,
            "embedding": Vector(vector),
            "recorded_at": time.time(),
        }
    )
    return f"Recorded memory for {incident_id}."


def search_similar_incidents(query: str, top_k: int = 3) -> str:
    """Find past resolved incidents similar in meaning to a description.

    This is semantic search, not keyword search -- "database connections
    maxed out" will match a past incident summarized as "Cloud SQL pool
    exhausted" even though they share no words.

    Args:
        query: a short description of the current symptom or suspected
            root cause, e.g. "Cloud SQL connection pool exhausted on
            payment-service".
        top_k: how many past incidents to return. Default 3.

    Returns:
        Up to `top_k` past incident summaries, most similar first, each
        prefixed with its incident_id, or a message saying none were
        found. Each result is a complete, standalone summary already --
        there's no separate detail to fetch for it.
    """
    vector = _embed(query)
    try:
        docs = (
            state.db()
            .collection(MEMORY_COLLECTION)
            .find_nearest(
                vector_field="embedding",
                query_vector=Vector(vector),
                limit=top_k,
                distance_measure=DistanceMeasure.COSINE,
            )
            .get()
        )
    except Exception as exc:
        return (
            f"error: vector search failed ({exc}). This usually means the "
            f"Firestore vector index on {MEMORY_COLLECTION}.embedding "
            f"hasn't been created yet -- see README.md's RAG section, or "
            f"treat this as 'no past incidents available yet' and proceed "
            f"without them."
        )
    if not docs:
        return "No similar past incidents found."
    results = [f"[{doc.to_dict().get('incident_id')}] {doc.to_dict().get('summary')}" for doc in docs]
    return "\n\n".join(results)
