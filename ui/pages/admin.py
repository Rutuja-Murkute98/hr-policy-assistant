"""The admin screen: HR can manage policies, watch indexing and see where the bot falls short.

It isn't linked from the chat screen. Open it by adding /admin to the chat screen's address.
The admin key is typed in each session and is never stored by this app. Roles come in Phase 4.
"""
from datetime import date

import requests
import streamlit as st
from ui_settings import api_url, tenant

API_URL = api_url()
TIMEOUT_SECONDS = 150  # long enough for a free hosted API and database to wake up
FILE_TYPES = ["pdf", "docx", "txt", "html", "htm", "md", "markdown"]
JOB_LABELS = {"queued": "Waiting", "running": "Indexing", "succeeded": "Done", "failed": "Failed"}


def call_api(method: str, path: str, **kwargs) -> tuple[object | None, str]:
    """Return (data, ""), or (None, a message explaining what went wrong)."""
    try:
        response = requests.request(method, f"{API_URL}{path}", timeout=TIMEOUT_SECONDS,
                                    headers={"X-Admin-Key": st.session_state.get("admin_key", "")}, **kwargs)
    except requests.RequestException:
        return None, "The helpdesk API is waking up or unavailable. Please try again in a minute."
    if response.ok:
        return response.json(), ""
    try:
        detail = response.json()["detail"]
        fallback = "That request wasn't valid. Check the details and try again."
        return None, detail if isinstance(detail, str) else fallback
    except (ValueError, KeyError):
        return None, f"Something went wrong (error {response.status_code}). Please try again."


def show_upload() -> None:
    st.caption("A file with the same name becomes a new version of that policy, and only the sections that "
               "changed use embedding quota. The old version is kept.")
    with st.form("upload_form", clear_on_submit=True):
        upload = st.file_uploader("Policy file", type=FILE_TYPES, max_upload_size=5)
        title = st.text_input("Title (optional)", placeholder="Taken from the file if left empty")
        categories = sorted({*tenant()["document_categories"].values(), tenant()["default_category"]})
        category = st.selectbox("Category (optional)", categories, index=None)
        version_label = st.text_input("Version label (optional)", placeholder="Such as 3.0")
        effective = st.date_input("Effective date (optional)", value=None, min_value=date(2000, 1, 1))
        uploaded_by = st.text_input("Your name", max_chars=100)
        if not st.form_submit_button("Upload", type="primary"):
            return
    if not upload or not uploaded_by.strip():
        st.error("Choose a file and enter your name.")
        return
    fields = {"title": title, "category": category or "", "version_label": version_label,
              "uploaded_by": uploaded_by.strip()}
    if effective:
        fields["effective_date"] = effective.isoformat()
    result, problem = call_api("POST", "/admin/documents", files={"file": (upload.name, upload.getvalue())},
                               data=fields)
    if problem:
        st.error(problem)
    elif result["status"] == "unchanged":
        st.info(f"{result['file']} hasn't changed, so nothing was indexed.")
    else:
        st.session_state.admin_message = f"Version {result['version']} of {result['file']} is being indexed."
        st.rerun()


def show_documents(documents: list[dict]) -> None:
    st.dataframe(documents, hide_index=True, use_container_width=True, column_config={
        "file": "File", "policy": "Policy", "chunks": "Sections", "version": "Version", "category": "Category",
        "version_label": "Label", "effective_date": "Effective", "uploaded_by": "Uploaded by"})
    if not documents:
        return
    chosen = st.selectbox("Policy", [document["file"] for document in documents], index=None,
                          placeholder="Choose a policy to see its versions or remove it")
    if not chosen:
        return
    versions, problem = call_api("GET", f"/admin/documents/{chosen}/versions")
    if problem:
        st.caption("No version history is recorded for this policy yet.")
    else:
        st.dataframe(versions, hide_index=True, use_container_width=True)
    confirmed = st.checkbox("The bot should stop answering from this policy")
    if st.button("Remove", disabled=not confirmed):
        result, problem = call_api("DELETE", f"/admin/documents/{chosen}")
        if problem:
            st.error(problem)
        else:
            st.session_state.admin_message = f"{result['file']} was removed. Its old versions are kept as history."
            st.rerun()


def show_jobs() -> None:
    jobs, problem = call_api("GET", "/admin/jobs")
    if problem:
        st.error(problem)
        return
    if not jobs:
        st.caption("No uploads yet.")
        return
    if st.button("Refresh", icon=":material/refresh:"):
        st.rerun()
    st.dataframe([{"File": job["file"], "Version": job["version"],
                   "Status": JOB_LABELS.get(job["status"], job["status"]),
                   "Embedded": f"{job['embedded']} of {job['to_embed']}", "Tries": job["attempts"],
                   "Problem": job["error"] or "", "By": job["created_by"]} for job in jobs],
                 hide_index=True, use_container_width=True)
    for job in (job for job in jobs if job["can_retry"]):
        if st.button(f"Retry {job['file']}", key=f"retry_{job['id']}"):
            _, problem = call_api("POST", f"/admin/jobs/{job['id']}/retry")
            if problem:
                st.error(problem)
            else:
                st.session_state.admin_message = f"Retrying {job['file']}."
                st.rerun()


def show_unanswered() -> None:
    rows, problem = call_api("GET", "/admin/unanswered")
    if problem:
        st.error(problem)
    elif not rows:
        st.caption("Nothing yet.")
    else:
        st.caption("Questions the bot couldn't answer, most asked first. These may show gaps in the policies.")
        st.dataframe(rows, hide_index=True, use_container_width=True,
                     column_config={"question": "Question", "asked": "Times asked", "last_asked": "Last asked"})


def show_feedback() -> None:
    summary, problem = call_api("GET", "/admin/feedback")
    if problem:
        st.error(problem)
        return
    left, right = st.columns(2)
    left.metric("Thumbs up", summary["thumbs_up"])
    right.metric("Thumbs down", summary["thumbs_down"])
    if summary["recent_negative"]:
        st.caption("Recent answers rated down")
        st.dataframe(summary["recent_negative"], hide_index=True, use_container_width=True)


st.set_page_config(page_title="HR Helpdesk admin", page_icon=":material/admin_panel_settings:")
st.title("Policy admin")

st.text_input("Admin key", type="password", key="admin_key")
if not st.session_state.get("admin_key"):
    st.info("Enter the admin key to manage the policies.")
    st.stop()

current, problem = call_api("GET", "/admin/documents")
if problem:
    st.error(problem)
    st.stop()

if "admin_message" in st.session_state:
    st.success(st.session_state.pop("admin_message"))

documents_tab, upload_tab, jobs_tab, unanswered_tab, feedback_tab = st.tabs(
    [f"Policies ({len(current)})", "Upload", "Indexing jobs", "Unanswered questions", "Feedback"])
with documents_tab:
    show_documents(current)
with upload_tab:
    show_upload()
with jobs_tab:
    show_jobs()
with unanswered_tab:
    show_unanswered()
with feedback_tab:
    show_feedback()
