using System;
using System.Collections.Generic;
using System.Reflection;
using MechJebLib.FuelFlowSimulation;
using MuMech;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    /// <summary>
    /// Registers the /mj-* routes. This class references no MechJeb type, so the plugin loads and the
    /// other routes keep working when MechJeb is missing; each route checks MechJebInfo first and only
    /// then enters MechJebOps (where the MechJeb types live).
    /// </summary>
    internal static class MechJebRoutes
    {
        public static void Register(Router r)
        {
            Add(r, "POST", "/mj-ascent", 15000, "ascent", "Configure (every setting explicit, echoed back) and engage MechJeb's ascent autopilot.");
            Add(r, "POST", "/mj-execute-node", 15000, "execute-node", "Execute the next (or all) maneuver node(s) with MechJeb's node executor.");
            Add(r, "POST", "/mj-land", 15000, "land", "Engage MechJeb's landing autopilot (targeted or not); touchdown speed is required.");
            Add(r, "GET", "/mj-landing-prediction", 15000, "landing-prediction", "MechJeb's landing prediction (enables the predictor; poll while pending).");
            Add(r, "POST", "/mj-landing-prediction", 15000, "landing-prediction", "Landing prediction with predictor settings {deployChutes, limitChutesStage}.");
            Add(r, "POST", "/mj-rendezvous", 15000, "rendezvous", "Engage MechJeb's rendezvous autopilot toward a target vessel.");
            Add(r, "POST", "/mj-dock", 15000, "dock", "Engage MechJeb's docking autopilot between explicit (or unambiguous) ports.");
            Add(r, "POST", "/mj-plan", 60000, "plan", "Compute maneuver nodes with a MechJeb planner operation; place=false is a dry run.");
            Add(r, "POST", "/mj-abort", 15000, "abort", "Really disable MechJeb modules (ascent, landing, node, rendezvous, dock, staging, smartass, predictor).");
            Add(r, "GET", "/mj-status", 15000, "status", "Every MechJeb module's enabled flag and status, target, nodes, ports (NaN-safe).");
            Add(r, "GET", "/mj-stage-stats", 15000, "stage-stats", "MechJeb per-stage delta-v/TWR/burn time (flight or editor); poll while pending.");
            Add(r, "POST", "/mj-stage-stats", 15000, "stage-stats", "Stage stats with editor options {body, altitude, mach}.");
        }

        private static void Add(Router r, string method, string path, int timeoutMs, string op, string summary)
        {
            r.Main(method, path, timeoutMs, delegate(BridgeRequest req)
            {
                MechJebInfo.Require();
                return MechJebOps.Invoke(op, req);
            }, summary);
        }
    }

    /// <summary>The MechJeb 2.15.3 calls (compile-verified against the installed MechJeb2.dll).</summary>
    internal static class MechJebOps
    {
        private static readonly string[] AbortModules = { "all", "ascent", "landing", "node", "rendezvous", "dock", "staging", "smartass", "predictor" };

        // MechJebCore instances (typed object so this class loads without MechJeb) whose autostager
        // /mj-execute-node claimed for the node executor. Main thread only.
        private static readonly List<object> NodeStagingClaims = new List<object>();

        private static readonly string[] PlanOperations =
        {
            "circularize", "apoapsis", "periapsis", "ellipticize", "eccentricity", "semi_major", "inclination", "lan",
            "longitude", "plane", "kill_rel_vel", "hohmann", "lambert", "interplanetary", "course_correction",
            "moon_return", "resonant_orbit"
        };

        public static JObj Invoke(string op, BridgeRequest req)
        {
            switch (op)
            {
                case "ascent": return Ascent(req);
                case "execute-node": return ExecuteNode(req);
                case "land": return Land(req);
                case "landing-prediction": return LandingPrediction(req);
                case "rendezvous": return Rendezvous(req);
                case "dock": return Dock(req);
                case "plan": return Plan(req);
                case "abort": return Abort(req);
                case "status": return Status(req);
                case "stage-stats": return StageStats(req);
                default: throw new BridgeException(404, "Unknown MechJeb operation '" + op + "'.");
            }
        }

        /// <summary>Releases autostager claims once their node executor is idle; called every frame.</summary>
        internal static void Tick()
        {
            for (int i = NodeStagingClaims.Count - 1; i >= 0; i--)
            {
                MechJebCore core = NodeStagingClaims[i] as MechJebCore;
                if (core == null) // destroyed with its vessel (Unity null)
                {
                    NodeStagingClaims.RemoveAt(i);
                    continue;
                }
                if (!core.Node.Enabled)
                {
                    core.Staging.Users.Remove(core.Node);
                    NodeStagingClaims.RemoveAt(i);
                }
            }
        }

        // ------------------------------------------------------------------ shared

        private static MechJebCore RequireCore(out Vessel vessel)
        {
            vessel = Util.RequireActiveVessel();
            MechJebCore core = vessel.GetMasterMechJeb();
            if (core == null)
            {
                throw new BridgeException(409, "The active vessel has no MechJeb core.",
                    "Install MechJebForAll.cfg (adds MechJebCore to every command part) and reload, or add a MechJeb part.");
            }
            return core;
        }

        private static bool Set(BridgeRequest req, string key, EditableDoubleMult target, JObj applied)
        {
            double? v = req.Num(key);
            if (!v.HasValue)
            {
                return false;
            }
            target.Val = v.Value;
            applied[key] = v.Value;
            return true;
        }

        private static bool Set(BridgeRequest req, string key, EditableInt target, JObj applied)
        {
            int? v = req.Int(key);
            if (!v.HasValue)
            {
                return false;
            }
            target.Val = v.Value;
            applied[key] = v.Value;
            return true;
        }

        private static bool? Flag(BridgeRequest req, string key, JObj applied)
        {
            bool? b = req.Bool(key);
            if (b.HasValue)
            {
                applied[key] = b.Value;
            }
            return b;
        }

        private static List<object> Nodes(Vessel vessel)
        {
            List<object> list = new List<object>();
            if (vessel.patchedConicSolver == null)
            {
                return list;
            }
            double now = Planetarium.GetUniversalTime();
            foreach (ManeuverNode n in vessel.patchedConicSolver.maneuverNodes)
            {
                JObj o = new JObj();
                o["ut"] = n.UT;
                o["timeTo_s"] = n.UT - now;
                o["prograde_mps"] = n.DeltaV.z;
                o["normal_mps"] = n.DeltaV.y;
                o["radial_mps"] = n.DeltaV.x;
                o["dv_mps"] = n.DeltaV.magnitude;
                list.Add(o);
            }
            return list;
        }

        private static ITargetable ResolveTarget(BridgeRequest req, bool required)
        {
            string bodyName = req.Str("targetBody");
            if (bodyName != null)
            {
                return Util.RequireBody(bodyName);
            }
            if (req.Has("targetPersistentId"))
            {
                return Util.ResolveVessel(req, "targetPersistentId", null, true);
            }
            string name = req.Str("target");
            if (name != null)
            {
                CelestialBody body = Util.FindBody(name);
                if (body != null)
                {
                    return body;
                }
                return Util.ResolveVessel(req, "targetPersistentId", "target", true);
            }
            if (required)
            {
                throw new BridgeException(400, "Missing target: pass targetPersistentId (vessel), target (vessel or body name) or targetBody.");
            }
            return null;
        }

        private static void SetTarget(MechJebCore core, ITargetable target)
        {
            // FlightGlobals only sets vessel.targetObject, which MechJeb reads on its next FixedUpdate;
            // setting core.Target directly avoids acting on a stale target in this frame.
            FlightGlobals.fetch.SetVesselTarget(target);
            core.Target.Set(target);
        }

        private static JObj TargetInfo(MechJebCore core)
        {
            JObj t = new JObj();
            MechJebModuleTargetController tc = core.Target;
            t["exists"] = tc.NormalTargetExists;
            t["name"] = tc.NormalTargetExists ? tc.Name : null;
            t["type"] = tc.Target != null ? tc.Target.GetType().Name : null;
            t["distance_m"] = tc.NormalTargetExists ? (object)(double)tc.Distance : null;
            t["relativeSpeed_mps"] = tc.NormalTargetExists ? (object)tc.RelativeVelocity.magnitude : null;
            if (tc.PositionTargetExists && tc.targetBody != null)
            {
                JObj p = new JObj();
                p["body"] = tc.targetBody.bodyName;
                p["lat"] = (double)tc.targetLatitude;
                p["lon"] = (double)tc.targetLongitude;
                t["position"] = p;
            }
            else
            {
                t["position"] = null;
            }
            return t;
        }

        // ------------------------------------------------------------------ ascent

        private static bool AscentEngaged(MechJebCore core)
        {
            MechJebModuleAscentClassicAutopilot classic = core.GetComputerModule<MechJebModuleAscentClassicAutopilot>();
            MechJebModuleAscentPVGAutopilot pvg = core.GetComputerModule<MechJebModuleAscentPVGAutopilot>();
            return (classic != null && classic.Enabled) || (pvg != null && pvg.Enabled);
        }

        private static JObj Ascent(BridgeRequest req)
        {
            Vessel vessel;
            MechJebCore core = RequireCore(out vessel);
            MechJebModuleAscentSettings s = core.AscentSettings;
            MechJebModuleStagingController st = core.Staging;
            if (AscentEngaged(core))
            {
                throw new BridgeException(409, "MechJeb's ascent autopilot is already engaged.",
                    "POST /mj-abort {modules: ascent} first, then configure and engage again.");
            }
            JObj applied = new JObj();
            string type = req.Choice("ascentType", new[] { "classic", "pvg" }, null); // guidance is the caller's choice
            double altitude = req.RequireNum("altitude");
            double inclination = req.RequireNum("inclination");
            bool autostage = req.RequireBool("autostage");
            s.AscentType = type == "pvg" ? AscentType.PVG : AscentType.CLASSIC;
            applied["ascentType"] = type;
            s.DesiredOrbitAltitude.Val = altitude;
            s.DesiredInclination.Val = inclination;
            s.Autostage = autostage; // must precede engaging: OnModuleEnabled reads it
            applied["altitude"] = s.DesiredOrbitAltitude.Val;
            applied["inclination"] = s.DesiredInclination.Val;
            applied["autostage"] = s.Autostage;

            bool? b;
            if ((b = Flag(req, "autoPath", applied)).HasValue) s.AutoPath = b.Value;
            double? f;
            if ((f = req.Num("autoTurnPercent")).HasValue) { s.AutoTurnPerc = (float)f.Value; applied["autoTurnPercent"] = f.Value; }
            if ((f = req.Num("autoTurnSpeedFactor")).HasValue) { s.AutoTurnSpdFactor = (float)f.Value; applied["autoTurnSpeedFactor"] = f.Value; }
            Set(req, "turnStartAltitude", s.TurnStartAltitude, applied);
            Set(req, "turnStartVelocity", s.TurnStartVelocity, applied);
            Set(req, "turnEndAltitude", s.TurnEndAltitude, applied);
            Set(req, "turnEndAngle", s.TurnEndAngle, applied);
            Set(req, "turnShapeExponent", s.TurnShapeExponent, applied);
            if ((b = Flag(req, "limitAoA", applied)).HasValue) s.LimitAoA = b.Value;
            Set(req, "maxAoA", s.MaxAoA, applied);
            Set(req, "aoaFadeoutPressure", s.AOALimitFadeoutPressure, applied);
            if ((b = Flag(req, "correctiveSteering", applied)).HasValue) s.CorrectiveSteering = b.Value;
            Set(req, "correctiveSteeringGain", s.CorrectiveSteeringGain, applied);
            if ((b = Flag(req, "forceRoll", applied)).HasValue) s.ForceRoll = b.Value;
            Set(req, "verticalRoll", s.VerticalRoll, applied);
            Set(req, "turnRoll", s.TurnRoll, applied);
            Set(req, "rollAltitude", s.RollAltitude, applied);
            if ((b = Flag(req, "skipCircularization", applied)).HasValue) s.SkipCircularization = b.Value;
            Set(req, "desiredLan", s.DesiredLan, applied);
            Set(req, "launchPhaseAngle", s.LaunchPhaseAngle, applied);
            Set(req, "launchLanDifference", s.LaunchLANDifference, applied);
            if ((b = Flag(req, "autodeploySolarPanels", applied)).HasValue) s.AutodeploySolarPanels = b.Value;
            if ((b = Flag(req, "autoDeployAntennas", applied)).HasValue) s.AutoDeployAntennas = b.Value;
            if ((b = Flag(req, "limitQaEnabled", applied)).HasValue) s.LimitQaEnabled = b.Value;
            Set(req, "limitQa", s.LimitQa, applied);
            ApplyStaging(req, st, applied);

            bool engage = req.Bool("engage", true);
            MechJebModuleAscentBaseAutopilot ap = s.AscentAutopilot;
            if (engage)
            {
                if (ap == null)
                {
                    throw new BridgeException(503, "MechJeb has no " + type + " ascent autopilot module.");
                }
                ap.Users.Add(core);
            }
            JObj d = new JObj();
            d["engaged"] = ap != null && ap.Enabled;
            d["applied"] = applied;
            d["effective"] = AscentSettings(s, st);
            d["status"] = ap != null ? ap.Status : null;
            d["situation"] = vessel.situation.ToString();
            if (engage && vessel.situation == Vessel.Situations.PRELAUNCH)
            {
                d["note"] = "MechJeb does not ignite from PRELAUNCH: set throttle and activate the first stage to start the ascent.";
            }
            return d;
        }

        private static void ApplyStaging(BridgeRequest req, MechJebModuleStagingController st, JObj applied)
        {
            bool? b;
            Set(req, "autostagePreDelay", st.AutostagePreDelay, applied);
            Set(req, "autostagePostDelay", st.AutostagePostDelay, applied);
            Set(req, "autostageLimit", st.AutostageLimit, applied);
            Set(req, "clampAutoStageThrustPct", st.ClampAutoStageThrustPct, applied);
            Set(req, "fairingMaxDynamicPressure", st.FairingMaxDynamicPressure, applied);
            Set(req, "fairingMinAltitude", st.FairingMinAltitude, applied);
            Set(req, "fairingMaxAerothermalFlux", st.FairingMaxAerothermalFlux, applied);
            if ((b = Flag(req, "hotStaging", applied)).HasValue) st.HotStaging = b.Value;
            Set(req, "hotStagingLeadTime", st.HotStagingLeadTime, applied);
            if ((b = Flag(req, "dropSolids", applied)).HasValue) st.DropSolids = b.Value;
            Set(req, "dropSolidsLeadTime", st.DropSolidsLeadTime, applied);
        }

        private static JObj AscentSettings(MechJebModuleAscentSettings s, MechJebModuleStagingController st)
        {
            JObj e = new JObj();
            e["ascentType"] = s.AscentType.ToString();
            e["altitude"] = s.DesiredOrbitAltitude.Val;
            e["inclination"] = s.DesiredInclination.Val;
            e["autostage"] = s.Autostage;
            e["autoPath"] = s.AutoPath;
            e["autoTurnPercent"] = s.AutoTurnPerc;
            e["autoTurnSpeedFactor"] = s.AutoTurnSpdFactor;
            e["turnStartAltitude"] = s.TurnStartAltitude.Val;
            e["turnStartVelocity"] = s.TurnStartVelocity.Val;
            e["turnEndAltitude"] = s.TurnEndAltitude.Val;
            e["turnEndAngle"] = s.TurnEndAngle.Val;
            e["turnShapeExponent"] = s.TurnShapeExponent.Val;
            try
            {
                // What the classic path will actually use when autoPath is on (computed from the body).
                e["autoTurnStartAltitude"] = s.AutoTurnStartAltitude;
                e["autoTurnStartVelocity"] = s.AutoTurnStartVelocity;
                e["autoTurnEndAltitude"] = s.AutoTurnEndAltitude;
            }
            catch (Exception)
            {
                e["autoTurnStartAltitude"] = null;
            }
            e["limitAoA"] = s.LimitAoA;
            e["maxAoA"] = s.MaxAoA.Val;
            e["aoaFadeoutPressure"] = s.AOALimitFadeoutPressure.Val;
            e["correctiveSteering"] = s.CorrectiveSteering;
            e["correctiveSteeringGain"] = s.CorrectiveSteeringGain.Val;
            e["forceRoll"] = s.ForceRoll;
            e["verticalRoll"] = s.VerticalRoll.Val;
            e["turnRoll"] = s.TurnRoll.Val;
            e["rollAltitude"] = s.RollAltitude.Val;
            e["skipCircularization"] = s.SkipCircularization;
            e["desiredLan"] = s.DesiredLan.Val;
            e["launchPhaseAngle"] = s.LaunchPhaseAngle.Val;
            e["launchLanDifference"] = s.LaunchLANDifference.Val;
            e["autodeploySolarPanels"] = s.AutodeploySolarPanels;
            e["autoDeployAntennas"] = s.AutoDeployAntennas;
            e["limitQaEnabled"] = s.LimitQaEnabled;
            e["limitQa"] = s.LimitQa.Val;
            e["autostagePreDelay"] = st.AutostagePreDelay.Val;
            e["autostagePostDelay"] = st.AutostagePostDelay.Val;
            e["autostageLimit"] = st.AutostageLimit.Val;
            e["clampAutoStageThrustPct"] = st.ClampAutoStageThrustPct.Val;
            e["fairingMaxDynamicPressure"] = st.FairingMaxDynamicPressure.Val;
            e["fairingMinAltitude"] = st.FairingMinAltitude.Val;
            e["fairingMaxAerothermalFlux"] = st.FairingMaxAerothermalFlux.Val;
            e["hotStaging"] = st.HotStaging;
            e["hotStagingLeadTime"] = st.HotStagingLeadTime.Val;
            e["dropSolids"] = st.DropSolids;
            e["dropSolidsLeadTime"] = st.DropSolidsLeadTime.Val;
            return e;
        }

        // ------------------------------------------------------------------ node executor

        private static JObj ExecuteNode(BridgeRequest req)
        {
            Vessel vessel;
            MechJebCore core = RequireCore(out vessel);
            if (vessel.patchedConicSolver == null || vessel.patchedConicSolver.maneuverNodes.Count == 0)
            {
                throw new BridgeException(409, "The active vessel has no maneuver node.", "Create one first (node_create or POST /mj-plan place=true).");
            }
            MechJebModuleNodeExecutor ex = core.Node;
            if (ex.Enabled)
            {
                throw new BridgeException(409, "MechJeb's node executor is already running.", "POST /mj-abort {modules: node} first.");
            }
            bool all = req.RequireBool("all");
            bool autowarp = req.RequireBool("autowarp");
            JObj applied = new JObj();
            ex.Autowarp = autowarp;
            applied["all"] = all;
            applied["autowarp"] = ex.Autowarp;
            Set(req, "leadTime", ex.LeadTime, applied);
            bool? b;
            if ((b = Flag(req, "rcsOnly", applied)).HasValue) ex.RCSOnly = b.Value;
            if ((b = Flag(req, "killRollRotation", applied)).HasValue) ex.KillRollRotation = b.Value;
            List<object> warnings = new List<object>();
            if (req.Has("tolerance"))
            {
                warnings.Add("tolerance is not a setting of MechJeb 2.15.3's node executor (it ends the burn itself); ignored.");
            }
            bool? autostage = Flag(req, "autostage", applied);
            if (autostage.HasValue)
            {
                if (autostage.Value)
                {
                    // Claimed in the executor's name and released by Tick() once it goes idle: MechJeb
                    // itself never releases the autostager after a burn, and a stager left on fires
                    // decouplers during later burns (a lost heat shield in the old project).
                    core.Staging.Users.Add(ex);
                    if (!NodeStagingClaims.Contains(core))
                    {
                        NodeStagingClaims.Add(core);
                    }
                }
                else
                {
                    core.Staging.Users.Clear();
                }
            }
            else if (core.Staging.Enabled)
            {
                warnings.Add("MechJeb's autostager is enabled and will stage during this burn; pass autostage=false to release it.");
            }

            if (all)
            {
                ex.ExecuteAllNodes(core);
            }
            else
            {
                ex.ExecuteOneNode(core);
            }
            ManeuverNode first = vessel.patchedConicSolver.maneuverNodes[0];
            JObj d = new JObj();
            d["executing"] = ex.Enabled;
            d["applied"] = applied;
            JObj eff = new JObj();
            eff["autowarp"] = ex.Autowarp;
            eff["leadTime"] = ex.LeadTime.Val;
            eff["rcsOnly"] = ex.RCSOnly;
            eff["killRollRotation"] = ex.KillRollRotation;
            eff["autostage"] = core.Staging.Enabled;
            eff["autostageReleasedAfterBurn"] = autostage.HasValue && autostage.Value;
            d["effective"] = eff;
            d["state"] = ex.State.ToString();
            d["nodes"] = Nodes(vessel);
            d["timeToNode_s"] = first.UT - Planetarium.GetUniversalTime();
            if (ex.Autowarp)
            {
                warnings.Add("MechJeb does not rails-warp while it is still turning toward the node; for a distant node warp to shortly before it first.");
            }
            d["warnings"] = warnings;
            return d;
        }

        // ------------------------------------------------------------------ landing

        private static JObj Land(BridgeRequest req)
        {
            Vessel vessel;
            MechJebCore core = RequireCore(out vessel);
            MechJebModuleLandingAutopilot land = core.Landing;
            if (land.Enabled)
            {
                throw new BridgeException(409, "MechJeb's landing autopilot is already engaged.", "POST /mj-abort {modules: landing} first.");
            }
            JObj applied = new JObj();
            bool targeted = req.RequireBool("targeted");
            double lat = 0;
            double lon = 0;
            if (targeted)
            {
                lat = req.RequireNum("lat");
                lon = req.RequireNum("lon");
            }
            double touchdownSpeed = req.RequireNum("touchdownSpeed");
            bool deployGears = req.RequireBool("deployGears");
            bool deployChutes = req.RequireBool("deployChutes");
            land.TouchdownSpeed.Val = touchdownSpeed;
            land.DeployGears = deployGears;
            land.DeployChutes = deployChutes;
            applied["targeted"] = targeted;
            applied["touchdownSpeed"] = land.TouchdownSpeed.Val;
            applied["deployGears"] = land.DeployGears;
            applied["deployChutes"] = land.DeployChutes;
            Set(req, "limitGearsStage", land.LimitGearsStage, applied);
            Set(req, "limitChutesStage", land.LimitChutesStage, applied);
            bool? b;
            if ((b = Flag(req, "rcsAdjustment", applied)).HasValue) land.RCSAdjustment = b.Value;

            if (targeted)
            {
                core.Target.SetPositionTarget(vessel.mainBody, lat, lon);
                land.LandAtPositionTarget(core);
            }
            else
            {
                land.LandUntargeted(core);
            }
            JObj eff = new JObj();
            eff["touchdownSpeed"] = land.TouchdownSpeed.Val;
            eff["deployGears"] = land.DeployGears;
            eff["limitGearsStage"] = land.LimitGearsStage.Val;
            eff["deployChutes"] = land.DeployChutes;
            eff["limitChutesStage"] = land.LimitChutesStage.Val;
            eff["rcsAdjustment"] = land.RCSAdjustment;
            eff["landAtTarget"] = land.LandAtTarget;
            JObj d = new JObj();
            d["landing"] = land.Enabled;
            d["applied"] = applied;
            d["effective"] = eff;
            d["target"] = TargetInfo(core);
            d["status"] = land.Status;
            d["rcsActionGroup"] = vessel.ActionGroups[KSPActionGroup.RCS];
            return d;
        }

        private static JObj LandingPrediction(BridgeRequest req)
        {
            Vessel vessel;
            MechJebCore core = RequireCore(out vessel);
            MechJebModuleLandingPredictions pred = core.GetComputerModule<MechJebModuleLandingPredictions>();
            if (pred == null)
            {
                throw new BridgeException(503, "MechJeb has no landing predictions module.");
            }
            bool? b = req.Bool("deployChutes");
            if (b.HasValue)
            {
                pred.deployChutes = b.Value;
            }
            int? stage = req.Int("limitChutesStage");
            if (stage.HasValue)
            {
                pred.limitChutesStage = stage.Value;
            }
            pred.Users.Add(core); // the predictor only simulates while it has a user
            JObj d = new JObj();
            d["enabled"] = pred.Enabled;
            d["deployChutes"] = pred.deployChutes;
            d["limitChutesStage"] = pred.limitChutesStage;
            ReentrySimulation.Result r = pred.Result;
            if (r == null)
            {
                d["pending"] = true;
                d["note"] = "The first simulation runs in the background; poll again (the game must be unpaused for it to progress).";
                return d;
            }
            double now = Planetarium.GetUniversalTime();
            d["pending"] = false;
            d["outcome"] = r.Outcome.ToString();
            d["body"] = r.Body != null ? r.Body.bodyName : null;
            d["endUT"] = r.EndUT;
            d["timeToEnd_s"] = r.EndUT - now;
            d["latitude"] = r.EndPosition.Latitude;
            d["longitude"] = r.EndPosition.Longitude;
            d["endAltitudeASL_m"] = r.EndASL;
            d["maxDragGees"] = r.MaxDragGees;
            d["deltaVExpended_mps"] = r.DeltaVExpended;
            d["aerobrake"] = r.AeroBrake;
            d["simulatedAtUT"] = r.InputUT;
            d["note"] = "The predictor keeps simulating while enabled; POST /mj-abort {modules: predictor} stops it.";
            return d;
        }

        // ------------------------------------------------------------------ rendezvous and docking

        private static JObj Rendezvous(BridgeRequest req)
        {
            Vessel vessel;
            MechJebCore core = RequireCore(out vessel);
            Vessel target = Util.ResolveVessel(req, "targetPersistentId", "target", true);
            if (target == vessel)
            {
                throw new BridgeException(400, "The target is the active vessel itself.");
            }
            MechJebModuleRendezvousAutopilot ap = core.GetComputerModule<MechJebModuleRendezvousAutopilot>();
            if (ap.Enabled)
            {
                throw new BridgeException(409, "MechJeb's rendezvous autopilot is already engaged.", "POST /mj-abort {modules: rendezvous} first.");
            }
            JObj applied = new JObj();
            double desiredDistance = req.RequireNum("desiredDistance");
            double maxPhasingOrbits = req.RequireNum("maxPhasingOrbits");
            double maxClosingSpeed = req.RequireNum("maxClosingSpeed");
            ap.desiredDistance.Val = desiredDistance;
            ap.maxPhasingOrbits.Val = maxPhasingOrbits;
            ap.maxClosingSpeed.Val = maxClosingSpeed;
            applied["desiredDistance"] = ap.desiredDistance.Val;
            applied["maxPhasingOrbits"] = ap.maxPhasingOrbits.Val;
            applied["maxClosingSpeed"] = ap.maxClosingSpeed.Val;
            bool? rcs = Flag(req, "rcs", applied);
            if (rcs.HasValue)
            {
                vessel.ActionGroups.SetGroup(KSPActionGroup.RCS, rcs.Value);
            }
            SetTarget(core, target);
            if (!core.Target.NormalTargetExists)
            {
                throw new BridgeException(409, "MechJeb did not accept " + target.vesselName + " as a target.");
            }
            ap.Users.Add(core);
            JObj d = new JObj();
            d["enabled"] = ap.Enabled;
            d["target"] = Util.VesselRef(target);
            d["applied"] = applied;
            d["rcsActionGroup"] = vessel.ActionGroups[KSPActionGroup.RCS];
            d["targetInfo"] = TargetInfo(core);
            d["status"] = ap.status;
            return d;
        }

        private static bool PortReady(ModuleDockingNode port)
        {
            return port != null && port.state != null && port.state.StartsWith("Ready", StringComparison.Ordinal);
        }

        private static ModuleDockingNode SinglePort(Vessel v, string role, string idParam)
        {
            List<ModuleDockingNode> ready = new List<ModuleDockingNode>();
            List<string> described = new List<string>();
            foreach (Part p in v.parts)
            {
                ModuleDockingNode port = p.FindModuleImplementing<ModuleDockingNode>();
                if (port == null)
                {
                    continue;
                }
                described.Add(p.persistentId + " (index " + v.parts.IndexOf(p) + ", " + port.nodeType + ", " + port.state + ")");
                if (PortReady(port))
                {
                    ready.Add(port);
                }
            }
            if (ready.Count == 1)
            {
                return ready[0];
            }
            throw new BridgeException(ready.Count == 0 ? 409 : 400,
                (ready.Count == 0 ? "No free (Ready) docking port on " : "Several free docking ports on ") + v.vesselName
                + " (" + role + "): " + (described.Count > 0 ? string.Join("; ", described.ToArray()) : "none") + ".",
                ready.Count == 0 ? null : "Pass " + idParam + " to choose one.");
        }

        private static ModuleDockingNode PortOf(Part p, string what)
        {
            ModuleDockingNode port = p.FindModuleImplementing<ModuleDockingNode>();
            if (port == null)
            {
                throw new BridgeException(400, what + " part " + Util.PartTitle(p) + " (" + p.persistentId + ") is not a docking port.");
            }
            if (!PortReady(port))
            {
                throw new BridgeException(409, what + " port " + p.persistentId + " is not free (state '" + port.state + "').");
            }
            return port;
        }

        private static JObj Dock(BridgeRequest req)
        {
            Vessel vessel;
            MechJebCore core = RequireCore(out vessel);
            MechJebModuleDockingAutopilot dock = core.GetComputerModule<MechJebModuleDockingAutopilot>();
            if (dock.Enabled)
            {
                throw new BridgeException(409, "MechJeb's docking autopilot is already engaged.", "POST /mj-abort {modules: dock} first.");
            }

            // Ports: an explicit id wins; otherwise the port already chosen in the game (target docking port,
            // control-from part, e.g. set through kRPC); otherwise the vessel's only free port.
            ModuleDockingNode targetPort;
            string targetSource;
            Part targetPart = Util.ResolvePart(req, "targetPortPart", null);
            if (targetPart != null)
            {
                targetPort = PortOf(targetPart, "Target");
                targetSource = "explicit";
            }
            else
            {
                Vessel tv = Util.ResolveVessel(req, "targetPersistentId", "target", false);
                ModuleDockingNode chosen = FlightGlobals.fetch.VesselTarget as ModuleDockingNode;
                if (chosen != null && PortReady(chosen) && chosen.vessel != vessel && (tv == null || chosen.vessel == tv))
                {
                    targetPort = chosen;
                    targetSource = "game target";
                }
                else if (tv != null)
                {
                    if (!tv.loaded)
                    {
                        throw new BridgeException(409, tv.vesselName + " is not loaded (out of physics range); close in with /mj-rendezvous first.");
                    }
                    targetPort = SinglePort(tv, "target", "targetPortPartId");
                    targetSource = "only free port";
                }
                else
                {
                    throw new BridgeException(400, "Missing target: pass targetPortPartId, or targetPersistentId/target (vessel), or set a docking port as the game target.");
                }
            }
            if (targetPort.vessel == vessel)
            {
                throw new BridgeException(400, "The target port is on the active vessel.");
            }
            ModuleDockingNode ownPort;
            string ownSource;
            Part ownPart = Util.ResolvePart(req, "ownPortPart", vessel);
            Part controlPart = vessel.GetReferenceTransformPart();
            ModuleDockingNode controlPort = controlPart != null ? controlPart.FindModuleImplementing<ModuleDockingNode>() : null;
            if (ownPart != null)
            {
                ownPort = PortOf(ownPart, "Own");
                ownSource = "explicit";
            }
            else if (controlPort != null && PortReady(controlPort))
            {
                ownPort = controlPort;
                ownSource = "control-from part";
            }
            else
            {
                ownPort = SinglePort(vessel, "own", "ownPortPartId");
                ownSource = "only free port";
            }
            if (ownPort.vessel != vessel)
            {
                throw new BridgeException(400, "ownPortPartId is not on the active vessel.");
            }

            JObj applied = new JObj();
            double speedLimit = req.RequireNum("speedLimit");
            bool forceRoll = req.RequireBool("forceRoll");
            bool overrideSafe = req.RequireBool("overrideSafeDistance");
            bool overrideSize = req.Bool("overrideTargetSize", false);
            double roll = forceRoll ? req.RequireNum("roll") : 0;
            double safeDistance = overrideSafe ? req.RequireNum("safeDistance") : 0;
            double targetSize = overrideSize ? req.RequireNum("targetSize") : 0;
            dock.speedLimit.Val = speedLimit;
            dock.forceRol = forceRoll;
            if (forceRoll)
            {
                dock.rol.Val = roll;
            }
            dock.overrideSafeDistance = overrideSafe;
            if (overrideSafe)
            {
                dock.overridenSafeDistance.Val = safeDistance;
            }
            dock.overrideTargetSize = overrideSize;
            if (overrideSize)
            {
                dock.overridenTargetSize.Val = targetSize;
            }
            dock.drawBoundingBox = false;
            applied["speedLimit"] = speedLimit;
            applied["forceRoll"] = forceRoll;
            applied["overrideSafeDistance"] = overrideSafe;
            applied["overrideTargetSize"] = overrideSize;
            bool? rcs = Flag(req, "rcs", applied);
            if (rcs.HasValue)
            {
                vessel.ActionGroups.SetGroup(KSPActionGroup.RCS, rcs.Value);
            }

            List<object> warnings = new List<object>();
            if (ownPort.nodeType != targetPort.nodeType)
            {
                warnings.Add("Port types differ (own " + ownPort.nodeType + ", target " + targetPort.nodeType + "): they may not couple.");
            }
            if (!vessel.ActionGroups[KSPActionGroup.RCS])
            {
                warnings.Add("The RCS action group is off; MechJeb docks on RCS translation.");
            }

            Part previousControl = vessel.GetReferenceTransformPart();
            ownPort.MakeReferenceTransform(); // control from our port so MechJeb aligns the right axis
            SetTarget(core, targetPort);
            if (!core.Target.NormalTargetExists)
            {
                if (previousControl != null && previousControl != ownPort.part)
                {
                    vessel.SetReferenceTransform(previousControl, false); // a refused call leaves control-from as it was
                }
                throw new BridgeException(409, "MechJeb did not accept the target port.");
            }
            int partsBefore = vessel.parts.Count;
            dock.Users.Add(core);

            JObj eff = new JObj();
            eff["speedLimit"] = dock.speedLimit.Val;
            eff["forceRoll"] = dock.forceRol;
            eff["roll"] = dock.rol.Val;
            eff["overrideSafeDistance"] = dock.overrideSafeDistance;
            eff["safeDistance"] = dock.overridenSafeDistance.Val;
            eff["overrideTargetSize"] = dock.overrideTargetSize;
            eff["targetSize"] = dock.overridenTargetSize.Val;
            JObj d = new JObj();
            d["enabled"] = dock.Enabled;
            d["ownPort"] = Util.PartRef(ownPort.part);
            d["targetPort"] = Util.PartRef(targetPort.part);
            JObj sources = new JObj();
            sources["own"] = ownSource;
            sources["target"] = targetSource;
            d["portSelection"] = sources;
            d["targetVessel"] = Util.VesselRef(targetPort.vessel);
            d["previousControlFrom"] = previousControl != null ? Util.PartRef(previousControl) : null;
            d["partCountBefore"] = partsBefore;
            d["applied"] = applied;
            d["effective"] = eff;
            d["status"] = dock.status;
            d["warnings"] = warnings;
            d["note"] = "The autopilot also switches itself off on success: confirm docking by the own port state 'Docked' or a jump in part count (GET /mj-status).";
            return d;
        }

        // ------------------------------------------------------------------ maneuver planner

        private static JObj Plan(BridgeRequest req)
        {
            Vessel vessel;
            MechJebCore core = RequireCore(out vessel);
            string opName = req.Choice("operation", PlanOperations, null);
            bool place = req.Bool("place", false);
            string planFrom = req.Choice("planFrom", new[] { "current", "last_node" }, "current");
            bool replaceExisting = req.Bool("replaceExisting", false);
            List<ManeuverNode> existing = vessel.patchedConicSolver != null ? vessel.patchedConicSolver.maneuverNodes : new List<ManeuverNode>();
            if (replaceExisting && (!place || planFrom == "last_node"))
            {
                throw new BridgeException(400, "replaceExisting needs place=true and planFrom=current.");
            }
            if (place && planFrom == "current" && existing.Count > 0 && !replaceExisting)
            {
                throw new BridgeException(409, "The vessel already has " + existing.Count + " maneuver node(s); a plan from the current orbit would not match them.",
                    "Pass replaceExisting=true to delete them first, or planFrom=last_node to plan after the last node.");
            }

            ITargetable target = ResolveTarget(req, false);
            if (target != null)
            {
                SetTarget(core, target);
            }
            JObj settings = new JObj();
            Operation op = BuildOperation(opName, req, core, settings);
            ConfigureTime(op, req, settings);

            Orbit orbit = vessel.orbit;
            double ut = Planetarium.GetUniversalTime();
            if (planFrom == "last_node")
            {
                if (existing.Count == 0)
                {
                    throw new BridgeException(409, "planFrom=last_node needs an existing maneuver node.");
                }
                ManeuverNode last = existing[existing.Count - 1];
                orbit = last.nextPatch;
                if (orbit == null)
                {
                    throw new BridgeException(409, "The last maneuver node has no computed trajectory after it yet.",
                        "Let the game run a frame (or re-read the nodes) and retry.");
                }
                ut = last.UT;
            }

            List<ManeuverParameters> plan = op.MakeNodes(orbit, ut, core.Target);
            string error = op.GetErrorMessage();
            if (plan == null || plan.Count == 0)
            {
                throw new BridgeException(409, "MechJeb could not plan '" + opName + "': " + (string.IsNullOrEmpty(error) ? "no maneuver produced" : error),
                    "Check the target, time reference and operation parameters (settings are echoed in docs/BRIDGE_API.md).");
            }

            List<object> nodes = new List<object>();
            Orbit o = orbit;
            for (int i = 0; i < plan.Count; i++)
            {
                ManeuverParameters mp = plan[i];
                Vector3d local = o.DeltaVToManeuverNodeCoordinates(mp.UT, mp.dV);
                JObj n = new JObj();
                n["ut"] = mp.UT;
                n["timeTo_s"] = mp.UT - Planetarium.GetUniversalTime();
                n["dv_mps"] = mp.dV.magnitude;
                n["prograde_mps"] = local.z;
                n["normal_mps"] = local.y;
                n["radial_mps"] = local.x;
                // Later nodes are expressed on the orbit perturbed by the earlier burns (no SOI changes);
                // a first node past the end of its patch (an SOI change) is placed on another patch.
                n["componentsExact"] = i == 0 && WithinPatch(o, mp.UT);
                nodes.Add(n);
                o = o.PerturbedOrbit(mp.UT, mp.dV);
            }

            JObj d = new JObj();
            d["operation"] = opName;
            d["planFrom"] = planFrom;
            d["target"] = core.Target.NormalTargetExists ? TargetInfo(core) : null;
            d["settings"] = settings;
            d["nodes"] = nodes;
            d["placed"] = place;
            if (!string.IsNullOrEmpty(error))
            {
                d["plannerMessage"] = error;
            }
            if (place)
            {
                if (replaceExisting)
                {
                    while (vessel.patchedConicSolver.maneuverNodes.Count > 0)
                    {
                        vessel.patchedConicSolver.maneuverNodes[0].RemoveSelf();
                    }
                }
                foreach (ManeuverParameters mp in plan)
                {
                    vessel.PlaceManeuverNode(orbit, mp.dV, mp.UT);
                }
                d["vesselNodes"] = Nodes(vessel);
            }
            return d;
        }

        /// <summary>Whether ut lies on this patch (before an SOI change or impact ends it).</summary>
        private static bool WithinPatch(Orbit o, double ut)
        {
            switch (o.patchEndTransition)
            {
                case Orbit.PatchTransitionType.ENCOUNTER:
                case Orbit.PatchTransitionType.ESCAPE:
                case Orbit.PatchTransitionType.IMPACT:
                    return ut <= o.EndUT;
                default:
                    return true; // INITIAL, FINAL, MANEUVER: the conic itself continues
            }
        }

        private static Operation BuildOperation(string name, BridgeRequest req, MechJebCore core, JObj settings)
        {
            switch (name)
            {
                case "circularize":
                    return new OperationCircularize();
                case "apoapsis":
                {
                    OperationApoapsis op = new OperationApoapsis();
                    op.NewApA.Val = Required(req, "newApoapsis", settings);
                    return op;
                }
                case "periapsis":
                {
                    OperationPeriapsis op = new OperationPeriapsis();
                    op.NewPeA.Val = Required(req, "newPeriapsis", settings);
                    return op;
                }
                case "ellipticize":
                {
                    OperationEllipticize op = new OperationEllipticize();
                    op.NewApA.Val = Required(req, "newApoapsis", settings);
                    op.NewPeA.Val = Required(req, "newPeriapsis", settings);
                    return op;
                }
                case "eccentricity":
                {
                    OperationEccentricity op = new OperationEccentricity();
                    op.NewEcc.Val = Required(req, "newEccentricity", settings);
                    return op;
                }
                case "semi_major":
                {
                    OperationSemiMajor op = new OperationSemiMajor();
                    op.NewSma.Val = Required(req, "newSemiMajorAxis", settings);
                    return op;
                }
                case "inclination":
                {
                    OperationInclination op = new OperationInclination();
                    op.NewInc.Val = Required(req, "newInclination", settings);
                    return op;
                }
                case "lan":
                    // MechJeb reads the new LAN from the target controller's longitude field.
                    core.Target.targetLongitude = Required(req, "newLan", settings);
                    return new OperationLan();
                case "longitude":
                    core.Target.targetLongitude = Required(req, "newLongitude", settings);
                    return new OperationLongitude();
                case "plane":
                    return new OperationPlane();
                case "kill_rel_vel":
                    return new OperationKillRelVel();
                case "hohmann":
                {
                    OperationGeneric op = new OperationGeneric();
                    op.Rendezvous = RequiredFlag(req, "rendezvous", settings);
                    op.Capture = RequiredFlag(req, "capture", settings);
                    bool? b = req.Bool("planCapture");
                    op.PlanCapture = b.HasValue ? b.Value : op.Capture;
                    b = req.Bool("coplanar");
                    if (b.HasValue)
                    {
                        op.Coplanar = b.Value;
                    }
                    double? lag = req.Num("lagTime");
                    if (lag.HasValue)
                    {
                        op.LagTime.Val = lag.Value;
                    }
                    settings["planCapture"] = op.PlanCapture;
                    settings["coplanar"] = op.Coplanar;
                    settings["lagTime"] = op.LagTime.Val;
                    return op;
                }
                case "lambert":
                {
                    OperationLambert op = new OperationLambert();
                    op.InterceptInterval.Val = Required(req, "interceptInterval", settings);
                    return op;
                }
                case "interplanetary":
                {
                    OperationInterplanetaryTransfer op = new OperationInterplanetaryTransfer();
                    op.WaitForPhaseAngle = RequiredFlag(req, "waitForPhaseAngle", settings);
                    return op;
                }
                case "course_correction":
                {
                    OperationCourseCorrection op = new OperationCourseCorrection();
                    if (core.Target.Target is CelestialBody)
                    {
                        op.CourseCorrectFinalPeA.Val = Required(req, "finalPeriapsis", settings);
                    }
                    else
                    {
                        op.InterceptDistance.Val = Required(req, "interceptDistance", settings);
                    }
                    return op;
                }
                case "moon_return":
                {
                    OperationMoonReturn op = new OperationMoonReturn();
                    op.MoonReturnAltitude.Val = Required(req, "moonReturnAltitude", settings);
                    return op;
                }
                case "resonant_orbit":
                {
                    OperationResonantOrbit op = new OperationResonantOrbit();
                    op.ResonanceNumerator.Val = req.RequireInt("resonanceNumerator");
                    op.ResonanceDenominator.Val = req.RequireInt("resonanceDenominator");
                    settings["resonanceNumerator"] = op.ResonanceNumerator.Val;
                    settings["resonanceDenominator"] = op.ResonanceDenominator.Val;
                    return op;
                }
                default:
                    throw new BridgeException(400, "Unsupported operation '" + name + "'.");
            }
        }

        private static double Required(BridgeRequest req, string key, JObj settings)
        {
            double v = req.RequireNum(key);
            settings[key] = v;
            return v;
        }

        private static bool RequiredFlag(BridgeRequest req, string key, JObj settings)
        {
            bool v = req.RequireBool(key);
            settings[key] = v;
            return v;
        }

        /// <summary>
        /// Sets the operation's time selector (a private field, static for most operations). timeRef is
        /// required whenever the operation has one; its allowed values are listed in the error.
        /// </summary>
        private static void ConfigureTime(Operation op, BridgeRequest req, JObj settings)
        {
            FieldInfo field = op.GetType().GetField("_timeSelector", BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance);
            if (field == null)
            {
                if (req.Has("timeRef"))
                {
                    throw new BridgeException(400, "This operation computes its own burn time; drop timeRef.");
                }
                settings["timeRef"] = "computed";
                return;
            }
            TimeSelector selector = (TimeSelector)(field.IsStatic ? field.GetValue(null) : field.GetValue(op));
            FieldInfo allowedField = typeof(TimeSelector).GetField("_allowedTimeRef", BindingFlags.Instance | BindingFlags.NonPublic);
            TimeReference[] allowed = selector != null && allowedField != null ? (TimeReference[])allowedField.GetValue(selector) : null;
            if (selector == null || allowed == null || allowed.Length == 0)
            {
                throw new BridgeException(500, "Could not read this MechJeb operation's time selector.");
            }
            string[] names = new string[allowed.Length];
            for (int i = 0; i < allowed.Length; i++)
            {
                names[i] = allowed[i].ToString().ToLowerInvariant();
            }
            string choice = req.Choice("timeRef", names, allowed.Length == 1 ? names[0] : null);
            int index = Array.IndexOf(names, choice);
            selector._currentTimeRef = index;
            settings["timeRef"] = choice;
            settings["allowedTimeRefs"] = names;
            if (allowed[index] == TimeReference.X_FROM_NOW)
            {
                selector.LeadTime.Val = Required(req, "leadTime", settings);
            }
            else if (allowed[index] == TimeReference.ALTITUDE)
            {
                selector.CircularizeAltitude.Val = Required(req, "atAltitude", settings);
            }
        }

        // ------------------------------------------------------------------ abort and status

        private static JObj Abort(BridgeRequest req)
        {
            Vessel vessel;
            MechJebCore core = RequireCore(out vessel);
            List<string> modules = req.StrList("modules");
            if (modules == null || modules.Count == 0)
            {
                throw new BridgeException(400, "Missing required parameter 'modules' (comma list of: " + string.Join(", ", AbortModules) + ").");
            }
            HashSet<string> wanted = new HashSet<string>();
            foreach (string m in modules)
            {
                string key = m.Trim().ToLowerInvariant();
                if (Array.IndexOf(AbortModules, key) < 0)
                {
                    throw new BridgeException(400, "Unknown module '" + m + "' (allowed: " + string.Join(", ", AbortModules) + ").");
                }
                wanted.Add(key);
            }
            bool all = wanted.Contains("all");
            JObj released = new JObj();
            if (all || wanted.Contains("ascent"))
            {
                bool was = AscentEngaged(core);
                MechJebModuleAscentClassicAutopilot classic = core.GetComputerModule<MechJebModuleAscentClassicAutopilot>();
                MechJebModuleAscentPVGAutopilot pvg = core.GetComputerModule<MechJebModuleAscentPVGAutopilot>();
                if (classic != null)
                {
                    classic.Users.Clear();
                }
                if (pvg != null)
                {
                    pvg.Users.Clear();
                }
                released["ascent"] = Change(was, AscentEngaged(core));
            }
            if (all || wanted.Contains("landing"))
            {
                MechJebModuleLandingAutopilot land = core.Landing;
                bool was = land.Enabled;
                if (was)
                {
                    land.StopLanding();
                }
                land.Users.Clear();
                released["landing"] = Change(was, land.Enabled);
            }
            if (all || wanted.Contains("node"))
            {
                MechJebModuleNodeExecutor ex = core.Node;
                bool was = ex.Enabled;
                ex.Abort();
                ex.Users.Clear();
                released["node"] = Change(was, ex.Enabled);
            }
            if (all || wanted.Contains("rendezvous"))
            {
                MechJebModuleRendezvousAutopilot rv = core.GetComputerModule<MechJebModuleRendezvousAutopilot>();
                bool was = rv.Enabled;
                rv.Users.Clear();
                released["rendezvous"] = Change(was, rv.Enabled);
            }
            if (all || wanted.Contains("dock"))
            {
                MechJebModuleDockingAutopilot dock = core.GetComputerModule<MechJebModuleDockingAutopilot>();
                bool was = dock.Enabled;
                dock.Users.Clear();
                released["dock"] = Change(was, dock.Enabled);
            }
            if (all || wanted.Contains("staging"))
            {
                bool was = core.Staging.Enabled;
                core.Staging.Users.Clear();
                released["staging"] = Change(was, core.Staging.Enabled);
            }
            if (all || wanted.Contains("smartass"))
            {
                MechJebModuleSmartASS smartass = core.GetComputerModule<MechJebModuleSmartASS>();
                if (smartass != null)
                {
                    bool was = smartass.target != MechJebModuleSmartASS.Target.OFF;
                    smartass.target = MechJebModuleSmartASS.Target.OFF;
                    smartass.Engage(true);
                    released["smartass"] = Change(was, false);
                }
            }
            if (all || wanted.Contains("predictor"))
            {
                MechJebModuleLandingPredictions pred = core.GetComputerModule<MechJebModuleLandingPredictions>();
                if (pred != null)
                {
                    bool was = pred.Enabled;
                    pred.Users.Remove(core); // the landing autopilot keeps its own claim while it runs
                    released["predictor"] = Change(was, pred.Enabled);
                }
            }
            if (all)
            {
                core.Attitude.attitudeDeactivate();
            }
            if (req.Bool("zeroThrottle", false))
            {
                FlightInputHandler.state.mainThrottle = 0f;
                vessel.ctrlState.mainThrottle = 0f;
            }
            JObj d = new JObj();
            d["modules"] = released;
            d["attitudeControllerEnabled"] = core.Attitude.Enabled;
            d["throttleControllerEnabled"] = core.Thrust.Enabled;
            d["mainThrottle"] = FlightInputHandler.state.mainThrottle;
            d["note"] = "MechJeb leaves the throttle where its last command put it; pass zeroThrottle=true or set the throttle yourself.";
            return d;
        }

        private static JObj Change(bool was, bool now)
        {
            JObj o = new JObj();
            o["wasEnabled"] = was;
            o["enabled"] = now;
            return o;
        }

        private static JObj Status(BridgeRequest req)
        {
            JObj d = new JObj();
            bool flight = HighLogic.LoadedSceneIsFlight && FlightGlobals.ActiveVessel != null;
            d["flight"] = flight;
            if (!flight)
            {
                return d;
            }
            Vessel vessel = FlightGlobals.ActiveVessel;
            MechJebCore core = vessel.GetMasterMechJeb();
            d["hasCore"] = core != null;
            JObj v = Util.VesselRef(vessel);
            v["partCount"] = vessel.parts.Count;
            v["apoapsisAlt_m"] = vessel.orbit != null ? vessel.orbit.ApA : double.NaN;
            v["periapsisAlt_m"] = vessel.orbit != null ? vessel.orbit.PeA : double.NaN;
            Part control = vessel.GetReferenceTransformPart();
            v["controlFrom"] = control != null ? Util.PartRef(control) : null;
            d["vessel"] = v;
            List<object> ports = new List<object>();
            foreach (Part p in vessel.parts)
            {
                ModuleDockingNode port = p.FindModuleImplementing<ModuleDockingNode>();
                if (port != null)
                {
                    JObj po = Util.PartRef(p);
                    po["state"] = port.state;
                    po["nodeType"] = port.nodeType;
                    ports.Add(po);
                }
            }
            d["dockingPorts"] = ports;
            d["nodes"] = Nodes(vessel);
            if (core == null)
            {
                return d;
            }
            d["version"] = core.version;

            JObj m = new JObj();
            MechJebModuleAscentSettings s = core.AscentSettings;
            MechJebModuleAscentBaseAutopilot ascent = s != null ? s.AscentAutopilot : null;
            JObj a = new JObj();
            a["enabled"] = AscentEngaged(core);
            a["type"] = s != null ? s.AscentType.ToString() : null;
            a["status"] = ascent != null ? ascent.Status : null;
            m["ascent"] = a;

            MechJebModuleLandingAutopilot land = core.Landing;
            JObj l = new JObj();
            l["enabled"] = land.Enabled;
            l["status"] = land.Enabled ? land.Status : null;
            l["landAtTarget"] = land.LandAtTarget;
            m["landing"] = l;

            MechJebModuleNodeExecutor ex = core.Node;
            JObj n = new JObj();
            n["enabled"] = ex.Enabled;
            n["state"] = ex.State.ToString();
            n["autowarp"] = ex.Autowarp;
            m["node"] = n;

            MechJebModuleRendezvousAutopilot rv = core.GetComputerModule<MechJebModuleRendezvousAutopilot>();
            JObj r = new JObj();
            r["enabled"] = rv.Enabled;
            r["status"] = rv.status;
            m["rendezvous"] = r;

            MechJebModuleDockingAutopilot dock = core.GetComputerModule<MechJebModuleDockingAutopilot>();
            JObj dk = new JObj();
            dk["enabled"] = dock.Enabled;
            dk["status"] = dock.status;
            dk["step"] = dock.dockingStep.ToString();
            m["dock"] = dk;

            JObj sg = new JObj();
            sg["enabled"] = core.Staging.Enabled;
            try
            {
                sg["status"] = core.Staging.AutostageStatus();
            }
            catch (Exception)
            {
                sg["status"] = null;
            }
            m["staging"] = sg;

            MechJebModuleSmartASS smartass = core.GetComputerModule<MechJebModuleSmartASS>();
            JObj sa = new JObj();
            sa["target"] = smartass != null ? smartass.target.ToString() : null;
            sa["mode"] = smartass != null ? smartass.mode.ToString() : null;
            m["smartass"] = sa;

            MechJebModuleLandingPredictions pred = core.GetComputerModule<MechJebModuleLandingPredictions>();
            JObj pr = new JObj();
            pr["enabled"] = pred != null && pred.Enabled;
            m["predictor"] = pr;

            JObj at = new JObj();
            at["enabled"] = core.Attitude.Enabled;
            m["attitude"] = at;
            JObj th = new JObj();
            th["enabled"] = core.Thrust.Enabled;
            m["thrust"] = th;
            d["modules"] = m;
            d["target"] = TargetInfo(core);
            return d;
        }

        // ------------------------------------------------------------------ stage stats

        private static JObj StageStats(BridgeRequest req)
        {
            MechJebCore core = null;
            CelestialBody gravityBody;
            string scene;
            if (HighLogic.LoadedSceneIsFlight && FlightGlobals.ActiveVessel != null)
            {
                Vessel vessel;
                core = RequireCore(out vessel);
                gravityBody = vessel.mainBody;
                scene = "flight";
            }
            else if (HighLogic.LoadedSceneIsEditor && EditorLogic.fetch != null && EditorLogic.fetch.ship != null)
            {
                foreach (Part p in EditorLogic.fetch.ship.parts)
                {
                    core = p.FindModuleImplementing<MechJebCore>();
                    if (core != null)
                    {
                        break;
                    }
                }
                if (core == null)
                {
                    throw new BridgeException(409, "The editor ship has no MechJeb core (needs a command part with MechJebCore).");
                }
                gravityBody = null;
                scene = "editor";
            }
            else
            {
                throw new BridgeException(409, "Stage stats need the flight scene or an editor with a ship (scene " + HighLogic.LoadedScene + ").");
            }

            MechJebModuleStageStats stats = core.GetComputerModule<MechJebModuleStageStats>();
            if (scene == "editor")
            {
                string bodyName = req.Str("body");
                if (bodyName != null)
                {
                    stats.EditorBody = Util.RequireBody(bodyName);
                }
                double? alt = req.Num("altitude");
                if (alt.HasValue)
                {
                    stats.AltSLT = alt.Value;
                }
                double? mach = req.Num("mach");
                if (mach.HasValue)
                {
                    stats.Mach = mach.Value;
                }
                gravityBody = stats.EditorBody ?? FlightGlobals.GetHomeBody();
            }
            stats.RequestUpdate();
            double g = gravityBody != null ? gravityBody.GeeASL : 1.0;
            JObj d = new JObj();
            d["scene"] = scene;
            d["pending"] = stats.VacStats.Count == 0 && stats.AtmoStats.Count == 0;
            d["body"] = gravityBody != null ? gravityBody.bodyName : null;
            d["atmoAltitude_m"] = stats.AltSLT;
            d["mach"] = stats.Mach;
            d["liveAtmosphere"] = stats.LiveSLT;
            d["vac"] = StageList(stats.VacStats, g);
            d["atmo"] = StageList(stats.AtmoStats, g);
            if ((bool)d["pending"])
            {
                d["note"] = "MechJeb simulates asynchronously; poll again in a moment.";
            }
            return d;
        }

        private static JObj StageList(List<FuelStats> list, double geeAsl)
        {
            List<object> stages = new List<object>();
            double dv = 0;
            double time = 0;
            for (int i = 0; i < list.Count; i++)
            {
                FuelStats f = list[i];
                JObj o = new JObj();
                o["index"] = i;
                o["kspStage"] = f.KSPStage;
                o["deltaV_mps"] = f.DeltaV;
                o["burnTime_s"] = f.DeltaTime;
                o["startMass_t"] = f.StartMass;
                o["endMass_t"] = f.EndMass;
                o["stagedMass_t"] = f.StagedMass;
                o["resourceMass_t"] = f.ResourceMass;
                o["thrust_kn"] = f.Thrust;
                o["isp_s"] = f.Isp;
                o["maxAccel_mps2"] = f.MaxAccel;
                o["startTwr"] = f.StartTWR(geeAsl);
                o["maxTwr"] = f.MaxTWR(geeAsl);
                stages.Add(o);
                dv += f.DeltaV;
                time += f.DeltaTime;
            }
            JObj d = new JObj();
            d["stages"] = stages;
            d["totalDeltaV_mps"] = dv;
            d["totalBurnTime_s"] = time;
            return d;
        }
    }
}
