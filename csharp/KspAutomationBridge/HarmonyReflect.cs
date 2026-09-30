using System;
using System.Reflection;

namespace KspAutomationBridge
{
    /// <summary>
    /// Applies Harmony patches through reflection, so the plugin builds without a reference to
    /// 0Harmony.dll and still loads when Harmony is absent. Pure .NET (tested offline).
    /// </summary>
    internal static class HarmonyReflect
    {
        /// <summary>Patches original with a static postfix; returns null on success, else why it failed.</summary>
        public static string Postfix(string harmonyId, MethodBase original, MethodInfo postfix)
        {
            if (original == null || postfix == null)
            {
                return "method to patch or postfix not found";
            }
            Type harmonyType = null;
            Type methodType = null;
            foreach (Assembly assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                if (assembly.GetName().Name == "0Harmony")
                {
                    harmonyType = assembly.GetType("HarmonyLib.Harmony");
                    methodType = assembly.GetType("HarmonyLib.HarmonyMethod");
                    break;
                }
            }
            if (harmonyType == null || methodType == null)
            {
                return "Harmony (0Harmony.dll, HarmonyLib 2.x) is not loaded";
            }
            MethodInfo patch = harmonyType.GetMethod("Patch", new[] { typeof(MethodBase), methodType, methodType, methodType, methodType });
            if (patch == null)
            {
                return "unsupported Harmony version (no Patch(original, prefix, postfix, transpiler, finalizer))";
            }
            try
            {
                object harmony = Activator.CreateInstance(harmonyType, harmonyId);
                object hook = Activator.CreateInstance(methodType, postfix);
                patch.Invoke(harmony, new[] { original, null, hook, null, null });
                return null;
            }
            catch (TargetInvocationException ex)
            {
                return "Harmony patch failed: " + (ex.InnerException ?? ex).Message;
            }
            catch (Exception ex)
            {
                return "Harmony patch failed: " + ex.Message;
            }
        }
    }
}
