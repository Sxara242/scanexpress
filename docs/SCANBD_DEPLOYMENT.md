# scanbd / scanbm deployment for physical scanner buttons

This document covers the Phase 3 hardware path for Scanner Manager.

The application-side state machine is already responsible for Document mode,
Batch mode and Finish. scanbd must only detect a physical event and forward its
target value. Do not duplicate workflow logic in scanbd scripts.

## Architecture

The production path is:

    physical button
        -> scanbd
        -> scripts/scanbd_event_adapter.py
        -> local Unix socket
        -> HardwareEventDispatcher
        -> Phase 2 Document/Batch state machine
        -> scanimage through SANE net backend
        -> scanbm
        -> saned/local pixma backend
        -> USB scanner

The Unix socket is deliberately separate from the web server. Physical-button
events are therefore not exposed through Tailscale Serve or any HTTP reverse
proxy.

The scanbd action adapter acknowledges quickly. Actual scanning happens on a
serialized background worker after the action script can return to scanbd.

## Why scanbm is required

scanbd opens and polls the scanner and therefore owns the local USB device while
it is monitoring buttons. Direct scanimage access would race scanbd for that
device.

scanbm is the supported coordination path: scan applications use SANE's net
backend; scanbm asks scanbd to release the scanner, runs saned against the local
backend, then tells scanbd to resume polling when the scan ends.

For this reason the Scanner Manager process must use the SANE **net** backend
when scanbd polling is enabled. The scanbd/saned side must use the real local
backend such as pixma.

Upstream scanbd documentation describes the same separation: the ordinary SANE
configuration used by applications contains only the net backend, while the
scanbd/saned SANE_CONFIG_DIR contains the actual local scanner backend.

## Reference Canon LiDE 400 mapping

Real hardware validation produced:

| Physical button | SCANBD_TARGET | Scanner Manager action |
| --- | ---: | --- |
| PDF Start | 5 | document |
| PDF Finish | 6 | finish |
| Auto Scan | 2 | batch |
| Copy | 1 | ignore/reserved |
| Send | 3 | ignore/reserved |

This mapping belongs in deployment configuration, not generic application
business logic.

Reference environment value:

    SCANEXPRESS_HARDWARE_TARGET_MAP=5=document,6=finish,2=batch,1=ignore,3=ignore

Other scanners may use completely different target values.

## Duplicate protection

A physical button may produce repeated scanbd trigger activity. Debug logging
alone does not prove that an action script executes twice, so real action-count
testing is still required.

Scanner Manager nevertheless protects the workflow at three levels:

1. same source/target/device is rejected while an earlier matching event is
   queued or executing;
2. completed matching events are ignored inside the configurable monotonic
   debounce window (1.5 seconds in the reference service);
3. actual scanning remains serialized by the existing scanner/device lock and
   by the hardware event worker.

Finish remains state-aware and safe when no session is open.

## Files

- `hardware_events.py`: target map, debounce, queue and Unix socket server.
- `scripts/scanbd_event_adapter.py`: tiny scanbd action client.
- `deploy/scanbd/pixma.conf.example`: LiDE/pixma action example.
- `scanexpress.service.template`: enables the local socket in the reference
  systemd deployment.

## Required SANE topology

When enabling physical polling on the host:

**Application SANE config**

`/etc/sane.d/dll.conf` should expose the net backend to Scanner Manager.

`/etc/sane.d/net.conf` should point to the local scanbm/saned listener and use
a bounded connect timeout.

Typical intent:

    net

and:

    connect_timeout = 3
    localhost

**scanbd/saned SANE config**

The separate SANE configuration used by scanbd/scanbm must include the actual
local backend (for the reference scanner, pixma) and must not recurse back into
the net backend.

Exact Ubuntu/Debian package paths and unit names must be verified on the target
host before editing because packaging can differ between releases.

## Production invariants

- Keep Gunicorn at one worker for the reference deployment. The Unix socket,
  in-process hardware queue and scanner lock live in that worker.
- Threads are fine; the hardware worker itself is serialized.
- Do not expose the hardware event socket over TCP.
- Do not make scanbd scripts perform scanning themselves.
- Do not bypass the Phase 2 state machine.
- Do not delete a captured page/item on a later scan failure.
- Paperless outages must not cause rescanning.

## Hardware acceptance sequence

Software CI cannot prove scanbd/scanbm/USB behavior. On the actual Dell + LiDE
400, Phase 3 is not complete until these checks pass:

1. scanbd remains running as a real daemon/process.
2. scanbm/saned local proxy is listening and Scanner Manager scanimage reaches
   the LiDE through the net backend.
3. a logger proves how many adapter invocations one physical press actually
   creates.
4. PDF Start from IDLE scans exactly one page with no terminal command.
5. another PDF Start adds exactly one page to the same Document session.
6. PDF Finish creates one multi-page Pending PDF.
7. Auto Scan from IDLE creates one Batch item per press.
8. PDF Finish closes Batch without merging items.
9. duplicate/bounce testing never produces an unintended second scan.
10. scanbd resumes polling after every scan.
11. service/browser restart does not lose an open session or Pending capture.

Do not call Phase 3 hardware-complete before those real-device gates pass.
