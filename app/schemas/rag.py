from pydantic import BaseModel, Field


class RAGQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    limit: int = Field(default=5, ge=1, le=20)


class RetrievedChunkReference(BaseModel):
    chunk_id: str
    document_id: int
    score: float


class RAGQueryResponse(BaseModel):
    answer: str
    retrieved_chunks: list[RetrievedChunkReference]