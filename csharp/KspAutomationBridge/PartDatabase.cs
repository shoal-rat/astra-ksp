using System;
using System.Collections.Generic;
using System.Globalization;
using UnityEngine;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    /// <summary>
    /// The game's loaded part catalog (post ModuleManager, post part compilation). The full detail level
    /// carries the geometry and module data the craft writer needs: attach nodes, surface node, attach
    /// rules, bounds, engines (every mode), decouplers, chutes, command, reaction wheels.
    /// </summary>
    internal static class PartDatabase
    {
        public static void Register(Router r)
        {
            r.Main("GET", "/part-database", 120000, Basic, "Basic catalog (pre-2.0 schema): name, title, category, mass, first engine, resources.");
            r.Main("POST", "/part-database", 120000, Query, "Catalog with detail=basic|full, optional names filter.");
            r.Main("POST", "/part/resolve", 15000, Resolve, "Resolve a craft-file part id (e.g. 'fuelTank_4294' or 'fuelTank') to a loaded part.");
        }

        private static IEnumerable<AvailablePart> Parts(ICollection<string> names)
        {
            if (HighLogic.LoadedScene == GameScenes.LOADING || PartLoader.LoadedPartsList == null)
            {
                throw new BridgeException(503, "The part database is not loaded yet (scene " + HighLogic.LoadedScene + ").",
                    "Wait until GET /ping reports scene MAINMENU or later.");
            }
            HashSet<string> wanted = names != null && names.Count > 0 ? new HashSet<string>(names, StringComparer.Ordinal) : null;
            List<AvailablePart> result = new List<AvailablePart>();
            foreach (AvailablePart ap in PartLoader.LoadedPartsList)
            {
                if (ap != null && ap.partPrefab != null && (wanted == null || wanted.Contains(ap.name)))
                {
                    result.Add(ap);
                }
            }
            return result;
        }

        private static JObj Basic(BridgeRequest req)
        {
            return Build(false, req.StrList("names"));
        }

        private static JObj Query(BridgeRequest req)
        {
            string detail = req.Choice("detail", new[] { "basic", "full" }, "basic");
            return Build(detail == "full", req.StrList("names"));
        }

        private static JObj Build(bool full, List<string> names)
        {
            List<object> parts = new List<object>();
            List<object> errors = new List<object>();
            foreach (AvailablePart ap in Parts(names))
            {
                try
                {
                    parts.Add(full ? Full(ap) : BasicEntry(ap));
                }
                catch (Exception ex)
                {
                    errors.Add(ap.name + ": " + ex.GetType().Name + ": " + ex.Message);
                }
            }
            JObj d = new JObj();
            d["count"] = parts.Count;
            d["detail"] = full ? "full" : "basic";
            d["parts"] = parts;
            if (errors.Count > 0)
            {
                d["errors"] = errors;
            }
            if (names != null && names.Count > 0 && parts.Count < names.Count)
            {
                List<object> missing = new List<object>();
                HashSet<string> found = new HashSet<string>();
                foreach (object p in parts)
                {
                    found.Add((string)((JObj)p)["name"]);
                }
                foreach (string n in names)
                {
                    if (!found.Contains(n))
                    {
                        missing.Add(n);
                    }
                }
                d["missing"] = missing;
            }
            return d;
        }

        // ------------------------------------------------------------------ basic (pre-2.0 schema)

        private static JObj BasicEntry(AvailablePart ap)
        {
            Part prefab = ap.partPrefab;
            JObj p = new JObj();
            p["name"] = ap.name;
            p["title"] = ap.title;
            p["category"] = ap.category.ToString();
            p["bulkhead"] = ap.bulkheadProfiles ?? "";
            p["crewCapacity"] = prefab.CrewCapacity;
            p["dryMassT"] = prefab.mass;
            ModuleEngines engine = prefab.FindModuleImplementing<ModuleEngines>();
            if (engine != null)
            {
                p["maxThrustKn"] = engine.maxThrust;
                if (engine.atmosphereCurve != null)
                {
                    p["ispVacS"] = engine.atmosphereCurve.Evaluate(0f);
                    p["ispAslS"] = engine.atmosphereCurve.Evaluate(1f);
                }
            }
            JObj resources = new JObj();
            if (prefab.Resources != null)
            {
                foreach (PartResource r in prefab.Resources)
                {
                    resources[r.resourceName] = r.maxAmount;
                }
            }
            p["resources"] = resources;
            return p;
        }

        // ------------------------------------------------------------------ full

        private static JObj Full(AvailablePart ap)
        {
            Part prefab = ap.partPrefab;
            JObj p = new JObj();
            p["name"] = ap.name;
            p["title"] = ap.title;
            p["category"] = ap.category.ToString();
            p["cost"] = ap.cost;
            p["mass_t"] = prefab.mass;
            p["crewCapacity"] = prefab.CrewCapacity;
            p["bulkhead"] = ap.bulkheadProfiles ?? "";
            p["tags"] = ap.tags ?? "";
            p["techRequired"] = ap.TechRequired;
            p["bounds"] = Bounds(prefab);
            p["nodes"] = Nodes(prefab);
            p["srfNode"] = prefab.srfAttachNode != null ? NodeGeometry(prefab.srfAttachNode) : null;
            p["attachRules"] = AttachRules(prefab.attachRules);
            p["fuelCrossFeed"] = prefab.fuelCrossFeed;
            p["stageable"] = IsStageable(prefab);
            p["stagingIcon"] = prefab.stagingIcon ?? "";

            JObj resources = new JObj();
            if (prefab.Resources != null)
            {
                foreach (PartResource r in prefab.Resources)
                {
                    resources[r.resourceName] = Resource(r.amount, r.maxAmount, r.info != null ? r.info.density : 0f);
                }
            }
            JObj b9 = B9Tank(ap, resources);
            p["resources"] = resources;
            if (b9 != null)
            {
                p["b9Tank"] = b9;
            }

            p["engines"] = Engines(prefab);
            p["decoupler"] = Decoupler(prefab);
            p["parachute"] = Parachute(prefab);
            ModuleCommand command = prefab.FindModuleImplementing<ModuleCommand>();
            JObj cmd = null;
            if (command != null)
            {
                cmd = new JObj();
                cmd["minimumCrew"] = command.minimumCrew;
            }
            p["command"] = cmd;
            ModuleReactionWheel wheel = prefab.FindModuleImplementing<ModuleReactionWheel>();
            JObj rw = null;
            if (wheel != null)
            {
                rw = new JObj();
                rw["pitch"] = wheel.PitchTorque;
                rw["yaw"] = wheel.YawTorque;
                rw["roll"] = wheel.RollTorque;
            }
            p["reactionWheel"] = rw;
            List<object> modules = new List<object>();
            foreach (PartModule pm in prefab.Modules)
            {
                if (pm != null)
                {
                    modules.Add(pm.moduleName ?? pm.ClassName);
                }
            }
            p["modules"] = modules;
            p["maxTemp"] = prefab.maxTemp;
            p["skinMaxTemp"] = prefab.skinMaxTemp;
            p["crashTolerance"] = prefab.crashTolerance;
            return p;
        }

        private static JObj Resource(double amount, double max, float density)
        {
            JObj r = new JObj();
            r["amount"] = amount;
            r["max"] = max;
            r["density"] = density;
            return r;
        }

        private static List<object> Nodes(Part prefab)
        {
            List<object> nodes = new List<object>();
            if (prefab.attachNodes == null)
            {
                return nodes;
            }
            foreach (AttachNode n in prefab.attachNodes)
            {
                if (n == null)
                {
                    continue;
                }
                JObj o = NodeGeometry(n);
                o["id"] = n.id;
                o["size"] = n.size;
                nodes.Add(o);
            }
            return nodes;
        }

        private static JObj NodeGeometry(AttachNode n)
        {
            JObj o = new JObj();
            o["pos"] = Util.Vec(n.position);
            o["dir"] = Util.Vec(n.orientation);
            return o;
        }

        private static JObj AttachRules(AttachRules rules)
        {
            if (rules == null)
            {
                return null;
            }
            JObj o = new JObj();
            o["stack"] = rules.stack;
            o["srfAttach"] = rules.srfAttach;
            o["allowStack"] = rules.allowStack;
            o["allowSrfAttach"] = rules.allowSrfAttach;
            o["allowCollision"] = rules.allowCollision;
            o["allowDock"] = rules.allowDock;
            o["allowRotate"] = rules.allowRotate;
            o["allowRoot"] = rules.allowRoot;
            return o;
        }

        private static bool IsStageable(Part prefab)
        {
            if (!string.IsNullOrEmpty(prefab.stagingIcon))
            {
                return true;
            }
            foreach (PartModule pm in prefab.Modules)
            {
                if (pm is IStageSeparator || pm is ModuleEngines || pm is ModuleDecouplerBase || pm is ModuleParachute
                    || pm is ModuleProceduralFairing)
                {
                    return true;
                }
            }
            return false;
        }

        // ------------------------------------------------------------------ bounds

        /// <summary>
        /// Axis-aligned bounds in part-local metres from the visible meshes. Prefab renderers are inactive,
        /// so Renderer.bounds is empty; transform each shared mesh's local bounds into the part frame.
        /// Meshes that are hidden on the prefab (inactive objects, disabled renderers) are skipped.
        /// </summary>
        private static JObj Bounds(Part prefab)
        {
            Transform root = prefab.transform;
            Matrix4x4 toPart = root.worldToLocalMatrix;
            bool any = false;
            Bounds total = new Bounds();
            foreach (MeshFilter mf in prefab.GetComponentsInChildren<MeshFilter>(true))
            {
                Renderer renderer = mf.GetComponent<Renderer>();
                if (mf.sharedMesh == null || renderer == null || !renderer.enabled || !ActiveUnder(mf.transform, root))
                {
                    continue;
                }
                Encapsulate(ref total, ref any, toPart * mf.transform.localToWorldMatrix, mf.sharedMesh.bounds);
            }
            foreach (SkinnedMeshRenderer smr in prefab.GetComponentsInChildren<SkinnedMeshRenderer>(true))
            {
                if (smr.sharedMesh == null || !smr.enabled || !ActiveUnder(smr.transform, root))
                {
                    continue;
                }
                Encapsulate(ref total, ref any, toPart * smr.transform.localToWorldMatrix, smr.sharedMesh.bounds);
            }
            string source = "renderers";
            if (!any)
            {
                source = "colliders";
                foreach (Collider c in prefab.GetComponentsInChildren<Collider>(true))
                {
                    Bounds local;
                    if (!ColliderLocalBounds(c, out local))
                    {
                        continue;
                    }
                    Encapsulate(ref total, ref any, toPart * c.transform.localToWorldMatrix, local);
                }
            }
            JObj o = new JObj();
            o["size"] = any ? Util.Vec(total.size) : new[] { 0f, 0f, 0f };
            o["center"] = any ? Util.Vec(total.center) : new[] { 0f, 0f, 0f };
            o["source"] = any ? source : "none";
            return o;
        }

        private static bool ActiveUnder(Transform t, Transform root)
        {
            for (Transform x = t; x != null && x != root; x = x.parent)
            {
                if (!x.gameObject.activeSelf)
                {
                    return false;
                }
            }
            return true;
        }

        private static bool ColliderLocalBounds(Collider c, out Bounds local)
        {
            local = new Bounds();
            BoxCollider box = c as BoxCollider;
            if (box != null)
            {
                local = new Bounds(box.center, box.size);
                return true;
            }
            SphereCollider sphere = c as SphereCollider;
            if (sphere != null)
            {
                local = new Bounds(sphere.center, Vector3.one * sphere.radius * 2f);
                return true;
            }
            CapsuleCollider capsule = c as CapsuleCollider;
            if (capsule != null)
            {
                Vector3 size = Vector3.one * capsule.radius * 2f;
                size[capsule.direction] = Mathf.Max(capsule.height, capsule.radius * 2f);
                local = new Bounds(capsule.center, size);
                return true;
            }
            MeshCollider mesh = c as MeshCollider;
            if (mesh != null && mesh.sharedMesh != null)
            {
                local = mesh.sharedMesh.bounds;
                return true;
            }
            return false;
        }

        private static void Encapsulate(ref Bounds total, ref bool any, Matrix4x4 m, Bounds local)
        {
            Vector3 c = local.center;
            Vector3 e = local.extents;
            for (int i = 0; i < 8; i++)
            {
                Vector3 corner = new Vector3(
                    c.x + ((i & 1) == 0 ? -e.x : e.x),
                    c.y + ((i & 2) == 0 ? -e.y : e.y),
                    c.z + ((i & 4) == 0 ? -e.z : e.z));
                Vector3 p = m.MultiplyPoint3x4(corner);
                if (!any)
                {
                    total = new Bounds(p, Vector3.zero);
                    any = true;
                }
                else
                {
                    total.Encapsulate(p);
                }
            }
        }

        // ------------------------------------------------------------------ modules

        private static List<object> Engines(Part prefab)
        {
            List<object> list = new List<object>();
            ModuleGimbal gimbal = prefab.FindModuleImplementing<ModuleGimbal>();
            foreach (PartModule pm in prefab.Modules)
            {
                ModuleEngines e = pm as ModuleEngines;
                if (e == null)
                {
                    continue;
                }
                JObj o = new JObj();
                o["id"] = e.engineID;
                o["module"] = string.IsNullOrEmpty(e.moduleName) ? e.ClassName : e.moduleName; // ModuleEngines | ModuleEnginesFX
                o["type"] = e.engineType.ToString();
                o["maxThrust_kn"] = e.maxThrust;
                o["minThrust_kn"] = e.minThrust;
                o["isp_vac"] = e.atmosphereCurve != null ? e.atmosphereCurve.Evaluate(0f) : 0f;
                o["isp_asl"] = e.atmosphereCurve != null ? e.atmosphereCurve.Evaluate(1f) : 0f;
                o["atmosphereCurve"] = CurveKeys(e.atmosphereCurve);
                List<object> props = new List<object>();
                if (e.propellants != null)
                {
                    foreach (Propellant prop in e.propellants)
                    {
                        JObj po = new JObj();
                        po["name"] = prop.name;
                        po["ratio"] = prop.ratio;
                        props.Add(po);
                    }
                }
                o["propellants"] = props;
                o["throttleLocked"] = e.throttleLocked;
                o["gimbal_deg"] = gimbal != null ? gimbal.gimbalRange : 0f;
                if (e.useVelCurve && e.velCurve != null)
                {
                    o["velCurve"] = CurveKeys(e.velCurve);
                }
                if (e.useAtmCurve && e.atmCurve != null)
                {
                    o["atmCurve"] = CurveKeys(e.atmCurve);
                }
                list.Add(o);
            }
            return list;
        }

        /// <summary>
        /// FloatCurve keys as [time, value, inTangent, outTangent] (the first two positions are the
        /// pre-2.0 [time, value] pair), so a consumer can evaluate the Hermite curve exactly between keys.
        /// </summary>
        private static List<object> CurveKeys(FloatCurve curve)
        {
            List<object> keys = new List<object>();
            if (curve == null || curve.Curve == null)
            {
                return keys;
            }
            foreach (Keyframe k in curve.Curve.keys)
            {
                keys.Add(new[] { k.time, k.value, k.inTangent, k.outTangent });
            }
            return keys;
        }

        private static JObj Decoupler(Part prefab)
        {
            ModuleDecouplerBase dec = prefab.FindModuleImplementing<ModuleDecouplerBase>();
            if (dec == null)
            {
                return null;
            }
            JObj o = new JObj();
            o["ejectionForce"] = dec.ejectionForce;
            o["isOmni"] = dec.isOmniDecoupler;
            o["explosiveNodeId"] = dec.explosiveNodeID;
            o["radial"] = dec is ModuleAnchoredDecoupler;
            return o;
        }

        private static JObj Parachute(Part prefab)
        {
            ModuleParachute chute = prefab.FindModuleImplementing<ModuleParachute>();
            if (chute == null)
            {
                return null;
            }
            JObj o = new JObj();
            o["semiDeployedDrag"] = chute.semiDeployedDrag;
            o["fullyDeployedDrag"] = chute.fullyDeployedDrag;
            o["minAirPressureToOpen"] = chute.minAirPressureToOpen;
            o["deployAltitude"] = chute.deployAltitude;
            return o;
        }

        // ------------------------------------------------------------------ B9PartSwitch tanks

        /// <summary>
        /// B9PartSwitch adds its tank resources when a part instance starts, so the prefab lacks them.
        /// Reconstruct the default subtype's tank from the part config and B9_TANK_TYPE definitions and
        /// add any resource the prefab does not already list. Returns what was applied, or null.
        /// </summary>
        private static JObj B9Tank(AvailablePart ap, JObj resources)
        {
            ConfigNode config = ap.partConfig;
            if (config == null)
            {
                return null;
            }
            List<object> applied = new List<object>();
            JObj summary = null;
            foreach (ConfigNode module in config.GetNodes("MODULE"))
            {
                if (module.GetValue("name") != "ModuleB9PartSwitch")
                {
                    continue;
                }
                ConfigNode subtype = DefaultSubtype(module);
                string tankType = subtype != null ? subtype.GetValue("tankType") : null;
                if (string.IsNullOrEmpty(tankType))
                {
                    continue;
                }
                ConfigNode tank = FindTankType(tankType);
                if (tank == null)
                {
                    continue;
                }
                double volume = Num(module, "baseVolume", 0) * Num(subtype, "volumeMultiplier", 1) + Num(subtype, "volumeAdded", 0);
                double tankPercent = Num(tank, "percentFilled", 100);
                foreach (ConfigNode res in tank.GetNodes("RESOURCE"))
                {
                    string name = res.GetValue("name");
                    if (string.IsNullOrEmpty(name) || resources.ContainsKey(name))
                    {
                        continue;
                    }
                    double max = Num(res, "unitsPerVolume", 0) * volume;
                    double percent = Num(res, "percentFilled", Num(subtype, "percentFilled", tankPercent));
                    PartResourceDefinition def = PartResourceLibrary.Instance != null ? PartResourceLibrary.Instance.GetDefinition(name) : null;
                    resources[name] = Resource(max * percent / 100.0, max, def != null ? def.density : 0f);
                    applied.Add(name);
                }
                summary = new JObj();
                summary["moduleId"] = module.GetValue("moduleID");
                summary["subtype"] = subtype.GetValue("name");
                summary["tankType"] = tankType;
                summary["volume"] = volume;
                summary["tankMass_t"] = Num(tank, "tankMass", 0) * volume;
                summary["addedMass_t"] = Num(subtype, "addedMass", 0);
                summary["resourcesAdded"] = applied;
                summary["note"] = "Reconstructed from config; tankMass_t and addedMass_t are NOT included in mass_t.";
            }
            return applied.Count > 0 ? summary : null;
        }

        /// <summary>B9's default: an explicit currentSubtype, else the highest defaultSubtypePriority (first on ties).</summary>
        private static ConfigNode DefaultSubtype(ConfigNode module)
        {
            ConfigNode[] subtypes = module.GetNodes("SUBTYPE");
            if (subtypes.Length == 0)
            {
                return null;
            }
            string current = module.GetValue("currentSubtype");
            ConfigNode best = subtypes[0];
            double bestPriority = double.NegativeInfinity;
            foreach (ConfigNode s in subtypes)
            {
                if (current != null && s.GetValue("name") == current)
                {
                    return s;
                }
                double priority = Num(s, "defaultSubtypePriority", 0);
                if (priority > bestPriority)
                {
                    best = s;
                    bestPriority = priority;
                }
            }
            return best;
        }

        private static ConfigNode FindTankType(string name)
        {
            if (GameDatabase.Instance == null)
            {
                return null;
            }
            foreach (ConfigNode node in GameDatabase.Instance.GetConfigNodes("B9_TANK_TYPE"))
            {
                if (node.GetValue("name") == name)
                {
                    return node;
                }
            }
            return null;
        }

        private static double Num(ConfigNode node, string key, double fallback)
        {
            double value;
            string text = node != null ? node.GetValue(key) : null;
            return text != null && double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out value) ? value : fallback;
        }

        // ------------------------------------------------------------------ resolve

        private static JObj Resolve(BridgeRequest req)
        {
            string partId = req.RequireStr("partId");
            JObj d = new JObj();
            d["partId"] = partId;
            string resolved = KSPUtil.GetPartName(partId);
            AvailablePart ap = PartLoader.getPartInfoByName(resolved);
            d["resolvedName"] = resolved;
            d["found"] = ap != null;
            if (ap != null)
            {
                d["availableName"] = ap.name;
                d["title"] = ap.title;
                d["category"] = ap.category.ToString();
            }
            return d;
        }
    }
}
