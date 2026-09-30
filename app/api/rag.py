from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import AuthenticatedUser, get_current_user
from app.core.config import get_settings
from app.schemas.rag import RAGQueryRequest, RAGQueryResponse, RetrievedChunkReference
from app.services.audit_service import AuditWriteError
from app.services.embedding_service import EmbeddingService
from app.services.llm_service import create_llm_client
from app.services.qdrant_service import QdrantVectorStore, create_qdrant_client
from app.services.rag_service import AuthorizationBoundaryError, RAGService
from app.services.semantic_search_service import SemanticSearchService

router = APIRouter(prefix="/rag", tags=["rag"])


@lru_cache(maxsize=1)
def get_rag_service() -> RAGService:
    embedding_service = EmbeddingService()
    vector_store = QdrantVectorStore(
        client=create_qdrant_client(),
        vector_size=embedding_service.dimension,
    )
    vector_store.ensure_collection()
    semantic_search = SemanticSearchService(embedding_service, vector_store)
    return RAGService(semantic_search, create_llm_client())


@router.post("/query", response_model=RAGQueryResponse)
def query_rag(
    payload: RAGQueryRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    rag_service: RAGService = Depends(get_rag_service),
) -> RAGQueryResponse:
    try:
        result = rag_service.generate(
            question=payload.question,
            current_user=current_user,
            limit=payload.limit,
        )
    except AuthorizationBoundaryError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Retrieval failed authorization verification.",
        ) from exc
    except AuditWriteError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Required audit logging is unavailable.",
        ) from exc
    return RAGQueryResponse(
        answer=result.answer,
        retrieved_chunks=[
            RetrievedChunkReference(
                chunk_id=str(chunk.payload["chunk_id"]),
                document_id=int(chunk.payload["document_id"]),
                score=chunk.score,
            )
            for chunk in result.retrieved_chunks
        ],
    )