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
- The initial tests paired one desktop participant with an automatically invoked
  local participant. A later three-participant test is described below.
- Ordinary Gemini chat did not expose the tools in the tested setup. Antigravity
  mode did; a fresh chat was needed after MCP discovery.
- The selected desktop model belongs to the host chat. The bridge does not report
  Codex's exact underlying model identity.
- Five local-model replies in the Codex test appended `PASS` after a substantive
  answer. This is a model-output formatting issue; exact `PASS` remains the
  dispatcher's signal for skipping a turn.
- Elapsed times include waiting, inference and client tool handling. The test
  sizes and contexts differ.

## Three-participant follow-up

An isolated candidate completed nine accepted replies: three from native Codex,
three from native Gemini Antigravity and three from an existing local endpoint.
All nine turn IDs were unique. The room stopped at its configured turn limit.
Elapsed time from the first accepted reply to the last was 158.14 seconds;
this excludes client setup and is not an inference-speed benchmark.

The first attempt exposed a defect: rejoining an already enabled participant
cleared its pending turn. Joining is now idempotent, with a regression check that
rejoins during a live pending turn and then posts to the same turn successfully.
Two MCP participants without a local model also pass the dispatcher test.

Codex was started using the app's native task tool; Gemini was operated through
its window. No manual commands were needed between the nine successful replies.
This proves joint participation, **not one-button startup from UZEL**. The window
launcher remains experimental; a prepared Codex deep link alone does not send
the participation prompt. Continuous 30-minute operation and a live two-desktop
room without the local participant remain unverified.

## One-button follow-up

The complete UI flow was exercised in an isolated candidate: create a room with
the Codex + Gemini preset, select a live-catalog Codex model, enter the initial
question and click **Запустить участие**. No participation prompts were entered
manually in either client.

- Codex used `gpt-6-luna` through a private-stdio App Server session.
- Gemini used its Antigravity window, opened by a macOS Accessibility helper.
- No local model participated. Each client posted three accepted replies;
  all six turn IDs were unique. The room stopped at its turn limit.
- Elapsed time between the first and last accepted reply was 81.26 seconds.
  Setup is excluded; this is not a model-speed benchmark.
- A second launch of the same room produced one accepted reply per client and
  stopped at a two-turn limit. Old invitations were replaced by the launcher.
- No direct child processes remained under the candidate manager after shutdown.
  The original desktop app process remained alive throughout these checks.

The test exposed and fixed two integration issues: Gemini cached a server with
the same name across workspaces, and a model misread `waiting_external` as an
instruction to keep waiting. Gemini now receives a unique server name per launch;
the tool contract explicitly returns `participant.next_action` (`reply`, `wait`,
`stop`). The expected room ID is checked before joining.

Codex's internal desktop `create_thread` tool is not exposed as a general-purpose
API to UZEL. The launcher uses the documented App Server protocol instead.
It does not use `codex exec` or control the Codex window. Gemini still needs
macOS Accessibility permission and a free, logged-in Antigravity window.
Continuous 30-minute operation remains unverified.

## Main-service follow-up

The background main service was also exercised after granting Accessibility to
the dedicated **UZEL Gemini** app. Directly executing its binary inherited the
background Python process's permission context. The launcher now uses macOS
LaunchServices and private input/output files; Python needs no Accessibility
grant. A permission probe runs before client chats are created.

An initial attempt paused during Gemini's transition to a new chat. Two subsequent
launches succeeded, including a repeat started with **Запустить участие** in the
main UI. Each successful run accepted one reply from each desktop participant and
stopped at its two-turn limit, without a local model. All accepted turn IDs were
unique. No direct children remained under the manager after completion.
The 128-test Python manager suite passed. Window automation can still be affected
by client UI timing; this short trial is not a long-duration stability guarantee.

## Three-participant discussion follow-up

Two further main-service discussions each accepted 12 replies: four from Codex,
four from Gemini and four from a local endpoint model. All 24 accepted turn IDs
were unique; both discussions stopped at their configured turn limit. A live
`room_search` invocation in one discussion shared source snippets with the room.
One of these launches needed a retry after Gemini's new-chat input was not ready.

These are transport and turn-taking checks, not an assessment of answer accuracy.
Participants sometimes contradicted themselves or confused attribution; multiple
models do not by themselves verify an answer. The public screenshots use new
synthetic fixtures rather than the conversations from these checks.

This report contains aggregate receipts only. Credentials, connection files,
private prompts, conversation exports and machine-specific paths are excluded.
