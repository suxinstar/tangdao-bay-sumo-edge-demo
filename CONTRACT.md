# Local Tangdao Bay demonstration contract

This demonstration extends the SUMO project in a separate delivery folder. Real OSM road/building geometry; synthetic vehicles, acoustic detections, RSU deployment, compute/network timing and signal plans. It must never imply measured acoustic inference or deployed infrastructure.

## Files and ownership

- Map builder owns `scripts/build_map.py`, `data/*`, `scenario/*`, `MAP_NOTES.md`.
- Simulation/backend owns `server.py`, `simulation.py`, `tests/*`.
- Frontend owns `web/index.html`, `web/app.js`, `web/style.css`.
- Root integrates vendor assets, launchers, documentation and verification.

## scene.json

Coordinates are SUMO local meters, x=east and y=north. Three.js maps these to `(x, height, -y)`. Store all actual SUMO coordinates; frontend subtracts scene center for rendering.

```json
{
  "meta": {"name":"唐岛湾北岸", "bbox":[120.18,35.941,120.21,35.959], "source":"OpenStreetMap", "sourceUrl":"https://www.openstreetmap.org/copyright", "downloadedAt":"ISO UTC", "license":"ODbL 1.0", "notes":["..."]},
  "bounds":{"minX":0,"minY":0,"maxX":2000,"maxY":2000},
  "roads":[{"id":"edge id","name":"road name","shape":[[0,0],[10,10]],"width":7,"lanes":2,"speed":13.89}],
  "buildings":[{"id":"OSM id","polygon":[[0,0],[10,0],[10,10],[0,0]],"height":12,"heightSource":"osm_height|osm_levels|assumed","name":""}],
  "areas":[{"id":"OSM id","type":"water|park","polygon":[[0,0],[10,0],[0,0]]}],
  "intersections":[{"id":"SUMO tls id","x":100,"y":100}],
  "rsus":[{"id":"RSU_1","x":100,"y":115,"intersectionId":"SUMO tls id","sensingRadiusM":140,"serviceTimeS":1.6}]
}
```

SUMO scene entry is `scenario/tangdao.sumocfg`. Version 1.4 configures nine junction RSUs along Lijiang West Road and Changjiang Middle Road with north-south connections. `meta.focusBounds` is the 2.49 by 2.18 km demonstration region; `bounds` is the full imported 4.49 by 2.95 km road geometry. Road shapes include passenger and internal connection edges. Map metadata explicitly records synthetic signal timing and assumed building heights. New fields may be added while preserving existing field semantics.

## HTTP

- `GET /api/scene`: scene.json.
- `GET /api/state`: snapshot below; server simulation ticks independently under lock.
- `GET /api/state?vehicle=<URL-encoded ID>`: same snapshot plus `vehicleTrace`, collected under the same lock with identical `runId` and `simTime`. Follow mode uses one request every 500 ms. An unknown ID returns HTTP 200 with `vehicleTrace.status="unknown"`; standalone `/api/vehicle` retains its 404 behavior. Stale selection, control-command epoch and run responses are discarded by the frontend.
- `GET /api/integration`: model contracts, configured state and bounded shadow results.
- `POST /api/model/probe`: explicit audio-reference inference, HTTP 202 accepted or 429 rejected; no physical device command and no automatic replacement of synthetic control inputs. Complete contracts and executable examples: `docs/MODEL_INTEGRATION.md`.
- `GET /api/vehicle?id=<URL-encoded vehicle ID>`: complete current-run journey for one observed vehicle, without the global snapshot's recent-task/event limits. Indexed by vehicle so a follower does not download the whole run. Exactly one nonblank ID of at most 256 characters with no control characters is required (400 otherwise). Unknown or pre-reset IDs return 404 with `status:"unknown"` and empty task/event lists. No simulation mutation.
- `POST /api/control` JSON `{action:"pause"|"resume"|"reset"|"speed"|"scheduler", value:...}`. Speed values 0.5,1,2,4. Scheduler `least_finish` or `local`. Reset cleanly restarts seeded SUMO. A changed scheduler takes effect after reset, or server resets automatically and says so.
- `GET /api/health`: ready/backend/sumo status.
- `GET /api/export`: downloadable JSON of run configuration, metadata, counters and events.

```json
{
 "status":"running|paused|finished|error","simTime":15.2,"speed":1,"scheduler":"least_finish","error":null,
 "vehicles":[{"id":"v0","x":100,"y":100,"angle":90,"speed":7,"laneId":"edge_0"}],
 "signals":[{"id":"tls id","x":100,"y":100,"phase":0,"state":"GGrr","remaining":12,"mode":"baseline|extended","lastAction":"..."}],
 "rsus":[{"id":"RSU_1","queue":2,"busy":true,"completed":4,"offloaded":2}],
 "tasks":[{"id":"T0001","vehicleId":"v0","origin":"RSU_1","target":"RSU_2","x":100,"y":100,"created":12,"txEnd":12.4,"start":12.4,"finish":14,"returnEnd":14.2,"status":"transmitting|queued|processing|returning|done","offloaded":true}],
 "events":[{"id":1,"time":14.2,"type":"sense|dispatch|complete|signal","text":"中文说明","rsuId":"RSU_1","taskId":"T0001"}],
 "metrics":{"sensed":10,"completed":8,"offloaded":3,"meanLatency":1.8,"signalActions":2,"vehicles":20},
 "trace": {"taskId":"T0001","stages":[{"key":"sense","state":"done"},{"key":"dispatch","state":"done"},{"key":"compute","state":"active"},{"key":"signal","state":"waiting"}]}
}
```

### Single-vehicle journey

```json
{
 "runId":"current run identity", "simTime":40.2, "vehicleId":"v0",
 "status":"present|departed|unknown",
 "vehicle":{"id":"v0","x":100,"y":100,"angle":90,"speed":7,"laneId":"edge_0"},
 "firstSeen":1.2,"lastSeen":40.2,"enteredAt":1.2,"leftAt":null,"arrivedAt":null,
 "tasks":[{"id":"T00001","vehicleId":"v0","origin":"RSU_1","target":"RSU_2",
           "created":12,"txEnd":12.4,"start":12.4,"finish":14,"returnEnd":14.2,
           "status":"done","offloaded":true,"returned":true,"observedReturnTime":14.2,
           "controlCheckedTime":14.2,"controlApplied":false,"controlReason":"检测车道当前非可延长绿灯"}],
 "events":[{"id":1,"time":12,"type":"sense","vehicleId":"v0","taskId":"T00001","rsuId":"RSU_1","text":"..."}],
 "summary":{"sensed":1,"completed":1,"offloaded":1,"pending":0}
}
```

`present` means SUMO currently reports the vehicle. `departed` means it was observed in this run but is no longer present; its `vehicle` is the **last known** position/speed, never an extrapolated live position. `enteredAt` is the SUMO departure/entry event time, `leftAt` the first observed absence time, and `arrivedAt` exists only after an actual SUMO arrival event. If a short trip enters and arrives without a sampled position, `vehicle`, `firstSeen`, and `lastSeen` remain null. All timestamps are SUMO seconds; unavailable fields are null.

Tasks remain in creation order, with full timing, lane/location, and any observed control outcome. Related sense/dispatch/complete/signal/signal_skip events carry `vehicleId` and are retained in event order. Global events such as reset/error/finished are not attributed to a vehicle. Completion counts require `returned=true`; neither a predicted finish time nor leaving the road creates a completion. A departed vehicle's outstanding tasks can still complete under the existing simulation; the maximum run-time limit remains unchanged. On run reset the journey indexes are cleared. Clients must discard prior traces on `runId` change and must not silently switch to a different vehicle when the followed vehicle leaves.

## Signal safety and truthfulness

Use synthetic acoustic task results from vehicle encounters, not actual WAV recognition. Simple least predicted finish-time rule: transfer + assigned work remaining + service time. The task must complete before its result influences the controller. Extend an already safe green only for matching controlled incoming lanes; never jump across yellow/all-red, bound green duration and log applied/rejected actions. The original scenario signal plans are synthesized by SUMO, not real municipal timing. A few visible green responses should occur during validation; no fabricated counters. All animations derive from task timestamps and SUMO state. The UI shows these boundaries in a concise persistent data-status row and an expandable explanation.

## Audible demonstration and visual performance

`web/vehicle-audio.js` auditions a locally bundled CC0 engine sample. SUMO speed controls pitch and volume; AnalyserNode drives the displayed waveform/spectrum. No autoplay before a user click, no microphone permission, and no claim of measured sound pressure or recognition. Pause, hidden page, disconnected backend, free camera, departed vehicle and disposal stop audio. Source and license: `docs/AUDIO_SOURCES.md`.

Only camera-visible vehicles are uploaded to two instanced draw batches; the selected car is retained for stable picking/identity. Rendering culling does not remove vehicles from SUMO or global metrics. Light/standard render buffers are capped at 0.9/1.8 million pixels, respectively; frame scheduling caps remain 24/30 FPS. Audio charts share the existing scheduler with a 12.5 FPS cap.
