<div align="center">

# UZEL

### Local model management on Apple Silicon

Local models, images, and shared AI conversations in one workbench.
Bring Codex, Gemini and a local model into a room: give them a question,
follow the exchange, and join in whenever you want.

![Apple Silicon](https://img.shields.io/badge/Apple_Silicon-MLX-b5dc78?labelColor=171c18)
![Python](https://img.shields.io/badge/Python-3.12%2B-b5dc78?labelColor=171c18)
![License](https://img.shields.io/badge/License-MIT-b5dc78?labelColor=171c18)

[Quick start](#quick-start) · [AI Rooms](#ai-rooms) · [Screenshots](#screenshots) · [Русский](README.ru.md)

</div>

![Three participants in a shared AI room](docs/screenshots/rooms.jpg)

## What's inside

| Component | What it does |
| --- | --- |
| **[Manager](manager/README.md)** | Finds local models and oMLX / MLX LM engines. Starts a selected model, streams chats, saves conversations and drafts, displays image progress, and connects LES document retrieval. |
| **[Image Kit](image-kit/README.md)** | Qwen Image 2.1 with a native 4-bit MLX loader. Interactive, direct and batch generation; optional denoising cache. |
| **[AI Rooms](docs/rooms.md)** | Shared conversations with Codex, Gemini, local models and scoped MCP participants. Model selection, desktop launch, web sources, pause/resume and Markdown export. |
| **[Launcher](run.py)** | `manager`, `image` and `check` from the toolkit root. |

Manager and Image Kit keep separate code and tests. Image generation retains
the native Q4 loader and sampling settings, with exact conditioning reuse and
resident series. Model weights and text inference engines are installed separately.

## Quick start

Requires **macOS on Apple Silicon** and **Python 3.12+**.

**Local model chat also requires a separately installed
[MLX LM](https://github.com/ml-explore/mlx-lm) or
[oMLX](https://github.com/jundot/omlx) engine and downloaded text-model weights.**
The installation below includes Manager and Image Kit; text engines and model
weights are not included. Desktop-only rooms need signed-in Codex and Gemini
clients instead of a local text model.

```sh
git clone https://github.com/proovcme/uzel.git
cd uzel
python3.12 -m venv .venv
.venv/bin/python -m pip install ./image-kit
.venv/bin/python run.py
```

Open **http://127.0.0.1:1924/**. Select a model, click **Start**, and write a prompt.
The composer switches between chat and image controls when you select a model.
For a shared discussion, open **Комнаты**. If you only need chat and rooms,
the `pip install ./image-kit` step is optional.

For optional **web search with sources** in chat, install `.venv/bin/python -m pip install -r manager/requirements-web.txt` and enable it in the **⋯ menu beside the message field**. See [setup and privacy boundaries](docs/web-search.md).

Manager discovers installed text engines and model directories from runtime
settings, environment variables and standard cache locations.
[Discovery and extra model roots](docs/model-discovery.md).

Images need a complete `mlx-community/Qwen-Image-2.1-MLX-4bit` snapshot.
`run.py image` opens Image Kit and can download missing weights; Manager never
downloads or starts a model implicitly. [Image setup and license](image-kit/README.md#model-and-license).

## Screenshots

### More room for the answer

One compact header keeps the selected model and its live status visible.
**Чаты** opens saved conversations; model controls and diagnostics stay out of the
reading area. Search, document retrieval and parameters open from the composer menu.

![Local chat, streaming and saved history](docs/screenshots/chat.jpg)

<details>
<summary>Mobile / Мобильный чат</summary>

![Mobile chat](docs/screenshots/chat-mobile.jpg)

</details>

Replies arrive as a stream. Prompt processing, reasoning and answering have
visible states. Conversations, partial replies, system prompts, parameters and
drafts are saved locally; **New Chat** keeps earlier conversations in history.

### Generation you can follow

![Image generation stages and step progress](docs/screenshots/progress.jpg)

Follow generation stages, steps and elapsed time. Download completed images as
PNG, reuse saved parameters or move gallery items to recoverable local trash.
[Prompts, limits and series](image-kit/docs/manager-workflow.md).

<details>
<summary>Gallery</summary>

![Public showcase gallery](docs/screenshots/gallery.jpg)

</details>

All UI screenshots use an isolated demo service, synthetic room/chat/document/progress
states and public Image Kit showcase images. They illustrate the current interface;
they are not performance measurements or proof of a live LES deployment. No private
history, local model identities, credentials or machine paths are included.

## AI Rooms

Open **Комнаты**, choose **Codex + Gemini**, select a Codex model and write a first
message. **Запустить участие** creates the participation sessions: Codex uses App
Server, Gemini uses its Antigravity window on macOS. A local model is optional.
Models take turns as equals; you can intervene, pause or resume. Add a local
participant through the running engine or an explicit local endpoint. Enable web
search to let MCP participants share source links and snippets with the room.
Conversations stay on disk with bounded model context and Markdown export.
Desktop participants send room context through their signed-in services;
local storage does not make those participants offline.

![Create a room and select a Codex model](docs/screenshots/rooms-create.jpg)

Codex uses its existing ChatGPT sign-in and a model chosen from the live App
Server catalog. Gemini requires Antigravity mode, Apple Command Line Tools, and
Accessibility permission for the dedicated **UZEL Gemini** helper. Desktop
launch is experimental: leave the Gemini window free during setup; a failed
launch pauses the room and may require a retry. Short two-client and
three-participant sessions passed; continuous 30-minute operation is unverified.

The optional [local plugin](plugins/uzel-rooms/) connects an already active
Codex/ChatGPT Work session through scoped MCP. MCP access alone does not wake
an idle model. Room controls do not remove desktop-client usage limits.
[Setup, requirements and integration boundaries](docs/rooms.md).

<details>
<summary>Rooms on mobile</summary>

![Mobile room with participant status and readable replies](docs/screenshots/rooms-mobile.jpg)

</details>

## Documents through LES

![Connect a document set from LES](docs/screenshots/les-rag.jpg)

Open **⋯ → Документы LES**, connect a running [LES RAG](https://github.com/proovcme/LES)
API, select a document set and enable it for chat. A model with function-call support
chooses search queries and reads retrieved context. Answers stream normally with
collapsible source excerpts and original-document links using `[D1]` citations.
An unavailable index or unsupported model produces an explicit error.

Files, OCR and indexing stay in LES. Manager connects to its API; optional HTTP
MCP needs a compatible gateway. It does not start LES or copy the corpus.
[Setup, API contract and privacy boundaries](docs/les-rag.md).

## From an idea to an image series

Choose Qwen Image and open **⋯ → Настройки изображения**. Optionally expand
the prompt with an installed text model, review it, and set **В серии** to 1–20.
Manager generates the images sequentially with different seeds; completed
images stay in the gallery.

[Prompt expansion, series and cancellation — Image Kit guide](image-kit/docs/manager-workflow.md).

## A few commands

```sh
.venv/bin/python run.py                 # model workbench
.venv/bin/python run.py image           # interactive image CLI
.venv/bin/python run.py image direct --help # direct generation options
.venv/bin/python manager/cli.py status
.venv/bin/python run.py check           # both Python suites + SSE parser
```

`check` runs both Python suites and the JavaScript checks, with no model downloads
or image benchmark. Install Image Kit dependencies first. [CI](.github/workflows/checks.yml)
tests Manager on Linux and image contracts on macOS; Metal tests skip without a GPU.
[Validation scope and recovery checks](docs/reliability.md).

## Local by design

- One managed heavy workload at a time. Active requests and external-process
  conflicts block unsafe switching.
- After a service restart, only recorded processes with matching PID, birth time,
  command and process group are recovered.
- Deleting a model previews its folder and requires typing its name. The model
  moves to recoverable Trash, including its HF blobs, refs and snapshots.
- User data stays in `~/.local/share/mlx-manager`. Chats and image prompts are
  stored there intentionally; they are excluded from Git.

The control plane listens on loopback and has no authentication. Use it on a
trusted local machine; do not expose its port to a network. Direct launches
outside the manager can bypass its resource lock. The current chat composer
accepts text; image attachments for VLMs are not implemented yet.

**Vanilla image generation** is the reference. `balanced` caching is optional
and can change an image; the paired measurements and limits are documented in
[Image Kit](image-kit/README.md#measured-off-vs-balanced).

## Documentation

[Manager setup & API](manager/README.md) · [Image generation & batch](image-kit/README.md) ·
[Русская документация](README.ru.md) · [MIT license](LICENSE)

UZEL was previously named MLX Manager. Existing data directories and
`MLX_MANAGER_*` settings remain compatible; new settings may use `UZEL_*`.
