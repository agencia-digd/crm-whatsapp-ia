"""Chunking integral com overlap pequeno e cobertura por união de índices."""

from __future__ import annotations

from dataclasses import dataclass

from app.inspector_v2.models import ChunkDescriptor, ParsedMessage


@dataclass(frozen=True)
class MessageChunk:
    descriptor: ChunkDescriptor
    messages: tuple[ParsedMessage, ...]

    def render(self) -> str:
        return "\n".join(f"[#{message.index:06d}] {message.raw}" for message in self.messages)


def chunk_messages(
    messages: list[ParsedMessage],
    max_characters: int = 32_000,
    overlap_messages: int = 2,
) -> list[MessageChunk]:
    if max_characters <= 0:
        raise ValueError("max_characters deve ser positivo")
    if overlap_messages < 0:
        raise ValueError("overlap_messages não pode ser negativo")
    if not messages:
        return []

    boundaries: list[tuple[int, int]] = []
    start = 0
    while start < len(messages):
        end = start
        size = 0
        while end < len(messages):
            candidate_size = len(messages[end].raw) + 12
            if end > start and size + candidate_size > max_characters:
                break
            size += candidate_size
            end += 1
        boundaries.append((start, end))
        if end >= len(messages):
            break
        next_start = max(start + 1, end - overlap_messages)
        start = next_start

    chunks: list[MessageChunk] = []
    previous_end = 0
    for ordinal, (start, end) in enumerate(boundaries, start=1):
        raw_chunk = messages[start:end]
        overlap_before = max(0, previous_end - start)
        descriptor = ChunkDescriptor(
            ordinal=ordinal,
            first_message=raw_chunk[0].index,
            last_message=raw_chunk[-1].index,
            message_count=len(raw_chunk),
            overlap_before=overlap_before,
            character_count=sum(len(message.raw) for message in raw_chunk),
        )
        chunks.append(MessageChunk(descriptor=descriptor, messages=tuple(raw_chunk)))
        previous_end = end
    assert_full_coverage(messages, chunks)
    return chunks


def unique_processed_count(chunks: list[MessageChunk]) -> int:
    return len({message.index for chunk in chunks for message in chunk.messages})


def assert_full_coverage(messages: list[ParsedMessage], chunks: list[MessageChunk]) -> None:
    expected = list(range(1, len(messages) + 1))
    flattened = [message.index for chunk in chunks for message in chunk.messages]
    actual = sorted(set(flattened))
    if actual != expected:
        raise ValueError("chunking perdeu mensagens")
    for chunk in chunks:
        indexes = [message.index for message in chunk.messages]
        if indexes != sorted(indexes) or len(indexes) != len(set(indexes)):
            raise ValueError("chunk reordenou ou duplicou mensagens internamente")
    if any(current < previous for previous, current in zip(flattened, flattened[1:]) if current != previous):
        # Quedas pequenas são esperadas apenas na borda de overlap.
        for previous_chunk, current_chunk in zip(chunks, chunks[1:]):
            if current_chunk.descriptor.first_message > previous_chunk.descriptor.last_message + 1:
                raise ValueError("lacuna entre chunks")
