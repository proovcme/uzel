# UZEL

[Русская документация](README.ru.md)

A local workbench for MLX models on Apple Silicon: discover installed models,
start one, chat with streaming, or generate images with visible progress.
The control plane uses Python's standard library and listens on loopback only.
Models and inference engines remain separate installations.

## Run

Requirements: macOS on Apple Silicon, Python 3.12+, and a locally installed
[oMLX](https://github.com/jundot/omlx) or
[MLX LM](https://github.com/ml-explore/mlx-lm). The manager discovers executables
on PATH, in Homebrew, and in existing `uv tool` environments. It never downloads
models or starts one automatically at login.

```sh
python3.12 server.py
```

Open **http://127.0.0.1:1924/**. Pick a model, select Auto/oMLX/MLX LM, and click
Start. Text models use chat; Qwen Image switches to image controls. Only one
heavy workload is managed at a time. Models with vision configurations require
oMLX; the current composer accepts text prompts, not image attachments.

For an optional per-user login service, stop a manually running manager first:

```sh
python3.12 install_service.py
```

The generated LaunchAgent uses the current checkout and interpreter; no machine
paths are committed. Existing LaunchAgents are never overwritten.

## Everyday work

- **Chats:** streaming text, a reasoning disclosure when the engine emits it,
  prompt-processing/thinking/answering states, elapsed time, and interruption.
  New Chat keeps previous conversations in the history selector.
  Stop and browser disconnect close the upstream socket, including while the
  backend is processing the prompt before sending response headers.
- **Persistence:** chats, partial replies, drafts, system prompts and parameters
  are saved locally. An explicit saving/error indicator and retry button expose
  failures. Concurrent tabs cannot silently overwrite a newer saved revision.
- **Images:** preparation, encoder/transformer loading, denoising step/total,
  cache counters, decoding, elapsed time, and cancellation. Completed images
  appear in a gallery with PNG download and parameter reuse. Older images
  without recorded prompts remain visible; their parameters cannot be recovered.
  Cancellation escalates from interrupt to terminate and kill if the owned worker
  does not exit. A decoding result containing NaN or infinity fails without saving
  a PNG. Error details appear in the image activity panel.
- **Recovery:** after a service restart, the manager can reattach to its recorded
  model or image worker only when PID, birth time, command and process group
  still match. It reacquires the heavy-workload lock. Unrecognized processes
  are reported as conflicts and are never stopped by the manager. A chat stream
  interrupted by a page or service restart retains its last saved partial reply;
  it does not resume token generation automatically.
- **Delete:** preview the exact folder and size, type its name, then move it to
  same-volume Trash. HF cache deletion includes blobs, refs and all snapshots of
  that model. Active jobs and heavy-lock reservations block deletion. Restore
  by moving the folder back to its original location. oMLX KV caches remain;
  space is freed when the user empties Trash.

Local data lives in `~/.local/share/mlx-manager`: private SQLite workspace,
worker state, logs and `outputs/`. Conversations and image prompts are stored
there by design. Data stays on this computer; it is not part of this repository.
Back up this directory together with any custom configuration. The service
has no authentication and is intended for a trusted, single-user local machine.
Do not expose its port to a network.

## Models and configuration

Model locations are read from oMLX settings (including a relocated app data directory), MLX/oMLX LaunchAgents and running-server model arguments, Hugging Face environment settings, and `MODEL_ROOTS`. Standard `~/models` and `~/.lmstudio/models` folders are fallbacks. See [model discovery](../docs/model-discovery.md). Incomplete shard sets and embedding models are excluded; symlink aliases share one identity. Scanning remains bounded to model directories.

Create an ignored `config.local.json` in this checkout for extra roots or an
image engine installation. Values may use `~`:

```json
{
  "MODEL_ROOTS": ["~/extra-models"],
  "IMAGE_ROOT": "~/tools/mlx-image-kit",
  "IMAGE_PYTHON": "~/tools/mlx-image-kit/.venv/bin/python"
}
```

Image generation uses the existing
[MLX Image Kit](https://github.com/proovcme/mlx-image-kit) checkout and its Python
environment. The default is the bundled `../image-kit` directory, using its own virtual environment or the toolkit root `.venv`.
A complete cached `mlx-community/Qwen-Image-2.1-MLX-4bit` snapshot is required.
The tested image integration is Image Kit 0.5.0; loading, cache, sampling and
numerical behavior are delegated unchanged to that engine.
`IMAGE_CACHE` can override the HF model repository directory.

Path overrides also support `MLX_MANAGER_<NAME>` environment variables, such
as `MLX_MANAGER_DATA_ROOT` and `MLX_MANAGER_IMAGE_ROOT`. Environment values take
precedence over the local JSON file. `MLX_MANAGER_MODEL_ROOTS` accepts a JSON array of directory paths. `MLX_MANAGER_OMLX_EXECUTABLE` and `MLX_MANAGER_MLX_EXECUTABLE` can select runtime executables when they are not on `PATH`.

Selected text models run through the managed local inference server. It exposes
the OpenAI-compatible `/v1/models` and `/v1/chat/completions` routes.

## CLI and checks

```sh
python3.12 cli.py status
python3.12 cli.py mode image
python3.12 cli.py image 'A blue ceramic cup' --width 512 --height 512 --steps 4
python3.12 cli.py job
python3.12 cli.py cancel
python3.12 cli.py mode idle
python3.12 -m unittest discover -s tests -v
node --check static/app.js
node tests/test_stream_parser.js
```

There is no frontend build step. Node is only needed for the stream-parser test.
The control plane imports no MLX libraries. Direct inference launches outside
this manager can bypass its file lock; conflict detection is a guard, not OS
resource isolation. Interrupting a text stream closes the client connection;
how promptly inference stops depends on the selected engine.

GitHub Actions is enabled in [checks.yml](../.github/workflows/checks.yml). Manager tests run on Linux; image contracts run on macOS. Metal parity requires a GPU and is explicitly skipped on hosted runners without one. Full parity tests run locally on Apple Silicon before publishing.

## From an idea to an image series

Choose Qwen Image and enable **Улучшать промпт перед генерацией** (expand before generating).
Select an installed text model; a small Qwen3-4B is a practical starting point.
**Улучшить промпт** previews a rewrite without rendering. You can edit the resulting
prompt or restore your original idea. The automatic mode rewrites once, unloads the
text model, then generates images sequentially. No weights are downloaded.

Set **В серии** to 1–20. Each image uses the same prompt and settings, with seeds
`seed`, `seed + 1`, and so on (wrapping at 32 bits). The UI shows the image number
and its generation steps. Stop cancels the current image and all remaining images;
completed images stay in the gallery. Refreshing the page retains control. Restarting
the service interrupts the pending queue; it does not automatically launch more images.
The series status shows completed and remaining images, and retains the completed
count after cancellation, failure or interruption.

The compact local instructions follow Qwen's
[documented prompt rewrite approach](https://github.com/QwenLM/Qwen-Image-2.1/tree/main/prompt_rewrite):
a description of the finished frame, composition, materials and lighting; requested
lettering keeps its original script. This uses your chosen general text model,
not the dedicated PE checkpoint. A rewrite adds creative details and can alter the
result; review it when exact fidelity matters. Inference settings remain under your control.
Original and expanded prompts stay in local history. Failed or truncated rewrites
stop the workflow instead of being passed to the image model.

Optional external services use a [neutral, disabled-by-default adapter](../docs/external-chat.md). Memory diagnostics refresh independently of workload controls; their age is reported in the status API. Stored conversations remain readable after a model is removed.

### LES documents

Open **⋯ beside the message field → Документы LES** to connect a running LES API,
choose a document set and enable retrieval. The chat model selects search queries
and reads source context; references collapse beneath the answer. A model with
tool support is required. Files and indexing stay in LES.
See [connection setup and MCP transport limits](../docs/les-rag.md).

UZEL was previously named MLX Manager. Existing data directories and
`MLX_MANAGER_*` settings remain compatible; new settings may use `UZEL_*`.

## AI Rooms

The **Комнаты** workspace adds persistent equal-participant discussions, live
responses, time/turn limits, cancellation, Markdown export and scoped stdio MCP.
It reuses the selected text runtime and does not start subscription CLIs.
[Setup and limitations](../docs/rooms.md).
