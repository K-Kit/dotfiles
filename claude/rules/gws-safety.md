# GWS & Email Safety

Proactive guidance for Gmail/Drive/Calendar/Tasks via `gws` CLI or MCP. PreToolUse hooks (`block_gws_delete.sh`, `block_email_send.sh`) enforce the hard limits below only once `wire_harness_hooks.py --apply` has run — until then this file is the sole guard, so follow it proactively.

## Never Delete, Only Trash

Deletions across Google Workspace are irreversible. Use trash/archive (`messages trash`, `threads trash`, Drive trash) — never `delete`, `batchDelete`, `emptyTrash`, or `clear`. If something must be permanently gone, tell the user to do it via the Google Workspace UI.

## Never Send Email, Only Draft

Emails are irreversible once sent. Create drafts (`gws gmail +send --draft`, MCP `create_draft`) and let the user review and send manually — even if they say "send it." Never call `+send`/`+reply`/`+reply-all`/`+forward` without `--draft`, and never send an existing draft programmatically.

## Draft Formatting

Use `contentType: "text/html"` for Gmail drafts (MCP `create_draft`) — plain text loses line breaks when edited in Gmail's compose window. `<div>` per line, `<br>` between paragraphs; no inline `<br>` within a paragraph.
