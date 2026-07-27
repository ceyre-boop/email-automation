Automated Send Workflow
Purpose:
This workflow only sends an existing SOP-created draft and completes post-send labeling.

This workflow must not create drafts, generate email content, rewrite email content, classify emails, or perform SOP matching.

1. Locate Existing Draft
   Locate the draft already created by the SOP workflow for the original inbound email/thread.

Rules:

● Do not create a new draft.
● Do not modify the draft.
● Do not generate new email content.
● Do not re-run response matching.
● Do not modify labels before sending.
● Do not modify the original inbound email before sending. 2. Reply-To Handling
Before sending the draft, check whether the matched talent/inbox is listed in the Reply-To Routing List.

If the matched talent/inbox is listed:

● Set Reply-To to: talent-mgmt@taboost.me

If the matched talent/inbox is not listed:

● Leave Reply-To blank/default

Important:

● Do not add the Reply-To email to the email body.
● Do not change the approved response wording.
● Do not change the sender/from address.
● Do not apply Reply-To to all drafts automatically. 3. Send Gate
Send only if:

● exactly one matching draft exists
● the draft is associated with the original inbound email/thread
● the draft was created from an approved SOP response
● the draft body does not contain classification text, internal reasons, metadata, or SOP notes
● the Reply-To field is either correctly set based on the Reply-To Routing List or left blank/default when not required
If no matching draft exists, more than one matching draft exists, or validation fails:

● stop
● do not send
● do not create another draft
● do not modify labels
● add INBOX label back to the original inbound email if it was removed during draft creation 4. Send Existing Draft
If the send gate passes:

● send the existing draft
● do not create, rewrite, regenerate, or edit any email content 5. Post-Send Handling
Only after the draft is successfully sent:

● Apply label: A Initial Response
● Do not create or apply any other label
● Do not add the INBOX label back 6. Failed / Rejected Draft Handling
If the draft is not sent for any reason:

● do not apply A Initial Response
● add INBOX label back to the original inbound email
● do not apply any other label
● leave the email available for human review
