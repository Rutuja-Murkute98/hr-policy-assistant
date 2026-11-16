"""The retrieval experiments' pieces, summaries and pooling. No model or database is used. See D61."""
from langchain_core.documents import Document

from app.rag.retrieval import Candidate
from evaluation.retrieval_experiments import MULTI_PART, merged, pieces_of, small_sections, summarised_sections

SECTION = Document(id="Leave > 4.2. Sick Leave > 0", metadata={"source": "Leave_Policy.md", "section": "4.2. Sick Leave"},
                   page_content="Leave Policy > 4.2. Sick Leave\n\n"
                                "All employees are entitled to 12 days of Sick Leave per calendar year, credited on "
                                "the first of January. Unused Sick Leave can't be carried forward to the next year, "
                                "and it lapses on the last day of December. A medical certificate is needed for "
                                "more than 2 days in a row, from a registered doctor. Sick Leave taken during a "
                                "notice period is checked by HR before it is approved, and any leave beyond the "
                                "balance is treated as Loss of Pay. Short.")


def test_a_section_is_cut_into_pieces_of_whole_sentences():
    pieces = pieces_of(SECTION.page_content.partition("\n\n")[2])
    assert len(pieces) == 2
    assert all(len(piece) >= 100 for piece in pieces)
    assert pieces[-1].endswith("Loss of Pay. Short.")  # a sentence is never cut
    assert " ".join(pieces) == " ".join(SECTION.page_content.partition("\n\n")[2].split())


def test_each_piece_keeps_its_headings_and_names_its_section():
    pieces = small_sections({str(SECTION.id): SECTION})
    assert [piece.id for piece in pieces] == ["Leave > 4.2. Sick Leave > 0 ~ 0", "Leave > 4.2. Sick Leave > 0 ~ 1"]
    assert all(piece.page_content.startswith("Leave Policy > 4.2. Sick Leave\n\n") for piece in pieces)
    assert {piece.metadata["parent"] for piece in pieces} == {SECTION.id}


def test_the_summary_goes_after_the_headings_and_isnt_part_of_the_body():
    [summarised] = summarised_sections({str(SECTION.id): SECTION}, {"Leave_Policy.md": "Covers all paid leave."})
    heading, _, body = summarised.page_content.partition("\n\n")
    assert heading == "Leave Policy > 4.2. Sick Leave\nCovers all paid leave."
    assert body == SECTION.page_content.partition("\n\n")[2]


def candidate(chunk_id: str, distance: float, vector_rank=None, keyword_rank=None, parent: str = "") -> Candidate:
    return Candidate(chunk=Document(id=chunk_id, page_content=chunk_id, metadata={"parent": parent}),
                     distance=distance, embedding=[distance], vector_rank=vector_rank, keyword_rank=keyword_rank,
                     confidence=1 - distance)


def test_pooled_results_keep_the_closest_distance_and_best_ranks():
    first = [candidate("a", 0.4, vector_rank=3), candidate("b", 0.2, vector_rank=1)]
    second = [candidate("a", 0.1, keyword_rank=2), candidate("c", 0.5, vector_rank=2)]
    pooled = {str(found.chunk.id): found for found in merged([first, second], key=lambda found: str(found.chunk.id))}
    assert set(pooled) == {"a", "b", "c"}
    assert (pooled["a"].distance, pooled["a"].vector_rank, pooled["a"].keyword_rank) == (0.1, 3, 2)
    assert pooled["a"].embedding == [0.1] and round(pooled["a"].confidence, 3) == 0.9


def test_pieces_are_pooled_into_their_whole_section():
    sections = {"S1": Document(id="S1", page_content="the whole section")}
    pooled = merged([[candidate("S1 ~ 0", 0.3, vector_rank=2, parent="S1"),
                      candidate("S1 ~ 1", 0.2, vector_rank=1, parent="S1")]],
                    key=lambda found: found.chunk.metadata["parent"], sections=sections)
    assert len(pooled) == 1 and pooled[0].chunk.page_content == "the whole section"
    assert (pooled[0].distance, pooled[0].vector_rank) == (0.2, 1)


def test_only_questions_that_may_ask_more_than_one_thing_are_split():
    assert MULTI_PART.search("How many sick days do I get, and can they be carried forward?")
    assert MULTI_PART.search("What is the notice period? Is it negotiable?")
    assert not MULTI_PART.search("How many sick days do I get?")
