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

These are individual matched-run samples on variable shared runners, not a
quality guarantee or robust speed estimate. The coordinator's ComfyUI process
peaked at 14.52–14.55 GiB RSS in these tests; worker processes peaked around
14 GiB, leaving little margin on a 16 GB machine. Three further prompts are
being tested before considering either variant suitable for an opt-in service.

One stress run, `41836644947`, failed when a worker treated a transient GitHub
504 while polling rendezvous state as fatal. The harness now retries bounded
transient HTTP/network failures; its recovery is exercised against an actual
local HTTP server. The failed render is not counted as a quality result.

## Worker loss

Run `41821054697` deliberately stopped a remote worker after both tiled renders.
With experimental `SDXL_FALLBACK_LOCAL=1`, the coordinator discarded the
incomplete tile prediction, switched to full-frame local inference for the
remaining steps, and returned a valid synthetic PNG in 96.1 s. The coordinator
stays degraded for that patched model until rebuilt. This prevents partial tile
results advancing a scheduler; it does not prove production restart, queue,
network-partition, 24/7 handover or Telegram delivery behavior. The default is
still to fail the experiment rather than silently enable fallback.

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
