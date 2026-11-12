"""The answer cache's key, with no database."""
from langchain_core.messages import HumanMessage, SystemMessage

from app.rag.answer_cache import answer_key, normalised


def test_case_spacing_and_closing_marks_make_no_difference():
    assert normalised("  How many  earned leave days?? ") == normalised("how many earned leave days")
    assert normalised("What is a PIP?!") == "what is a pip"


def test_different_words_stay_different():
    assert normalised("Earned leave on probation?") != normalised("Earned leave after probation?")
    assert normalised("Is TDS deducted?") != normalised("Is TDS not deducted?")


def test_a_new_model_or_a_changed_prompt_gets_a_new_key():
    prompt = [SystemMessage(content="Answer from the excerpts."), HumanMessage(content="Question: ")]
    changed = [SystemMessage(content="Answer from the excerpts only."), HumanMessage(content="Question: ")]
    assert answer_key("google:model-a", prompt) == answer_key("google:model-a", prompt)
    assert answer_key("google:model-a", prompt) != answer_key("google:model-b", prompt)
    assert answer_key("google:model-a", prompt) != answer_key("google:model-a", changed)
