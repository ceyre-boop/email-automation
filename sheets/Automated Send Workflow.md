Automated Send Workflow
Purpose:
This workflow only sends an existing SOP-created draft and completes post-send labeling.
This workflow must not create drafts, generate email content, rewrite email content, classify emails, or perform SOP matching.
1. Locate Existing Draft
Locate the draft already created by the SOP workflow for the original inbound email/thread.
Rules:
Do not create a new draft.
Do not modify the draft.
Do not generate new email content.
Do not re-run response matching.
Do not modify labels before sending.
Do not modify the original inbound email before sending.
2. Reply-To Handling
Before sending the draft, check the matched talent inbox against the Reply-To Routing List in the SOP.
Set the Reply-To field based on the matched inbox only.
Reply-To routing:
If the matched inbox is listed under talent-mgmt@taboost.me:
Set Reply-To to: talent-mgmt@taboost.me
If the matched inbox is listed under creator-mgmt@taboost.me:
Set Reply-To to: creator-mgmt@taboost.me
If the matched inbox is listed under partnerships@taboost.me:
Set Reply-To to: partnerships@taboost.me
If the matched inbox is not listed in the Reply-To Routing List:
Leave Reply-To blank/default
Important:
Match based on the inbound talent inbox, not the sender’s email address.
Do not guess the Reply-To address.
Do not apply the same Reply-To address to all drafts.
Do not add the Reply-To email to the email body.
Do not change the approved response wording.
Do not change the sender/from address.
Do not send if the Reply-To field conflicts with the SOP Reply-To Routing List.
3. Send Gate
Send only if:
exactly one matching draft exists
the draft is associated with the original inbound email/thread
the draft was created from an approved SOP response
the draft body does not contain classification text, internal reasons, metadata, or SOP notes
the Reply-To field matches the SOP Reply-To Routing List for the matched talent inbox, or is blank/default if the inbox is not listed
If no matching draft exists, more than one matching draft exists, or validation fails:
stop
do not send
do not create another draft
do not modify labels
add INBOX label back to the original inbound email if it was removed during draft creation
4. Send Existing Draft
If the send gate passes:
send the existing draft
do not create, rewrite, regenerate, or edit any email content
5. Post-Send Handling
Only after the draft is successfully sent:
Apply label: A Initial Response
Do not create or apply any other label
Do not add the INBOX label back
6. Failed / Rejected Draft Handling
If the draft is not sent for any reason:
do not apply A Initial Response
add INBOX label back to the original inbound email
do not apply any other label
leave the email available for human review
