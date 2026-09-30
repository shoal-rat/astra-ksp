using System;
using System.Collections.Generic;
using System.IO;
using UnityEngine;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    /// <summary>Game-side helpers shared by the route files: guards, lookups by explicit id, conversions.</summary>
    internal static class Util
    {
        public static float[] Vec(Vector3 v)
        {
            return new[] { v.x, v.y, v.z };
        }

        public static double[] Vec(Vector3d v)
        {
            return new[] { v.x, v.y, v.z };
        }

        // ------------------------------------------------------------------ guards

        public static void RequireGame()
        {
            if (HighLogic.CurrentGame == null)
            {
                throw new BridgeException(409, "No save is loaded (scene " + HighLogic.LoadedScene + ").",
                    "POST /load-save {saveFolder} first.");
            }
        }

        public static Vessel RequireActiveVessel()
        {
            if (!HighLogic.LoadedSceneIsFlight || FlightGlobals.ActiveVessel == null)
            {
                throw new BridgeException(409, "This needs the flight scene with an active vessel (scene is "
                    + HighLogic.LoadedScene + ").", "Launch or POST /fly-vessel first; GET /state shows the scene.");
            }
            return FlightGlobals.ActiveVessel;
        }

        public static void RequireFlightScene()
        {
            if (!HighLogic.LoadedSceneIsFlight)
            {
                throw new BridgeException(409, "This needs the flight scene (scene is " + HighLogic.LoadedScene + ").",
                    "Launch or POST /fly-vessel first.");
            }
        }

        // ------------------------------------------------------------------ vessels

        public static Vessel FindVessel(uint persistentId)
        {
            if (FlightGlobals.Vessels == null)
            {
                return null;
            }
            foreach (Vessel v in FlightGlobals.Vessels)
            {
                if (v != null && v.persistentId == persistentId)
                {
                    return v;
                }
            }
            return null;
        }

        /// <summary>
        /// Resolves a vessel from `{idKey}` (persistentId) or `{nameKey}` (exact name; an ambiguous name is
        /// an error listing the candidates). Returns null when neither is given and it is not required.
        /// </summary>
        public static Vessel ResolveVessel(BridgeRequest req, string idKey, string nameKey, bool required)
        {
            uint? id = req.UInt(idKey);
            if (id.HasValue)
            {
                Vessel v = FindVessel(id.Value);
                if (v == null)
                {
                    throw new BridgeException(404, "No vessel with persistentId " + id.Value + ".", "GET /vessels lists vessels with their ids.");
                }
                return v;
            }
            string name = nameKey == null ? null : req.Str(nameKey);
            if (name == null)
            {
                if (required)
                {
                    throw new BridgeException(400, "Missing required parameter '" + idKey + "'"
                        + (nameKey != null ? " (or '" + nameKey + "')" : "") + ".");
                }
                return null;
            }
            List<Vessel> matches = new List<Vessel>();
            if (FlightGlobals.Vessels != null)
            {
                foreach (Vessel v in FlightGlobals.Vessels)
                {
                    if (v != null && string.Equals(v.vesselName, name, StringComparison.Ordinal))
                    {
                        matches.Add(v);
                    }
                }
                if (matches.Count == 0)
                {
                    foreach (Vessel v in FlightGlobals.Vessels)
                    {
                        if (v != null && string.Equals(v.vesselName, name, StringComparison.OrdinalIgnoreCase))
                        {
                            matches.Add(v);
                        }
                    }
                }
            }
            if (matches.Count == 0)
            {
                throw new BridgeException(404, "No vessel named '" + name + "'.", "GET /vessels lists vessels with their ids.");
            }
            if (matches.Count > 1)
            {
                List<string> ids = new List<string>();
                foreach (Vessel v in matches)
                {
                    ids.Add(v.persistentId + " (" + v.vesselType + ", " + v.situation + ")");
                }
                throw new BridgeException(409, matches.Count + " vessels are named '" + name + "': " + string.Join("; ", ids.ToArray()) + ".",
                    "Pass " + idKey + " to pick one.");
            }
            return matches[0];
        }

        public static JObj VesselRef(Vessel v)
        {
            JObj o = new JObj();
            o["name"] = v.vesselName;
            o["persistentId"] = v.persistentId;
            o["type"] = v.vesselType.ToString();
            o["situation"] = v.situation.ToString();
            o["body"] = v.mainBody != null ? v.mainBody.bodyName : null;
            o["loaded"] = v.loaded;
            return o;
        }

        // ------------------------------------------------------------------ parts

        public static Part FindLoadedPart(uint persistentId)
        {
            if (FlightGlobals.VesselsLoaded == null)
            {
                return null;
            }
            foreach (Vessel v in FlightGlobals.VesselsLoaded)
            {
                if (v == null || v.parts == null)
                {
                    continue;
                }
                foreach (Part p in v.parts)
                {
                    if (p != null && p.persistentId == persistentId)
                    {
                        return p;
                    }
                }
            }
            return null;
        }

        /// <summary>
        /// Resolves a part from `{prefix}Id` (persistentId, any loaded vessel) or `{prefix}Index` (index into
        /// indexVessel.parts, which is the order kRPC's vessel.parts.all uses). Null when neither is given.
        /// </summary>
        public static Part ResolvePart(BridgeRequest req, string prefix, Vessel indexVessel)
        {
            uint? id = req.UInt(prefix + "Id");
            if (id.HasValue)
            {
                Part p = FindLoadedPart(id.Value);
                if (p == null)
                {
                    throw new BridgeException(404, "No loaded part with persistentId " + id.Value + ".",
                        "GET /vessel-parts lists part ids and indices.");
                }
                return p;
            }
            int? index = req.Int(prefix + "Index");
            if (index.HasValue)
            {
                if (indexVessel == null)
                {
                    throw new BridgeException(400, prefix + "Index is not accepted here (no vessel to index into); pass " + prefix + "Id (persistentId).",
                        "GET /vessel-parts lists part ids.");
                }
                if (indexVessel.parts == null || index.Value < 0 || index.Value >= indexVessel.parts.Count)
                {
                    throw new BridgeException(404, "Part index " + index.Value + " is out of range for "
                        + (indexVessel != null ? indexVessel.vesselName : "the vessel") + ".", "GET /vessel-parts lists part ids and indices.");
                }
                return indexVessel.parts[index.Value];
            }
            return null;
        }

        public static JObj PartRef(Part p)
        {
            JObj o = new JObj();
            o["persistentId"] = p.persistentId;
            o["flightId"] = p.flightID;
            o["index"] = p.vessel != null && p.vessel.parts != null ? p.vessel.parts.IndexOf(p) : -1;
            o["name"] = p.partInfo != null ? p.partInfo.name : p.name;
            o["title"] = PartTitle(p);
            return o;
        }

        public static string PartTitle(Part p)
        {
            return p.partInfo != null ? p.partInfo.title : p.name;
        }

        // ------------------------------------------------------------------ crew and bodies

        /// <summary>Finds a seated kerbal by exact (case-insensitive) name in the loaded vessels.</summary>
        public static ProtoCrewMember FindSeatedKerbal(string name, out Part part)
        {
            part = null;
            if (FlightGlobals.VesselsLoaded == null)
            {
                return null;
            }
            foreach (Vessel v in FlightGlobals.VesselsLoaded)
            {
                if (v == null || v.isEVA || v.parts == null)
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
                        if (pcm != null && string.Equals(pcm.name, name, StringComparison.OrdinalIgnoreCase))
                        {
                            part = p;
                            return pcm;
                        }
                    }
                }
            }
            return null;
        }

        public static CelestialBody FindBody(string name)
        {
            if (FlightGlobals.Bodies == null)
            {
                return null;
            }
            foreach (CelestialBody b in FlightGlobals.Bodies)
            {
                if (b != null && string.Equals(b.bodyName, name, StringComparison.OrdinalIgnoreCase))
                {
                    return b;
                }
            }
            return null;
        }

        public static CelestialBody RequireBody(string name)
        {
            CelestialBody body = FindBody(name);
            if (body == null)
            {
                throw new BridgeException(404, "No celestial body named '" + name + "'.");
            }
            return body;
        }

        // ------------------------------------------------------------------ files

        public static string SavesRoot
        {
            get { return Path.GetFullPath(Path.Combine(KSPUtil.ApplicationRootPath, "saves")); }
        }

        /// <summary>A single path segment (save folder, save file, craft name): no separators, no '..'.</summary>
        public static string RequireSegment(string value, string what)
        {
            if (string.IsNullOrEmpty(value) || value.Contains("..") || value.IndexOfAny(new[] { '/', '\\', ':' }) >= 0
                || value.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
            {
                throw new BridgeException(400, "Invalid " + what + " '" + value + "': give a bare name, not a path.");
            }
            return value;
        }

        public static double SafeUt()
        {
            try
            {
                return Planetarium.fetch != null ? Planetarium.GetUniversalTime() : double.NaN;
            }
            catch (Exception)
            {
                return double.NaN;
            }
        }
    }
}
