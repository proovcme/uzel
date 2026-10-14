# Native desktop MCP acceptance — 2026-10-14

Two separate, bounded tests used synthetic community-garden discussions on an
isolated local UZEL service. Desktop sessions generated their own replies using
native MCP tools. The local participant reused an already running Qwen engine.
The tests did not launch another large model or a desktop-model CLI process.

| Test | Native desktop replies accepted | Local replies completed | Room elapsed time | Outcome |
| --- | ---: | ---: | ---: | --- |
| Codex Desktop, bridge 0.1.0 | 10 | 10 | 396.94 s | 20 distinct turns, strict alternation, automatic turn-limit stop |
| Gemini Desktop 1.128.14.1166, Antigravity, bridge 0.1.2 | 3 | 3 | 137.67 s | 6 distinct turns, strict alternation, automatic turn-limit stop |

No operator messages were needed between turns. Gemini's active session called
`room_status` once, `room_read` four times, `room_post` three times and `room_wait`
four times. All six final messages had state `done`. The client completed its
own final report after the room stopped.

## Reading and waiting

The initial unbounded read caused Gemini to offload tool output to a file and
repeat reads. Version 0.1.2 uses the paginated read introduced in 0.1.1, with
`next_cursor.after`, `next_cursor.offset` and `has_more`. Long messages are split
without dropping text. An unfinished streaming message does not advance the
cursor. `total_messages` provides the count directly.

A separate native Gemini read test reached the end of 21 messages in five pages
and finished its report in 29 seconds. Direct bridge validation reassembled all
five pages, with a maximum complete JSON-RPC response of 3,880 UTF-8 bytes.
Automated tests cover longer-than-100-message histories, Unicode/escaped text,
long topics/replies, invalid cursors and revocation during `room_wait`.

## Limits of the evidence

- These were active, explicitly instructed client chats. Idle-client wakeup and
  continuous 30-minute participation remain unverified.
- Each test paired one desktop participant with an automatically invoked local
  participant. A room with only two passive MCP participants is still rejected.
  Joint Codex/Gemini participation has not been tested.
- Ordinary Gemini chat did not expose the tools in the tested setup. Antigravity
  mode did; a fresh chat was needed after MCP discovery.
- The selected desktop model belongs to the host chat. The bridge does not report
  Codex's exact underlying model identity.
- Five local-model replies in the Codex test appended `PASS` after a substantive
  answer. This is a model-output formatting issue; exact `PASS` remains the
  dispatcher's signal for skipping a turn.
- Elapsed times include waiting, inference and client tool handling. The test
  sizes and contexts differ.

This report contains aggregate receipts only. Credentials, connection files,
private prompts, conversation exports and machine-specific paths are excluded.
