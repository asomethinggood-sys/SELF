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
