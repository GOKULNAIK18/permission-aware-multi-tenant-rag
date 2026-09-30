from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.core.config import get_settings


class EmbeddingService:
    """Replaceable adapter around the configured Sentence Transformers model."""

    def __init__(
        self,
        model_name: str | None = None,
        model_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.model_name = model_name or get_settings().embedding_model
        self._model_factory = model_factory
        self._model_instance: Any | None = None

    @property
    def dimension(self) -> int:
        model = self._model()
        get_dimension = getattr(model, "get_embedding_dimension", None)
        if get_dimension is None:
            get_dimension = model.get_sentence_embedding_dimension
        dimension = get_dimension()
        if not isinstance(dimension, int) or dimension <= 0:
            raise ValueError(f"Embedding model {self.model_name!r} did not report a valid vector dimension.")
        return dimension

    def embed_text(self, text: str) -> list[float]:
        vectors = self.embed_texts([text])
        return vectors[0]

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding input texts must be non-empty strings.")
        if not texts:
            return []

        encoded = self._model().encode(
            list(texts),
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        if hasattr(encoded, "tolist"):
            encoded = encoded.tolist()
        vectors = [[float(value) for value in vector] for vector in encoded]
        dimension = self.dimension
        if any(len(vector) != dimension for vector in vectors):
            raise ValueError("Embedding model returned vectors with an inconsistent dimension.")
        return vectors

    def _model(self) -> Any:
        if self._model_instance is None:
            if self._model_factory is None:
                from sentence_transformers import SentenceTransformer

                self._model_factory = SentenceTransformer
            self._model_instance = self._model_factory(self.model_name)
        return self._model_instance