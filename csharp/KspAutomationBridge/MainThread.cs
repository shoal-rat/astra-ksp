using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;

namespace KspAutomationBridge
{
    /// <summary>
    /// One unit of work for the Unity main thread. A job that has not started when its caller gives
    /// up is abandoned and never runs, so a timed-out request can never execute later as a ghost.
    /// </summary>
    internal sealed class MainThreadJob
    {
        private const int Queued = 0;
        private const int Running = 1;
        private const int Finished = 2;
        private const int Abandoned = 3;

        private readonly object _gate = new object();
        private readonly Func<BridgeResult> _work;
        private int _state;
        private BridgeResult _result;

        public readonly long Id;
        public readonly string Name;
        public readonly DateTime EnqueuedUtc;
        public DateTime StartedUtc;
        public DateTime FinishedUtc;

        public MainThreadJob(long id, string name, Func<BridgeResult> work)
        {
            Id = id;
            Name = name;
            _work = work;
            EnqueuedUtc = DateTime.UtcNow;
        }

        public string State
        {
            get
            {
                switch (Thread.VolatileRead(ref _state))
                {
                    case Queued: return "queued";
                    case Running: return "running";
                    case Finished: return "finished";
                    default: return "abandoned";
                }
            }
        }

        public BridgeResult Result
        {
            get
            {
                lock (_gate)
                {
                    return _result;
                }
            }
        }

        public bool IsFinished
        {
            get { return Thread.VolatileRead(ref _state) == Finished; }
        }

        /// <summary>Claims the job for execution; false when the caller already abandoned it.</summary>
        internal bool TryBegin()
        {
            if (Interlocked.CompareExchange(ref _state, Running, Queued) != Queued)
            {
                return false;
            }
            StartedUtc = DateTime.UtcNow;
            return true;
        }

        /// <summary>Marks a still-queued job as abandoned; false when it already started.</summary>
        internal bool TryAbandon()
        {
            return Interlocked.CompareExchange(ref _state, Abandoned, Queued) == Queued;
        }

        internal void Execute()
        {
            BridgeResult result;
            try
            {
                result = _work();
            }
            catch (Exception ex)
            {
                result = BridgeResult.FromException(ex);
            }
            if (result == null)
            {
                result = BridgeResult.Failure(500, "Handler '" + Name + "' returned no result.", null);
            }
            result.JobId = Id;
            lock (_gate)
            {
                _result = result;
                FinishedUtc = DateTime.UtcNow;
                Thread.VolatileWrite(ref _state, Finished);
                Monitor.PulseAll(_gate);
            }
        }

        /// <summary>Blocks the calling (HTTP) thread until the job finished or the timeout elapsed.</summary>
        internal bool Wait(int timeoutMs)
        {
            Stopwatch watch = Stopwatch.StartNew();
            lock (_gate)
            {
                while (Thread.VolatileRead(ref _state) != Finished)
                {
                    int remaining = timeoutMs - (int)watch.ElapsedMilliseconds;
                    if (remaining <= 0)
                    {
                        return false;
                    }
                    Monitor.Wait(_gate, remaining);
                }
                return true;
            }
        }
    }

    /// <summary>
    /// Queue of jobs drained by the addon's Update() on the Unity main thread. Keeps a short history
    /// so a caller whose request timed out can still look the outcome up (GET /job?id=N).
    /// </summary>
    internal sealed class MainThreadQueue
    {
        private const int HistorySize = 128;
        private const int ClientCheckMs = 200;

        private readonly ConcurrentQueue<MainThreadJob> _queue = new ConcurrentQueue<MainThreadJob>();
        private readonly object _historyLock = new object();
        private readonly Dictionary<long, MainThreadJob> _history = new Dictionary<long, MainThreadJob>();
        private readonly Queue<long> _historyOrder = new Queue<long>();
        private long _nextId;
        private long _abandoned;

        public int Depth
        {
            get { return _queue.Count; }
        }

        public long AbandonedCount
        {
            get { return Interlocked.Read(ref _abandoned); }
        }

        /// <summary>
        /// Runs work on the main thread and waits for it (call from a non-main thread). The job is
        /// abandoned, and never runs, if it has not started when the timeout elapses or when
        /// clientGone reports that the HTTP client hung up (its own timeout was shorter).
        /// </summary>
        public BridgeResult Run(string name, Func<BridgeResult> work, int timeoutMs, Func<bool> clientGone)
        {
            MainThreadJob job = new MainThreadJob(Interlocked.Increment(ref _nextId), name, work);
            Remember(job);
            _queue.Enqueue(job);
            Stopwatch watch = Stopwatch.StartNew();
            while (true)
            {
                int remaining = timeoutMs - (int)watch.ElapsedMilliseconds;
                if (remaining <= 0)
                {
                    break;
                }
                if (job.Wait(Math.Min(remaining, ClientCheckMs)))
                {
                    return job.Result;
                }
                if (clientGone != null && clientGone() && job.TryAbandon())
                {
                    Interlocked.Increment(ref _abandoned);
                    BridgeResult gone = BridgeResult.Failure(499,
                        "The client disconnected before the main thread started '" + name + "'; the job was abandoned and will NOT run.",
                        "Give the client a timeout longer than the route's (GET /routes).");
                    gone.JobId = job.Id;
                    return gone;
                }
            }

            string seconds = (timeoutMs / 1000.0).ToString("0.#", System.Globalization.CultureInfo.InvariantCulture);
            BridgeResult timeout;
            if (job.TryAbandon())
            {
                Interlocked.Increment(ref _abandoned);
                timeout = BridgeResult.Failure(504,
                    "The Unity main thread did not start '" + name + "' within " + seconds
                    + " s (the game is probably loading a scene or frozen). The job was abandoned and will NOT run.",
                    "Check GET /ping (answers without the main thread) or GET /state, then retry.");
            }
            else if (job.IsFinished)
            {
                return job.Result; // finished between the last wait and the abandon attempt
            }
            else
            {
                timeout = BridgeResult.Failure(504,
                    "'" + name + "' started on the main thread but did not finish within " + seconds
                    + " s; it will still complete.",
                    "Poll GET /job?id=" + job.Id + " for its outcome before retrying.");
            }
            timeout.JobId = job.Id;
            return timeout;
        }

        /// <summary>Executes queued jobs; must be called on the Unity main thread.</summary>
        public void Drain(int budgetMs)
        {
            Stopwatch watch = null;
            MainThreadJob job;
            while (_queue.TryDequeue(out job))
            {
                if (!job.TryBegin())
                {
                    continue; // abandoned by its caller: skip, never run
                }
                if (watch == null)
                {
                    watch = Stopwatch.StartNew();
                }
                job.Execute();
                if (watch.ElapsedMilliseconds > budgetMs)
                {
                    break; // leave the rest for the next frame
                }
            }
        }

        public MainThreadJob Find(long id)
        {
            lock (_historyLock)
            {
                MainThreadJob job;
                return _history.TryGetValue(id, out job) ? job : null;
            }
        }

        private void Remember(MainThreadJob job)
        {
            lock (_historyLock)
            {
                _history[job.Id] = job;
                _historyOrder.Enqueue(job.Id);
                while (_historyOrder.Count > HistorySize)
                {
                    _history.Remove(_historyOrder.Dequeue());
                }
            }
        }
    }
}
