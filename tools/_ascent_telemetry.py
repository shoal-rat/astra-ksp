"""Read-only ascent telemetry logger — diagnose WHY the crewed launch never circularizes.
Logs the active vessel's ascent state every few seconds so the post-separation failure mode is visible
(tumble = pitch wild; stall = apoapsis falling; control loss = heading wandering)."""
import sys, time
import krpc

name = sys.argv[1] if len(sys.argv) > 1 else "AI-Eve-Crew7"
dur = float(sys.argv[2]) if len(sys.argv) > 2 else 700.0
c = krpc.connect(name="ascent-telem", rpc_port=50000, stream_port=50001)
sc = c.space_center
t0 = time.time()
last_parts = None
while time.time() - t0 < dur:
    try:
        v = sc.active_vessel
        o = v.orbit
        if v.name != name:
            print(f"[{time.time()-t0:6.0f}s] active={v.name} (waiting for {name})", flush=True)
            time.sleep(5); continue
        fs = v.flight(v.surface_reference_frame)
        fb = v.flight(o.body.reference_frame)
        neng = sum(1 for e in v.parts.engines if e.active)
        np = len(v.parts.all)
        tag = ""
        if last_parts is not None and np < last_parts:
            tag = f"  <== STAGED (-{last_parts-np} parts)"
        last_parts = np
        print(f"[{time.time()-t0:6.0f}s] alt={fs.mean_altitude/1000:6.1f}km ap={o.apoapsis_altitude/1000:8.1f}km "
              f"pe={o.periapsis_altitude/1000:8.1f}km vspd={fb.vertical_speed:6.0f} spd={fb.speed:5.0f} "
              f"pitch={fs.pitch:5.0f} hdg={fs.heading:5.0f} eng={neng} thr={v.control.throttle:.2f} parts={np}{tag}",
              flush=True)
    except Exception as e:
        print(f"[{time.time()-t0:6.0f}s] err {type(e).__name__}: {e}", flush=True)
    time.sleep(5)
print("telemetry done", flush=True)
