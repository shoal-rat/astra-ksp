using System;
using System.Collections.Generic;
using System.Reflection;
using UnityEngine;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    /// <summary>
    /// Walks EVA kerbals to a surface point with the stock movement code, and flies short jetpack hops.
    ///
    /// Walking: KSP has no walk-to. A kerbal moves only while KerbalEVA.FixedUpdate sees a movement
    /// request (tgtRpos) that HandleMovementInput builds from the keyboard each physics tick; the facing
    /// (tgtFwd/tgtUp) is set from that request inside the same method. A Harmony postfix on
    /// HandleMovementInput replaces both with the direction to the target (what KerbalEVA.SetWaypoint
    /// does), so the stock FSM turns and walks (or bounds, in low gravity) exactly as if the player held
    /// a key. A walk that makes no progress for StallGameS of game time ends as "stalled" (time the
    /// kerbal spends packed or on rails does not count).
    ///
    /// Hopping: the same postfix sets the jetpack request (packTgtRPos, a world-space thrust fraction that
    /// UpdatePackLinear turns into linPower x thrustPercentage) from a velocity controller with gravity
    /// compensation (EvaMath.HopCommand): fly to a point beside the start at a chosen height, then stow
    /// the pack and drop. Used to leave a light lander without standing or walking on it: in low gravity
    /// a kerbal pushing off a lander can tip it over. A walk and a hop replace each other; Stop ends both.
    ///
    /// Harmony (GameData/000_Harmony) is used through reflection so the build needs no reference to it.
    /// </summary>
    public static class EvaWalker
    {
        private sealed class Order
        {
            public CelestialBody Body;
            public double Lat;
            public double Lon;
            public double Alt;
            public double ArrivalRadius;
            public double Distance = double.NaN;
            public DateTime StartedUtc;
            public double BestDistance = double.MaxValue;
            public double BestUt = double.NaN;
            public double LastUt = double.NaN; // UT of the previous tick, to skip time spent packed or on rails
        }

        private sealed class Hop
        {
            public CelestialBody Body;
            public double Lat;
            public double Lon;
            public double CruiseAlt; // altitude above the datum to hold while travelling
            public double MaxGameS;
            public double StartUt;
            public double LastUt = double.NaN;
            public double Distance = double.NaN;
            public double PeakAlt = double.MinValue;
            public bool Glide; // the pack cannot lift the kerbal: it glides outward from a height and sinks
            public DateTime StartedUtc;
        }

        private const double StallGameS = 12.0;
        private const double ProgressM = 0.5;

        private static readonly Dictionary<uint, Order> Orders = new Dictionary<uint, Order>();
        private static readonly Dictionary<uint, JObj> Outcomes = new Dictionary<uint, JObj>();
        private static readonly Dictionary<uint, Hop> Hops = new Dictionary<uint, Hop>();
        private static readonly Dictionary<uint, JObj> HopOutcomes = new Dictionary<uint, JObj>();
        private static FieldInfo _tgtRpos, _tgtFwd, _tgtUp, _fUp, _packTgtRPos, _linPower, _thrustPercentage;
        private static MethodInfo _toggleJetpack;
        private static string _unavailableReason = "not installed yet";

        internal static bool Available
        {
            get { return _tgtRpos != null && _tgtFwd != null && _unavailableReason == null; }
        }

        internal static bool HopAvailable
        {
            get { return Available && _packTgtRPos != null && _linPower != null; }
        }

        internal static string UnavailableReason
        {
            get { return _unavailableReason; }
        }

        /// <summary>Patches KerbalEVA.HandleMovementInput once; call on the main thread at startup.</summary>
        internal static void Install()
        {
            const BindingFlags Fields = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
            _tgtRpos = typeof(KerbalEVA).GetField("tgtRpos", Fields);
            _tgtFwd = typeof(KerbalEVA).GetField("tgtFwd", Fields);
            _tgtUp = typeof(KerbalEVA).GetField("tgtUp", Fields);
            _fUp = typeof(KerbalEVA).GetField("fUp", Fields);
            _packTgtRPos = typeof(KerbalEVA).GetField("packTgtRPos", Fields);
            _linPower = typeof(KerbalEVA).GetField("linPower", Fields);
            _thrustPercentage = typeof(KerbalEVA).GetField("thrustPercentage", Fields);
            _toggleJetpack = typeof(KerbalEVA).GetMethod("ToggleJetpack", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                null, new[] { typeof(bool) }, null);
            MethodInfo original = typeof(KerbalEVA).GetMethod("HandleMovementInput", BindingFlags.Instance | BindingFlags.NonPublic);
            if (_tgtRpos == null || _tgtFwd == null || _tgtUp == null || _fUp == null || original == null)
            {
                _unavailableReason = "KerbalEVA.tgtRpos/tgtFwd/tgtUp/fUp/HandleMovementInput not found in this KSP build";
                return;
            }
            _unavailableReason = HarmonyReflect.Postfix("astra.kspautomationbridge.evawalker", original,
                typeof(EvaWalker).GetMethod("HandleMovementInputPostfix"));
            if (_unavailableReason != null)
            {
                BridgeLog.Warn("EVA walking unavailable: " + _unavailableReason);
                return;
            }
            // A scene load (a restore, a quickload) starts another timeline, and the restored kerbal keeps its
            // persistentId: outcomes from before it would describe a walk or hop that no longer happened.
            SceneLoadedHook.Add(delegate { Orders.Clear(); Hops.Clear(); Outcomes.Clear(); HopOutcomes.Clear(); });
        }

        /// <summary>Harmony postfix (runs inside KerbalEVA.FixedUpdate, after the keyboard input).</summary>
        public static void HandleMovementInputPostfix(KerbalEVA __instance)
        {
            if ((Orders.Count == 0 && Hops.Count == 0) || __instance == null || __instance.vessel == null)
            {
                return;
            }
            Vessel v = __instance.vessel;
            Hop hop;
            if (Hops.TryGetValue(v.persistentId, out hop))
            {
                StepHop(__instance, v, hop);
                return;
            }
            Order order;
            if (!Orders.TryGetValue(v.persistentId, out order))
            {
                return;
            }
            try
            {
                if (v.mainBody != order.Body)
                {
                    Finish(v.persistentId, order, "left " + order.Body.bodyName);
                    return;
                }
                Vector3d here = __instance.transform.position;
                Vector3d target = order.Body.GetWorldSurfacePosition(order.Lat, order.Lon, order.Alt);
                Vector3d up = (here - order.Body.position).normalized;
                Vector3d delta = target - here;
                Vector3d horizontal = delta - Vector3d.Dot(delta, up) * up;
                order.Distance = horizontal.magnitude;
                if (order.Distance <= order.ArrivalRadius)
                {
                    Finish(v.persistentId, order, "arrived");
                    return;
                }
                double ut = Planetarium.GetUniversalTime();
                order.BestUt += EvaMath.RailsGap(order.LastUt, ut); // time packed or on rails is no stall
                order.LastUt = ut;
                if (double.IsNaN(order.BestUt) || order.Distance < order.BestDistance - ProgressM)
                {
                    order.BestDistance = order.Distance;
                    order.BestUt = ut;
                }
                else if (ut - order.BestUt > StallGameS)
                {
                    Finish(v.persistentId, order, "stalled");
                    return;
                }
                // Replace the keyboard request and the facing derived from it, as SetWaypoint does.
                Vector3 direction = (Vector3)(horizontal / order.Distance);
                _tgtRpos.SetValue(__instance, direction);
                _tgtFwd.SetValue(__instance, direction);
                _tgtUp.SetValue(__instance, _fUp.GetValue(__instance));
            }
            catch (Exception ex)
            {
                Finish(v.persistentId, order, "error: " + ex.Message);
            }
        }

        private static void StepHop(KerbalEVA eva, Vessel v, Hop hop)
        {
            try
            {
                double ut = Planetarium.GetUniversalTime();
                hop.StartUt += EvaMath.RailsGap(hop.LastUt, ut); // MaxGameS counts only simulated (thrusting) time
                hop.LastUt = ut;
                if (v.mainBody != hop.Body)
                {
                    EndHop(eva, v, hop, "left " + hop.Body.bodyName);
                    return;
                }
                if (ut - hop.StartUt > hop.MaxGameS)
                {
                    EndHop(eva, v, hop, "timed out");
                    return;
                }
                Vector3d here = eva.transform.position;
                Vector3d gravity = FlightGlobals.getGeeForceAtPosition(here);
                double full = JetpackAccel(eva, v);
                if (!EvaMath.CanHop(full, gravity.magnitude, v.radarAltitude) && full < gravity.magnitude)
                {
                    // Near the ground a pack weaker than gravity would only burn propellant until the timeout.
                    EndHop(eva, v, hop, hop.Glide
                        ? "glided down short of the target"
                        : "cannot lift: jetpack " + full.ToString("F2") + " m/s^2 < gravity " + gravity.magnitude.ToString("F2") + " m/s^2");
                    return;
                }
                if (!eva.JetpackDeployed)
                {
                    if (_toggleJetpack != null)
                    {
                        _toggleJetpack.Invoke(eva, new object[] { true });
                    }
                    if (!eva.JetpackDeployed)
                    {
                        eva.JetpackDeployed = true;
                    }
                }
                Vector3d up = (here - hop.Body.position).normalized;
                Vector3d target = hop.Body.GetWorldSurfacePosition(hop.Lat, hop.Lon, hop.CruiseAlt);
                Vector3d delta = target - here;
                Vector3d horizontal = delta - Vector3d.Dot(delta, up) * up;
                hop.Distance = horizontal.magnitude;
                double alt = hop.Body.GetAltitude(here);
                hop.PeakAlt = Math.Max(hop.PeakAlt, alt);
                if (hop.Distance <= EvaMath.HopReleaseM)
                {
                    EndHop(eva, v, hop, "released");
                    return;
                }
                // Velocity command: toward the point beside the start, holding the cruise height.
                double[] cmd = EvaMath.HopCommand(Arr(horizontal), Arr(up), hop.CruiseAlt - alt, Arr(v.srf_velocity), Arr(gravity), full);
                _packTgtRPos.SetValue(eva, new Vector3((float)cmd[0], (float)cmd[1], (float)cmd[2]));
                _tgtRpos.SetValue(eva, Vector3.zero);
            }
            catch (Exception ex)
            {
                EndHop(eva, v, hop, "error: " + ex.Message);
            }
        }

        private static void EndHop(KerbalEVA eva, Vessel v, Hop hop, string outcome)
        {
            Hops.Remove(v.persistentId);
            try
            {
                _packTgtRPos.SetValue(eva, Vector3.zero);
                if (eva.JetpackDeployed && _toggleJetpack != null)
                {
                    _toggleJetpack.Invoke(eva, new object[] { false });
                }
            }
            catch (Exception)
            {
            }
            HopOutcomes[v.persistentId] = DescribeHop(hop, outcome);
        }

        /// <summary>The jetpack's full-thrust acceleration (m/s^2) for this kerbal; needs HopAvailable.</summary>
        internal static double JetpackAccel(KerbalEVA eva, Vessel kerbal)
        {
            double thrustPct = _thrustPercentage != null ? Convert.ToDouble(_thrustPercentage.GetValue(eva)) : 100.0;
            return EvaMath.JetpackAccel(Convert.ToDouble(_linPower.GetValue(eva)), thrustPct, kerbal.totalMass);
        }

        private static double[] Arr(Vector3d v)
        {
            return new[] { v.x, v.y, v.z };
        }

        /// <summary>Walk to (lat, lon). Ends a hop in progress; returns true if it did.</summary>
        internal static bool Start(KerbalEVA eva, Vessel kerbal, double lat, double lon, double alt, double arrivalRadius)
        {
            Hop hop;
            bool endedHop = Hops.TryGetValue(kerbal.persistentId, out hop);
            if (endedHop)
            {
                EndHop(eva, kerbal, hop, "replaced by walk");
            }
            Order order = new Order();
            order.Body = kerbal.mainBody;
            order.Lat = lat;
            order.Lon = lon;
            order.Alt = alt;
            order.ArrivalRadius = arrivalRadius;
            order.StartedUtc = DateTime.UtcNow;
            Orders[kerbal.persistentId] = order;
            Outcomes.Remove(kerbal.persistentId);
            return endedHop;
        }

        /// <summary>Fly the jetpack to (lat, lon) holding cruiseAlt, then stow it and drop. Ends a walk in progress.</summary>
        internal static void StartHop(Vessel kerbal, double lat, double lon, double cruiseAlt, double maxGameS, bool glide)
        {
            Order order;
            if (Orders.TryGetValue(kerbal.persistentId, out order))
            {
                Finish(kerbal.persistentId, order, "replaced by hop");
            }
            Hop hop = new Hop();
            hop.Body = kerbal.mainBody;
            hop.Lat = lat;
            hop.Lon = lon;
            hop.CruiseAlt = cruiseAlt;
            hop.MaxGameS = maxGameS;
            hop.Glide = glide;
            hop.StartUt = Planetarium.GetUniversalTime();
            hop.LastUt = hop.StartUt; // rails time before the first tick does not count either
            hop.StartedUtc = DateTime.UtcNow;
            Hops[kerbal.persistentId] = hop;
            HopOutcomes.Remove(kerbal.persistentId);
        }

        /// <summary>Cancels the kerbal's walk and hop (the pack is zeroed and stowed); false if neither was running.</summary>
        internal static bool Stop(KerbalEVA eva, Vessel kerbal)
        {
            bool stopped = false;
            Order order;
            if (Orders.TryGetValue(kerbal.persistentId, out order))
            {
                Finish(kerbal.persistentId, order, "stopped");
                stopped = true;
            }
            Hop hop;
            if (Hops.TryGetValue(kerbal.persistentId, out hop))
            {
                EndHop(eva, kerbal, hop, "stopped");
                stopped = true;
            }
            return stopped;
        }

        private static void Finish(uint id, Order order, string outcome)
        {
            Orders.Remove(id);
            Outcomes[id] = Describe(order, outcome);
        }

        /// <summary>The kerbal's current or last walk order, or null.</summary>
        internal static JObj Status(Vessel kerbal)
        {
            Order order;
            if (Orders.TryGetValue(kerbal.persistentId, out order))
            {
                return Describe(order, "walking");
            }
            JObj done;
            return Outcomes.TryGetValue(kerbal.persistentId, out done) ? done : null;
        }

        /// <summary>The kerbal's current or last hop, or null.</summary>
        internal static JObj HopStatus(Vessel kerbal)
        {
            Hop hop;
            if (Hops.TryGetValue(kerbal.persistentId, out hop))
            {
                return DescribeHop(hop, "flying");
            }
            JObj done;
            return HopOutcomes.TryGetValue(kerbal.persistentId, out done) ? done : null;
        }

        private static JObj Describe(Order order, string state)
        {
            JObj o = new JObj();
            o["state"] = state;
            o["targetLatitude"] = order.Lat;
            o["targetLongitude"] = order.Lon;
            o["arrivalRadius_m"] = order.ArrivalRadius;
            o["distance_m"] = order.Distance;
            o["elapsed_s"] = (DateTime.UtcNow - order.StartedUtc).TotalSeconds;
            return o;
        }

        private static JObj DescribeHop(Hop hop, string state)
        {
            JObj o = new JObj();
            o["state"] = state;
            o["targetLatitude"] = hop.Lat;
            o["targetLongitude"] = hop.Lon;
            o["cruiseAltitude_m"] = hop.CruiseAlt;
            o["mode"] = hop.Glide ? "glide" : "lift";
            o["distance_m"] = hop.Distance;
            o["peakAltitude_m"] = hop.PeakAlt == double.MinValue ? (object)null : hop.PeakAlt;
            o["elapsed_s"] = (DateTime.UtcNow - hop.StartedUtc).TotalSeconds;
            return o;
        }
    }
}
