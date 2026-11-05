"""The admin screen: HR can add, replace and remove policies without a developer.

It isn't linked from the chat screen. Open it by adding /admin to the chat screen's address.
The admin key is typed in each session and is never stored by this app.
"""
import requests
import streamlit as st
from ui_settings import api_url

API_URL = api_url()
TIMEOUT_SECONDS = 150  # long enough for a free hosted API and database to wake up


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
        return None, detail if isinstance(detail, str) else "That request wasn't valid. Check the file and try again."
    except (ValueError, KeyError):
        return None, f"Something went wrong (error {response.status_code}). Please try again."


def describe(result: dict) -> str:
    return (f"{result['file']}: {result['added']} added, {result['updated']} updated, "
            f"{result['removed']} removed, {result['unchanged']} unchanged.")


st.set_page_config(page_title="HR Helpdesk admin", page_icon=":material/admin_panel_settings:")
st.title("Policy admin")
st.caption("Changes take effect in the chat as soon as they finish.")

st.text_input("Admin key", type="password", key="admin_key")
if not st.session_state.get("admin_key"):
    st.info("Enter the admin key to manage the policies.")
    st.stop()

documents, problem = call_api("GET", "/admin/documents")
if problem:
    st.error(problem)
    st.stop()

if "admin_message" in st.session_state:
    st.success(st.session_state.pop("admin_message"))

st.subheader("Add or replace a policy")
st.caption("Upload a Markdown file that starts with a title, such as `# Leave Policy`. A file with "
           "the same name replaces the old policy, and only the sections that changed are embedded.")
upload = st.file_uploader("Policy file", type=["md"], max_upload_size=1)
if upload and st.button("Upload", type="primary"):
    try:
        content = upload.getvalue().decode("utf-8")
    except UnicodeDecodeError:
        st.error("The file must be saved as UTF-8 text.")
    else:
        with st.spinner("Updating the knowledge base..."):
            result, problem = call_api("POST", "/admin/documents", json={"filename": upload.name, "content": content})
        if problem:
            st.error(problem)
        else:
            st.session_state.admin_message = describe(result)
            st.rerun()

st.subheader(f"Policies in the knowledge base ({len(documents)})")
st.dataframe(documents, column_config={"file": "File", "policy": "Policy", "chunks": "Sections"},
             hide_index=True, use_container_width=True)

if documents:
    st.subheader("Remove a policy")
    to_remove = st.selectbox("Policy to remove", [document["file"] for document in documents], index=None)
    confirmed = st.checkbox("The bot should stop answering from this policy", disabled=not to_remove)
    if st.button("Remove", disabled=not (to_remove and confirmed)):
        result, problem = call_api("DELETE", f"/admin/documents/{to_remove}")
        if problem:
            st.error(problem)
        else:
            st.session_state.admin_message = describe(result)
            st.rerun()
