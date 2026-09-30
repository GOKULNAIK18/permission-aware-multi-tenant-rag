from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from qdrant_client import models

from app.auth.acl import authorize_chunk, build_acl_filter
from app.auth.dependencies import AuthenticatedUser
from app.services.audit_service import AuditEventType, AuditLoggingService
from app.services.llm_service import LLMClient
from app.services.prompt_service import PromptBuilder
from app.services.semantic_search_service import SemanticSearchResult, SemanticSearchService

logger = logging.getLogger(__name__)


class AuthorizationBoundaryError(RuntimeError):
    """Raised when vector retrieval violates the application ACL check."""


@dataclass(frozen=True)
class RAGResult:
    answer: str
    retrieved_chunks: list[SemanticSearchResult]


class RAGService:
    """Orchestrates retrieval, prompt construction, and answer generation."""

    def __init__(
        self,
        retrieval_service: SemanticSearchService,
        llm_client: LLMClient,
        prompt_builder: PromptBuilder | None = None,
        audit_service: AuditLoggingService | None = None,
    ) -> None:
        self.retrieval_service = retrieval_service
        self.llm_client = llm_client
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.audit_service = audit_service or AuditLoggingService()

    def generate(
        self,
        question: str,
        current_user: AuthenticatedUser,
        limit: int = 5,
    ) -> RAGResult:
        request_id = str(uuid.uuid4())
        acl_filter = build_acl_filter(current_user)
        try:
            qdrant_results = self.retrieval_service.search(
                query=question,
                limit=limit,
                query_filter=acl_filter,
            )
        except Exception:
            logger.error(
                "RAG retrieval failed user_id=%s tenant_id=%s request_id=%s",
                current_user.user_id,
                current_user.tenant_id,
                request_id,
            )
            self.audit_service.record_rag_event(
                current_user=current_user,
                event_type=AuditEventType.RAG_RETRIEVAL_FAILED,
                result_count=0,
                failure_category="retrieval_failure",
                request_id=request_id,
            )
            raise
        unauthorized_chunks = [chunk for chunk in qdrant_results if not authorize_chunk(current_user, chunk)]
        if unauthorized_chunks:
            logger.error(
                "ACL defense-in-depth failure: Qdrant returned unauthorized results count=%d user_id=%s tenant_id=%s request_id=%s",
                len(unauthorized_chunks),
                current_user.user_id,
                current_user.tenant_id,
                request_id,
            )
            self.audit_service.record_rag_event(
                current_user=current_user,
                event_type=AuditEventType.RAG_AUTHORIZATION_FAILED,
                result_count=len(qdrant_results),
                failure_category="authorization_boundary_violation",
                request_id=request_id,
            )
            raise AuthorizationBoundaryError("Vector retrieval returned a chunk outside the authenticated user's ACL.")

        retrieved_chunks = qdrant_results
        self.audit_service.record_rag_event(
            current_user=current_user,
            event_type=AuditEventType.RAG_RETRIEVAL_SUCCEEDED,
            result_count=len(retrieved_chunks),
            request_id=request_id,
        )
        if not retrieved_chunks:
            return RAGResult(answer="No authorized documents were found.", retrieved_chunks=[])

        prompt = self.prompt_builder.build(question, retrieved_chunks)
        answer = self.llm_client.complete(prompt)
        return RAGResult(answer=answer, retrieved_chunks=retrieved_chunks)
