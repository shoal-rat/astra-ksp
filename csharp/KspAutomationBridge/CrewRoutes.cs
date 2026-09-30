using System;
using System.Collections.Generic;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    /// <summary>Crew roster, who sits where, and moving a kerbal between parts of one vessel.</summary>
    internal static class CrewRoutes
    {
        public static void Register(Router r)
        {
            r.Main("GET", "/crew-roster", 15000, Roster, "Every kerbal in the save with trait, level, status and current seat.");
            r.Main("GET", "/crew-list", 15000, CrewList, "Kerbals aboard loaded vessels (flight): vessel, part ids, seat, EVA.");
            r.Main("POST", "/transfer-crew", 15000, Transfer, "Move a kerbal to another crewable part of the SAME vessel.");
        }

        private static JObj Roster(BridgeRequest req)
        {
            Util.RequireGame();
            KerbalRoster roster = HighLogic.CurrentGame.CrewRoster;
            if (roster == null)
            {
                throw new BridgeException(409, "The loaded game has no crew roster.");
            }
            Dictionary<string, JObj> seats = SeatMap();
            List<object> list = new List<object>();
            HashSet<string> seen = new HashSet<string>();
            // KerbalRoster.Kerbals() has no zero-argument overload in 1.12; the typed collections cover everyone.
            IEnumerable<ProtoCrewMember>[] groups = { roster.Crew, roster.Tourist, roster.Applicants, roster.Unowned };
            foreach (IEnumerable<ProtoCrewMember> group in groups)
            {
                if (group == null)
                {
                    continue;
                }
                foreach (ProtoCrewMember pcm in group)
                {
                    if (pcm == null || pcm.name == null || !seen.Add(pcm.name))
                    {
                        continue;
                    }
                    JObj k = new JObj();
                    k["name"] = pcm.name;
                    k["type"] = pcm.type.ToString();
                    k["trait"] = pcm.trait;
                    k["level"] = pcm.experienceLevel;
                    k["experience"] = pcm.experience;
                    k["status"] = pcm.rosterStatus.ToString();
                    k["courage"] = pcm.courage;
                    k["stupidity"] = pcm.stupidity;
                    k["badass"] = pcm.isBadass;
                    k["veteran"] = pcm.veteran;
                    k["gender"] = pcm.gender.ToString();
                    JObj seat;
                    k["location"] = seats.TryGetValue(pcm.name, out seat) ? seat : null;
                    list.Add(k);
                }
            }
            JObj d = new JObj();
            d["count"] = list.Count;
            d["roster"] = list;
            d["available"] = roster.GetAvailableCrewCount();
            d["assigned"] = roster.GetAssignedCrewCount();
            d["kia"] = roster.GetKIACrewCount();
            d["missing"] = roster.GetMissingCrewCount();
            return d;
        }

        /// <summary>Kerbal name -> where they sit, from live vessels or (outside flight) the save state.</summary>
        private static Dictionary<string, JObj> SeatMap()
        {
            Dictionary<string, JObj> map = new Dictionary<string, JObj>(StringComparer.Ordinal);
            if (FlightGlobals.Vessels != null && FlightGlobals.Vessels.Count > 0)
            {
                foreach (Vessel v in FlightGlobals.Vessels)
                {
                    if (v == null)
                    {
                        continue;
                    }
                    if (v.loaded && v.parts != null)
                    {
                        foreach (Part p in v.parts)
                        {
                            if (p != null && p.protoModuleCrew != null)
                            {
                                foreach (ProtoCrewMember pcm in p.protoModuleCrew)
                                {
                                    map[pcm.name] = Seat(v.vesselName, v.persistentId, p.persistentId, p.partInfo != null ? p.partInfo.name : p.name, pcm.seatIdx, v.isEVA);
                                }
                            }
                        }
                    }
                    else if (v.protoVessel != null)
                    {
                        AddProtoSeats(map, v.protoVessel);
                    }
                }
            }
            else if (HighLogic.CurrentGame.flightState != null)
            {
                foreach (ProtoVessel pv in HighLogic.CurrentGame.flightState.protoVessels)
                {
                    if (pv != null)
                    {
                        AddProtoSeats(map, pv);
                    }
                }
            }
            return map;
        }

        private static void AddProtoSeats(Dictionary<string, JObj> map, ProtoVessel pv)
        {
            if (pv.protoPartSnapshots == null)
            {
                return;
            }
            foreach (ProtoPartSnapshot pps in pv.protoPartSnapshots)
            {
                if (pps == null || pps.protoModuleCrew == null)
                {
                    continue;
                }
                foreach (ProtoCrewMember pcm in pps.protoModuleCrew)
                {
                    map[pcm.name] = Seat(pv.vesselName, pv.persistentId, pps.persistentId, pps.partName, pcm.seatIdx, pv.vesselType == VesselType.EVA);
                }
            }
        }

        private static JObj Seat(string vesselName, uint vesselId, uint partId, string partName, int seat, bool eva)
        {
            JObj o = new JObj();
            o["vessel"] = vesselName;
            o["vesselPersistentId"] = vesselId;
            o["partPersistentId"] = partId;
            o["partName"] = partName;
            o["seat"] = seat;
            o["isEva"] = eva;
            return o;
        }

        private static JObj CrewList(BridgeRequest req)
        {
            Util.RequireFlightScene();
            List<object> list = new List<object>();
            foreach (Vessel v in FlightGlobals.VesselsLoaded)
            {
                if (v == null || v.parts == null)
                {
                    continue;
                }
                foreach (Part p in v.parts)
                {
                    if (p == null || p.protoModuleCrew == null)
                    {
                        continue;
                    }
                    foreach (ProtoCrewMember pcm in p.protoModuleCrew)
                    {
                        JObj k = new JObj();
                        k["name"] = pcm.name;
                        k["type"] = pcm.type.ToString();
                        k["trait"] = pcm.trait;
                        k["level"] = pcm.experienceLevel;
                        k["vessel"] = v.vesselName;
                        k["vesselPersistentId"] = v.persistentId;
                        k["isActiveVessel"] = v == FlightGlobals.ActiveVessel;
                        k["part"] = Util.PartRef(p);
                        k["seat"] = pcm.seatIdx;
                        k["isEva"] = v.isEVA;
                        list.Add(k);
                    }
                }
            }
            JObj d = new JObj();
            d["count"] = list.Count;
            d["crew"] = list;
            return d;
        }

        private static JObj Transfer(BridgeRequest req)
        {
            Util.RequireFlightScene();
            string name = req.RequireStr("crew");
            Part from;
            ProtoCrewMember pcm = Util.FindSeatedKerbal(name, out from);
            if (pcm == null)
            {
                throw new BridgeException(404, "No seated kerbal named '" + name + "' in a loaded vessel.", "GET /crew-list shows who sits where.");
            }
            Part to = Util.ResolvePart(req, "toPart", from.vessel);
            if (to == null)
            {
                throw new BridgeException(400, "Missing required parameter 'toPartId' (persistentId) or 'toPartIndex' (index in the kerbal's vessel).");
            }
            if (to == from)
            {
                throw new BridgeException(409, pcm.name + " is already in that part.");
            }
            if (to.vessel != from.vessel)
            {
                throw new BridgeException(409, "The target part belongs to another vessel (" + to.vessel.vesselName + "): crew transfer stays within one vessel.",
                    "Dock first (the vessels merge), or go on EVA and board from outside.");
            }
            int occupied = to.protoModuleCrew != null ? to.protoModuleCrew.Count : 0;
            if (to.CrewCapacity <= 0 || occupied >= to.CrewCapacity)
            {
                throw new BridgeException(409, Util.PartTitle(to) + " has no free seat (" + occupied + "/" + to.CrewCapacity + ").");
            }
            // The stock transfer dialog (CrewTransfer.IsValidPart) only offers parts with crewTransferAvailable.
            if (!to.crewTransferAvailable || !from.crewTransferAvailable)
            {
                throw new BridgeException(409, "KSP does not allow crew transfer " + (!from.crewTransferAvailable ? "out of " + Util.PartTitle(from) : "into " + Util.PartTitle(to)) + ".",
                    "Go on EVA (POST /eva-go) and board the other part from outside (POST /eva-board).");
            }

            // The same steps as the stock crew-transfer dialog (CrewTransfer.MoveCrewTo), minus the UI:
            // the active vessel's IVA is despawned now and respawned a frame later (despawned internals
            // are only destroyed at the end of the frame, so an immediate respawn can find them still there).
            Vessel vessel = from.vessel;
            from.RemoveCrewmember(pcm);
            to.AddCrewmember(pcm);
            GameEvents.onCrewTransferred.Fire(new GameEvents.HostedFromToAction<ProtoCrewMember, Part>(pcm, from, to));
            Vessel.CrewWasModified(vessel);
            if (vessel == FlightGlobals.ActiveVessel && AutomationBridgeAddon.Instance != null)
            {
                vessel.DespawnCrew();
                AutomationBridgeAddon.Instance.StartCoroutine(CallbackUtil.DelayedCallback(1, delegate
                {
                    if (vessel != null && vessel == FlightGlobals.ActiveVessel)
                    {
                        vessel.SpawnCrew();
                    }
                }));
            }

            bool arrived = to.protoModuleCrew != null && to.protoModuleCrew.Contains(pcm);
            JObj d = new JObj();
            d["crew"] = pcm.name;
            d["fromPart"] = Util.PartRef(from);
            d["toPart"] = Util.PartRef(to);
            d["vessel"] = Util.VesselRef(vessel);
            d["verified"] = arrived;
            d["note"] = "Moved within one vessel; the stock crew-passable hatch path is not checked.";
            return d;
        }
    }
}
