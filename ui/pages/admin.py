"""The admin screen: HR can manage policies, watch indexing and see where the bot falls short.

It isn't linked from the chat screen. Open it by adding /admin to the chat screen's address.
With sign-in, it needs an HR admin account. In demo mode, the shared admin key is typed in each
session and is never stored by this app.
"""
import base64
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


def show_preview(upload) -> None:
    """How the file will be read, section by section, beside its first pages, so HR can check before uploading."""
    if upload is None:
        return
    if st.button("Preview how it's read", key="preview_upload", icon=":material/preview:"):
        preview, problem = call_api("POST", "/admin/documents/preview",
                                    files={"file": (upload.name, upload.getvalue())})
        if problem:
            st.error(problem)
            return
        st.session_state.upload_preview = {**preview, "upload": upload.name}
    preview = st.session_state.get("upload_preview")
    if not preview or preview["upload"] != upload.name:
        return
    short = sum(section["too_short"] for section in preview["sections"])
    st.caption(f"Read as {len(preview['sections'])} sections" + (f", {short} too short to be searched" if short else "")
               + ". Check the headings and tables match the file before uploading. Nothing is saved yet.")
    if preview["warnings"]:
        st.warning("Some of this text looks like instructions to an AI: " + ", ".join(preview["warnings"]) + ".")
    pages, sections = st.columns(2) if preview["pages"] else (None, st.container())
    if pages is not None:
        with pages:
            for number, page in enumerate(preview["pages"], start=1):
                st.image(base64.b64decode(page), caption=f"Page {number}")
    with sections:
        for section in preview["sections"]:
            with st.container(border=True):
                # The file's own words, shown as plain text: an upload can't add links or images to this page
                st.text(section["section"] + (" (too short to be searched)" if section["too_short"] else ""))
                st.text(section["text"][:1500])


def show_category_suggestion(upload) -> str | None:
    """Offer the model's suggestion for the chosen file, and return it so the form can start on it."""
    if upload is None:
        return None
    if st.button("Suggest a category", key="suggest_category", icon=":material/auto_awesome:"):
        suggestion, problem = call_api("POST", "/admin/documents/suggest-category",
                                       files={"file": (upload.name, upload.getvalue())})
        if problem:
            st.error(problem)
            return None
        st.session_state.category_suggestion = {**suggestion, "file": upload.name}
    suggestion = st.session_state.get("category_suggestion")
    if not suggestion or suggestion["file"] != upload.name:
        return None
    if suggestion["suggested"]:
        st.info(f"Suggested category: **{suggestion['category']}**. {suggestion['reason']} "
                "Change it below if it's wrong.")
    else:
        st.warning(suggestion["reason"])
    return suggestion["category"]


def show_upload() -> None:
    st.caption("A file with the same name becomes a new version of that policy, and only the sections that "
               "changed use embedding quota. The old version is kept.")
    # A new key empties the file box after an upload, as a form can't hold the file and a separate suggestion button
    upload = st.file_uploader("Policy file", type=FILE_TYPES, max_upload_size=5,
                              key=f"policy_file_{st.session_state.get('upload_round', 0)}")
    categories = ORGANISATION.get("categories") or sorted(
        {*ORGANISATION["document_categories"].values(), ORGANISATION["default_category"]})
    suggested = show_category_suggestion(upload)
    show_preview(upload)
    with st.form("upload_form", clear_on_submit=True):
        title = st.text_input("Title (optional)", placeholder="Taken from the file if left empty")
        category = st.selectbox("Category (optional)", categories,
                                index=categories.index(suggested) if suggested in categories else None)
        version_label = st.text_input("Version label (optional)", placeholder="Such as 3.0")
        effective = st.date_input("Effective date (optional)", value=None, min_value=date(2000, 1, 1))
        review = st.date_input("Next review (optional)", value=None, min_value=date(2000, 1, 1),
                               help="Read from the policy's \"Next Review:\" line if left empty")
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
    if review:
        fields["review_date"] = review.isoformat()
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
        st.session_state.upload_round = st.session_state.get("upload_round", 0) + 1
        st.session_state.pop("category_suggestion", None)
        st.rerun()


def show_documents(documents: list[dict]) -> None:
    overdue = [document["policy"] for document in documents if document.get("review_overdue")]
    if overdue:
        st.warning(f"{len(overdue)} {'policy is' if len(overdue) == 1 else 'policies are'} past their review date: "
                   + ", ".join(overdue) + ".")
    st.dataframe(documents, hide_index=True, use_container_width=True, column_config={
        "file": "File", "policy": "Policy", "chunks": "Sections", "version": "Version", "category": "Category",
        "version_label": "Label", "effective_date": "Effective", "uploaded_by": "Uploaded by",
        "review_date": "Next review", "review_overdue": st.column_config.CheckboxColumn("Overdue")})
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
        st.dataframe(versions, hide_index=True, use_container_width=True,
                     column_config={"what_changed": st.column_config.TextColumn("What changed", width="large")})
    confirmed = st.checkbox("The bot should stop answering from this policy")
    if st.button("Remove", disabled=not confirmed):
        result, problem = call_api("DELETE", f"/admin/documents/{chosen}")
        if problem:
            st.error(problem)
        else:
            st.session_state.admin_message = f"{result['file']} was removed. Its old versions are kept as history."
            st.rerun()


def show_reindex() -> None:
    """A re-index for a new embedding model, while it runs or if it failed. A finished one isn't news."""
    status, problem = call_api("GET", "/admin/reindex")
    run = (status or {}).get("run")
    if problem or not run or run["status"] == "succeeded":
        return
    done = f"{run['embedded']} of {run['total']} sections" if run["total"] else "starting"
    if run["status"] == "failed":
        st.error(f"Moving to the new search model ({run['to_model']}) stopped at {done}. Answers still use the old "
                 f"one, {run['from_model']}, so nothing is broken. {run['error'] or ''}")
        if st.button("Retry the move", key="retry_reindex"):
            _, problem = call_api("POST", "/admin/reindex/retry")
            if problem:
                st.error(problem)
            else:
                st.session_state.admin_message = "Retrying. It carries on from where it stopped."
                st.rerun()
    else:
        st.info(f"Moving the policies to the new search model ({run['to_model']}): {done}. Answers use the old "
                f"one, {run['from_model']}, until it's done.")


def show_jobs() -> None:
    show_reindex()
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


AUDIENCE_TYPES = {"permanent": "Permanent", "contract": "Contract", "intern": "Interns"}
AUDIENCE_PROBATION = {"": "Anyone", "on_probation": "Only on probation", "confirmed": "Only confirmed"}


def section_name(chunk_id: str) -> str:
    """A chunk id is like "Leave_Policy > 4.1. Earned Leave > 0". The middle is the section."""
    parts = chunk_id.split(" > ")
    return parts[1] if len(parts) > 2 else chunk_id


def show_audiences() -> None:
    summary, problem = call_api("GET", "/admin/audiences")
    if problem:
        st.error(problem)
        return
    sections = summary["sections"]
    st.caption("Answers prefer the rules that fit the person asking, such as their band or office. The model suggests "
               "who each section is for, and you can correct it. A section is never hidden because of this.")
    if summary["untagged"]:
        st.info(f"{summary['untagged']} sections haven't been checked yet.")
        if st.button("Check them now", key="tag_audiences", icon=":material/auto_awesome:",
                     help="Uses one model call per policy"):
            _, problem = call_api("POST", "/admin/audiences/tag")
            if problem:
                st.error(problem)
            else:
                st.session_state.admin_message = "Checking. Refresh in a minute or two to see them."
                st.rerun()
    limited = [row for row in sections if row["description"] != "Everyone"]
    st.dataframe([{"Policy": row["file"], "Section": section_name(row["chunk_id"]), "Applies to": row["description"],
                   "Checked by": "HR" if row["source"] == "hr" else "Model"} for row in limited],
                 hide_index=True, use_container_width=True)
    st.caption(f"{len(limited)} of {len(sections)} checked sections apply to some employees only. "
               "The rest are for everyone.")
    chosen = st.selectbox("Section to correct", [row["chunk_id"] for row in sections], index=None)
    if not chosen:
        return
    current = next(row for row in sections if row["chunk_id"] == chosen)["applies_to"]
    with st.form("audience_form"):
        typed = st.text_input("Bands", ", ".join(str(band) for band in current.get("bands", [])),
                              placeholder="Such as 1, 2, 3. Empty means every band")
        min_band = st.number_input("Or this band and above", 1, 20, value=current.get("min_band"), step=1,
                                   placeholder="Empty")
        places = st.text_input("Offices", ", ".join(current.get("locations", [])),
                               placeholder="Such as Pune. Empty means every office")
        kinds = st.multiselect("Employment", list(AUDIENCE_TYPES), default=current.get("employment_types", []),
                               format_func=AUDIENCE_TYPES.get, placeholder="Everyone")
        probation = st.selectbox("Probation", list(AUDIENCE_PROBATION), format_func=AUDIENCE_PROBATION.get,
                                 index=list(AUDIENCE_PROBATION).index(current.get("probation") or ""))
        if not st.form_submit_button("Save", type="primary"):
            return
    try:
        bands = [int(band) for band in typed.replace(" ", "").split(",") if band]
    except ValueError:
        st.error("Bands are numbers, separated by commas.")
        return
    body = {"bands": bands, "min_band": min_band,
            "locations": [place.strip() for place in places.split(",") if place.strip()],
            "employment_types": kinds, "probation": probation or None}
    _, problem = call_api("PUT", f"/admin/audiences/{chosen}", json=body)
    if problem:
        st.error(problem)
    else:
        st.session_state.admin_message = "Saved. The model won't change it."
        st.rerun()


SOURCE_KINDS = {"folder": "A folder on the server", "google_drive": "Google Drive", "sharepoint": "SharePoint"}
SOURCE_STATUS = {"never": "Not synced yet", "running": "Syncing", "ok": "Up to date", "failed": "Failed"}


def source_summary(summary: dict | None) -> str:
    if not summary:
        return ""
    parts = [f"{summary[key]} {label}" for key, label in (("added", "added"), ("updated", "updated"),
                                                          ("removed", "removed")) if summary.get(key)]
    if summary.get("failed"):
        parts.append(f"{len(summary['failed'])} couldn't be read")
    return ", ".join(parts) or "No changes"


def show_sources() -> None:
    listing, problem = call_api("GET", "/admin/sources")
    if problem:
        st.error(problem)
        return
    sources, kinds = listing["sources"], listing["available_kinds"]
    st.caption("Policies can sync from where HR keeps them. Only new or changed files are read, and a file removed "
               "there stops being answered from, unless you choose to keep it.")
    if sources:
        st.dataframe([{"Name": source["name"], "Kind": SOURCE_KINDS.get(source["kind"], source["kind"]),
                       "Every": f"{source['every_minutes']} min", "On": source["enabled"],
                       "Status": SOURCE_STATUS.get(source["status"], source["status"]),
                       "Last sync": source["last_synced_at"] or "", "Result": source_summary(source["last_summary"]),
                       "Problem": source["last_error"] or ""} for source in sources],
                     hide_index=True, use_container_width=True)
        chosen = st.selectbox("Source", sources, format_func=lambda source: source["name"], index=None)
        if chosen:
            show_source(chosen)
    if not kinds:
        st.info("This server can't read any sources yet. Its owner sets SOURCES_ROOT for folders, or the Google Drive "
                "or SharePoint settings.")
        return
    with st.form("add_source", clear_on_submit=True):
        st.subheader("Add a source")
        kind = st.selectbox("Kind", kinds, format_func=lambda value: SOURCE_KINDS.get(value, value))
        name = st.text_input("Name", placeholder="Such as HR shared drive", max_chars=100)
        location = st.text_input("Where", help="A folder: its path inside your organisation's sources folder. "
                                               "Google Drive: the folder's id, after /folders/ in its address. "
                                               "SharePoint: site id, library id and folder, separated by |.")
        every = st.number_input("Sync every (minutes)", 15, 10080, value=60, step=15)
        keep = st.toggle("Keep policies whose file is removed there")
        if not st.form_submit_button("Add", type="primary"):
            return
    parts = [part.strip() for part in location.split("|")]
    settings = ({"path": location.strip()} if kind == "folder" else {"folder_id": location.strip()}
                if kind == "google_drive" else {"site_id": parts[0], "drive_id": parts[1] if len(parts) > 1 else "",
                                                "folder": parts[2] if len(parts) > 2 else ""})
    _, problem = call_api("POST", "/admin/sources", json={"kind": kind, "name": name, "settings": settings,
                                                          "every_minutes": every, "keep_removed": keep})
    if problem:
        st.error(problem)
    else:
        st.session_state.admin_message = f"{name} was added. It syncs at its first time, or press Sync now."
        st.rerun()


def show_source(source: dict) -> None:
    for failure in (source["last_summary"] or {}).get("failed", []):
        st.warning(f"{failure['file']}: {failure['reason']}")
    sync, pause, remove = st.columns(3)
    if sync.button("Sync now", key=f"sync_{source['id']}", disabled=source["status"] == "running"):
        _, problem = call_api("POST", f"/admin/sources/{source['id']}/sync")
        if problem:
            st.error(problem)
        else:
            st.session_state.admin_message = f"Syncing {source['name']}. Refresh in a minute to see the result."
            st.rerun()
    if pause.button("Pause" if source["enabled"] else "Resume", key=f"pause_{source['id']}"):
        _, problem = call_api("PATCH", f"/admin/sources/{source['id']}", json={"enabled": not source["enabled"]})
        if problem:
            st.error(problem)
        else:
            st.rerun()
    if remove.button("Stop syncing", key=f"remove_{source['id']}", help="Its policies stay"):
        _, problem = call_api("DELETE", f"/admin/sources/{source['id']}")
        if problem:
            st.error(problem)
        else:
            st.session_state.admin_message = f"{source['name']} won't sync any more. Its policies are still there."
            st.rerun()
    files, problem = call_api("GET", f"/admin/sources/{source['id']}/files")
    if not problem and files:
        st.dataframe(files, hide_index=True, use_container_width=True)


CLASH_VIEWS = {"open": "To look at", "resolved": "Resolved", "dismissed": "Not a clash"}


def show_clashes() -> None:
    st.caption("Places where two policies, or two parts of one, give a different figure for the same thing, such "
               "as days of leave or an amount. Each upload is checked. Fix the policy that's wrong and upload it "
               "again, or mark the pair as not a clash. A pair is checked again only when one of its texts changes.")
    view = st.segmented_control("Show", list(CLASH_VIEWS), format_func=CLASH_VIEWS.get, default="open",
                                key="clash_view") or "open"
    clashes, problem = call_api("GET", f"/admin/clashes?status={view}")
    if problem:
        st.error(problem)
        return
    if not clashes:
        st.info("Nothing to look at." if view == "open" else "None yet.")
    for number, clash in enumerate(clashes):
        with st.container(border=True):
            st.markdown(f"**{clash['file_a']}** · {clash['section_a']}")
            st.markdown(f"**{clash['file_b']}** · {clash['section_b']}")
            st.write(clash["explanation"])
            if view != "open":
                st.caption(f"Marked by {clash['decided_by'] or 'someone'}")
                continue
            resolved, dismissed = st.columns(2)
            for column, status, label in ((resolved, "resolved", "Fixed"), (dismissed, "dismissed", "Not a clash")):
                if column.button(label, key=f"clash_{status}_{number}"):
                    _, problem = call_api("PATCH", "/admin/clashes", json={
                        "chunk_a": clash["chunk_a"], "chunk_b": clash["chunk_b"], "status": status})
                    if problem:
                        st.error(problem)
                    else:
                        st.rerun()
    if st.button("Check every policy again", help="Only pairs not yet checked, or whose text changed, go to the model"):
        _, problem = call_api("POST", "/admin/clashes/check")
        if problem:
            st.error(problem)
        else:
            st.session_state.admin_message = "Checking every policy. Refresh in a few minutes to see what it found."
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

(documents_tab, upload_tab, sources_tab, jobs_tab, clashes_tab, audiences_tab, unanswered_tab, feedback_tab, usage_tab,
 users_tab, keys_tab, settings_tab, audit_tab) = st.tabs([f"Policies ({len(current)})", "Upload", "Sources",
                                                          "Indexing jobs", "Clashes", "Who rules apply to",
                                                          "Unanswered questions", "Feedback", "Usage", "People",
                                                          "API keys", "Settings", "Audit log"])
with documents_tab:
    show_documents(current)
with upload_tab:
    show_upload()
with sources_tab:
    show_sources()
with jobs_tab:
    show_jobs()
with clashes_tab:
    show_clashes()
with audiences_tab:
    show_audiences()
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
