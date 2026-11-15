"""Tag who each stored policy section applies to, for the first organisation. See docs/DECISIONS.md, D55.

    python -m pipeline.tag_audiences

The answer model reads one document per call, and only sections with no tag for their current text, so running it again
costs nothing for what's done. HR's corrections are never changed. Uploads are tagged on their own after indexing.
"""
from app.rag.store import run_migrations
from app.workflow import create_workflow

PAUSE_SECONDS = 5  # the free tier allows 15 calls a minute


def main() -> None:
    run_migrations()  # as the API does on start, so the tag table exists
    workflow = create_workflow()
    try:
        before = workflow.knowledge.audiences()["untagged"]
        print(f"{before} sections have no tag yet", flush=True)
        tagged = workflow.knowledge.tag_audiences(workflow.llm, pause_seconds=PAUSE_SECONDS)
        summary = workflow.knowledge.audiences()
        limited = [row for row in summary["sections"] if row["description"] != "Everyone"]
        print(f"Tagged {tagged}. {len(limited)} sections apply to some employees only, "
              f"{summary['untagged']} still have no tag")
    finally:
        workflow.close()


if __name__ == "__main__":
    main()
