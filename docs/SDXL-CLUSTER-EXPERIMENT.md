# Experimental four-runner SDXL: results and release gate

The normal `.draw` / `.imagegen` service is unchanged. This experiment is not a
24/7 service, does not deliver to Telegram, and is not a replacement for the
existing generator. `FourWorkerSDXL` distributes overlapping latent tiles to one
local and three authenticated remote predictors. It is approximate tiled
inference, not model sharding or an exact full-frame UNet.

## Controlled results

GitHub Actions run `41819920341` generated the same public lighthouse prompt and
seed at 768×768, with four Euler/SGM-uniform steps and CFG 1. On one warm
coordinator, overlap 8 took 78.1 s, overlap 16 took 75.1 s, and the full-frame
single worker took 90.1 s. Both distributed images differed substantially from
the same-seed reference (mean absolute RGB difference 35.91 and 32.77 out of
255, respectively). That difference is **not** a perceptual quality metric.
The saved synthetic images showed repeated lighthouses and horizons: overlap 16
reduced some repetition but did not eliminate it. The reference image did not
have that repeated scene. Results are not a claim of visual equivalence.

In run `41826005417`, a coarse global prediction mixed into **every** step took
114.1 s, versus 135.1 s single-worker; its image was visibly blurred and still
had repetition. Limiting that correction to the first step took 93.1 s and
retained more detail, but the synthetic image still showed repeated lighthouses
and horizon structure. It is not a validated quality fix. The different runs
also demonstrate substantial runner variability: run `41821054697` recorded
69.1 s, 75.1 s and 303.5 s for the same three modes. These are individual
samples, not stable guarantees or independent confidence intervals.

## Full-frame anchor and better tile context

The newer experimental mode performs the **first denoising step on the full
frame**, then coordinates all four runners on the remaining three steps. This is
not four-way sharding of every step. GitHub Actions run `41831019971` compared
it with same-seed single-worker images. For a lighthouse, the anchored image
no longer repeated the scene; it took 78.1 s versus 69.0 s single-worker. For a
portrait, it took 72.0 s versus 69.0 s, but a ghosted second face remained.

Two further experiments improved those two samples:

- Run `41831766188` encoded each tile's SDXL crop coordinates, retaining the
  full-frame anchor. The inspected lighthouse and portrait no longer showed the
  prominent duplication. Their times were 114.1 s vs 141.1 s and 108.1 s vs
  138.1 s, respectively, on that run's coordinator.
- Run `41833849828` used the full-frame anchor and a wider overlap instead of
  crop metadata. At overlap 32 it took 129.1 s vs 144.1 s for the lighthouse,
  and 120.1 s vs 135.2 s for the portrait; both inspected thumbnails appeared
  coherent. Overlap 24 for the portrait took 114.1 s. RGB differences were
  lower than the original tiled version, but are not a perceptual score.

The multi-prompt runs `41840414228` (crop coordinates, overlap 16) and
`41840895066` (overlap 24, no crop coordinates) each tested three more same-seed
cases against a warm full-frame reference: three people at a table, a hand
holding an apple, and a cat. In the crop run, distributed versus single times
were 108.1 vs 120.1 s, 93.0 vs 120.1 s, and 93.3 vs 120.1 s respectively.
In the wide-overlap run, the corresponding times were 126.1 vs 138.3 s, 114.1
vs 135.1 s, and 111.1 vs 135.1 s. At 256-pixel preview size, the generated
subjects remained recognisable and major duplicates were not apparent, though
compositions and details changed and these are not professional quality reviews.

These are individual matched-run samples on variable shared runners, not a
quality guarantee or robust speed estimate. The coordinator's ComfyUI process
peaked at 14.39–14.55 GiB RSS in these tests; worker processes peaked around
14 GiB, leaving little margin on a 16 GB machine. The image model and tiled
method can still make anatomical or other mistakes; five prompts are insufficient
to establish general reliability.

One stress run, `41836644947`, failed when a worker treated a transient GitHub
504 while polling rendezvous state as fatal. The harness now retries bounded
transient HTTP/network failures; its recovery is exercised against an actual
local HTTP server. The failed render is not counted as a quality result.

## Exact operation sharding and CPU precision

Exact full-context attention was also tested separately by splitting queries
across authenticated hosted runners while sharing the complete keys and values.
The outputs matched local attention within `2e-5`, but network overhead made
this slower even with persistent HTTPS. In run `41852393364`, 48×48 attention
was 0.064 s locally versus 1.418 s across four runners; 24×24 was 0.010 s
versus 1.123 s. These are synthetic operator timings, not whole-image times.
This exact sharding has **not** been integrated into the service.

A real 768px render (`41857076387`) recorded 137.20 s in the UNet, of which
18.07 s was attention math across 560 calls, excluding Q/K/V projections.
There were 40 full 48×48 self-attention calls (13.26 s) and 240 full 24×24
self-attention calls (3.91 s). The cold profiled render took 198.2 s; its
image was not compared against an unprofiled image. On these measurements,
networking each attention call is unlikely to improve end-to-end latency.

A separate synthetic 320-channel convolution (`41856218029`) measured 0.0142 s
FP32 versus 0.0040 s BF16 on a runner advertising native BF16 instructions.
That is not an image benchmark. ComfyUI normally avoids BF16 CPU computation,
even when BF16 UNet weights are requested, so the opt-in precision experiment
requires native instruction flags and verifies there is no FP32 manual cast.
In three native-BF16 runner jobs (`41859564033`), same-seed 768px warmed
renders took 96.1, 134.2 and 202.1 s in FP32 versus 34.0, 34.0 and 58.0 s in
BF16 respectively. The sampler was explicitly checked not to have been cached;
FP32 and BF16 weights used separate ComfyUI processes on each same host. The
BF16 UNet was confirmed to use no FP32 manual cast. Each pair had an RGB MAE
of 1.12/255. The lighthouse previews looked very similar at 256px, but that is
one prompt/seed and not a perceptual-quality guarantee. Peak process RSS was
13.59 GiB in BF16 and 14.41–14.58 GiB in FP32. A runner without native BF16
instructions cannot use this optimization; it must retain a safe fallback.
The experimental extension is not installed in production. Broader image and
lifecycle testing, including both bots, is required before deployment.

## Worker loss

Run `41821054697` deliberately stopped a remote worker after both tiled renders.
With experimental `SDXL_FALLBACK_LOCAL=1`, the coordinator discarded the
incomplete tile prediction, switched to full-frame local inference for the
remaining steps, and returned a valid synthetic PNG in 96.1 s. The coordinator
stays degraded for that patched model until rebuilt. This prevents partial tile
results advancing a scheduler; it does not prove production restart, queue,
network-partition, 24/7 handover or Telegram delivery behavior. The default is
still to fail the experiment rather than silently enable fallback.

## Experimental single-runner service and `.fastdraw`

A separate, opt-in service workflow (`.github/workflows/service-sdxl-experimental.yml`)
starts one runner on a marker push (`[telegram-sdxl-service]`), serves
DreamShaper XL Lightning at 4 Euler/SGM-uniform steps, CFG 1, 768×768, and
publishes `.services/sdxl-experimental.json` for about an hour. It is a
different runner from the chat bots and a different gate from `.draw`.

`.fastdraw <prompt>` is a hidden, owner-only command registered by
`havoc/features/experimental_draw.py`. It reads that discovery file, submits a
graph that is checked byte for byte by `scripts/sdxl_experimental_gate.py`
before ComfyUI sees it, polls `/history`, downloads the PNG through the same
gate and sends it to the chat. It holds at most two jobs and clears its own
entry from the queue when it finishes. It never changes `.draw`, and when the
experiment is offline, expired or starting it replies that it is unavailable
and points back at `.draw`.

This is still experimental: one model, one resolution, one scheduler, one
runner, and a short lifetime. It is not a 24/7 service, not a quality
guarantee and not a production switch.

## Access controls

Worker tunnels expose only the authenticated prediction route, not ComfyUI.
The separate experimental coordinator gateway verifies GitHub Actions OIDC
identity scoped to the repository, branch and two account workflows. It strips
credentials before forwarding and restricts POSTs to a fixed distributed SDXL
graph or deletion of specific queued prompt IDs; it rejects arbitrary ComfyUI
nodes. Redirects are refused when carrying worker credentials or OIDC requests.
These components have hosted HTTP preflight coverage but are not wired into the
live bots or deployed service.

## Before any production switch

- Resolve full-frame composition and repetition, and compare multiple prompts,
  seeds and resolutions visually against a warmed single-worker reference.
- Establish correct service identity and safe endpoint discovery; test actual
  bot authentication, private prompts and output delivery on **both** accounts.
- Implement and exercise runner lifetime, handover, bounded queue/cancellation,
  expiry, startup failures and network partitions. Keep the existing generator
  as a working fallback throughout.
- Measure end-to-end latency and peak memory on multiple matched runs; do not
  extrapolate one runner's CPU speed or one favorable sample to all users.
