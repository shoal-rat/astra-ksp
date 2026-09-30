using System;
using System.Collections.Generic;
using System.IO;
using UnityEngine;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    /// <summary>
    /// Screenshots by rendering the scene's cameras into a RenderTexture, so they work in every scene and
    /// while the KSP window is unfocused or covered (the game must still be updating).
    /// </summary>
    internal static class ScreenshotRoutes
    {
        private const int MinWidth = 64;
        private const int MaxWidth = 4096;

        public static void Register(Router r)
        {
            r.Main("POST", "/screenshot", 30000, Capture, "Render the current view to a PNG file {path (absolute .png), width, includeUi}.");
        }

        private static JObj Capture(BridgeRequest req)
        {
            string path = req.RequireStr("path");
            if (!Path.IsPathRooted(path) || !path.EndsWith(".png", StringComparison.OrdinalIgnoreCase))
            {
                throw new BridgeException(400, "path must be an absolute file path ending in .png.");
            }
            int width = Mathf.Clamp(req.Int("width") ?? 1280, MinWidth, MaxWidth);
            bool includeUi = req.Bool("includeUi", false);
            float aspect = Screen.width > 0 && Screen.height > 0 ? (float)Screen.height / Screen.width : 9f / 16f;
            int height = Mathf.Max(MinWidth / 2, Mathf.RoundToInt(width * aspect));

            List<Camera> cameras = SceneCameras(includeUi);
            if (cameras.Count == 0)
            {
                throw new BridgeException(409, "No active scene camera to render (scene " + HighLogic.LoadedScene + ").", "Retry once the scene has loaded.");
            }

            RenderTexture rt = new RenderTexture(width, height, 24, RenderTextureFormat.ARGB32);
            Texture2D image = new Texture2D(width, height, TextureFormat.RGB24, false);
            RenderTexture previousActive = RenderTexture.active;
            List<object> used = new List<object>();
            byte[] png;
            try
            {
                used.AddRange(RenderInto(rt, cameras));
                RenderTexture.active = rt;
                image.ReadPixels(new Rect(0, 0, width, height), 0, 0);
                image.Apply();
                png = image.EncodeToPNG();
            }
            finally
            {
                RenderTexture.active = previousActive;
                rt.Release();
                UnityEngine.Object.Destroy(rt);
                UnityEngine.Object.Destroy(image);
            }

            string dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir))
            {
                Directory.CreateDirectory(dir);
            }
            File.WriteAllBytes(path, png);
            JObj d = new JObj();
            d["path"] = path;
            d["width"] = width;
            d["height"] = height;
            d["bytes"] = png.Length;
            d["scene"] = HighLogic.LoadedScene.ToString();
            d["cameras"] = used;
            d["ut"] = Util.SafeUt();
            return d;
        }

        /// <summary>The cameras that draw the scene (and, if asked, the UI), in depth order.</summary>
        internal static List<Camera> SceneCameras(bool includeUi)
        {
            List<Camera> cameras = new List<Camera>();
            foreach (Camera cam in Camera.allCameras)
            {
                // Skip cameras that render into their own textures (portraits, docking cams) and, unless
                // asked, the UI cameras.
                if (cam == null || !cam.enabled || cam.targetTexture != null)
                {
                    continue;
                }
                if (!includeUi && cam.name.IndexOf("UI", StringComparison.Ordinal) >= 0)
                {
                    continue;
                }
                cameras.Add(cam);
            }
            cameras.Sort(delegate(Camera a, Camera b) { return a.depth.CompareTo(b.depth); });
            return cameras;
        }

        /// <summary>Renders each camera into rt in order, restoring its own target; returns their names.</summary>
        internal static List<object> RenderInto(RenderTexture rt, List<Camera> cameras)
        {
            List<object> used = new List<object>();
            foreach (Camera cam in cameras)
            {
                RenderTexture previousTarget = cam.targetTexture;
                cam.targetTexture = rt;
                try
                {
                    cam.Render();
                    used.Add(cam.name);
                }
                finally
                {
                    cam.targetTexture = previousTarget;
                }
            }
            return used;
        }
    }
}
