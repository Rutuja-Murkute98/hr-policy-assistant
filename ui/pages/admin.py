"""The admin screen: HR can manage policies, watch indexing and see where the bot falls short.

It isn't linked from the chat screen. Open it by adding /admin to the chat screen's address.
With sign-in, it needs an HR admin account. In demo mode, the shared admin key is typed in each
session and is never stored by this app.
"""
import json
from datetime import date

import requests
import streamlit as st
from sign_in import auth_token, bearer_headers, password_mode, require_sign_in, sign_out
from ui_settings import api_url, tenant

API_URL = api_url()
SESSION = require_sign_in(API_URL)  # None in demo mode
ORGANISATION = SESSION["organisation"] if SESSION else tenant()
TIMEOUT_SECONDS = 150  # long enough for a free hosted API and database to wake up
FILE_TYPES = ["pdf", "docx", "txt", "html", "htm", "md", "markdown"]
JOB_LABELS = {"queued": "Waiting", "running": "Indexing", "succeeded": "Done", "failed": "Failed"}


def request_headers() -> dict[str, str]:
    if SESSION:
        return bearer_headers(auth_token())
    return {"X-Admin-Key": st.session_state.get("admin_key", "")}


def call_api(method: str, path: str, **kwargs) -> tuple[object | None, str]:
    """Return (data, ""), or (None, a message explaining what went wrong)."""
    try:
        response = requests.request(method, f"{API_URL}{path}", timeout=TIMEOUT_SECONDS,
                                    headers=request_headers(), **kwargs)
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
        categories = ORGANISATION.get("categories") or sorted(
            {*ORGANISATION["document_categories"].values(), ORGANISATION["default_category"]})
        category = st.selectbox("Category (optional)", categories, index=None)
        version_label = st.text_input("Version label (optional)", placeholder="Such as 3.0")
        effective = st.date_input("Effective date (optional)", value=None, min_value=date(2000, 1, 1))
        uploaded_by = SESSION["email"] if SESSION else st.text_input("Your name", max_chars=100)
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
        message = f"Version {result['version']} of {result['file']} is being indexed."
        if result.get("too_short"):
            message += f" These sections are too short to be searched: {', '.join(result['too_short'])}."
        if result.get("warnings"):
            message += (" Please check this file: some of its text looks like instructions to an AI "
                        f"({', '.join(result['warnings'])}). The helpdesk won't follow them, but someone may "
                        "have added them on purpose.")
        st.session_state.admin_message = message
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
    show_test_candidates()


def show_test_candidates() -> None:
    """Hand unanswered questions and answers rated down to whoever looks after the evaluation."""
    st.divider()
    st.caption("Unanswered questions and answers rated down can become test questions, so the evaluation checks "
               "what people really ask. Personal details were already masked when they were saved.")
    if not st.button("Prepare test questions"):
        return
    candidates, problem = call_api("GET", "/admin/test-candidates")
    if problem:
        st.error(problem)
    elif not candidates:
        st.caption("Nothing to add yet.")
    else:
        st.download_button(f"Download {len(candidates)} test questions", json.dumps(candidates, indent=1),
                           file_name="test_candidates.json", mime="application/json")


def show_users() -> None:
    users, problem = call_api("GET", "/admin/users")
    if problem:
        st.error(problem)
        return
    st.dataframe(users, hide_index=True, use_container_width=True, column_order=(
        "email", "employee_id", "display_name", "role", "active"))
    with st.form("add_user", clear_on_submit=True):
        email = st.text_input("Work email")
        employee_id = st.text_input("Employee ID (optional)", help="Links this person to their HR records")
        role = st.selectbox("Role", ["employee", "hr_admin"])
        if st.form_submit_button("Add person", type="primary"):
            _, problem = call_api("POST", "/admin/users", json={"email": email.strip(), "role": role,
                                                                "employee_id": employee_id.strip() or None})
            if problem:
                st.error(problem)
            else:
                next_step = ("Choose them below and create a set-up code, so they can set a password."
                             if password_mode() else "They can now sign in.")
                st.session_state.admin_message = f"{email.strip()} was added. {next_step}"
                st.rerun()
    show_person(users)


def show_person(users: list[dict]) -> None:
    """Link someone to their HR records, or let them set a password."""
    people = {user["email"]: user for user in users}
    chosen = st.selectbox("Person", list(people), index=None, placeholder="Choose someone to link or help sign in")
    if not chosen:
        return
    person = people[chosen]
    with st.form("link_employee"):
        employee_id = st.text_input("Employee ID", value=person["employee_id"] or "",
                                    help="Links this person to their HR records, for leave and payslip questions")
        if st.form_submit_button("Save") and employee_id.strip():
            _, problem = call_api("PATCH", f"/admin/users/{person['id']}", json={"employee_id": employee_id.strip()})
            if problem:
                st.error(problem)
            else:
                st.session_state.admin_message = f"{chosen} is now linked to {employee_id.strip()}."
                st.rerun()
    if password_mode() and st.button("Create a set-up code", key="setup_code", icon=":material/key:",
                                     help="For someone you added, or who has forgotten their password"):
        created, problem = call_api("POST", f"/admin/users/{person['id']}/setup-code")
        if problem:
            st.error(problem)
        else:
            st.code(created["code"], language=None)
            st.warning(f"Give this code to {chosen} privately. It's shown only once, works once, and expires in "
                       f"{created['expires_in_hours']} hours. They enter it under \"Forgot your password\" "
                       "on the sign-in page.")


def show_api_keys() -> None:
    keys, problem = call_api("GET", "/admin/api-keys")
    if problem:
        st.error(problem)
        return
    st.caption("A key lets an integration, such as a Slack bot, use the helpdesk for this organisation.")
    st.dataframe(keys, hide_index=True, use_container_width=True, column_order=(
        "name", "key_prefix", "created_at", "last_used_at", "revoked"))
    with st.form("add_key", clear_on_submit=True):
        name = st.text_input("Name", placeholder="Such as Slack bot", max_chars=100)
        if st.form_submit_button("Create key", type="primary") and name.strip():
            created, problem = call_api("POST", "/admin/api-keys", json={"name": name.strip()})
            if problem:
                st.error(problem)
            else:
                st.code(created["key"], language=None)
                st.warning("Copy this key now. It won't be shown again.")
    active = {key["name"]: key["id"] for key in keys if not key["revoked"]}
    chosen = st.selectbox("Key to revoke", list(active), index=None) if active else None
    if chosen and st.button(f"Revoke {chosen}", key="revoke_key"):
        _, problem = call_api("DELETE", f"/admin/api-keys/{active[chosen]}")
        if problem:
            st.error(problem)
        else:
            st.session_state.admin_message = f"{chosen} no longer works."
            st.rerun()


STEP_NAMES = {"cached_answer": "Answering a repeated question", "understand": "Reading the message",
              "confirm_out_of_scope": "Checking an off-topic message", "retrieve": "Searching the policies",
              "rewrite_query": "Rewording a weak search",
              "generate": "Writing the answer", "select_tool": "Choosing a self-service action"}


def show_usage() -> None:
    days = st.selectbox("Period", [1, 7, 30, 90], index=1, format_func=lambda value: f"Last {value} days")
    usage, problem = call_api("GET", f"/admin/metrics?days={days}")
    if problem:
        st.error(problem)
        return
    speed = usage["latency_ms"]
    first, second, third, fourth = st.columns(4)
    first.metric("Questions", usage["requests"])
    second.metric("Failed", usage["failures"])
    third.metric("Refused", usage["refusals"])
    fourth.metric("Typical answer", f"{(speed['total_p50'] or 0) / 1000:.1f} s")
    first, second, third, fourth = st.columns(4)
    first.metric("Slowest 5% from", f"{(speed['total_p95'] or 0) / 1000:.1f} s")
    second.metric("Tokens", f"{usage['input_tokens'] + usage['output_tokens']:,}")
    third.metric("Estimated cost", f"${usage['estimated_cost_usd']:.2f}")
    fourth.metric("Tool failures", f"{usage['tool_failures']} of {usage['tool_calls']}")
    if usage.get("steps_ms"):
        st.caption("Where the time goes, in seconds per step. The slowest step is the one to work on.")
        st.dataframe([{"Step": STEP_NAMES.get(step, step), "Typical": round((times["p50"] or 0) / 1000, 1),
                       "Slowest 5% from": round((times["p95"] or 0) / 1000, 1), "Answers": times["count"]}
                      for step, times in usage["steps_ms"].items()], hide_index=True, use_container_width=True)
    st.caption("Counts and times only: no questions or answers are stored here. The cost uses the prices in "
               "the server's settings, and is 0 on a free tier.")


def show_audit() -> None:
    events, problem = call_api("GET", "/admin/audit")
    if problem:
        st.error(problem)
    elif not events:
        st.caption("Nothing yet.")
    else:
        st.dataframe(events, hide_index=True, use_container_width=True, column_order=(
            "created_at", "actor", "action", "target", "details"))


TONES = {"neutral": "Plain", "friendly": "Friendly", "formal": "Formal"}
LENGTHS = {"short": "Short", "normal": "Normal", "detailed": "Detailed"}
SETTING_NAMES = {
    "company_name": "Company name", "legal_name": "Legal name", "hr_email": "HR email", "product_name": "Product name",
    "logo_initials": "Logo initials", "categories": "Categories", "default_category": "Default category",
    "chat_model": "Answer model", "temperature": "Temperature", "min_confidence": "Evidence needed to answer",
    "min_chunk_confidence": "Evidence needed per excerpt", "grade_min_confidence": "Reword searches below",
    "out_of_scope_override_confidence": "Answer off-topic-looking questions above",
    "skip_understand_confidence": "Skip reading clear questions above", "retrieve_k": "Excerpts per answer",
    "refusal_wording": "Refusal wording", "tone": "Tone", "answer_length": "Answer length",
    "self_service_enabled": "Self-service", "cache_answers": "Reuse answers to repeated questions",
    "skip_understand_when_clear": "Skip reading clear first questions",
}
SCORE_HELP = ("How far the best match must stand out from the rest of the policies. Higher refuses more often, "
              "lower risks answering from weak matches.")


def settings_changes(new: dict, values: dict) -> dict:
    """Only what the admin changed. An emptied refusal wording goes back to the standard one."""
    changes = {name: value for name, value in new.items() if value != values[name]}
    if "refusal_wording" in changes and not changes["refusal_wording"].strip():
        changes["refusal_wording"] = None
    return changes


def score_input(column, name: str, values: dict, **options) -> float:
    return column.number_input(SETTING_NAMES[name], 0.0, 10.0, value=float(values[name]), step=0.1, **options)


def show_settings() -> None:
    current, problem = call_api("GET", "/admin/settings")
    if problem:
        st.error(problem)
        return
    values, changed = current["effective"], current["changed"]
    st.caption("These apply to this organisation only, from the next question. Anything not changed here follows "
               "the server's settings. Every change is in the audit log.")
    if changed:
        st.caption("Changed here: " + ", ".join(SETTING_NAMES.get(name, name) for name in changed))
    models = current["allowed_chat_models"]
    new: dict = {}
    with st.form("settings_form"):
        st.subheader("Branding")
        left, right = st.columns(2)
        new["company_name"] = left.text_input("Company name", values["company_name"], max_chars=100)
        new["legal_name"] = right.text_input("Legal name", values["legal_name"], max_chars=150)
        new["product_name"] = left.text_input("Product name", values["product_name"], max_chars=60)
        new["logo_initials"] = right.text_input("Logo initials", values["logo_initials"], max_chars=3)
        new["hr_email"] = st.text_input("HR email", values["hr_email"], max_chars=200)

        st.subheader("Answers")
        left, right = st.columns(2)
        new["tone"] = left.selectbox("Tone", list(TONES), index=list(TONES).index(values["tone"]),
                                     format_func=TONES.get)
        new["answer_length"] = right.selectbox("Answer length", list(LENGTHS),
                                               index=list(LENGTHS).index(values["answer_length"]),
                                               format_func=LENGTHS.get)
        new["refusal_wording"] = st.text_area(
            "Refusal wording", values["refusal_wording"], max_chars=400,
            placeholder="Leave empty for the standard wording, which gives the HR email.",
            help="What the bot says when the policies don't answer a question. {hr_email} and {company_name} "
                 "are filled in.")

        st.subheader("Topics")
        typed = st.text_input("Categories", ", ".join(values["categories"]),
                              help="Lowercase words, separated by commas, such as leave, payroll. New uploads "
                                   "are filed under one of these. Policies already filed keep their category.")
        new["categories"] = [category.strip().lower() for category in typed.split(",") if category.strip()]
        new["default_category"] = st.text_input("Default category", values["default_category"], max_chars=30)

        st.subheader("Model")
        left, right = st.columns(2)
        chosen = values["chat_model"]
        new["chat_model"] = left.selectbox("Answer model", models,
                                           index=models.index(chosen) if chosen in models else 0,
                                           help="Only the models the server's owner allowed are listed.")
        new["temperature"] = right.number_input("Temperature", 0.0, 2.0, value=values["temperature"], step=0.1,
                                                placeholder="The model's default")

        st.subheader("Search")
        left, right = st.columns(2)
        new["min_confidence"] = score_input(left, "min_confidence", values, help=SCORE_HELP)
        new["min_chunk_confidence"] = score_input(right, "min_chunk_confidence", values)
        new["grade_min_confidence"] = score_input(left, "grade_min_confidence", values)
        new["out_of_scope_override_confidence"] = score_input(right, "out_of_scope_override_confidence", values)
        new["skip_understand_confidence"] = score_input(left, "skip_understand_confidence", values)
        new["retrieve_k"] = right.number_input(SETTING_NAMES["retrieve_k"], 1, int(current["max_excerpts"]),
                                               value=int(values["retrieve_k"]), step=1)
        st.caption("The server's values were measured on the test questions. Run the evaluation after changing them.")

        st.subheader("Switches")
        new["self_service_enabled"] = st.toggle(
            SETTING_NAMES["self_service_enabled"], values["self_service_enabled"],
            help="Leave balances, payslips, leave requests and tickets, each needing the employee's approval.")
        new["cache_answers"] = st.toggle(SETTING_NAMES["cache_answers"], values["cache_answers"])
        new["skip_understand_when_clear"] = st.toggle(SETTING_NAMES["skip_understand_when_clear"],
                                                      values["skip_understand_when_clear"])
        saved = st.form_submit_button("Save", type="primary")
    if saved:
        changes = settings_changes(new, values)
        if not changes:
            st.info("Nothing was changed.")
        else:
            _, problem = call_api("PATCH", "/admin/settings", json=changes)
            if problem:
                st.error(problem)
            else:
                st.session_state.admin_message = "Saved. The next question uses the new settings."
                st.rerun()
    if changed:
        reset = st.multiselect("Put back to the server's setting", list(changed),
                               format_func=lambda name: SETTING_NAMES.get(name, name))
        if reset and st.button("Put back", key="reset_settings"):
            _, problem = call_api("PATCH", "/admin/settings", json={name: None for name in reset})
            if problem:
                st.error(problem)
            else:
                st.session_state.admin_message = "Those settings now follow the server's again."
                st.rerun()


st.set_page_config(page_title="HR Helpdesk admin", page_icon=":material/admin_panel_settings:")
st.markdown("[← Back to chat](/)")
st.title("Policy admin")

if SESSION:
    st.button(f"Sign out ({SESSION['email']})", icon=":material/logout:", on_click=sign_out)
    if SESSION["role"] == "employee":
        st.error("This page is for HR admins. Please ask your HR team if you need access.")
        st.stop()
else:
    st.text_input("Admin key", type="password", key="admin_key")
    if not st.session_state.get("admin_key"):
        st.info("Enter the admin key to manage the policies.")
        st.stop()

current, problem = call_api("GET", "/admin/documents")
if problem:
    st.error(problem)
    st.stop()

# Always here, even when empty: if the tabs moved up or down the page, Streamlit would reopen the first tab
with st.container():
    if "admin_message" in st.session_state:
        st.success(st.session_state.pop("admin_message"))

(documents_tab, upload_tab, jobs_tab, unanswered_tab, feedback_tab, usage_tab, users_tab, keys_tab, settings_tab,
 audit_tab) = st.tabs([f"Policies ({len(current)})", "Upload", "Indexing jobs", "Unanswered questions", "Feedback",
                       "Usage", "People", "API keys", "Settings", "Audit log"])
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
with usage_tab:
    show_usage()
with users_tab:
    show_users()
with keys_tab:
    show_api_keys()
with settings_tab:
    show_settings()
with audit_tab:
    show_audit()
