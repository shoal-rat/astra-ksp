using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using Unity.Collections;
using UnityEngine;
using UnityEngine.Rendering;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    /// <summary>
    /// Continuous capture for video. At the end of rendered frames, at most `fps` times per real second,
    /// the scene cameras are rendered into a texture at the requested size. With an ffmpeg executable
    /// the frame is read back asynchronously (no GPU stall) and piped raw to ffmpeg, which encodes
    /// H.264 into video.mkv in its own process; without one, frames are JPEG-encoded and written as
    /// frame_000001.jpg, ... Frames are taken only while game time advances, so the crew's paused
    /// deliberation never appears in the footage. Every frame gets a row in frames.csv (UT and vessel
    /// state, for captions and editing); /record/mark appends labeled events to marks.csv (ASTRA marks
    /// every tool call except the read-only instruments). Recording stops by itself when the disk runs low or ffmpeg exits. Numbering,
    /// the index rows and the writer thread live in FrameQueue (tested offline).
    /// </summary>
    internal static class Recorder
    {
        private const int MaxQueued = 24;
        private const int MaxInFlight = 6;
        private const long MinFreeBytes = 3L * 1024 * 1024 * 1024;
        private const int DiskCheckEvery = 150;

        // UT moves once per physics step (0.02 s of game time), so at a high capture rate a slot can fall
        // between two steps in normal play. Only UT that stays put this long (real seconds: several
        // physics steps, also at physics warp) means the game is frozen.
        private const float FrozenUtRealS = 0.1f;
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        // The current or last recording's queue: after a stop whose writer did not finish in time it is
        // still flushing, and Start refuses until it is done.
        private static FrameQueue _queue;
        private static bool _active;
        private static string _stopRequest; // set where tearing down is unsafe (a readback callback), acted on in Tick
        private static string _dir;
        private static bool _video;
        private static Process _ffmpeg;
        private static Stream _ffmpegIn;
        private static string _ffmpegLast = "";
        private static string _output = "";
        private static int _width, _height, _quality, _crf;
        private static float _fps;
        private static bool _includeUi, _skipPaused;
        private static int _dropped, _skipped, _inFlight;
        private static double _seenUt = double.NaN;
        private static float _utChangedReal;
        private static float _nextDue;
        private static float _startedReal;
        private static double _captureMs;
        private static StreamWriter _index, _marks;
        private static RenderTexture _rt;
        private static Texture2D _tex;
        private static string _stopReason = "";
        private static string _lastError = "";

        public static void Register(Router r)
        {
            r.Main("POST", "/record/start", 20000, Start,
                "Start recording {dir (absolute), ffmpeg (exe: H.264 video.mkv; else JPEG frames), fps (15), width, height, crf (20), preset (veryfast), quality (85), includeUi (true), skipPaused (true)}.");
            r.Main("POST", "/record/stop", 90000, Stop, "Stop recording; flushes pending frames, finishes the video and returns the totals.");
            r.Main("GET", "/record/status", 15000, Status, "Whether a recording runs, its folder, frames written, dropped and skipped.");
            r.Main("POST", "/record/mark", 15000, Mark, "Append a labeled event {label, detail} to marks.csv at the current frame.");
        }

        /// <summary>Runs for the life of the addon: one capture attempt after each rendered frame.</summary>
        internal static IEnumerator Loop()
        {
            WaitForEndOfFrame endOfFrame = new WaitForEndOfFrame();
            while (true)
            {
                yield return endOfFrame;
                if (!_active)
                {
                    continue;
                }
                try
                {
                    Tick();
                }
                catch (Exception ex)
                {
                    _lastError = ex.GetType().Name + ": " + ex.Message;
                    _dropped++;
                }
            }
        }

        private static void Tick()
        {
            if (_stopRequest != null)
            {
                StopNow(_stopRequest);
                return;
            }
            if (_video && _ffmpeg != null && _ffmpeg.HasExited)
            {
                StopNow("ffmpeg exited (" + _ffmpeg.ExitCode + "): " + _ffmpegLast);
                return;
            }
            float now = Time.realtimeSinceStartup;
            double ut = Util.SafeUt();
            if (ut != _seenUt) // every rendered frame, so a still UT is timed in real seconds
            {
                _seenUt = ut;
                _utChangedReal = now;
            }
            if (now < _nextDue)
            {
                return;
            }
            _nextDue += 1f / _fps;
            if (_nextDue < now)
            {
                _nextDue = now + 1f / _fps; // fell behind: no burst of catch-up frames
            }
            GameScenes scene = HighLogic.LoadedScene;
            if (scene != GameScenes.FLIGHT && scene != GameScenes.SPACECENTER && scene != GameScenes.TRACKSTATION)
            {
                _skipped++;
                return;
            }
            bool paused = Time.timeScale == 0f || (HighLogic.LoadedSceneIsFlight && FlightDriver.Pause);
            bool frozen = now - _utChangedReal > FrozenUtRealS;
            if (_skipPaused && (paused || frozen))
            {
                _skipped++;
                return;
            }
            if (_queue.Full || (_video && _inFlight >= MaxInFlight))
            {
                _dropped++;
                return;
            }
            List<Camera> cameras = ScreenshotRoutes.SceneCameras(_includeUi);
            if (cameras.Count == 0)
            {
                _skipped++;
                return;
            }

            float t0 = Time.realtimeSinceStartup;
            string row = IndexRow(now - _startedReal, ut, scene);
            if (_video)
            {
                ScreenshotRoutes.RenderInto(_rt, cameras);
                FrameQueue queue = _queue;
                _inFlight++;
                AsyncGPUReadback.Request(_rt, 0, TextureFormat.RGBA32,
                    delegate(AsyncGPUReadbackRequest request) { OnReadback(request, row, queue); });
            }
            else
            {
                RenderTexture previousActive = RenderTexture.active;
                byte[] jpg;
                try
                {
                    ScreenshotRoutes.RenderInto(_rt, cameras);
                    RenderTexture.active = _rt;
                    _tex.ReadPixels(new Rect(0, 0, _width, _height), 0, 0);
                    _tex.Apply();
                    jpg = _tex.EncodeToJPG(_quality);
                }
                finally
                {
                    RenderTexture.active = previousActive;
                }
                Accepted(_queue.Accept(jpg, jpg.Length, false, row));
            }
            _captureMs += (Time.realtimeSinceStartup - t0) * 1000.0;
        }

        /// <summary>Unity's readback callback (main thread, from its own dispatch or WaitAllRequests).</summary>
        private static void OnReadback(AsyncGPUReadbackRequest request, string row, FrameQueue queue)
        {
            if (queue != _queue)
            {
                return; // a previous recording's readback
            }
            _inFlight = Math.Max(0, _inFlight - 1);
            if (!_active || _stopRequest != null)
            {
                return;
            }
            try
            {
                if (request.hasError)
                {
                    _dropped++;
                    return;
                }
                int length = _width * _height * 4;
                byte[] buffer = queue.Rent();
                if (buffer == null)
                {
                    _dropped++;
                    return;
                }
                NativeArray<byte> data = request.GetData<byte>(0);
                if (data.Length != length)
                {
                    _dropped++;
                    _lastError = "readback returned " + data.Length + " bytes, expected " + length;
                    queue.Return(buffer);
                    return;
                }
                data.CopyTo(buffer);
                Accepted(queue.Accept(buffer, length, true, row));
            }
            catch (Exception ex)
            {
                _dropped++;
                _lastError = ex.GetType().Name + ": " + ex.Message;
            }
        }

        /// <summary>
        /// After frame `number` was queued: every DiskCheckEvery frames, check the free space. A low disk
        /// only requests the stop; Tick carries it out, never a readback callback.
        /// </summary>
        private static void Accepted(int number)
        {
            if (number > 0 && number % DiskCheckEvery == 0 && _stopRequest == null && FreeBytes(_dir) < MinFreeBytes)
            {
                _stopRequest = "disk space below " + (MinFreeBytes >> 30) + " GB";
            }
        }

        private static string IndexRow(float realS, double ut, GameScenes scene)
        {
            Vessel v = HighLogic.LoadedSceneIsFlight ? FlightGlobals.ActiveVessel : null;
            StringBuilder sb = new StringBuilder(160);
            sb.Append(realS.ToString("F3", Inv)).Append(',');
            sb.Append(ut.ToString("F3", Inv)).Append(',');
            sb.Append(scene.ToString()).Append(',');
            sb.Append(TimeWarp.CurrentRate.ToString("G4", Inv)).Append(',');
            if (v != null)
            {
                sb.Append(Csv(v.vesselName)).Append(',');
                sb.Append(v.mainBody != null ? v.mainBody.bodyName : "").Append(',');
                sb.Append(v.situation.ToString()).Append(',');
                sb.Append(v.altitude.ToString("F1", Inv)).Append(',');
                sb.Append(v.radarAltitude.ToString("F1", Inv)).Append(',');
                sb.Append(v.srfSpeed.ToString("F2", Inv)).Append(',');
                sb.Append(v.obt_speed.ToString("F2", Inv)).Append(',');
                sb.Append(v.verticalSpeed.ToString("F2", Inv)).Append(',');
                sb.Append(v.isEVA ? "1" : "0");
            }
            else
            {
                sb.Append(",,,,,,,,");
            }
            return sb.ToString();
        }

        private static string Csv(string s)
        {
            if (string.IsNullOrEmpty(s))
            {
                return "";
            }
            if (s.IndexOfAny(new[] { ',', '"', '\n', '\r' }) < 0)
            {
                return s;
            }
            return "\"" + s.Replace("\"", "\"\"").Replace("\r", " ").Replace("\n", " ") + "\"";
        }

        private static long FreeBytes(string dir)
        {
            try
            {
                return new DriveInfo(Path.GetPathRoot(dir)).AvailableFreeSpace;
            }
            catch (Exception)
            {
                return long.MaxValue;
            }
        }

        private static JObj Start(BridgeRequest req)
        {
            if (_active)
            {
                throw new BridgeException(409, "Already recording into " + _dir + ".", "POST /record/stop first.");
            }
            if (_queue != null && _queue.WriterAlive)
            {
                throw new BridgeException(409, "The previous recording is still writing its last frames into " + _dir + ".",
                    "POST /record/stop again to wait for it, then start.");
            }
            string dir = req.RequireStr("dir");
            if (!Path.IsPathRooted(dir))
            {
                throw new BridgeException(400, "dir must be an absolute folder path.");
            }
            Directory.CreateDirectory(dir);
            if (Directory.GetFiles(dir, "frame_*.jpg").Length > 0 || File.Exists(Path.Combine(dir, "video.mkv")))
            {
                throw new BridgeException(409, "The folder already holds a recording: " + dir, "Record into a new folder.");
            }
            if (FreeBytes(dir) < MinFreeBytes)
            {
                throw new BridgeException(507, "Less than " + (MinFreeBytes >> 30) + " GB free on the recording drive.");
            }
            string ffmpeg = req.Str("ffmpeg", "");
            if (ffmpeg.Length > 0 && !File.Exists(ffmpeg))
            {
                throw new BridgeException(400, "ffmpeg not found: " + ffmpeg);
            }
            if (ffmpeg.Length > 0 && !SystemInfo.supportsAsyncGPUReadback)
            {
                throw new BridgeException(501, "This GPU/driver cannot read frames back asynchronously.", "Omit ffmpeg to record JPEG frames.");
            }
            _video = ffmpeg.Length > 0;
            _fps = Mathf.Clamp((float)(req.Num("fps") ?? 15.0), 1f, 60f);
            _quality = Mathf.Clamp(req.Int("quality") ?? 85, 10, 100);
            _crf = Mathf.Clamp(req.Int("crf") ?? 20, 0, 51);
            string preset = req.Choice("preset", new[] { "ultrafast", "superfast", "veryfast", "faster", "fast", "medium" }, "veryfast");
            _includeUi = req.Bool("includeUi", true);
            _skipPaused = req.Bool("skipPaused", true);
            _width = Mathf.Clamp(req.Int("width") ?? Screen.width, 64, 3840);
            float aspect = Screen.width > 0 && Screen.height > 0 ? (float)Screen.height / Screen.width : 9f / 16f;
            _height = Mathf.Clamp(req.Int("height") ?? Mathf.RoundToInt(_width * aspect), 64, 2160);
            _width -= _width % 2; // video encoders want even sizes
            _height -= _height % 2;

            _dir = dir;
            try
            {
                // The file opens that can fail (a locked or read-only folder) come before anything that
                // is expensive to undo; on any failure AbortStart leaves nothing running or open.
                _index = new StreamWriter(Path.Combine(dir, "frames.csv"), false, new UTF8Encoding(false));
                _index.WriteLine("frame,real_s,ut,scene,warp,vessel,body,situation,altitude,radar_altitude,surface_speed,orbital_speed,vertical_speed,eva");
                _marks = new StreamWriter(Path.Combine(dir, "marks.csv"), false, new UTF8Encoding(false));
                _marks.WriteLine("frame,real_s,ut,label,detail");
                _marks.AutoFlush = true;
                Action<FrameQueue.Frame> sink;
                if (_video)
                {
                    _output = Path.Combine(dir, "video.mkv");
                    ProcessStartInfo psi = new ProcessStartInfo(ffmpeg,
                        "-hide_banner -loglevel warning -y -f rawvideo -pix_fmt rgba -s " + _width + "x" + _height
                        + " -r " + _fps.ToString("G6", Inv) + " -i - -vf vflip -c:v libx264 -preset " + preset
                        + " -crf " + _crf + " -pix_fmt yuv420p \"" + _output + "\"");
                    psi.UseShellExecute = false;
                    psi.RedirectStandardInput = true;
                    psi.RedirectStandardError = true;
                    psi.RedirectStandardOutput = false;
                    psi.CreateNoWindow = true;
                    _ffmpegLast = "";
                    _ffmpeg = Process.Start(psi);
                    _ffmpeg.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
                    {
                        if (!string.IsNullOrEmpty(e.Data))
                        {
                            _ffmpegLast = e.Data;
                        }
                    };
                    _ffmpeg.BeginErrorReadLine();
                    Stream pipe = _ffmpeg.StandardInput.BaseStream;
                    _ffmpegIn = pipe;
                    sink = delegate(FrameQueue.Frame f) { pipe.Write(f.Data, 0, f.Length); };
                }
                else
                {
                    _output = dir;
                    _tex = new Texture2D(_width, _height, TextureFormat.RGB24, false);
                    sink = delegate(FrameQueue.Frame f)
                    {
                        File.WriteAllBytes(Path.Combine(dir, "frame_" + f.Number.ToString("D6", Inv) + ".jpg"), f.Data);
                    };
                }
                _rt = new RenderTexture(_width, _height, 24, RenderTextureFormat.ARGB32);
                _queue = new FrameQueue(_index, sink, MaxQueued, MaxQueued + MaxInFlight, _width * _height * 4);
                _queue.Start();
            }
            catch (Exception)
            {
                AbortStart();
                throw;
            }
            _dropped = _skipped = _inFlight = 0;
            _captureMs = 0;
            _stopReason = _lastError = "";
            _stopRequest = null;
            _startedReal = Time.realtimeSinceStartup;
            _nextDue = _startedReal;
            _seenUt = double.NaN;
            _utChangedReal = _startedReal;
            _active = true;
            BridgeLog.Info("Recording " + _width + "x" + _height + " @" + _fps + " fps into " + _output);
            return Snapshot();
        }

        /// <summary>Undoes a Start that failed part-way: no ffmpeg left waiting on its input, nothing left open.</summary>
        private static void AbortStart()
        {
            if (_ffmpeg != null)
            {
                try
                {
                    if (!_ffmpeg.HasExited)
                    {
                        _ffmpeg.Kill();
                    }
                }
                catch (Exception)
                {
                }
                _ffmpeg.Dispose();
                _ffmpeg = null;
                _ffmpegIn = null;
            }
            ReleaseResources();
        }

        private static JObj Stop(BridgeRequest req)
        {
            if (!_active && (_queue == null || !_queue.WriterAlive))
            {
                JObj idle = Snapshot();
                idle["note"] = "not recording";
                return idle;
            }
            StopNow("requested"); // also waits again for a writer that an earlier stop left flushing
            return Snapshot();
        }

        private static void StopNow(string reason)
        {
            bool wasActive = _active;
            if (_video && _active)
            {
                try
                {
                    AsyncGPUReadback.WaitAllRequests(); // the last frames' callbacks run now
                }
                catch (Exception ex)
                {
                    _lastError = "waiting for readbacks: " + ex.Message;
                }
            }
            _active = false;
            _stopRequest = null;
            if (wasActive)
            {
                _stopReason = reason;
            }
            if (_queue != null && !_queue.Finish(30000))
            {
                // Keep the queue: Start refuses while its writer runs, and it never sees a later recording.
                _lastError = "the writer did not finish within 30 s; POST /record/stop again to wait for it";
            }
            if (_ffmpeg != null)
            {
                try
                {
                    _ffmpegIn.Close(); // end of input: ffmpeg finishes the file
                    if (!_ffmpeg.WaitForExit(60000))
                    {
                        _lastError = "ffmpeg did not finish within 60 s";
                        _ffmpeg.Kill();
                    }
                    else if (_ffmpeg.ExitCode != 0)
                    {
                        _lastError = "ffmpeg exit " + _ffmpeg.ExitCode + ": " + _ffmpegLast;
                    }
                }
                catch (Exception ex)
                {
                    _lastError = "closing ffmpeg: " + ex.Message;
                }
                _ffmpeg.Dispose();
                _ffmpeg = null;
                _ffmpegIn = null;
            }
            ReleaseResources();
            BridgeLog.Info("Recording stopped (" + reason + "): " + Frames() + " frames in " + _output);
        }

        private static void ReleaseResources()
        {
            CloseQuietly(ref _index);
            CloseQuietly(ref _marks);
            if (_rt != null)
            {
                _rt.Release();
                UnityEngine.Object.Destroy(_rt);
                _rt = null;
            }
            if (_tex != null)
            {
                UnityEngine.Object.Destroy(_tex);
                _tex = null;
            }
        }

        private static int Frames()
        {
            return _queue != null ? _queue.Frames : 0;
        }

        private static void CloseQuietly(ref StreamWriter w)
        {
            if (w == null)
            {
                return;
            }
            try
            {
                w.Flush();
                w.Dispose();
            }
            catch (Exception)
            {
            }
            w = null;
        }

        private static JObj Status(BridgeRequest req)
        {
            if (_active && _index != null)
            {
                _index.Flush();
            }
            return Snapshot();
        }

        private static JObj Mark(BridgeRequest req)
        {
            string label = req.RequireStr("label");
            string detail = req.Str("detail", "");
            JObj d = new JObj();
            d["recording"] = _active;
            if (!_active || _marks == null)
            {
                return d;
            }
            if (_video && _inFlight > 0)
            {
                // A captured frame is numbered only when its readback completes. Complete those first
                // (their callbacks run inside this call), so the mark names the last frame captured at or
                // before it. Nothing is in flight while the game is paused.
                try
                {
                    AsyncGPUReadback.WaitAllRequests();
                }
                catch (Exception ex)
                {
                    _lastError = "waiting for readbacks: " + ex.Message;
                }
            }
            int frame = Frames();
            float realS = Time.realtimeSinceStartup - _startedReal;
            _marks.WriteLine(frame.ToString(Inv) + "," + realS.ToString("F3", Inv) + "," + Util.SafeUt().ToString("F3", Inv)
                + "," + Csv(label) + "," + Csv(detail));
            d["frame"] = frame;
            return d;
        }

        private static JObj Snapshot()
        {
            JObj d = new JObj();
            d["recording"] = _active;
            d["mode"] = _video ? "video" : "jpeg";
            d["dir"] = _dir;
            d["output"] = _output;
            d["width"] = _width;
            d["height"] = _height;
            d["fps"] = _fps;
            d["crf"] = _crf;
            d["quality"] = _quality;
            d["includeUi"] = _includeUi;
            d["skipPaused"] = _skipPaused;
            FrameQueue q = _queue;
            int frames = Frames();
            d["frames"] = frames;
            d["written"] = q != null ? q.Written : 0;
            d["failedWrites"] = q != null ? q.Failed : 0; // frames with a frames.csv row that are missing from the output
            d["flushing"] = !_active && q != null && q.WriterAlive;
            d["megabytes"] = Math.Round((q != null ? q.Bytes : 0L) / 1048576.0, 1);
            d["dropped"] = _dropped;
            d["skipped"] = _skipped;
            d["avgCaptureMs"] = frames > 0 ? Math.Round(_captureMs / frames, 1) : 0.0;
            d["seconds"] = _active ? Math.Round(Time.realtimeSinceStartup - _startedReal, 1) : 0.0;
            d["stopReason"] = _stopReason;
            d["lastError"] = _lastError.Length > 0 || q == null ? _lastError : q.LastError;
            return d;
        }

        /// <summary>Stop cleanly when KSP shuts down so the last frames, the index and the video reach disk.</summary>
        internal static void Shutdown()
        {
            if (_active)
            {
                StopNow("KSP shutting down");
            }
        }
    }
}
