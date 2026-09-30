using System;
using System.Collections.Generic;
using System.Reflection;
using System.Threading;
using UnityEngine;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    /// <summary>
    /// EVA: go outside, walk, board through a hatch, plant a flag. kRPC cannot do any of these.
    /// Every call names its kerbal explicitly; nothing picks "the first" kerbal or seat.
    /// </summary>
    internal static class EvaRoutes
    {
        private const int PollIntervalMs = 250;

        // A kerbal at the foot of a lander ladder can climb to the hatch, which the bridge cannot drive;
        // boarding from farther away would be a teleport. Roughly one ladder height.
        private const double BoardReachM = 5.0;

        // The part whose hatch trigger the kerbal touches (private in KerbalEVA; null if a KSP update renamed it).
        private static readonly FieldInfo AirlockPartField = typeof(KerbalEVA).GetField("currentAirlockPart", BindingFlags.Instance | BindingFlags.NonPublic);

        public static void Register(Router r)
        {
            r.Main("POST", "/eva-go", 15000, Go, "Send a named seated kerbal on EVA (any situation; warnings instead of a landed-only rule).");
            r.Main("POST", "/eva-board", 15000, Board, "Board an EVA kerbal through the hatch it is touching (optionally a specific part).");
            r.Main("POST", "/eva-walk-to", 15000, WalkTo, "Walk an EVA kerbal to lat/lon or bearing+distance (stock walking, driven each physics tick).");
            r.Main("POST", "/eva-hop", 15000, HopTo, "Jetpack hop {crew, bearing, distance, rise, maxS} (or {crew, stop: true}): fly beside the start at a height, then stow the pack and drop.");
            r.Main("GET", "/eva-status", 15000, Status, "EVA kerbals in physics range: position, FSM state, ladder, jetpack, flags carried, hatch in reach.");
            r.Main("POST", "/eva-status", 15000, Status, "Same as GET /eva-status; body {crew} narrows to one kerbal.");
            r.Inline("POST", "/eva-plant-flag", PlantFlag, "Plant a flag with an EVA kerbal; verified when a new Flag vessel appears and is named.");
            r.Inline("POST", "/eva-flag", GoAndPlant, "Convenience: /eva-go, wait until standing on the ground, then /eva-plant-flag.");
        }

        // ------------------------------------------------------------------ helpers

        /// <summary>The loaded EVA vessel of the named kerbal, or null.</summary>
        internal static Vessel FindEvaVessel(string crew)
        {
            if (FlightGlobals.VesselsLoaded == null)
            {
                return null;
            }
            foreach (Vessel v in FlightGlobals.VesselsLoaded)
            {
                if (v == null || !v.isEVA)
                {
                    continue;
                }
                if (string.Equals(v.vesselName, crew, StringComparison.OrdinalIgnoreCase))
                {
                    return v;
                }
                foreach (ProtoCrewMember pcm in v.GetVesselCrew())
                {
                    if (pcm != null && string.Equals(pcm.name, crew, StringComparison.OrdinalIgnoreCase))
                    {
                        return v;
                    }
                }
            }
            return null;
        }

        private static Vessel RequireEvaVessel(string crew)
        {
            Vessel v = FindEvaVessel(crew);
            if (v == null)
            {
                Part seat;
                bool seated = Util.FindSeatedKerbal(crew, out seat) != null;
                throw new BridgeException(404, crew + " is not on EVA in physics range" + (seated ? " (still seated in " + Util.PartTitle(seat) + ")." : "."),
                    seated ? "POST /eva-go {crew} first." : "GET /crew-list shows where every loaded kerbal is.");
            }
            return v;
        }

        private static KerbalEVA EvaModule(Vessel v)
        {
            KerbalEVA eva = v.evaController;
            if (eva == null && v.parts != null && v.parts.Count > 0)
            {
                eva = v.parts[0].FindModuleImplementing<KerbalEVA>();
            }
            if (eva == null)
            {
                throw new BridgeException(500, v.vesselName + " has no KerbalEVA module.");
            }
            return eva;
        }

        private static string FsmState(KerbalEVA eva)
        {
            return eva.fsm != null && eva.fsm.currentStateName != null ? eva.fsm.currentStateName : "";
        }

        private static Part AirlockPart(KerbalEVA eva)
        {
            return AirlockPartField != null ? AirlockPartField.GetValue(eva) as Part : null;
        }

        private static double HatchDistance(Vessel kerbal, Part part)
        {
            Vector3d hatch = part.airlock != null ? (Vector3d)part.airlock.position : (Vector3d)part.transform.position;
            return Vector3d.Distance(kerbal.GetWorldPos3D(), hatch);
        }

        private static string Biome(Vessel v)
        {
            try
            {
                return v.mainBody != null ? ScienceUtil.GetExperimentBiome(v.mainBody, v.latitude, v.longitude) : null;
            }
            catch (Exception)
            {
                return null;
            }
        }

        private static bool GamePaused()
        {
            return FlightDriver.Pause || Time.timeScale == 0f;
        }

        /// <summary>
        /// The stock EVA rules that FlightEVA.spawnEVA itself does NOT check (the crew portrait's
        /// KerbalPortrait.CanEVA and GameVariables.GetEVALockedReason apply them before calling it):
        /// the difficulty's CanEVA, tourists, inactive kerbals, parts flagged NoAutoEVA, and the career
        /// Astronaut Complex level (before the upgrade, EVA only while landed or splashed on the home
        /// body). Null when EVA is allowed. Skipping these would let career saves bypass progression.
        /// </summary>
        internal static string EvaLockedReason(ProtoCrewMember pcm, Part part, Vessel from)
        {
            if (!HighLogic.CurrentGame.Parameters.Flight.CanEVA)
            {
                return "EVA is disabled in this game's difficulty settings";
            }
            if (pcm.type == ProtoCrewMember.KerbalType.Tourist)
            {
                return "tourists do not go on EVA";
            }
            if (pcm.inactive)
            {
                return "the kerbal is inactive";
            }
            if (part.NoAutoEVA)
            {
                return Util.PartTitle(part) + " does not allow EVA from its crew";
            }
            GameVariables rules = GameVariables.Instance;
            if (rules != null)
            {
                bool unlocked = rules.UnlockedEVA(ScenarioUpgradeableFacilities.GetFacilityLevel(SpaceCenterFacility.AstronautComplex));
                if (!rules.EVAIsPossible(unlocked, from))
                {
                    return unlocked
                        ? "KSP does not allow EVA right now (time warp?)"
                        : "the Astronaut Complex is not upgraded, so EVA is only possible landed or splashed on "
                          + (Planetarium.fetch != null && Planetarium.fetch.Home != null ? Planetarium.fetch.Home.bodyName : "the home body");
                }
            }
            return null;
        }

        /// <summary>Runs work on the main thread from an inline (HTTP-thread) handler, surfacing failures.</summary>
        private static JObj OnMain(string name, Handler work, BridgeRequest req, int timeoutMs)
        {
            BridgeResult r = AutomationBridgeAddon.Jobs.Run(name, delegate { return Router.Execute(work, req); }, timeoutMs, req.ClientGone);
            if (!r.Ok)
            {
                throw new BridgeException(r.Status, r.Error, r.Hint);
            }
            return r.Data;
        }

        // ------------------------------------------------------------------ go

        private static JObj Go(BridgeRequest req)
        {
            Util.RequireFlightScene();
            string crew = req.RequireStr("crew");
            Part part;
            ProtoCrewMember pcm = Util.FindSeatedKerbal(crew, out part);
            if (pcm == null)
            {
                Vessel already = FindEvaVessel(crew);
                throw new BridgeException(already != null ? 409 : 404,
                    already != null ? crew + " is already on EVA." : "No seated kerbal named '" + crew + "' in a loaded vessel.",
                    already != null ? "GET /eva-status {crew}." : "GET /crew-list shows who sits where.");
            }
            Vessel from = part.vessel;
            if (TimeWarp.CurrentRateIndex > 0)
            {
                throw new BridgeException(409, "Time warp is active; KSP cannot spawn an EVA kerbal while warping.", "Stop warp first.");
            }
            if (part.airlock == null)
            {
                throw new BridgeException(409, Util.PartTitle(part) + " has no hatch (airlock): " + pcm.name + " cannot exit from it.",
                    "Transfer the kerbal to a part with a hatch (POST /transfer-crew) first.");
            }
            string locked = EvaLockedReason(pcm, part, from);
            if (locked != null)
            {
                throw new BridgeException(409, pcm.name + " may not go on EVA: " + locked + ".",
                    "These are the game's own EVA rules (the crew portrait's EVA button applies the same checks).");
            }
            List<object> warnings = new List<object>();
            if (!from.LandedOrSplashed)
            {
                warnings.Add("Vessel is " + from.situation + ", not landed: " + pcm.name + " will float free; grab the ladder or use the jetpack.");
            }
            if (from.mainBody != null && from.mainBody.atmosphere && from.altitude < from.mainBody.atmosphereDepth && from.srfSpeed > 5)
            {
                warnings.Add("Moving at " + from.srfSpeed.ToString("0.0") + " m/s inside the atmosphere: aerodynamic forces act on the kerbal.");
            }

            // tryAllHatches lets KSP fall back to another hatch when the primary one is blocked.
            KerbalEVA eva = FlightEVA.fetch != null ? FlightEVA.fetch.spawnEVA(pcm, part, part.airlock, true) : null;
            if (eva == null)
            {
                bool obstructed = false;
                try
                {
                    obstructed = FlightEVA.HatchIsObstructed(part, part.airlock);
                }
                catch (Exception)
                {
                    // Diagnostic only.
                }
                throw new BridgeException(409, "KSP refused the EVA from " + Util.PartTitle(part)
                    + (obstructed ? ": the hatch is obstructed." : " (spawnEVA returned null)."),
                    obstructed ? "Parts or terrain block the exit; move the kerbal to another crew part (POST /transfer-crew)."
                        : "Check the career EVA unlock and that no menu or dialog is open.");
            }
            Vessel ev = eva.vessel;
            JObj d = new JObj();
            d["crew"] = pcm.name;
            d["evaVessel"] = Util.VesselRef(ev);
            d["fromVessel"] = Util.VesselRef(from);
            d["fromPart"] = Util.PartRef(part);
            d["body"] = ev.mainBody != null ? ev.mainBody.bodyName : null;
            d["biome"] = Biome(ev);
            d["latitude"] = ev.latitude;
            d["longitude"] = ev.longitude;
            d["activeVessel"] = FlightGlobals.ActiveVessel != null ? FlightGlobals.ActiveVessel.vesselName : null;
            d["warnings"] = warnings;
            return d;
        }

        // ------------------------------------------------------------------ board

        private static JObj Board(BridgeRequest req)
        {
            Util.RequireFlightScene();
            string crew = req.RequireStr("crew");
            Vessel ev = RequireEvaVessel(crew);
            KerbalEVA eva = EvaModule(ev);
            Part touching = AirlockPart(eva);
            Part target = Util.ResolvePart(req, "part", null);
            string partName = req.Str("part");
            if (target == null && partName != null)
            {
                target = NearestCrewPart(ev, partName);
                if (target == null)
                {
                    throw new BridgeException(404, "No crew part named '" + partName + "' with a hatch and a free seat in physics range.",
                        "Use the internal part name (GET /vessel-parts) or partId.");
                }
            }
            if (target == null)
            {
                target = touching ?? NearestCrewPart(ev, null);
            }
            if (target == null)
            {
                throw new BridgeException(409, "No crew part with a free seat and a hatch is in physics range of " + crew + ".");
            }
            if (target.airlock == null)
            {
                throw new BridgeException(409, Util.PartTitle(target) + " has no hatch to board through.");
            }
            double distance = HatchDistance(ev, target);
            if (touching != target && distance > BoardReachM)
            {
                throw new BridgeException(409, crew + " is " + distance.ToString("0.0") + " m from the hatch of " + Util.PartTitle(target)
                    + " (boarding reach is " + BoardReachM.ToString("0") + " m).",
                    "Walk closer first (POST /eva-walk-to toward the vessel); boarding never teleports.");
            }
            int occupied = target.protoModuleCrew != null ? target.protoModuleCrew.Count : 0;
            if (target.CrewCapacity <= 0 || occupied >= target.CrewCapacity)
            {
                throw new BridgeException(409, Util.PartTitle(target) + " has no free seat (" + occupied + "/" + target.CrewCapacity + ").");
            }
            if (!HighLogic.CurrentGame.Parameters.Flight.CanBoard)
            {
                // KerbalEVA.BoardPart only shows a screen message and returns in this case.
                throw new BridgeException(409, "Boarding is disabled in this game's difficulty settings.");
            }
            List<ProtoCrewMember> evaCrew = ev.GetVesselCrew();
            ProtoCrewMember pcm = evaCrew != null && evaCrew.Count > 0 ? evaCrew[0] : null;
            Vessel into = target.vessel;
            eva.BoardPart(target);
            // KerbalEVA.BoardPart seats the kerbal synchronously, unless the kerbal's inventory cannot be
            // stored in the part or KSP asks what to do with carried science (a dialog in the game).
            bool boarded = pcm != null && target.protoModuleCrew != null && target.protoModuleCrew.Contains(pcm);
            JObj d = new JObj();
            d["crew"] = crew;
            d["part"] = Util.PartRef(target);
            d["vessel"] = Util.VesselRef(into);
            d["distanceToHatch_m"] = distance;
            d["boarded"] = boarded;
            d["note"] = boarded
                ? "Seated; KSP finishes switching to the vessel over the next frames (GET /crew-list)."
                : "Not seated yet: KSP is probably waiting on a dialog about carried science, or refused because the kerbal's inventory does not fit in the part (see the screen: camera_look). The kerbal is still on EVA.";
            return d;
        }

        // ------------------------------------------------------------------ walk

        private static JObj WalkTo(BridgeRequest req)
        {
            Util.RequireFlightScene();
            string crew = req.RequireStr("crew");
            Vessel ev = RequireEvaVessel(crew);
            KerbalEVA eva = EvaModule(ev);
            if (req.Bool("stop", false))
            {
                return StopMoving(crew, eva, ev);
            }
            if (!EvaWalker.Available)
            {
                throw new BridgeException(503, "Walking is unavailable: " + EvaWalker.UnavailableReason + ".",
                    "KSP has no built-in walk-to; the bridge drives the stock walk through a Harmony patch.");
            }
            double arrivalRadius = req.Num("arrivalRadius") ?? 1.0;
            if (arrivalRadius <= 0)
            {
                throw new BridgeException(400, "arrivalRadius must be positive (metres).");
            }
            CelestialBody body = ev.mainBody;
            double radius = body.Radius;
            double curLat = ev.latitude;
            double curLon = ev.longitude;
            double lat;
            double lon;
            if (req.Has("lat") || req.Has("lon"))
            {
                lat = req.RequireNum("lat");
                lon = req.RequireNum("lon");
                if (lat < -90.0 || lat > 90.0)
                {
                    throw new BridgeException(400, "lat must be within -90..90 degrees (got " + lat + ").");
                }
                lon = ((lon % 360.0) + 540.0) % 360.0 - 180.0;
            }
            else if (req.Has("bearing") || req.Has("distance"))
            {
                // Direct great-circle problem on the body's sphere: start point, initial bearing, distance.
                double bearing = req.RequireNum("bearing") * Math.PI / 180.0;
                double distanceM = req.RequireNum("distance");
                if (distanceM <= 0)
                {
                    throw new BridgeException(400, "distance must be positive (metres; turn with bearing instead of walking backwards).");
                }
                double angular = distanceM / radius;
                double phi1 = curLat * Math.PI / 180.0;
                double lam1 = curLon * Math.PI / 180.0;
                double phi2 = Math.Asin(Math.Sin(phi1) * Math.Cos(angular) + Math.Cos(phi1) * Math.Sin(angular) * Math.Cos(bearing));
                double lam2 = lam1 + Math.Atan2(Math.Sin(bearing) * Math.Sin(angular) * Math.Cos(phi1),
                    Math.Cos(angular) - Math.Sin(phi1) * Math.Sin(phi2));
                lat = phi2 * 180.0 / Math.PI;
                lon = ((lam2 * 180.0 / Math.PI) + 540.0) % 360.0 - 180.0;
            }
            else
            {
                throw new BridgeException(400, "Give either lat+lon (degrees) or bearing (degrees clockwise from north) + distance (m).");
            }

            List<object> warnings = new List<object>();
            if (!ev.Landed)
            {
                warnings.Add(crew + " is " + ev.situation + ", not standing on the ground: the walk starts once the kerbal has surface contact.");
            }
            if (eva.OnALadder)
            {
                warnings.Add(crew + " is on a ladder; walking starts after letting go.");
            }
            double terrainAlt = body.TerrainAltitude(lat, lon, true);
            if (body.ocean && terrainAlt < 0)
            {
                warnings.Add("The target is under water (terrain " + terrainAlt.ToString("0") + " m).");
            }
            double targetAlt = body.ocean ? Math.Max(terrainAlt, 0.0) : terrainAlt;
            if (EvaWalker.Start(eva, ev, lat, lon, targetAlt, arrivalRadius))
            {
                warnings.Add("The hop in progress was ended (pack stowed): the kerbal drops, then walks once it stands.");
            }
            else if (eva.JetpackDeployed)
            {
                warnings.Add("The jetpack is deployed; a kerbal flying the pack does not walk.");
            }

            double distance;
            double bearingDeg;
            Geodesic(curLat, curLon, lat, lon, radius, out distance, out bearingDeg);
            JObj d = new JObj();
            d["crew"] = crew;
            d["body"] = body.bodyName;
            d["fromLatitude"] = curLat;
            d["fromLongitude"] = curLon;
            d["targetLatitude"] = lat;
            d["targetLongitude"] = lon;
            d["targetTerrainAlt_m"] = terrainAlt;
            d["distance_m"] = distance;
            d["bearing_deg"] = bearingDeg;
            d["bodyRadius_m"] = radius;
            d["arrivalRadius_m"] = arrivalRadius;
            d["walking"] = true;
            d["warnings"] = warnings;
            d["note"] = "The kerbal walks in a straight line while the game runs (unpause); poll GET /eva-status: walk.state becomes 'arrived'. Obstacles and steep slopes can stall it; POST /eva-walk-to {crew, stop: true} cancels.";
            return d;
        }

        /// <summary>{stop: true} on /eva-walk-to or /eva-hop: cancel the kerbal's walk and hop.</summary>
        private static JObj StopMoving(string crew, KerbalEVA eva, Vessel ev)
        {
            JObj stopped = new JObj();
            stopped["crew"] = crew;
            stopped["stopped"] = EvaWalker.Stop(eva, ev);
            stopped["walk"] = EvaWalker.Status(ev);
            stopped["hop"] = EvaWalker.HopStatus(ev);
            return stopped;
        }

        private static JObj HopTo(BridgeRequest req)
        {
            Util.RequireFlightScene();
            string crew = req.RequireStr("crew");
            Vessel ev = RequireEvaVessel(crew);
            KerbalEVA eva = EvaModule(ev);
            if (req.Bool("stop", false))
            {
                return StopMoving(crew, eva, ev);
            }
            if (!EvaWalker.HopAvailable)
            {
                throw new BridgeException(503, "Jetpack hops are unavailable: " + (EvaWalker.UnavailableReason ?? "KerbalEVA.packTgtRPos/linPower not found") + ".");
            }
            if (!eva.HasJetpack)
            {
                throw new BridgeException(409, crew + " carries no jetpack.", "Kerbals take one from the pod's inventory in career games; walk instead.");
            }
            if (eva.Fuel <= 0.0)
            {
                throw new BridgeException(409, crew + "'s jetpack is out of EVA propellant.");
            }
            if (ev.situation == Vessel.Situations.ORBITING || ev.situation == Vessel.Situations.ESCAPING
                || ev.situation == Vessel.Situations.DOCKED || ev.srfSpeed > 5.0)
            {
                throw new BridgeException(409, crew + " is " + ev.situation + " at " + ev.srfSpeed.ToString("0.0")
                    + " m/s over the ground: a hop starts from the surface (or just off it, after an EVA).",
                    "Hops are for leaving a landed vessel or crossing an obstacle; in space the kerbal floats at the hatch.");
            }
            double bearing = req.RequireNum("bearing");
            double distanceM = req.RequireNum("distance");
            double rise = req.Num("rise") ?? 1.0;
            double maxS = req.Num("maxS") ?? 25.0;
            if (distanceM <= 0 || distanceM > 50)
            {
                throw new BridgeException(400, "distance must be within 0..50 m (a hop clears a vessel; walk further).");
            }
            if (rise < 0 || rise > 10)
            {
                throw new BridgeException(400, "rise must be within 0..10 m above the start.");
            }
            if (maxS <= 0 || maxS > 120)
            {
                throw new BridgeException(400, "maxS must be within 0..120 s of game time (a hop of up to 50 m takes well under a minute).");
            }
            double jetpackAccel = EvaWalker.JetpackAccel(eva, ev);
            double gravity = FlightGlobals.getGeeForceAtPosition(eva.transform.position).magnitude;
            double height = ev.radarAltitude;
            if (!EvaMath.CanHop(jetpackAccel, gravity, height))
            {
                throw new BridgeException(409, crew + "'s jetpack gives " + jetpackAccel.ToString("F2") + " m/s^2 at its thrust setting against "
                    + gravity.ToString("F2") + " m/s^2 of gravity here, " + height.ToString("0.0") + " m above the terrain: it can neither lift the kerbal ("
                    + EvaMath.LiftMargin.ToString("0.0#") + "x gravity needed) nor glide from this height.", "Walk instead, or step out without a hop.");
            }
            bool glide = !EvaMath.CanLift(jetpackAccel, gravity);
            CelestialBody body = ev.mainBody;
            double lat, lon;
            Destination(ev.latitude, ev.longitude, bearing, distanceM, body.Radius, out lat, out lon);
            double cruiseAlt = ev.altitude + rise;
            EvaWalker.StartHop(ev, lat, lon, cruiseAlt, maxS, glide);
            JObj d = new JObj();
            d["crew"] = crew;
            d["body"] = body.bodyName;
            d["fromLatitude"] = ev.latitude;
            d["fromLongitude"] = ev.longitude;
            d["targetLatitude"] = lat;
            d["targetLongitude"] = lon;
            d["cruiseAltitude_m"] = cruiseAlt;
            d["bearing_deg"] = bearing;
            d["distance_m"] = distanceM;
            d["jetpackFuel"] = eva.Fuel;
            d["jetpackAccel_mps2"] = jetpackAccel;
            d["gravity_mps2"] = gravity;
            d["mode"] = glide ? "glide" : "lift";
            d["maxS"] = maxS;
            d["note"] = "The kerbal flies while the game runs; GET /eva-status: hop.state becomes 'released' over the target, then the kerbal drops to the ground. POST /eva-hop {crew, stop: true} cancels.";
            return d;
        }

        /// <summary>Direct great-circle problem: start point, initial bearing (deg), distance (m) on a sphere.</summary>
        internal static void Destination(double latDeg, double lonDeg, double bearingDeg, double distanceM, double radius, out double lat, out double lon)
        {
            double angular = distanceM / radius;
            double br = bearingDeg * Math.PI / 180.0;
            double phi1 = latDeg * Math.PI / 180.0;
            double lam1 = lonDeg * Math.PI / 180.0;
            double phi2 = Math.Asin(Math.Sin(phi1) * Math.Cos(angular) + Math.Cos(phi1) * Math.Sin(angular) * Math.Cos(br));
            double lam2 = lam1 + Math.Atan2(Math.Sin(br) * Math.Sin(angular) * Math.Cos(phi1),
                Math.Cos(angular) - Math.Sin(phi1) * Math.Sin(phi2));
            lat = phi2 * 180.0 / Math.PI;
            lon = ((lam2 * 180.0 / Math.PI) + 540.0) % 360.0 - 180.0;
        }

        /// <summary>Haversine distance and initial bearing (degrees clockwise from north) on a sphere.</summary>
        internal static void Geodesic(double lat1, double lon1, double lat2, double lon2, double radius, out double distance, out double bearingDeg)
        {
            double toRad = Math.PI / 180.0;
            double phi1 = lat1 * toRad;
            double phi2 = lat2 * toRad;
            double dPhi = (lat2 - lat1) * toRad;
            double dLam = (lon2 - lon1) * toRad;
            double a = Math.Sin(dPhi / 2) * Math.Sin(dPhi / 2) + Math.Cos(phi1) * Math.Cos(phi2) * Math.Sin(dLam / 2) * Math.Sin(dLam / 2);
            distance = radius * 2.0 * Math.Atan2(Math.Sqrt(a), Math.Sqrt(Math.Max(0.0, 1.0 - a)));
            double y = Math.Sin(dLam) * Math.Cos(phi2);
            double x = Math.Cos(phi1) * Math.Sin(phi2) - Math.Sin(phi1) * Math.Cos(phi2) * Math.Cos(dLam);
            bearingDeg = (Math.Atan2(y, x) / toRad + 360.0) % 360.0;
        }

        // ------------------------------------------------------------------ status

        private static JObj Status(BridgeRequest req)
        {
            Util.RequireFlightScene();
            string crew = req.Str("crew");
            List<object> kerbals = new List<object>();
            JObj match = null;
            foreach (Vessel v in FlightGlobals.VesselsLoaded)
            {
                if (v == null || !v.isEVA)
                {
                    continue;
                }
                JObj k = Describe(v);
                kerbals.Add(k);
                if (crew != null && FindEvaVessel(crew) == v)
                {
                    match = k;
                }
            }
            JObj d = new JObj();
            d["count"] = kerbals.Count;
            d["kerbals"] = kerbals;
            d["paused"] = GamePaused();
            d["flagPlanting"] = FlagPlanting.RecentList();
            if (crew != null)
            {
                if (match == null)
                {
                    RequireEvaVessel(crew); // throws with a precise reason
                }
                d["kerbal"] = match;
            }
            return d;
        }

        private static JObj Describe(Vessel v)
        {
            JObj k = new JObj();
            List<ProtoCrewMember> crew = v.GetVesselCrew();
            k["name"] = crew != null && crew.Count > 0 && crew[0] != null ? crew[0].name : v.vesselName;
            k["vessel"] = Util.VesselRef(v);
            k["isActiveVessel"] = v == FlightGlobals.ActiveVessel;
            k["body"] = v.mainBody != null ? v.mainBody.bodyName : null;
            k["biome"] = Biome(v);
            k["latitude"] = v.latitude;
            k["longitude"] = v.longitude;
            k["altitude_m"] = v.altitude;
            k["radarAltitude_m"] = v.radarAltitude;
            k["surfaceSpeed_mps"] = v.srfSpeed;
            k["horizontalSpeed_mps"] = v.horizontalSrfSpeed;
            k["verticalSpeed_mps"] = v.verticalSpeed;
            k["landed"] = v.Landed;
            k["splashed"] = v.Splashed;
            KerbalEVA eva = v.evaController;
            if (eva != null)
            {
                k["fsmState"] = FsmState(eva);
                k["onLadder"] = eva.OnALadder;
                k["ladderPart"] = eva.LadderPart != null ? Util.PartRef(eva.LadderPart) : null;
                Part hatch = AirlockPart(eva);
                k["hatchPart"] = hatch != null ? Util.PartRef(hatch) : null;
                if (hatch != null)
                {
                    k["hatchVessel"] = Util.VesselRef(hatch.vessel);
                }
                k["hasJetpack"] = eva.HasJetpack;
                k["jetpackDeployed"] = eva.JetpackDeployed;
                k["jetpackFuel"] = eva.Fuel;
                k["jetpackFuelCapacity"] = eva.FuelCapacity;
                k["flagItems"] = eva.flagItems;
                string blocker = FlagPlanting.PlantBlocker(eva);
                k["canPlantFlag"] = blocker == null;
                k["plantBlocker"] = blocker;
            }
            k["walk"] = EvaWalker.Status(v);
            k["hop"] = EvaWalker.HopStatus(v);
            k["nearestHatch"] = NearestHatch(v);
            return k;
        }

        /// <summary>
        /// The closest crew part with a hatch and a free seat on another loaded vessel, optionally only
        /// parts with the given internal name.
        /// </summary>
        private static Part NearestCrewPart(Vessel kerbal, string partName)
        {
            Part best = null;
            double bestDistance = double.MaxValue;
            foreach (Vessel v in FlightGlobals.VesselsLoaded)
            {
                if (v == null || v == kerbal || v.isEVA || v.parts == null)
                {
                    continue;
                }
                foreach (Part p in v.parts)
                {
                    if (p == null || p.airlock == null || p.CrewCapacity <= 0 || (p.protoModuleCrew != null && p.protoModuleCrew.Count >= p.CrewCapacity))
                    {
                        continue;
                    }
                    if (partName != null && (p.partInfo == null || !string.Equals(p.partInfo.name, partName, StringComparison.OrdinalIgnoreCase)))
                    {
                        continue;
                    }
                    double distance = HatchDistance(kerbal, p);
                    if (distance < bestDistance)
                    {
                        best = p;
                        bestDistance = distance;
                    }
                }
            }
            return best;
        }

        private static JObj NearestHatch(Vessel kerbal)
        {
            Part best = NearestCrewPart(kerbal, null);
            if (best == null)
            {
                return null;
            }
            double distance = HatchDistance(kerbal, best);
            JObj o = Util.PartRef(best);
            o["vessel"] = Util.VesselRef(best.vessel);
            o["distance_m"] = distance;
            o["withinBoardingReach"] = distance <= BoardReachM;
            return o;
        }

        // ------------------------------------------------------------------ flags

        private static JObj PlantFlag(BridgeRequest req)
        {
            string crew = req.RequireStr("crew");
            string siteName = req.Str("siteName") ?? req.Str("name"); // 'name' is accepted as an alias
            string plaque = req.Str("plaque");
            double waitS = Math.Max(0.0, Math.Min(120.0, req.Num("waitS") ?? 20.0));
            JObj status = OnMain("eva-plant-flag", delegate(BridgeRequest r)
            {
                Util.RequireFlightScene();
                Vessel ev = RequireEvaVessel(crew);
                JObj o = FlagPlanting.Describe(FlagPlanting.Begin(ev, EvaModule(ev), crew, siteName, plaque));
                o["paused"] = GamePaused();
                return o;
            }, req, 15000);
            long id = Convert.ToInt64(status["watchId"]);
            bool paused = (bool)status["paused"];
            DateTime deadline = DateTime.UtcNow.AddSeconds(paused ? 0.0 : waitS);
            while (!(bool)status["verified"] && (string)status["state"] != "lost" && (string)status["state"] != "expired"
                && DateTime.UtcNow < deadline)
            {
                Thread.Sleep(PollIntervalMs);
                status = OnMain("eva-plant-flag status", delegate(BridgeRequest r)
                {
                    JObj o = FlagPlanting.Describe(FlagPlanting.Find(id));
                    o["paused"] = GamePaused();
                    return o;
                }, req, 10000);
            }
            string state = (string)status["state"];
            if (state == "lost" || state == "expired")
            {
                throw new BridgeException(409, "The flag was not planted: " + status["detail"] + ".",
                    "Check GET /eva-status (fsmState, flagItems, canPlantFlag) and GET /vessels (type Flag).");
            }
            status["commanded"] = true;
            if (!(bool)status["verified"])
            {
                status["pending"] = true;
                status["note"] = paused
                    ? "Commanded while the game is paused: the flag appears once game time runs, and the bridge names it then. Follow it in GET /eva-status (flagPlanting)."
                    : "Still planting after " + waitS.ToString("0") + " s; the bridge keeps watching. Follow it in GET /eva-status (flagPlanting).";
            }
            return status;
        }

        private static JObj GoAndPlant(BridgeRequest req)
        {
            string crew = req.RequireStr("crew");
            double settleS = Math.Max(1.0, Math.Min(120.0, req.Num("settleS") ?? 30.0));
            JObj went = OnMain("eva-flag go", Go, req, 15000);
            DateTime deadline = DateTime.UtcNow.AddSeconds(settleS);
            bool ready = false;
            while (DateTime.UtcNow < deadline)
            {
                Thread.Sleep(PollIntervalMs * 2);
                JObj s = OnMain("eva-flag settle", delegate(BridgeRequest r)
                {
                    Vessel ev = RequireEvaVessel(crew);
                    JObj o = new JObj();
                    o["ready"] = FlagPlanting.CanPlant(EvaModule(ev));
                    o["paused"] = GamePaused();
                    return o;
                }, req, 10000);
                if ((bool)s["paused"])
                {
                    throw new BridgeException(409, crew + " is on EVA but the game is paused, so the kerbal cannot settle and plant.",
                        "Unpause, then POST /eva-plant-flag {crew}.");
                }
                if ((bool)s["ready"])
                {
                    ready = true;
                    break;
                }
            }
            if (!ready)
            {
                throw new BridgeException(504, crew + " is on EVA but did not stand idle on the ground within " + settleS.ToString("0") + " s.",
                    "Climb down the ladder / let the kerbal land, then POST /eva-plant-flag {crew}.");
            }
            JObj planted = PlantFlag(req);
            planted["eva"] = went;
            return planted;
        }
    }
}
