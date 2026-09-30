using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;

namespace KspAutomationBridge
{
    // This file holds the transport layer: logging hooks, the request/response model, the router and
    // the raw TCP HTTP/1.1 server. It references no Unity or KSP types so it can be tested outside KSP.

    internal delegate Dictionary<string, object> Handler(BridgeRequest request);

    /// <summary>Log sinks (wired to UnityEngine.Debug by the addon) plus the last unexpected error.</summary>
    internal static class BridgeLog
    {
        public static Action<string> InfoSink;
        public static Action<string> WarnSink;
        public static Action<string> ErrorSink;

        private static readonly object Gate = new object();
        private static string _lastError = "";
        private static DateTime _lastErrorUtc;

        public static void Info(string message)
        {
            Emit(InfoSink, message);
        }

        public static void Warn(string message)
        {
            Emit(WarnSink, message);
        }

        public static void RecordError(string summary, Exception ex)
        {
            lock (Gate)
            {
                _lastError = summary;
                _lastErrorUtc = DateTime.UtcNow;
            }
            Emit(ErrorSink, summary + (ex != null ? "\n" + ex : ""));
        }

        public static string LastError
        {
            get
            {
                lock (Gate)
                {
                    return _lastError;
                }
            }
        }

        public static string LastErrorUtc
        {
            get
            {
                lock (Gate)
                {
                    return _lastError.Length == 0 ? null : _lastErrorUtc.ToString("o", CultureInfo.InvariantCulture);
                }
            }
        }

        private static void Emit(Action<string> sink, string message)
        {
            if (sink == null)
            {
                return;
            }
            try
            {
                sink("[KspAutomationBridge] " + message);
            }
            catch (Exception)
            {
                // Logging must never take a request down.
            }
        }
    }

    /// <summary>An expected failure: becomes {"ok":false,"error":...,"hint":...} with the given HTTP status.</summary>
    internal sealed class BridgeException : Exception
    {
        public readonly int Status;
        public readonly string Hint;

        public BridgeException(int status, string message, string hint)
            : base(message)
        {
            Status = status;
            Hint = hint;
        }

        public BridgeException(int status, string message)
            : this(status, message, null)
        {
        }
    }

    internal sealed class BridgeResult
    {
        public bool Ok;
        public int Status;
        public string Error;
        public string Hint;
        public Dictionary<string, object> Data;
        public long JobId;

        public static BridgeResult Success(Dictionary<string, object> data)
        {
            BridgeResult r = new BridgeResult();
            r.Ok = true;
            r.Status = 200;
            r.Data = data ?? new Dictionary<string, object>();
            return r;
        }

        public static BridgeResult Failure(int status, string error, string hint)
        {
            BridgeResult r = new BridgeResult();
            r.Ok = false;
            r.Status = status;
            r.Error = error;
            r.Hint = hint;
            r.Data = new Dictionary<string, object>();
            return r;
        }

        public static BridgeResult FromException(Exception ex)
        {
            while (ex is TargetInvocationException && ex.InnerException != null)
            {
                ex = ex.InnerException;
            }
            BridgeException be = ex as BridgeException;
            if (be != null)
            {
                return Failure(be.Status, be.Message, be.Hint);
            }
            if (ex is ArgumentException || ex is FormatException)
            {
                return Failure(400, ex.Message, "See docs/BRIDGE_API.md for this endpoint's parameters.");
            }
            string summary = ex.GetType().Name + ": " + ex.Message;
            BridgeLog.RecordError(summary, ex);
            return Failure(500, summary, "Unexpected plugin error; KSP.log has the stack trace.");
        }

        public string ToJson()
        {
            Dictionary<string, object> envelope = new Dictionary<string, object>();
            envelope["ok"] = Ok;
            if (!Ok)
            {
                envelope["error"] = Error ?? "unknown error";
                if (!string.IsNullOrEmpty(Hint))
                {
                    envelope["hint"] = Hint;
                }
            }
            if (Data != null)
            {
                foreach (KeyValuePair<string, object> kv in Data)
                {
                    if (kv.Key != "ok" && kv.Key != "error" && kv.Key != "hint" && kv.Key != "jobId")
                    {
                        envelope[kv.Key] = kv.Value;
                    }
                }
            }
            if (JobId > 0)
            {
                envelope["jobId"] = JobId;
            }
            return Json.Serialize(envelope);
        }
    }

    /// <summary>
    /// A parsed request. Parameters come from the query string and the JSON body (body wins). Every
    /// typed getter accepts native JSON values and their string forms, because the Python client
    /// sends every value as a string. An empty string counts as "not given".
    /// </summary>
    internal sealed class BridgeRequest
    {
        public readonly string Method;
        public readonly string Path;
        public readonly Dictionary<string, object> Params;

        /// <summary>True once the HTTP client has closed its connection (null outside the server).</summary>
        public Func<bool> ClientGone;

        public BridgeRequest(string method, string path, Dictionary<string, object> parameters)
        {
            Method = method;
            Path = path;
            Params = parameters ?? new Dictionary<string, object>(StringComparer.Ordinal);
        }

        public bool Has(string key)
        {
            object value;
            return Params.TryGetValue(key, out value) && !IsAbsent(value);
        }

        public string Str(string key)
        {
            object value = Raw(key);
            if (value == null)
            {
                return null;
            }
            if (value is string)
            {
                return (string)value;
            }
            if (value is bool)
            {
                return (bool)value ? "true" : "false";
            }
            if (value is double)
            {
                return ((double)value).ToString("R", CultureInfo.InvariantCulture);
            }
            throw new BridgeException(400, "Parameter '" + key + "' must be a string, not a JSON " + (value is IList ? "array" : "object") + ".");
        }

        public string Str(string key, string fallback)
        {
            string s = Str(key);
            return s ?? fallback;
        }

        public string RequireStr(string key)
        {
            string s = Str(key);
            if (s == null)
            {
                throw Missing(key);
            }
            return s;
        }

        /// <summary>
        /// A finite number. "NaN" and "Infinity" parse as doubles but are refused: every numeric
        /// parameter is a physical quantity, and a NaN handed to MechJeb or the EVA walker corrupts
        /// the vessel state instead of failing.
        /// </summary>
        public double? Num(string key)
        {
            object value = Raw(key);
            if (value == null)
            {
                return null;
            }
            double d;
            if (value is double)
            {
                d = (double)value;
            }
            else
            {
                string s = value as string;
                if (s == null || !double.TryParse(s.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out d))
                {
                    throw Invalid(key, "a number", value);
                }
            }
            if (double.IsNaN(d) || double.IsInfinity(d))
            {
                throw Invalid(key, "a finite number", value);
            }
            return d;
        }

        public double RequireNum(string key)
        {
            double? d = Num(key);
            if (!d.HasValue)
            {
                throw Missing(key);
            }
            return d.Value;
        }

        public bool? Bool(string key)
        {
            object value = Raw(key);
            if (value == null)
            {
                return null;
            }
            if (value is bool)
            {
                return (bool)value;
            }
            if (value is double)
            {
                double d = (double)value;
                if (d == 0.0 || d == 1.0)
                {
                    return d == 1.0;
                }
            }
            string s = value as string;
            if (s != null)
            {
                switch (s.Trim().ToLowerInvariant())
                {
                    case "true": case "1": case "yes": case "on": return true;
                    case "false": case "0": case "no": case "off": return false;
                }
            }
            throw Invalid(key, "a boolean (true/false)", value);
        }

        public bool RequireBool(string key)
        {
            bool? b = Bool(key);
            if (!b.HasValue)
            {
                throw Missing(key);
            }
            return b.Value;
        }

        public bool Bool(string key, bool fallback)
        {
            bool? b = Bool(key);
            return b.HasValue ? b.Value : fallback;
        }

        public int? Int(string key)
        {
            double? d = Num(key);
            if (!d.HasValue)
            {
                return null;
            }
            if (d.Value != Math.Floor(d.Value) || d.Value < int.MinValue || d.Value > int.MaxValue)
            {
                throw Invalid(key, "an integer", Raw(key));
            }
            return (int)d.Value;
        }

        public int RequireInt(string key)
        {
            int? i = Int(key);
            if (!i.HasValue)
            {
                throw Missing(key);
            }
            return i.Value;
        }

        /// <summary>Unsigned 32-bit ids (KSP persistentId / flightID).</summary>
        public uint? UInt(string key)
        {
            object value = Raw(key);
            if (value == null)
            {
                return null;
            }
            uint u;
            string s = value as string;
            if (s != null && uint.TryParse(s.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out u))
            {
                return u;
            }
            if (value is double)
            {
                double d = (double)value;
                if (d >= 0 && d <= uint.MaxValue && d == Math.Floor(d))
                {
                    return (uint)d;
                }
            }
            throw Invalid(key, "an unsigned 32-bit integer id", value);
        }

        /// <summary>A lower-cased choice validated against the allowed values.</summary>
        public string Choice(string key, string[] allowed, string fallback)
        {
            string s = Str(key);
            if (s == null)
            {
                if (fallback == null)
                {
                    throw new BridgeException(400, "Missing required parameter '" + key + "' (one of: " + string.Join(", ", allowed) + ").");
                }
                return fallback;
            }
            string lower = s.Trim().ToLowerInvariant();
            if (Array.IndexOf(allowed, lower) < 0)
            {
                throw new BridgeException(400, "Parameter '" + key + "' must be one of: " + string.Join(", ", allowed) + " (got '" + s + "').");
            }
            return lower;
        }

        /// <summary>A list of strings from a JSON array or a comma-separated string.</summary>
        public List<string> StrList(string key)
        {
            object value = Raw(key);
            if (value == null)
            {
                return null;
            }
            List<string> result = new List<string>();
            IList list = value as IList;
            if (list != null)
            {
                foreach (object item in list)
                {
                    if (item != null && Convert.ToString(item, CultureInfo.InvariantCulture).Trim().Length > 0)
                    {
                        result.Add(Convert.ToString(item, CultureInfo.InvariantCulture).Trim());
                    }
                }
                return result;
            }
            string s = value as string;
            if (s == null)
            {
                throw Invalid(key, "a list (JSON array or comma-separated string)", value);
            }
            // Also tolerates a stringified list such as "['a', 'b']" (what str(list) gives in Python).
            s = s.Trim();
            if (s.StartsWith("[", StringComparison.Ordinal) && s.EndsWith("]", StringComparison.Ordinal))
            {
                s = s.Substring(1, s.Length - 2);
            }
            foreach (string part in s.Split(','))
            {
                string item = part.Trim().Trim('\'', '"').Trim();
                if (item.Length > 0)
                {
                    result.Add(item);
                }
            }
            return result;
        }

        private object Raw(string key)
        {
            object value;
            if (!Params.TryGetValue(key, out value) || IsAbsent(value))
            {
                return null;
            }
            return value;
        }

        private static bool IsAbsent(object value)
        {
            string s = value as string;
            return value == null || (s != null && s.Trim().Length == 0);
        }

        private static BridgeException Missing(string key)
        {
            return new BridgeException(400, "Missing required parameter '" + key + "'.",
                "See docs/BRIDGE_API.md for this endpoint's parameters.");
        }

        private static BridgeException Invalid(string key, string expected, object got)
        {
            string shown = got is string ? "'" + got + "'" : Json.Serialize(got);
            return new BridgeException(400, "Parameter '" + key + "' must be " + expected + " (got " + shown + ").");
        }
    }

    internal sealed class Route
    {
        public string Method;
        public string Path;
        public bool MainThread;
        public int TimeoutMs;
        public Handler Handler;
        public string Summary;
    }

    /// <summary>Maps "METHOD /path" to handlers; main-thread routes run through the job queue.</summary>
    internal sealed class Router
    {
        private readonly Dictionary<string, Route> _routes = new Dictionary<string, Route>(StringComparer.Ordinal);
        private readonly List<Route> _ordered = new List<Route>();
        private readonly MainThreadQueue _jobs;

        public Router(MainThreadQueue jobs)
        {
            _jobs = jobs;
        }

        public MainThreadQueue Jobs
        {
            get { return _jobs; }
        }

        /// <summary>A route whose handler touches KSP/Unity state and therefore runs on the main thread.</summary>
        public void Main(string method, string path, int timeoutMs, Handler handler, string summary)
        {
            Add(method, path, true, timeoutMs, handler, summary);
        }

        /// <summary>A route whose handler is thread-safe and runs on the HTTP thread.</summary>
        public void Inline(string method, string path, Handler handler, string summary)
        {
            Add(method, path, false, 0, handler, summary);
        }

        private void Add(string method, string path, bool mainThread, int timeoutMs, Handler handler, string summary)
        {
            Route route = new Route();
            route.Method = method;
            route.Path = path;
            route.MainThread = mainThread;
            route.TimeoutMs = timeoutMs;
            route.Handler = handler;
            route.Summary = summary;
            _routes[method + " " + path] = route;
            _ordered.Add(route);
        }

        /// <summary>GET /routes (self-description) and GET /job (outcome of a queued main-thread job).</summary>
        public void RegisterBuiltins()
        {
            Inline("GET", "/routes", delegate(BridgeRequest req)
            {
                List<object> list = new List<object>();
                foreach (Route r in _ordered)
                {
                    Dictionary<string, object> item = new Dictionary<string, object>();
                    item["method"] = r.Method;
                    item["path"] = r.Path;
                    item["mainThread"] = r.MainThread;
                    item["timeout_s"] = r.MainThread ? (object)(r.TimeoutMs / 1000.0) : null;
                    item["summary"] = r.Summary;
                    list.Add(item);
                }
                Dictionary<string, object> data = new Dictionary<string, object>();
                data["count"] = list.Count;
                data["routes"] = list;
                return data;
            }, "List every endpoint with its method, thread and timeout.");

            Inline("GET", "/job", delegate(BridgeRequest req)
            {
                long id = (long)req.RequireNum("id");
                MainThreadJob job = _jobs.Find(id);
                if (job == null)
                {
                    throw new BridgeException(404, "No job " + id + " in the recent history (the last 128 jobs are kept).");
                }
                Dictionary<string, object> data = new Dictionary<string, object>();
                data["id"] = job.Id;
                data["name"] = job.Name;
                data["state"] = job.State;
                data["enqueuedUtc"] = job.EnqueuedUtc.ToString("o", CultureInfo.InvariantCulture);
                BridgeResult result = job.Result;
                if (result != null)
                {
                    Dictionary<string, object> outcome = new Dictionary<string, object>();
                    outcome["ok"] = result.Ok;
                    outcome["status"] = result.Status;
                    outcome["error"] = result.Error;
                    outcome["data"] = result.Data;
                    data["result"] = outcome;
                }
                return data;
            }, "Look up a recent main-thread job by id (e.g. after a 504).");
        }

        public BridgeResult Dispatch(BridgeRequest request)
        {
            Route route;
            if (!_routes.TryGetValue(request.Method + " " + request.Path, out route))
            {
                List<string> allowed = new List<string>();
                foreach (Route r in _ordered)
                {
                    if (r.Path == request.Path)
                    {
                        allowed.Add(r.Method);
                    }
                }
                if (allowed.Count > 0)
                {
                    return BridgeResult.Failure(405, "Method " + request.Method + " is not allowed on " + request.Path
                        + "; use " + string.Join(" or ", allowed.ToArray()) + ".", null);
                }
                return BridgeResult.Failure(404, "Unknown route: " + request.Method + " " + request.Path,
                    "GET /routes lists every endpoint.");
            }
            Handler handler = route.Handler;
            if (route.MainThread)
            {
                return _jobs.Run(route.Method + " " + route.Path, delegate { return Execute(handler, request); }, route.TimeoutMs,
                    request.ClientGone);
            }
            return Execute(handler, request);
        }

        public static BridgeResult Execute(Handler handler, BridgeRequest request)
        {
            try
            {
                return BridgeResult.Success(handler(request));
            }
            catch (Exception ex)
            {
                return BridgeResult.FromException(ex);
            }
        }
    }

    /// <summary>Raised while reading a request that cannot be served; becomes an error response.</summary>
    internal sealed class HttpError : Exception
    {
        public readonly int Status;

        public HttpError(int status, string message)
            : base(message)
        {
            Status = status;
        }
    }

    /// <summary>
    /// Minimal HTTP/1.1 server on a raw TcpListener (HttpListener is unreliable under Unity's Mono).
    /// One background thread per connection, Connection: close, bodies read as exactly
    /// Content-Length bytes and decoded as UTF-8.
    /// </summary>
    internal sealed class HttpServer
    {
        public const int MaxHeaderBytes = 32 * 1024;
        public const int MaxBodyBytes = 8 * 1024 * 1024;
        public const int MaxConnections = 32;
        public const int SocketTimeoutMs = 15000;
        public const int LingerMs = 2000;

        private static readonly byte[] HeaderTerminator = { 13, 10, 13, 10 };

        private readonly Router _router;
        private TcpListener _listener;
        private Thread _acceptThread;
        private volatile bool _running;
        private int _active;

        public HttpServer(Router router)
        {
            _router = router;
        }

        public bool Running
        {
            get { return _running; }
        }

        public int Port { get; private set; }

        public void Start(IPAddress address, int port)
        {
            _listener = new TcpListener(address, port);
            _listener.Start();
            Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
            _running = true;
            _acceptThread = new Thread(AcceptLoop);
            _acceptThread.IsBackground = true;
            _acceptThread.Name = "KspAutomationBridge accept";
            _acceptThread.Start();
        }

        public void Stop()
        {
            _running = false;
            try
            {
                if (_listener != null)
                {
                    _listener.Stop();
                }
            }
            catch (Exception)
            {
                // Shutdown races are harmless.
            }
        }

        private void AcceptLoop()
        {
            while (_running)
            {
                TcpClient client;
                try
                {
                    client = _listener.AcceptTcpClient();
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (SocketException)
                {
                    if (!_running)
                    {
                        return;
                    }
                    continue;
                }
                catch (Exception ex)
                {
                    BridgeLog.RecordError("Listener error", ex);
                    continue;
                }

                if (Interlocked.Increment(ref _active) > MaxConnections)
                {
                    Interlocked.Decrement(ref _active);
                    RejectBusy(client);
                    continue;
                }
                Thread worker = new Thread(Serve);
                worker.IsBackground = true;
                worker.Name = "KspAutomationBridge request";
                worker.Start(client);
            }
        }

        private void Serve(object state)
        {
            TcpClient client = (TcpClient)state;
            try
            {
                client.ReceiveTimeout = SocketTimeoutMs;
                client.SendTimeout = SocketTimeoutMs;
                client.NoDelay = true;
                NetworkStream stream = client.GetStream();
                BridgeResult result;
                try
                {
                    BridgeRequest request = ReadRequest(stream);
                    if (request == null)
                    {
                        return; // the client connected and closed without sending anything
                    }
                    request.ClientGone = delegate { return PeerClosed(client); };
                    result = _router.Dispatch(request);
                }
                catch (HttpError e)
                {
                    result = BridgeResult.Failure(e.Status, e.Message, null);
                }
                catch (FormatException e)
                {
                    result = BridgeResult.Failure(400, e.Message, "The request body must be a JSON object.");
                }
                catch (IOException)
                {
                    throw; // the client went away mid-request: nobody to answer
                }
                catch (Exception e)
                {
                    result = BridgeResult.FromException(e);
                }
                WriteResponse(stream, result);
            }
            catch (IOException)
            {
                // Client went away or timed out; nothing to answer.
            }
            catch (SocketException)
            {
            }
            catch (ObjectDisposedException)
            {
            }
            catch (Exception ex)
            {
                BridgeLog.RecordError("HTTP worker crashed", ex);
            }
            finally
            {
                LingeringClose(client, LingerMs);
                try
                {
                    client.Close();
                }
                catch (Exception)
                {
                }
                Interlocked.Decrement(ref _active);
            }
        }

        /// <summary>
        /// Ends the response with a FIN and swallows request bytes the server never read before closing.
        /// A request refused before its body was read (403, 411, 413, a malformed Content-Length) leaves
        /// bytes in the receive buffer, and closing a socket with unread data sends a TCP RST that can
        /// destroy the response before the client reads it (the client then sees "connection reset"
        /// instead of the error). Bounded by lingerMs; the client normally closes at once.
        /// </summary>
        internal static void LingeringClose(TcpClient client, int lingerMs)
        {
            try
            {
                Socket s = client.Client;
                if (s == null || !s.Connected)
                {
                    return;
                }
                s.Shutdown(SocketShutdown.Send);
                byte[] sink = new byte[16384];
                System.Diagnostics.Stopwatch watch = System.Diagnostics.Stopwatch.StartNew();
                while (true)
                {
                    int remaining = lingerMs - (int)watch.ElapsedMilliseconds;
                    if (remaining <= 0 || !s.Poll(remaining * 1000, SelectMode.SelectRead))
                    {
                        return; // the client keeps its end open: give up, the response is already out
                    }
                    if (s.Receive(sink) <= 0)
                    {
                        return; // the client closed: a clean close now
                    }
                }
            }
            catch (Exception)
            {
                // Already reset or closed by the client: nothing left to protect.
            }
        }

        /// <summary>
        /// Whether the peer closed its end (a client that timed out and hung up). A readable socket
        /// with no bytes pending means FIN or RST was received.
        /// </summary>
        internal static bool PeerClosed(TcpClient client)
        {
            try
            {
                Socket s = client.Client;
                return s == null || (s.Poll(0, SelectMode.SelectRead) && s.Available == 0);
            }
            catch (Exception)
            {
                return true;
            }
        }

        /// <summary>
        /// Refuses requests a web browser could send on behalf of a web page: any page can POST to
        /// 127.0.0.1 (cross-site "simple" requests need no CORS preflight), and DNS rebinding lets a
        /// page read GET responses under its own Host name. Local tools (astra, curl) send none of
        /// these headers; a URL typed into the address bar (Sec-Fetch-Site: none) still works.
        /// </summary>
        internal static void CheckNotCrossSite(Dictionary<string, string> headers)
        {
            string value;
            if (headers.TryGetValue("Origin", out value) && value.Trim().Length > 0)
            {
                throw new HttpError(403, "Requests from web pages (Origin '" + value + "') are refused: the bridge controls the game.");
            }
            if (headers.TryGetValue("Sec-Fetch-Site", out value) && !value.Trim().Equals("none", StringComparison.OrdinalIgnoreCase))
            {
                throw new HttpError(403, "Cross-site browser requests (Sec-Fetch-Site '" + value + "') are refused.");
            }
            if (headers.TryGetValue("Host", out value) && value.Trim().Length > 0 && !IsLoopbackHost(value))
            {
                throw new HttpError(403, "Host '" + value + "' is not a loopback name (DNS rebinding guard); use 127.0.0.1 or localhost.");
            }
        }

        internal static bool IsLoopbackHost(string host)
        {
            string h = host.Trim().ToLowerInvariant();
            if (h.StartsWith("[", StringComparison.Ordinal))
            {
                int end = h.IndexOf(']');
                h = end > 0 ? h.Substring(1, end - 1) : h;
            }
            else
            {
                int colon = h.IndexOf(':');
                if (colon >= 0)
                {
                    h = h.Substring(0, colon);
                }
            }
            return h == "127.0.0.1" || h == "localhost" || h == "::1";
        }

        private static void RejectBusy(TcpClient client)
        {
            try
            {
                client.SendTimeout = 2000;
                WriteResponse(client.GetStream(), BridgeResult.Failure(503,
                    "Too many concurrent bridge connections (" + MaxConnections + ").", "Retry shortly."));
            }
            catch (Exception)
            {
            }
            finally
            {
                LingeringClose(client, 50); // short: this runs on the accept thread
                try
                {
                    client.Close();
                }
                catch (Exception)
                {
                }
            }
        }

        /// <summary>Reads one request; returns null when the peer closed before sending a byte.</summary>
        internal static BridgeRequest ReadRequest(Stream stream)
        {
            byte[] chunk = new byte[4096];
            MemoryStream head = new MemoryStream();
            int headerEnd = -1;
            while (headerEnd < 0)
            {
                int n = stream.Read(chunk, 0, chunk.Length);
                if (n <= 0)
                {
                    if (head.Length == 0)
                    {
                        return null;
                    }
                    throw new HttpError(400, "Connection closed in the middle of the request headers.");
                }
                head.Write(chunk, 0, n);
                headerEnd = IndexOf(head.GetBuffer(), (int)head.Length, HeaderTerminator);
                if (headerEnd < 0 && head.Length > MaxHeaderBytes)
                {
                    throw new HttpError(431, "Request headers exceed " + MaxHeaderBytes + " bytes.");
                }
            }

            byte[] raw = head.ToArray();
            string headerText = Encoding.UTF8.GetString(raw, 0, headerEnd);
            string[] lines = headerText.Split(new[] { "\r\n" }, StringSplitOptions.None);
            string[] requestLine = lines[0].Split(' ');
            if (requestLine.Length < 2)
            {
                throw new HttpError(400, "Malformed request line.");
            }
            string method = requestLine[0].Trim().ToUpperInvariant();
            string target = requestLine[1].Trim();

            Dictionary<string, string> headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 1; i < lines.Length; i++)
            {
                int colon = lines[i].IndexOf(':');
                if (colon > 0)
                {
                    headers[lines[i].Substring(0, colon).Trim()] = lines[i].Substring(colon + 1).Trim();
                }
            }
            CheckNotCrossSite(headers);

            string transferEncoding;
            if (headers.TryGetValue("Transfer-Encoding", out transferEncoding)
                && transferEncoding.IndexOf("chunked", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                throw new HttpError(411, "Chunked request bodies are not supported; send Content-Length.");
            }
            int contentLength = 0;
            string lengthText;
            if (headers.TryGetValue("Content-Length", out lengthText)
                && (!int.TryParse(lengthText, NumberStyles.Integer, CultureInfo.InvariantCulture, out contentLength) || contentLength < 0))
            {
                throw new HttpError(400, "Invalid Content-Length.");
            }
            if (contentLength > MaxBodyBytes)
            {
                throw new HttpError(413, "Request body exceeds " + MaxBodyBytes + " bytes.");
            }

            byte[] body = new byte[contentLength];
            int bodyStart = headerEnd + HeaderTerminator.Length;
            int have = Math.Min(raw.Length - bodyStart, contentLength);
            if (have > 0)
            {
                Buffer.BlockCopy(raw, bodyStart, body, 0, have);
            }
            string expect;
            if (have < contentLength && headers.TryGetValue("Expect", out expect)
                && expect.Equals("100-continue", StringComparison.OrdinalIgnoreCase))
            {
                byte[] cont = Encoding.ASCII.GetBytes("HTTP/1.1 100 Continue\r\n\r\n");
                stream.Write(cont, 0, cont.Length);
            }
            while (have < contentLength)
            {
                int n = stream.Read(body, have, contentLength - have);
                if (n <= 0)
                {
                    throw new HttpError(400, "Request body is shorter than its Content-Length.");
                }
                have += n;
            }

            string path = target;
            string query = "";
            int q = target.IndexOf('?');
            if (q >= 0)
            {
                path = target.Substring(0, q);
                query = target.Substring(q + 1);
            }
            path = Uri.UnescapeDataString(path);
            if (path.Length > 1 && path.EndsWith("/", StringComparison.Ordinal))
            {
                path = path.TrimEnd('/');
            }

            Dictionary<string, object> parameters = ParseQuery(query);
            string bodyText = Encoding.UTF8.GetString(body).TrimStart('\uFEFF');
            if (bodyText.Trim().Length > 0)
            {
                Dictionary<string, object> fields = Json.Parse(bodyText) as Dictionary<string, object>;
                if (fields == null)
                {
                    throw new HttpError(400, "The request body must be a JSON object.");
                }
                foreach (KeyValuePair<string, object> kv in fields)
                {
                    parameters[kv.Key] = kv.Value;
                }
            }
            return new BridgeRequest(method, path, parameters);
        }

        internal static Dictionary<string, object> ParseQuery(string query)
        {
            Dictionary<string, object> result = new Dictionary<string, object>(StringComparer.Ordinal);
            if (string.IsNullOrEmpty(query))
            {
                return result;
            }
            foreach (string pair in query.Split('&'))
            {
                if (pair.Length == 0)
                {
                    continue;
                }
                int eq = pair.IndexOf('=');
                string key = eq < 0 ? pair : pair.Substring(0, eq);
                string value = eq < 0 ? "" : pair.Substring(eq + 1);
                result[Uri.UnescapeDataString(key.Replace('+', ' '))] = Uri.UnescapeDataString(value.Replace('+', ' '));
            }
            return result;
        }

        internal static void WriteResponse(Stream stream, BridgeResult result)
        {
            byte[] body = Encoding.UTF8.GetBytes(result.ToJson());
            string head = "HTTP/1.1 " + result.Status + " " + ReasonPhrase(result.Status) + "\r\n"
                + "Content-Type: application/json; charset=utf-8\r\n"
                + "Content-Length: " + body.Length.ToString(CultureInfo.InvariantCulture) + "\r\n"
                + "Cache-Control: no-store\r\n"
                + "Connection: close\r\n\r\n";
            byte[] headBytes = Encoding.ASCII.GetBytes(head);
            stream.Write(headBytes, 0, headBytes.Length);
            stream.Write(body, 0, body.Length);
            stream.Flush();
        }

        private static string ReasonPhrase(int status)
        {
            switch (status)
            {
                case 200: return "OK";
                case 400: return "Bad Request";
                case 403: return "Forbidden";
                case 404: return "Not Found";
                case 405: return "Method Not Allowed";
                case 409: return "Conflict";
                case 411: return "Length Required";
                case 413: return "Payload Too Large";
                case 431: return "Request Header Fields Too Large";
                case 499: return "Client Closed Request";
                case 500: return "Internal Server Error";
                case 503: return "Service Unavailable";
                case 504: return "Gateway Timeout";
                default: return "Error";
            }
        }

        private static int IndexOf(byte[] haystack, int length, byte[] needle)
        {
            for (int i = 0; i + needle.Length <= length; i++)
            {
                int j = 0;
                while (j < needle.Length && haystack[i + j] == needle[j])
                {
                    j++;
                }
                if (j == needle.Length)
                {
                    return i;
                }
            }
            return -1;
        }
    }
}
