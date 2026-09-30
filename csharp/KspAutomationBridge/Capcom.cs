using System;
using System.Collections.Generic;
using System.Globalization;
using JObj = System.Collections.Generic.Dictionary<string, object>;

namespace KspAutomationBridge
{
    // The CAPCOM message store and its HTTP routes. Pure .NET (no Unity types): the in-game panel that
    // draws it lives in CapcomPanel.cs.

    internal sealed class CapcomMessage
    {
        public long Seq;
        public string From; // "ai" or "player"
        public string Level; // info | warn | alert
        public string Text;
        public DateTime Utc;
        public double Ut; // NaN when unknown (AI messages arrive on HTTP threads)

        public JObj ToJson()
        {
            JObj o = new JObj();
            o["seq"] = Seq;
            o["from"] = From;
            o["level"] = Level;
            o["text"] = Text;
            o["utc"] = Utc.ToString("o", CultureInfo.InvariantCulture);
            o["ut"] = Ut;
            return o;
        }
    }

    /// <summary>
    /// The CAPCOM thread: a thread-safe log of messages between the AI crew and the player. The AI posts
    /// over HTTP; the player types into the in-game panel. Nothing here needs the main thread except the
    /// screen notices, which are queued and shown by the panel's Update().
    /// </summary>
    internal static class CapcomLog
    {
        public const int Capacity = 1000;
        public const int MaxTextLength = 4000;

        private static readonly object Gate = new object();
        private static readonly List<CapcomMessage> Messages = new List<CapcomMessage>();
        private static readonly Queue<CapcomMessage> PendingNotices = new Queue<CapcomMessage>();
        private static long _seq;
        private static long _lastPlayerSeq;
        private static bool _openRequested;
        private static DateTime _lastInboxPollUtc = DateTime.MinValue;

        public static CapcomMessage Post(string from, string level, string text, double ut)
        {
            if (text.Length > MaxTextLength)
            {
                text = text.Substring(0, MaxTextLength) + "...";
            }
            lock (Gate)
            {
                CapcomMessage m = new CapcomMessage();
                m.Seq = ++_seq;
                m.From = from;
                m.Level = level;
                m.Text = text;
                m.Utc = DateTime.UtcNow;
                m.Ut = ut;
                Messages.Add(m);
                if (Messages.Count > Capacity)
                {
                    Messages.RemoveRange(0, Messages.Count - Capacity);
                }
                if (from == "player")
                {
                    _lastPlayerSeq = m.Seq;
                }
                else if (level != "info")
                {
                    PendingNotices.Enqueue(m);
                    if (level == "alert")
                    {
                        _openRequested = true;
                    }
                }
                return m;
            }
        }

        public static List<CapcomMessage> Tail(int limit)
        {
            lock (Gate)
            {
                int start = Math.Max(0, Messages.Count - limit);
                return Messages.GetRange(start, Messages.Count - start);
            }
        }

        public static List<CapcomMessage> PlayerSince(long since)
        {
            lock (Gate)
            {
                _lastInboxPollUtc = DateTime.UtcNow;
                List<CapcomMessage> result = new List<CapcomMessage>();
                foreach (CapcomMessage m in Messages)
                {
                    if (m.Seq > since && m.From == "player")
                    {
                        result.Add(m);
                    }
                }
                return result;
            }
        }

        public static long LastSeq
        {
            get
            {
                lock (Gate)
                {
                    return _seq;
                }
            }
        }

        public static JObj Summary()
        {
            lock (Gate)
            {
                JObj o = new JObj();
                o["lastSeq"] = _seq;
                o["lastPlayerSeq"] = _lastPlayerSeq;
                return o;
            }
        }

        /// <summary>Seconds since the AI last read the inbox, or -1 if it never did.</summary>
        public static double SecondsSinceInboxPoll()
        {
            lock (Gate)
            {
                return _lastInboxPollUtc == DateTime.MinValue ? -1 : (DateTime.UtcNow - _lastInboxPollUtc).TotalSeconds;
            }
        }

        public static bool TakeNotice(out CapcomMessage message)
        {
            lock (Gate)
            {
                message = PendingNotices.Count > 0 ? PendingNotices.Dequeue() : null;
                return message != null;
            }
        }

        public static bool TakeOpenRequest()
        {
            lock (Gate)
            {
                bool open = _openRequested;
                _openRequested = false;
                return open;
            }
        }

        public static void Clear()
        {
            lock (Gate)
            {
                Messages.Clear();
                PendingNotices.Clear();
            }
        }
    }

    internal static class CapcomRoutes
    {
        private static readonly string[] Levels = { "info", "warn", "alert" };

        public static void Register(Router r)
        {
            r.Inline("POST", "/capcom", Post, "Post an AI message to the in-game CAPCOM panel {text, level: info|warn|alert}.");
            r.Inline("GET", "/capcom", Tail, "The tail of the CAPCOM thread (both directions) {limit}.");
            r.Inline("GET", "/capcom/inbox", Inbox, "Player messages newer than {since} (a seq number).");
            r.Inline("POST", "/capcom/inbox", Inbox, "Same as GET /capcom/inbox with {since} in the body.");
        }

        private static JObj Post(BridgeRequest req)
        {
            string text = req.RequireStr("text").Trim();
            if (text.Length == 0)
            {
                throw new BridgeException(400, "text is empty.");
            }
            string level = req.Choice("level", Levels, "info");
            CapcomMessage m = CapcomLog.Post("ai", level, text, double.NaN);
            JObj d = new JObj();
            d["seq"] = m.Seq;
            d["level"] = level;
            d["shownOnScreen"] = level != "info";
            return d;
        }

        private static JObj Tail(BridgeRequest req)
        {
            int limit = Math.Max(1, Math.Min(CapcomLog.Capacity, req.Int("limit") ?? 50));
            List<object> list = new List<object>();
            foreach (CapcomMessage m in CapcomLog.Tail(limit))
            {
                list.Add(m.ToJson());
            }
            JObj d = new JObj();
            d["messages"] = list;
            d["lastSeq"] = CapcomLog.LastSeq;
            return d;
        }

        private static JObj Inbox(BridgeRequest req)
        {
            long since = (long)(req.Num("since") ?? 0);
            List<object> list = new List<object>();
            foreach (CapcomMessage m in CapcomLog.PlayerSince(since))
            {
                list.Add(m.ToJson());
            }
            JObj d = new JObj();
            d["messages"] = list;
            d["count"] = list.Count;
            d["lastSeq"] = CapcomLog.LastSeq;
            return d;
        }
    }
}
