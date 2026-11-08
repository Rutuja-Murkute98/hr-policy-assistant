# India's DPDP Act: notes for this project

These are engineering notes on the Digital Personal Data Protection Act, 2023, and the DPDP Rules, 2025, written to
guide design. **They aren't legal advice, and nobody with legal training has reviewed them.** Before any real employee
data is used, a lawyer should check them against the current text, including when each part of the Rules takes effect.

**The project today holds sample data only: fake policies and fake employees. Real HR documents must not be used on
Gemini's free tier.** Google's free tier terms allow what's sent to be used to improve its products, which can't be
squared with holding employees' personal data.

## Who's who

| Term in the Act | Here |
|---|---|
| Data Principal | An employee using the helpdesk |
| Data Fiduciary | The employer: the organisation that decides why the data is used |
| Data Processor | Whoever runs this software for the employer, plus sub-processors: the model provider, Neon, Render, Streamlit Cloud and the sign-in provider |

## What the helpdesk holds about a person

| Data | Where | How long | Protection |
|---|---|---|---|
| Work email, name, employee id, role | `users` | Until an admin removes it | Row-Level Security |
| Chat questions and answers | `messages`, `conversations` | `CONVERSATION_RETENTION_DAYS`, 90 by default | Masked before storing. The person can delete it |
| Questions the bot couldn't answer | `unanswered_questions` | Same as chats | Masked. No user is stored |
| Ratings and comments | `feedback` | Removed with the answer they refer to | Masked |
| Requests waiting for approval | `pending_actions`, checkpoint tables | Until decided, or 24 hours | Masked question. Finished runs are deleted straight away |
| Tool use | `tool_audit_log` | `AUDIT_RETENTION_DAYS`, 365 by default | Masked arguments, and the app can't change entries |
| Accounts | `users`, `user_credentials`, `user_sessions` | Until removed, and sessions until they expire | Passwords and session tokens are only stored as hashes. The app's tenant role can't read them |
| Sign-ins and admin changes | `audit_events` | `AUDIT_RETENTION_DAYS`, 365 by default | Masked, and the app can't change entries |
| Profile details and photo | `users`, `user_profiles` | Until removed, or the account is | Given by the person, who can change or remove them |
| Whether someone's chats are saved | `user_preferences` | Until removed | With saving off, questions and answers aren't stored, except actions such as leave requests |
| Rate limit counts | `rate_limit_hits` | Minutes | Holds a user id or client id, and a time |
| Mock leave and tickets | `hr_leave_requests`, `hr_tickets` | Not removed: a real HR system would set this | Masked question. Fake data only |

## What the Act asks for, and where this stands

| Requirement | Status | Detail |
|---|---|---|
| Notice saying what's collected and why (s.5) | **Not built** | The employer has to write this. The chat screen has no notice or link yet |
| A lawful basis (s.4, s.6, s.7) | **Employer's decision** | Section 7 allows some employment uses without consent, so a lawyer should decide if it covers a helpdesk chat. Nothing in the app records consent |
| Use only for the stated purpose (s.6) | Partial | The code doesn't use chats for anything else, but nothing stops an employer's admin reading feedback or unanswered questions |
| Security safeguards (s.8(5)) | Partial | Tenant isolation, role checks, masking, hashed keys and audit logs are built and tested. Encryption in transit and at rest depend on the hosting, and haven't been checked here |
| Telling the Board and people about a breach (s.8(6)) | **Not built** | There's no breach process or alerting. Phase 5 adds monitoring, but the process is the employer's |
| Erase data no longer needed (s.8(7)) | Implemented and tested | Retention periods for chats and audit entries, and a person can delete their history |
| Right to access (s.11) | Partial | A person can list and read their chats. They can't get everything in one export, such as audit entries |
| Right to correct and erase (s.12) | Partial | They can delete chats. Audit entries with their id stay until the retention period, as a record of what happened. A lawyer should confirm this is acceptable. An admin can deactivate an account, but there's no route that removes it entirely |
| Grievance contact (s.13) | **Not built** | The HR email is shown, but it isn't named as a grievance contact |
| Children's data (s.9) | Doesn't apply | Employees are adults. Interns under 18 would need checking |
| Sending data outside India (s.16) | **Needs checking** | Model providers, Neon and Render may process it elsewhere. The government can restrict countries by notification. Choose regions deliberately, and check the current list |
| Significant Data Fiduciary duties (s.10) | Probably doesn't apply | Only if the government names the organisation. A lawyer should confirm |

## What the masking does and doesn't do

Questions are masked before anything else happens to them, so the model provider, stored chats, tickets and logs only
see the masked text. It covers email, Indian phone numbers, PAN, Aadhaar, IFSC, card numbers and labelled bank accounts.

It **doesn't** catch names, free-text addresses, health details written in words, or numbers in unusual formats, and
it can occasionally mask something harmless. Policy text in the documents themselves isn't masked, so a policy that
lists someone's phone number sends it to the model. Treat masking as reducing harm, not as making personal data safe to
send to any provider.

## Before using real employee data

1. Get legal advice on these notes and the lawful basis.
2. Use a paid model tier with terms that rule out training on your data, or a model you host. Check the region.
3. Set `AUTH_MODE=oidc`. Demo mode lets anyone with the shared key name any employee.
4. Add a privacy notice and grievance contact to the chat screen.
5. Sign data processing agreements with each sub-processor.
6. Set retention periods with HR and legal, and agree what's kept for audit.
7. Write a breach response process, with named owners.
8. Replace the mock HR system, and decide what its retention should be.
9. Add a data export and account removal route if legal advice says they're needed.
10. Check encryption and backups on the hosting you pick.
