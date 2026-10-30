"""Facts she keeps: remember, forget, memories, recall.

These go through the same dispatcher a live message takes, and they check the
real table afterwards — "she said she kept it" is not the same as "the row is
there", and only the second one survives a restart.
"""

from __future__ import annotations

import pytest
from conftest import invoke


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


async def _rows(runtime) -> list[str]:
    return [row.content for row in await runtime.memory.list(100, limit=50)]


async def test_remember_stores_the_exact_sentence(runtime) -> None:
    sent = await invoke(runtime, ".remember Sara's birthday is 3 May")
    assert "kept" in _said(sent)
    assert "Sara's birthday is 3 May" in _said(sent)
    assert await _rows(runtime) == ["Sara's birthday is 3 May"]


async def test_remember_twice_says_it_is_already_known(runtime) -> None:
    await invoke(runtime, ".remember the flat is on the fifth floor")
    sent = await invoke(runtime, ".remember the flat is on the fifth floor")
    assert "already" in _said(sent).lower()
    assert len(await _rows(runtime)) == 1


async def test_remember_without_a_fact_asks_for_one(runtime) -> None:
    sent = await invoke(runtime, ".remember")
    assert ".remember" in _said(sent) and "keep" in _said(sent).lower()
    assert await _rows(runtime) == []


async def test_a_very_long_fact_is_cut_not_rejected(runtime) -> None:
    sent = await invoke(runtime, ".remember " + "word " * 400)
    assert "kept" in _said(sent)
    stored = await _rows(runtime)
    assert len(stored) == 1
    assert len(stored[0]) <= 400


async def test_forget_by_words_removes_every_match_and_names_the_count(runtime) -> None:
    await invoke(runtime, ".remember the flat is on the fifth floor")
    await invoke(runtime, ".remember the flat key is with the neighbour")
    await invoke(runtime, ".remember he hates cilantro")
    sent = await invoke(runtime, ".forget the flat")
    assert "2" in _said(sent)
    assert await _rows(runtime) == ["he hates cilantro"]


async def test_forget_with_nothing_matching_says_so(runtime) -> None:
    await invoke(runtime, ".remember he hates cilantro")
    sent = await invoke(runtime, ".forget the moon")
    assert "nothing" in _said(sent).lower()
    assert await _rows(runtime) == ["he hates cilantro"]


async def test_forget_by_number_drops_exactly_that_row(runtime) -> None:
    await invoke(runtime, ".remember first thing")
    await invoke(runtime, ".remember second thing")
    rows = await runtime.memory.list(100)
    target = rows[0]
    sent = await invoke(runtime, f".forget #{target.id}")
    assert "forgot" in _said(sent).lower()
    assert await _rows(runtime) == [row.content for row in rows[1:]]


async def test_forget_an_id_that_does_not_exist_is_honest(runtime) -> None:
    sent = await invoke(runtime, ".forget #999")
    assert "999" in _said(sent)
    assert "no" in _said(sent).lower() or "not" in _said(sent).lower()


async def test_memories_with_nothing_kept_says_so(runtime) -> None:
    sent = await invoke(runtime, ".memories")
    assert "not keeping anything" in _said(sent)


async def test_memories_lists_ids_that_forget_accepts(runtime) -> None:
    await invoke(runtime, ".remember one")
    await invoke(runtime, ".remember two")
    sent = await invoke(runtime, ".memories")
    text = _said(sent)
    rows = await runtime.memory.list(100)
    for row in rows:
        assert f"#{row.id}" in text
    assert "2" in text


async def test_memories_respects_the_count_argument(runtime) -> None:
    for index in range(6):
        await invoke(runtime, f".remember fact number {index}")
    sent = await invoke(runtime, ".memories 2")
    text = _said(sent)
    rows = await runtime.memory.list(100)
    assert f"#{rows[0].id}" in text
    assert f"#{rows[1].id}" in text
    assert f"#{rows[2].id}" not in text


async def test_recall_finds_a_fact_by_a_word_inside_it(runtime) -> None:
    await invoke(runtime, ".remember the car needs new tyres")
    await invoke(runtime, ".remember he hates cilantro")
    sent = await invoke(runtime, ".recall tyres")
    text = _said(sent).lower()
    assert "car needs new tyres" in text
    assert "cilantro" not in text


async def test_recall_with_nothing_matching_is_honest(runtime) -> None:
    await invoke(runtime, ".remember he hates cilantro")
    sent = await invoke(runtime, ".recall submarines")
    assert "submarines" in _said(sent).lower()
    assert "nothing" in _said(sent).lower()


async def test_recall_without_a_query_asks_for_one(runtime) -> None:
    sent = await invoke(runtime, ".recall")
    assert "look" in _said(sent).lower()


async def test_facts_are_per_chat(runtime) -> None:
    await invoke(runtime, ".remember this is the group fact", chat_id=-1001)
    sent = await invoke(runtime, ".memories", chat_id=-1002)
    assert "not keeping anything" in _said(sent)


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("forget what I said about the flat", "forget the flat"),
        ("what do you remember", "memories"),
        ("what do you know about the flat", "recall the flat"),
    ],
)
async def test_memory_sentences_reach_the_command(sentence: str, expected: str) -> None:
    from havoc.services.intent import detect

    found = detect(sentence)
    assert found is not None, sentence
    assert found.lines[0].startswith(expected)
