using System;
using System.Collections.Generic;
using System.Reflection;
using UnityEngine;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    /// <summary>
    /// Plants a flag and follows it until it exists and is named. Planting is an animation: the flag
    /// vessel appears only after some game time, then KSP opens its naming dialog. A watch started by
    /// Begin() is advanced every frame by Tick(), so a plant commanded while the game is paused
    /// completes (and is named as requested) whenever the game next runs. Main thread only.
    /// </summary>
    internal static class FlagPlanting
    {
        // Plumbing limits, in seconds of running game time (paused time does not count). KSP creates the
        // flag vessel when the plant animation STARTS and opens the naming dialog when it ENDS
        // (KerbalEVA.flagPlant_OnLeave -> FlagSite.OnPlacementComplete), so the dialog is waited for
        // as long as the plant itself; naming directly early would leave that dialog (and its control
        // lock) open with nobody to answer it.
        private const double NameDirectlyAfterS = 5.0; // only when the dialog's private members are missing
        private const double GiveUpAfterS = 30.0; // no new flag vessel, or no naming dialog, after this
        private const int HistorySize = 16;

        // Private FlagSite members of the stock naming dialog; null if a KSP update renamed them.
        private static readonly FieldInfo RenameDialogField = typeof(FlagSite).GetField("SiteRenameDialog", BindingFlags.Instance | BindingFlags.NonPublic);
        private static readonly FieldInfo SiteNameField = typeof(FlagSite).GetField("siteName", BindingFlags.Instance | BindingFlags.NonPublic);
        private static readonly FieldInfo NewPlaqueField = typeof(FlagSite).GetField("newPlaqueText", BindingFlags.Instance | BindingFlags.NonPublic);
        private static readonly MethodInfo AcceptRenameMethod = typeof(FlagSite).GetMethod("AcceptSiteRename", BindingFlags.Instance | BindingFlags.NonPublic);

        internal sealed class Watch
        {
            public long Id;
            public string Crew;
            public string SiteName;
            public string Plaque;
            public string Body;
            public string Biome;
            public HashSet<uint> FlagsBefore = new HashSet<uint>();
            public uint FlagId;
            public double RunningS; // game-running seconds in the current phase
            public DateTime StartedUtc;
            public string State = "planting"; // planting -> placing -> named | lost | expired
            public string Detail;
            public string NamedVia;
            public JObj Flag;

            public bool Done
            {
                get { return State == "named" || State == "lost" || State == "expired"; }
            }
        }

        private static readonly List<Watch> Active = new List<Watch>();
        private static readonly List<Watch> Recent = new List<Watch>();
        private static long _nextId;

        /// <summary>Validates, commands the plant and starts a watch. Works while the game is paused.</summary>
        internal static Watch Begin(Vessel kerbal, KerbalEVA eva, string crew, string siteName, string plaque)
        {
            if (siteName != null && !Vessel.IsValidVesselName(siteName))
            {
                throw new BridgeException(400, "siteName '" + siteName + "' is not a valid vessel name.");
            }
            // PlantFlag() spends a flag item even when the FSM ignores the command, so check everything first.
            string blocker = PlantBlocker(eva);
            if (blocker != null)
            {
                throw new BridgeException(409, crew + " cannot plant a flag: " + blocker + ".",
                    "Wait until GET /eva-status shows the kerbal standing idle on the ground (canPlantFlag=true).");
            }
            Watch w = new Watch();
            w.Id = ++_nextId;
            w.Crew = crew;
            w.SiteName = siteName;
            w.Plaque = plaque;
            w.Body = kerbal.mainBody != null ? kerbal.mainBody.bodyName : null;
            try
            {
                w.Biome = ScienceUtil.GetExperimentBiome(kerbal.mainBody, kerbal.latitude, kerbal.longitude);
            }
            catch (Exception)
            {
                w.Biome = null;
            }
            w.StartedUtc = DateTime.UtcNow;
            foreach (Vessel v in FlightGlobals.Vessels)
            {
                if (v != null && v.vesselType == VesselType.Flag)
                {
                    w.FlagsBefore.Add(v.persistentId);
                }
            }
            eva.PlantFlag();
            Active.Add(w);
            Recent.Add(w);
            if (Recent.Count > HistorySize)
            {
                Recent.RemoveAt(0);
            }
            return w;
        }

        internal static bool CanPlant(KerbalEVA eva)
        {
            return PlantBlocker(eva) == null;
        }

        /// <summary>
        /// Why the kerbal cannot plant a flag now, or null. Mirrors the stock rules of the private
        /// KerbalEVA.CanPlantFlag (flag carried, ground contact, not ragdolling, not in construction
        /// mode, and the career Astronaut Complex level that unlocks flags) plus landed and an FSM state
        /// that accepts the plant event. The active-vessel rule of the stock button is not applied:
        /// any EVA kerbal in physics range may plant.
        /// </summary>
        internal static string PlantBlocker(KerbalEVA eva)
        {
            try
            {
                if (eva.flagItems <= 0)
                {
                    return "no flag carried (flagItems = 0)";
                }
                Vessel v = eva.vessel;
                if (!v.Landed)
                {
                    return "the kerbal is " + v.situation + "; flags are planted standing on solid ground";
                }
                if (!eva.part.GroundContact)
                {
                    return "no ground contact (bouncing or falling)";
                }
                if (eva.isRagdoll)
                {
                    return "the kerbal is ragdolling";
                }
                if (eva.InConstructionMode)
                {
                    return "the kerbal is in construction mode";
                }
                GameVariables rules = GameVariables.Instance;
                if (rules != null && !rules.UnlockedEVAFlags(ScenarioUpgradeableFacilities.GetFacilityLevel(SpaceCenterFacility.AstronautComplex)))
                {
                    return "the Astronaut Complex is not upgraded far enough to plant flags (career)";
                }
                if (eva.fsm == null || eva.fsm.CurrentState == null || !eva.fsm.CurrentState.StateEvents.Contains(eva.On_flagPlantStart))
                {
                    return "FSM state '" + (eva.fsm != null ? eva.fsm.currentStateName : "") + "' does not accept the plant command";
                }
                return null;
            }
            catch (Exception ex)
            {
                return "state unreadable (" + ex.GetType().Name + ")";
            }
        }

        /// <summary>Advances every open watch; called from the addon's Update().</summary>
        internal static void Tick()
        {
            if (Active.Count == 0)
            {
                return;
            }
            if (!HighLogic.LoadedSceneIsFlight)
            {
                foreach (Watch w in Active)
                {
                    w.State = "lost";
                    w.Detail = "the flight scene was left";
                }
                Active.Clear();
                return;
            }
            bool running = !(FlightDriver.Pause || Time.timeScale == 0f);
            float dt = Time.unscaledDeltaTime;
            for (int i = Active.Count - 1; i >= 0; i--)
            {
                Watch w = Active[i];
                try
                {
                    Advance(w, running, dt);
                }
                catch (Exception ex)
                {
                    w.State = "lost";
                    w.Detail = "error: " + ex.Message;
                }
                if (w.Done)
                {
                    Active.RemoveAt(i);
                }
            }
        }

        private static void Advance(Watch w, bool running, float dt)
        {
            if (running)
            {
                w.RunningS += dt;
            }
            Vessel flag = null;
            if (w.FlagId == 0)
            {
                foreach (Vessel v in FlightGlobals.Vessels)
                {
                    if (v != null && v.vesselType == VesselType.Flag && !w.FlagsBefore.Contains(v.persistentId))
                    {
                        flag = v;
                        w.FlagId = v.persistentId;
                        w.State = "placing";
                        w.RunningS = 0;
                        break;
                    }
                }
                if (flag == null)
                {
                    if (w.RunningS > GiveUpAfterS)
                    {
                        w.State = "expired";
                        w.Detail = "no flag appeared after " + GiveUpAfterS.ToString("0") + " s of game time";
                    }
                    return;
                }
            }
            else
            {
                flag = Util.FindVessel(w.FlagId);
                if (flag == null)
                {
                    w.State = "lost";
                    w.Detail = "the new flag vessel disappeared (knocked over or removed)";
                    return;
                }
            }

            FlagSite site = flag.parts != null && flag.parts.Count > 0 ? flag.parts[0].FindModuleImplementing<FlagSite>() : null;
            if (site == null)
            {
                return; // parts not built yet
            }
            bool canAnswer = RenameDialogField != null && SiteNameField != null && NewPlaqueField != null && AcceptRenameMethod != null;
            object dialog = canAnswer ? RenameDialogField.GetValue(site) : null;
            if (dialog != null)
            {
                // Placement finished and KSP asks for a name: answer like the dialog's OK button
                // (AcceptSiteRename, then the afterDialog callback, which fires afterFlagPlanted).
                SiteNameField.SetValue(site, w.SiteName ?? flag.vesselName);
                NewPlaqueField.SetValue(site, w.Plaque ?? site.PlaqueText ?? "");
                AcceptRenameMethod.Invoke(site, null);
                GameEvents.afterFlagPlanted.Fire(site);
                Named(w, flag, site, "stock dialog");
            }
            else if (w.RunningS > (canAnswer ? GiveUpAfterS : NameDirectlyAfterS))
            {
                // No dialog (the private members changed, or none appeared): set the fields directly.
                if (canAnswer)
                {
                    w.Detail = "KSP never opened the naming dialog; name and plaque were set directly";
                }
                if (w.SiteName != null)
                {
                    flag.vesselName = w.SiteName;
                }
                if (w.Plaque != null)
                {
                    site.PlaqueText = w.Plaque;
                }
                Named(w, flag, site, "direct");
            }
        }

        private static void Named(Watch w, Vessel flag, FlagSite site, string via)
        {
            JObj f = Util.VesselRef(flag);
            f["latitude"] = flag.latitude;
            f["longitude"] = flag.longitude;
            f["plaque"] = site.PlaqueText;
            f["placedBy"] = site.placedBy;
            w.Flag = f;
            w.NamedVia = via;
            w.State = "named";
        }

        internal static Watch Find(long id)
        {
            foreach (Watch w in Recent)
            {
                if (w.Id == id)
                {
                    return w;
                }
            }
            return null;
        }

        internal static JObj Describe(Watch w)
        {
            if (w == null)
            {
                throw new BridgeException(404, "That flag watch is no longer in the recent history.");
            }
            JObj o = new JObj();
            o["watchId"] = w.Id;
            o["crew"] = w.Crew;
            o["state"] = w.State;
            o["verified"] = w.State == "named";
            o["flag"] = w.Flag;
            o["namedVia"] = w.NamedVia;
            o["requestedSiteName"] = w.SiteName;
            o["requestedPlaque"] = w.Plaque;
            o["body"] = w.Body;
            o["biome"] = w.Biome;
            o["detail"] = w.Detail;
            o["elapsed_s"] = (DateTime.UtcNow - w.StartedUtc).TotalSeconds;
            return o;
        }

        /// <summary>The recent watches (newest last), for /eva-status.</summary>
        internal static List<object> RecentList()
        {
            List<object> list = new List<object>();
            foreach (Watch w in Recent)
            {
                list.Add(Describe(w));
            }
            return list;
        }
    }
}
