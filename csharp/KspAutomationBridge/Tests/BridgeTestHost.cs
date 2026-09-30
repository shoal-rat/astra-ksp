// Offline test host for the bridge's transport layer (Json.cs, Http.cs, MainThread.cs, Capcom.cs) and
// the pure helpers (HarmonyReflect.cs, PausePlanner.cs, EvaMath.cs, FrameQueue.cs).
// It is NOT part of the plugin build (the build only compiles csharp/KspAutomationBridge/*.cs and
// Properties/*.cs). test_bridge_host.py compiles it with the same C# 5 csc and drives it over HTTP.
//
//   BridgeTestHost.exe serve   -> prints "PORT <n>" and serves until stdin closes
//   BridgeTestHost.exe json    -> reads JSON on stdin, writes Json.Serialize(Json.Parse(stdin)) or "ERROR <msg>"
//   BridgeTestHost.exe harmony [0Harmony.dll] -> patches a probe method via HarmonyReflect, prints "VALUE <n>" or "ERROR <msg>"
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text;
using System.Threading;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    internal static class BridgeTestHost
    {
        private static readonly MainThreadQueue Jobs = new MainThreadQueue();
        private static volatile int _freezeUntilTick;
        private static int _counter;

        public static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            string mode = args.Length > 0 ? args[0] : "serve";
            if (mode == "json")
            {
                return JsonRoundTrip();
            }
            if (mode == "harmony")
            {
                return HarmonyProbe(args.Length > 1 ? args[1] : null);
            }
            return Serve();
        }

        private static int HarmonyProbe(string harmonyPath)
        {
            if (harmonyPath != null)
            {
                Assembly.LoadFrom(harmonyPath);
            }
            string error = HarmonyReflect.Postfix("astra.test", typeof(Probe).GetMethod("Step"), typeof(ProbeHooks).GetMethod("StepPostfix"));
            if (error != null)
            {
                Console.Out.Write("ERROR " + error);
                return 3;
            }
            Probe probe = new Probe();
            probe.Step();
            Console.Out.Write("VALUE " + probe.Value);
            return 0;
        }

        private static int JsonRoundTrip()
        {
            string input = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false)).ReadToEnd();
            try
            {
                Console.Out.Write(Json.Serialize(Json.Parse(input)));
                return 0;
            }
            catch (FormatException ex)
            {
                Console.Out.Write("ERROR " + ex.Message);
                return 2;
            }
        }

        private static int Serve()
        {
            BridgeLog.InfoSink = delegate(string m) { Console.Error.WriteLine(m); };
            BridgeLog.WarnSink = BridgeLog.InfoSink;
            BridgeLog.ErrorSink = BridgeLog.InfoSink;
            Router r = new Router(Jobs);
            r.RegisterBuiltins();
            CapcomRoutes.Register(r);

            r.Inline("GET", "/echo", Echo, "echo params");
            r.Inline("POST", "/echo", Echo, "echo params");
            r.Main("POST", "/test/typed", 5000, Typed, "typed getters");
            r.Main("POST", "/test/counter", 500, delegate(BridgeRequest req)
            {
                JObj d = new JObj();
                d["counter"] = Interlocked.Increment(ref _counter);
                return d;
            }, "increments a counter on the main thread");
            r.Main("POST", "/test/counter-patient", 10000, delegate(BridgeRequest req)
            {
                JObj d = new JObj();
                d["counter"] = Interlocked.Increment(ref _counter);
                return d;
            }, "increments the counter; long route timeout");
            r.Inline("GET", "/test/counter-value", delegate(BridgeRequest req)
            {
                JObj d = new JObj();
                d["counter"] = Thread.VolatileRead(ref _counter);
                return d;
            }, "reads the counter");
            r.Inline("POST", "/test/freeze", delegate(BridgeRequest req)
            {
                _freezeUntilTick = Environment.TickCount + req.RequireInt("ms");
                return new JObj();
            }, "stalls the simulated main thread");
            r.Main("POST", "/test/slow", 500, delegate(BridgeRequest req)
            {
                Thread.Sleep(req.RequireInt("ms"));
                JObj d = new JObj();
                d["slept"] = true;
                return d;
            }, "blocks the main thread");
            r.Main("GET", "/test/throw", 5000, delegate(BridgeRequest req)
            {
                throw new InvalidOperationException("boom");
            }, "unexpected exception");
            r.Main("GET", "/test/bridge-error", 5000, delegate(BridgeRequest req)
            {
                throw new BridgeException(409, "wrong scene", "go to flight");
            }, "expected failure");
            r.Inline("GET", "/test/values", Values, "awkward values");
            r.Inline("POST", "/test/pause-plan", delegate(BridgeRequest req)
            {
                JObj d = new JObj();
                float timeScale = (float)req.RequireNum("timeScale");
                bool flightDriverPause = req.RequireBool("flightDriverPause");
                bool flightLoaded = PausePlanner.FlightLoaded(req.RequireBool("flightStarted"), req.RequireBool("flightDriverExists"),
                    req.RequireBool("sceneLoadRequested"));
                d["flightLoaded"] = flightLoaded;
                d["action"] = PausePlanner.Plan(req.RequireBool("want"), flightDriverPause, timeScale, req.RequireBool("menuOpen"), flightLoaded).ToString();
                d["isPaused"] = PausePlanner.IsPaused(req.RequireBool("inFlight"), flightDriverPause, timeScale);
                return d;
            }, "exposes PausePlanner (the decisions behind POST /pause)");
            r.Inline("POST", "/test/hop-command", delegate(BridgeRequest req)
            {
                JObj d = new JObj();
                double full = EvaMath.JetpackAccel(req.RequireNum("linPower"), req.RequireNum("thrustPercentage"), req.RequireNum("mass"));
                double[] horizontal = Vec(req, "horizontal");
                double[] gravity = Vec(req, "gravity");
                double[] cmd = EvaMath.HopCommand(horizontal, Vec(req, "up"), req.RequireNum("heightError"), Vec(req, "surfaceVelocity"), gravity, full);
                d["jetpackAccel"] = full;
                d["canLift"] = EvaMath.CanLift(full, EvaMath.Norm(gravity));
                d["canHop"] = EvaMath.CanHop(full, EvaMath.Norm(gravity), req.Num("height") ?? 0.0);
                d["released"] = EvaMath.Norm(horizontal) <= EvaMath.HopReleaseM;
                d["command"] = cmd;
                d["magnitude"] = EvaMath.Norm(cmd);
                return d;
            }, "exposes EvaMath (the jetpack hop controller)");
            r.Inline("POST", "/test/rails-gap", delegate(BridgeRequest req)
            {
                JObj d = new JObj();
                d["gap"] = EvaMath.RailsGap(req.Num("lastUt") ?? double.NaN, req.RequireNum("ut"));
                return d;
            }, "exposes EvaMath.RailsGap (walk stall and hop timers)");
            r.Inline("POST", "/test/frame-queue", FrameQueueScenario, "runs a FrameQueue scenario (the recorder's numbering, pool and writer)");
            r.Inline("POST", "/test/player", delegate(BridgeRequest req)
            {
                CapcomMessage m = CapcomLog.Post("player", "info", req.RequireStr("text"), 123.5);
                JObj d = new JObj();
                d["seq"] = m.Seq;
                return d;
            }, "simulates the player typing in the panel");

            HttpServer server = new HttpServer(r);
            server.Start(IPAddress.Loopback, 0);
            Thread pump = new Thread(Pump);
            pump.IsBackground = true;
            pump.Start();
            Console.Out.WriteLine("PORT " + server.Port);
            Console.Out.Flush();
            Console.In.ReadToEnd(); // run until the test closes stdin
            server.Stop();
            return 0;
        }

        /// <summary>Stands in for Unity's Update(): drains the queue ~60 times a second unless frozen.</summary>
        private static void Pump()
        {
            while (true)
            {
                if (Environment.TickCount - _freezeUntilTick >= 0)
                {
                    Jobs.Drain(100);
                }
                Thread.Sleep(15);
            }
        }

        private static JObj Echo(BridgeRequest req)
        {
            JObj types = new JObj();
            foreach (KeyValuePair<string, object> kv in req.Params)
            {
                types[kv.Key] = kv.Value == null ? "null" : kv.Value.GetType().Name;
            }
            JObj d = new JObj();
            d["method"] = req.Method;
            d["path"] = req.Path;
            d["params"] = req.Params;
            d["types"] = types;
            return d;
        }

        private static JObj Typed(BridgeRequest req)
        {
            JObj d = new JObj();
            d["num"] = req.Num("num");
            d["flag"] = req.Bool("flag");
            d["int"] = req.Int("int");
            d["id"] = req.UInt("id");
            d["str"] = req.Str("str");
            d["choice"] = req.Choice("choice", new[] { "alpha", "beta" }, "alpha");
            d["list"] = req.StrList("list");
            if (req.Has("need"))
            {
                d["need"] = req.RequireNum("need");
            }
            if (req.Bool("requireMissing", false))
            {
                req.RequireNum("absentParameter");
            }
            return d;
        }

        private static double[] Vec(BridgeRequest req, string key)
        {
            List<object> list = req.Params[key] as List<object>;
            return new[] { Convert.ToDouble(list[0]), Convert.ToDouble(list[1]), Convert.ToDouble(list[2]) };
        }

        private static List<object> Lines(StringWriter w)
        {
            List<object> lines = new List<object>();
            foreach (string line in w.ToString().Split(new[] { "\r\n", "\n" }, StringSplitOptions.RemoveEmptyEntries))
            {
                lines.Add(line);
            }
            return lines;
        }

        private static bool WaitFor(Func<bool> condition, int timeoutMs)
        {
            int until = Environment.TickCount + timeoutMs;
            while (!condition())
            {
                if (Environment.TickCount - until >= 0)
                {
                    return false;
                }
                Thread.Sleep(5);
            }
            return true;
        }

        /// <summary>FrameQueue scenarios; each returns what the Python test asserts.</summary>
        private static JObj FrameQueueScenario(BridgeRequest req)
        {
            string scenario = req.RequireStr("scenario");
            StringWriter index = new StringWriter();
            List<object> sunk = new List<object>();
            JObj d = new JObj();
            if (scenario == "order")
            {
                // Pooled and unpooled frames: row N and the Nth frame the sink sees are the same frame.
                FrameQueue q = new FrameQueue(index, delegate(FrameQueue.Frame f)
                {
                    lock (sunk)
                    {
                        sunk.Add(f.Number + ":" + f.Data[0] + ":" + f.Length);
                    }
                }, 24, 3, 4);
                q.Start();
                List<object> numbers = new List<object>();
                for (int i = 1; i <= 5; i++)
                {
                    bool pooled = i <= 3;
                    byte[] data = pooled ? q.Rent() : new byte[4];
                    data[0] = (byte)(10 + i);
                    numbers.Add(q.Accept(data, 4, pooled, "row" + i));
                }
                d["finished"] = q.Finish(5000);
                d["numbers"] = numbers;
                d["index"] = Lines(index);
                d["sunk"] = sunk;
                d["frames"] = q.Frames;
                d["written"] = q.Written;
                d["bytes"] = q.Bytes;
            }
            else if (scenario == "drain")
            {
                // Finish right after queueing: the writer still writes every queued frame before it exits.
                FrameQueue q = new FrameQueue(index, delegate(FrameQueue.Frame f)
                {
                    Thread.Sleep(20);
                    lock (sunk)
                    {
                        sunk.Add(f.Number);
                    }
                }, 24, 0, 1);
                q.Start();
                for (int i = 1; i <= 8; i++)
                {
                    q.Accept(new byte[1], 1, false, "r");
                }
                d["finished"] = q.Finish(5000);
                d["writerAlive"] = q.WriterAlive;
                d["written"] = q.Written;
                d["sunk"] = sunk;
            }
            else if (scenario == "cap")
            {
                FrameQueue pool = new FrameQueue(index, delegate(FrameQueue.Frame f) { }, 24, 3, 4);
                byte[] a = pool.Rent();
                byte[] b = pool.Rent();
                byte[] c = pool.Rent();
                d["threeRented"] = a != null && b != null && c != null;
                d["fourthRefused"] = pool.Rent() == null;
                pool.Return(b);
                d["returnedReused"] = pool.Rent() == b;
                pool.Return(new byte[1]); // another size: not pooled
                d["foreignDropped"] = pool.Rent() == null;
                pool.Return(a);
                pool.Finish(1000);
                d["closedRefuses"] = pool.Rent() == null;

                // The writer blocks on its first frame: two more wait, which is Full at maxQueued 2.
                ManualResetEvent entered = new ManualResetEvent(false);
                ManualResetEvent release = new ManualResetEvent(false);
                FrameQueue q = new FrameQueue(index, delegate(FrameQueue.Frame f)
                {
                    entered.Set();
                    release.WaitOne(5000);
                }, 2, 0, 1);
                q.Start();
                q.Accept(new byte[1], 1, false, "r");
                entered.WaitOne(5000);
                d["fullWithNoneWaiting"] = q.Full;
                q.Accept(new byte[1], 1, false, "r");
                q.Accept(new byte[1], 1, false, "r");
                d["fullAtTwoWaiting"] = q.Full;
                release.Set();
                d["finished"] = q.Finish(5000);
                d["fullAfter"] = q.Full;
                d["written"] = q.Written;
            }
            else if (scenario == "stale")
            {
                // A late frame of a finished recording (its readback completed after the stop).
                FrameQueue q = new FrameQueue(index, delegate(FrameQueue.Frame f)
                {
                    lock (sunk)
                    {
                        sunk.Add(f.Number);
                    }
                }, 24, 2, 4);
                q.Start();
                q.Accept(new byte[4], 4, false, "before");
                d["finished"] = q.Finish(5000);
                d["lateNumber"] = q.Accept(new byte[4], 4, false, "late");
                d["closed"] = q.Closed;
                d["index"] = Lines(index);
                d["sunk"] = sunk;
                d["frames"] = q.Frames;
            }
            else if (scenario == "sink-throws")
            {
                // Every write fails: each frame is counted, and its pooled buffer still goes back.
                FrameQueue q = new FrameQueue(index, delegate(FrameQueue.Frame f)
                {
                    throw new IOException("disk gone");
                }, 24, 2, 4);
                q.Start();
                byte[] a = q.Rent();
                byte[] b = q.Rent();
                d["capBefore"] = q.Rent() == null;
                q.Accept(a, 4, true, "r1");
                q.Accept(b, 4, true, "r2");
                d["bothFailed"] = WaitFor(delegate { return q.Failed == 2; }, 5000);
                byte[] c = q.Rent();
                byte[] e = q.Rent();
                d["recycled"] = c != null && e != null;
                d["capAfter"] = q.Rent() == null;
                d["written"] = q.Written;
                d["failed"] = q.Failed;
                d["lastError"] = q.LastError;
                d["index"] = Lines(index);
                d["finished"] = q.Finish(5000);
            }
            else
            {
                throw new BridgeException(400, "unknown scenario " + scenario);
            }
            return d;
        }

        private static JObj Values(BridgeRequest req)
        {
            JObj nested = new JObj();
            nested["list"] = new List<object> { 1, 2.5, "x", null, true };
            nested["floats"] = new[] { 0.1f, 1e-7f };
            JObj d = new JObj();
            d["nan"] = double.NaN;
            d["inf"] = double.PositiveInfinity;
            d["fnan"] = float.NaN;
            d["big"] = 4294967295u;
            d["long"] = 9007199254740993L;
            d["neg"] = -0.5;
            d["text"] = "line1\nline2\t\"quoted\" \\ \u0001 \u2028 \u9ed8\u8ba4";
            d["nested"] = nested;
            d["empty"] = new JObj();
            d["enum"] = DayOfWeek.Friday;
            return d;
        }
    }

    public sealed class Probe
    {
        public int Value;

        [MethodImpl(MethodImplOptions.NoInlining)]
        public void Step()
        {
            Value += 1;
        }
    }

    public static class ProbeHooks
    {
        public static void StepPostfix(Probe __instance)
        {
            __instance.Value += 100;
        }
    }
}
