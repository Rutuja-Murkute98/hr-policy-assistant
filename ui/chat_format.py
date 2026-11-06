"""Turn chat data into what the chat screen shows. No Streamlit here, so it's easy to test."""
import html
import re

CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")  # such as [1] or [2, 4]


def source_anchor(message_index: int, ref: int) -> str:
    return f"src-{message_index}-{ref}"


def link_citations(answer: str, sources: list[dict], message_index: int) -> str:
    """Make each [1] in an answer a link to that source. Numbers with no source stay as text."""
    refs = {source["ref"] for source in sources}

    def link(match: re.Match) -> str:
        labels = [label.strip() for label in match[1].split(",")]
        linked = [f"[{label}](#{source_anchor(message_index, int(label))})" if int(label) in refs else label
                  for label in labels]
        return f"[{', '.join(linked)}]"

    return CITATION.sub(link, answer)


def sources_html(sources: list[dict], message_index: int) -> str:
    """Build the sources list, with each source's policy text one click away.

    Everything comes from the policies or the model, so it's all escaped. It stays on one line
    because a blank line would make the page treat the rest as Markdown. A citation links to the
    text inside a source, because the browser then opens every closed section around that text.
    """
    items = []
    for source in sources:
        ref = int(source["ref"])
        anchor = source_anchor(message_index, ref)
        title = (f'<span class="source-ref">{ref}</span><span class="source-title">'
                 f'<b>{html.escape(source["policy"])}</b>'
                 f'<span class="source-section">{html.escape(source["section"])}</span></span>')
        text = re.sub(r"\s*\n\s*", "<br>", html.escape(source.get("text") or "").strip())
        body = (f'<blockquote id="{anchor}">{text}</blockquote>' if text
                else f'<div class="source-empty" id="{anchor}">No preview available.</div>')
        items.append(f'<details class="source-item"><summary>{title}</summary>{body}</details>')
    return f'<details class="sources"><summary>Sources ({len(sources)})</summary>{"".join(items)}</details>'


def error_html(text: str) -> str:
    return f'<div class="error-note" role="alert">{html.escape(text)}</div>'


def usable_history(messages: list[dict]) -> list[dict]:
    """Drop failed replies and the questions that caused them, so they can't affect follow-ups."""
    return [message for index, message in enumerate(messages)
            if not message.get("error")
            and not (index + 1 < len(messages) and messages[index + 1].get("error"))]
