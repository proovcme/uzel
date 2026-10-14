# Installation and recovery checks

## Rooms publication gate, 2026-10-14

A clean export of the staged release passed 129 Manager Python tests, 60 Image
Kit tests, all five JavaScript regression suites and syntax checks. The Gemini
Swift helper compiled separately without launching UI automation. Image tests
used the exported source, excluding unrelated local acceleration experiments.

Secret scanning passed for the staged changes and all 25 pre-release commits.
The exported text was also checked for private machine paths and model aliases;
local documentation links and plugin/bridge consistency passed. Ten refreshed
UI screenshots use isolated synthetic fixtures, with no EXIF metadata.

The desktop launcher remains experimental. Short live two- and three-participant
checks are documented [separately](benchmarks/desktop-mcp-2026-10-14.md); they do
not establish long-duration stability or the factual correctness of replies.

## Earlier installation checks

Validated on Apple Silicon with Python 3.12. This is a fresh virtual environment
and isolated user-data check on an existing Mac, not a test on a second physical
Mac or a factory-reset machine.

## Installation

- Created a new virtual environment without system site packages.
- Installed `./image-kit` with pip build isolation and dependency resolution.
- `pip check` reported no broken requirements.
- Ran the toolkit from a separate copy without private configuration, history,
  model weights or experimental probes.
- Started the manager with an empty home/cache/data directory and a separate
  loopback port. The UI opened, reported an empty catalog and free memory, and
  did not launch inference or download weights.
- The initial installation check passed 48 manager tests and 57 image-engine tests.
  JavaScript syntax and SSE parser checks passed too. Node is a development-test
  dependency; the browser UI requires no frontend build.

Text inference engines are installed separately. Image dependencies are pinned
at MLX 0.32.2 and MFLUX 0.20.0; no model weights are bundled.

## Failure and cancellation

Tests exercise real small subprocesses and local HTTP servers rather than GPU
models. They cover:

- An owned process ignoring both interrupt and terminate is killed and reaped.
  Recovered processes still require matching identity before any signal.
- A missing text-runtime executable reports failure. A later launch can retry,
  and a failed launch releases the manager's memory reservation when no workload
  remains.
- Chat cancellation interrupts a stalled stream and a request waiting for
  response headers. Its active-request count returns to zero; the next chat can
  start. Browser disconnect closes the upstream connection as well.
- Cancellation arriving before stream registration prevents inference from
  starting for that request identifier.
- A cancelled image is not reported as successful even if the worker exits zero
  and leaves an output file.
- Completed resident-series images remain available, previous watchers cannot
  overwrite a later job, and model release completes before terminal success.
- Restart recovery retains completed-series counts and marks the remaining queue
  interrupted, without automatically starting more work.
- Non-finite decoder output cannot replace an existing PNG or publish a new one.
  Finite output is saved atomically.

The queue is one sequential image series, not a scheduler for independent users
or parallel GPU workloads. A cancelled worker gets ten seconds after interrupt,
then five seconds after terminate, then kill. Session release first allows thirty
seconds for normal cleanup. Completed images and saved partial chat replies stay
in local history.

These changes do not alter weights, precision, seed, scheduler or denoising steps.
Published numerical parity and timing evidence remains in
[the acceleration report](acceleration.md). No new speedup percentage is claimed
from these lifecycle tests.

## Web-search validation

The optional search feature adds ten manager tests, bringing that suite to 58. Tests cover provider failure without a fabricated answer, unsafe source URLs, preserved history turns, worker cancellation and deadlines, streaming metadata and non-streaming responses. SSE parser checks cover search metadata as well as byte fragmentation and UTF-8. A live query returned five sources and a completed local-model answer with numbered references. An isolated UI replay of that live trace verified clickable sources, saved history and toggle restoration after reload.

## Publication gate, 2026-10-04

The current suites pass 68 manager and 59 image-engine tests on the local Apple M4,
plus JS syntax, SSE parsing, history navigation and streaming-rendering checks.
A clean source export without private configuration also passed. Delayed metric
collection and catalog discovery tests verify that chat completion can acquire the
control lock. A model-free browser fixture checked missing-model history, mobile
overflow and sequential requests with retained conversation turns. New chat
screenshots contain neutral fixture data and no EXIF metadata.

GitHub Actions now runs the manager suite on Linux and the image suite on macOS.
The first M1 run exposed bitwise drift in conditioning reuse. Unvalidated GPUs now
execute the original joint forward, with strict equality assertions retained.
Metal tests explicitly skip only when there is no Metal device; local M4 and M1 CI
otherwise exercise the hardware-specific optimized or original path.
