using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Threading;

namespace KspAutomationBridge
{
    /// <summary>
    /// The recorder's queue, kept free of Unity types so it is tested offline (Tests/BridgeTestHost.cs).
    /// Accept numbers a frame, writes its frames.csv row and queues it; one writer thread hands the
    /// frames, in that order, to a sink (a JPEG file or the ffmpeg pipe). Frame N of the video is row N
    /// of frames.csv. One instance per recording: a writer that outlives its stop never sees a later
    /// recording's frames, and a late callback of a finished recording is refused (no row, no frame).
    /// Video frame buffers come from a pool capped at maxBuffers; the writer returns each one after the
    /// sink ran, also when the sink threw.
    /// </summary>
    internal sealed class FrameQueue
    {
        internal sealed class Frame
        {
            public int Number;
            public byte[] Data;
            public int Length;
            public bool Pooled;
        }

        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        private readonly object _gate = new object();
        private readonly Queue<Frame> _pending = new Queue<Frame>();
        private readonly Stack<byte[]> _pool = new Stack<byte[]>();
        private readonly TextWriter _index;
        private readonly Action<Frame> _sink;
        private readonly int _maxQueued;
        private readonly int _maxBuffers;
        private readonly int _bufferLength;
        private readonly Thread _writer;
        private volatile bool _closed;
        private int _allocated;
        private int _frames;
        private int _written;
        private int _failed;
        private long _bytes;
        private volatile string _lastError = "";

        /// <param name="index">frames.csv; Accept writes "number,row" (caller's thread).</param>
        /// <param name="sink">Writes one frame (writer thread).</param>
        /// <param name="maxQueued">Full is true at this many frames waiting for the writer.</param>
        /// <param name="maxBuffers">At most this many pooled buffers exist (Rent returns null beyond).</param>
        /// <param name="bufferLength">Size of a pooled buffer in bytes.</param>
        public FrameQueue(TextWriter index, Action<Frame> sink, int maxQueued, int maxBuffers, int bufferLength)
        {
            _index = index;
            _sink = sink;
            _maxQueued = maxQueued;
            _maxBuffers = maxBuffers;
            _bufferLength = bufferLength;
            _writer = new Thread(WriterLoop);
            _writer.IsBackground = true;
            _writer.Name = "ASTRA recorder";
        }

        public void Start()
        {
            _writer.Start();
        }

        /// <summary>Frames numbered so far (the last frame number).</summary>
        public int Frames
        {
            get { return Thread.VolatileRead(ref _frames); }
        }

        /// <summary>Frames the sink finished.</summary>
        public int Written
        {
            get { return Thread.VolatileRead(ref _written); }
        }

        /// <summary>Frames whose sink threw: they have a frames.csv row but are missing from the output.</summary>
        public int Failed
        {
            get { return Thread.VolatileRead(ref _failed); }
        }

        public long Bytes
        {
            get { return Interlocked.Read(ref _bytes); }
        }

        public string LastError
        {
            get { return _lastError; }
        }

        public bool Closed
        {
            get { return _closed; }
        }

        public bool WriterAlive
        {
            get { return _writer.IsAlive; }
        }

        /// <summary>Whether the writer is maxQueued frames behind (drop the next capture).</summary>
        public bool Full
        {
            get
            {
                lock (_gate)
                {
                    return _pending.Count >= _maxQueued;
                }
            }
        }

        /// <summary>A buffer of bufferLength bytes, or null when all maxBuffers are in use or the queue is closed.</summary>
        public byte[] Rent()
        {
            lock (_gate)
            {
                if (_closed)
                {
                    return null;
                }
                if (_pool.Count > 0)
                {
                    return _pool.Pop();
                }
                if (_allocated < _maxBuffers)
                {
                    _allocated++;
                    return new byte[_bufferLength];
                }
                return null;
            }
        }

        /// <summary>Gives a rented buffer back (one of another size is dropped).</summary>
        public void Return(byte[] buffer)
        {
            if (buffer == null || buffer.Length != _bufferLength)
            {
                return;
            }
            lock (_gate)
            {
                _pool.Push(buffer);
            }
        }

        /// <summary>
        /// Numbers the frame, writes its index row and queues it. Returns the frame number, or 0 when the
        /// queue is closed (a late frame of a finished recording: nothing is written, the buffer goes back).
        /// Call from one thread (the main thread).
        /// </summary>
        public int Accept(byte[] data, int length, bool pooled, string row)
        {
            if (_closed)
            {
                if (pooled)
                {
                    Return(data);
                }
                return 0;
            }
            int number = _frames + 1;
            try
            {
                _index.WriteLine(number.ToString(Inv) + "," + row);
            }
            catch (Exception)
            {
                if (pooled)
                {
                    Return(data);
                }
                throw; // not numbered, not queued
            }
            Thread.VolatileWrite(ref _frames, number);
            Frame f = new Frame();
            f.Number = number;
            f.Data = data;
            f.Length = length;
            f.Pooled = pooled;
            lock (_gate)
            {
                _pending.Enqueue(f);
                Monitor.Pulse(_gate);
            }
            return number;
        }

        /// <summary>
        /// Closes the queue (Accept and Rent refuse from now on) and waits for the writer to write every
        /// queued frame. False if it is still writing after timeoutMs; call again to wait longer.
        /// </summary>
        public bool Finish(int timeoutMs)
        {
            lock (_gate)
            {
                _closed = true;
                Monitor.PulseAll(_gate);
            }
            return !_writer.IsAlive || _writer.Join(timeoutMs);
        }

        private void WriterLoop()
        {
            while (true)
            {
                Frame f;
                lock (_gate)
                {
                    while (_pending.Count == 0 && !_closed)
                    {
                        Monitor.Wait(_gate);
                    }
                    if (_pending.Count == 0)
                    {
                        return; // closed and drained
                    }
                    f = _pending.Dequeue();
                }
                try
                {
                    _sink(f);
                    Interlocked.Increment(ref _written);
                    Interlocked.Add(ref _bytes, f.Length);
                }
                catch (Exception ex)
                {
                    Interlocked.Increment(ref _failed);
                    _lastError = "writing frame " + f.Number + " failed: " + ex.Message;
                }
                finally
                {
                    if (f.Pooled)
                    {
                        Return(f.Data);
                    }
                }
            }
        }
    }
}
