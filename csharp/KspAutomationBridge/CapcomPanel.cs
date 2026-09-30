using System;
using System.Collections.Generic;
using System.Globalization;
using KSP.UI.Screens;
using UnityEngine;

namespace KspAutomationBridge
{
    /// <summary>
    /// The in-game CAPCOM panel (IMGUI): a scrolling two-way thread plus an input box. Toggled by F8 or
    /// the toolbar button; opens itself when the AI posts an alert. Main thread only.
    /// </summary>
    internal sealed class CapcomWindow
    {
        private const int WindowId = 0x41535452; // "ASTR"
        private const string InputControl = "AstraCapcomInput";
        private const string LockId = "AstraCapcomPanel";

        private Rect _rect = new Rect(80f, 80f, 520f, 460f);
        private bool _visible;
        private string _input = "";
        private Vector2 _scroll;
        private long _renderedSeq = -1;
        private string _lastError;
        private ControlTypes _lock = ControlTypes.None;
        private ApplicationLauncherButton _button;
        private Texture2D _icon;
        private GUIStyle _info;
        private GUIStyle _warn;
        private GUIStyle _alert;
        private GUIStyle _player;
        private GUIStyle _dim;

        public void Start()
        {
            GameEvents.onGUIApplicationLauncherReady.Add(AddButton);
            GameEvents.onGUIApplicationLauncherDestroyed.Add(ForgetButton);
            AddButton();
        }

        public void Destroy()
        {
            GameEvents.onGUIApplicationLauncherReady.Remove(AddButton);
            GameEvents.onGUIApplicationLauncherDestroyed.Remove(ForgetButton);
            if (_button != null && ApplicationLauncher.Instance != null)
            {
                ApplicationLauncher.Instance.RemoveModApplication(_button);
            }
            _button = null;
            Unlock();
        }

        public void Update()
        {
            try
            {
                if (Input.GetKeyDown(KeyCode.F8))
                {
                    SetVisible(!_visible);
                }
                if (CapcomLog.TakeOpenRequest())
                {
                    SetVisible(true);
                }
                CapcomMessage notice;
                while (CapcomLog.TakeNotice(out notice))
                {
                    ScreenMessages.PostScreenMessage("CAPCOM: " + notice.Text, notice.Level == "alert" ? 10f : 6f,
                        ScreenMessageStyle.UPPER_CENTER, notice.Level == "alert" ? new Color(1f, 0.35f, 0.3f) : new Color(1f, 0.85f, 0.3f));
                }
            }
            catch (Exception ex)
            {
                BridgeLog.RecordError("CAPCOM update failed", ex);
            }
        }

        public void OnGUI()
        {
            if (!_visible)
            {
                Unlock();
                return;
            }
            try
            {
                if (_info == null)
                {
                    BuildStyles();
                }
                Event e = Event.current;
                if (e.type == EventType.MouseDown && !_rect.Contains(e.mousePosition))
                {
                    GUIUtility.keyboardControl = 0; // clicking the game releases the text box (and the key lock)
                }
                _rect = GUILayout.Window(WindowId, _rect, Draw, "ASTRA CAPCOM");
                // Typing must not stage, throttle or switch vessels; hovering only guards editor part picking.
                bool typing = GUI.GetNameOfFocusedControl() == InputControl;
                bool hovering = _rect.Contains(e.mousePosition);
                SetLock(typing ? ControlTypes.ALLBUTCAMERAS
                    : hovering && HighLogic.LoadedSceneIsEditor ? ControlTypes.EDITOR_SOFT_LOCK : ControlTypes.None);
            }
            catch (Exception ex)
            {
                // OnGUI runs several times per frame: log a failure once, not every call.
                if (_lastError != ex.Message)
                {
                    _lastError = ex.Message;
                    BridgeLog.RecordError("CAPCOM panel draw failed", ex);
                }
            }
        }

        private void Draw(int id)
        {
            double sincePoll = CapcomLog.SecondsSinceInboxPoll();
            string link = sincePoll < 0 ? "AI has not read the inbox yet"
                : sincePoll < 120 ? "AI listening (read " + sincePoll.ToString("0") + " s ago)"
                : "AI last read the inbox " + (sincePoll / 60).ToString("0") + " min ago";
            GUILayout.BeginHorizontal();
            GUILayout.Label(AutomationBridgeAddon.Listening ? "Bridge :" + AutomationBridgeAddon.Port : "Bridge offline", _dim);
            GUILayout.FlexibleSpace();
            GUILayout.Label(link, _dim);
            if (GUILayout.Button("x", GUILayout.Width(22f)))
            {
                SetVisible(false);
            }
            GUILayout.EndHorizontal();

            List<CapcomMessage> messages = CapcomLog.Tail(200);
            long last = messages.Count > 0 ? messages[messages.Count - 1].Seq : 0;
            if (last != _renderedSeq)
            {
                _scroll.y = float.MaxValue; // follow new messages
                _renderedSeq = last;
            }
            _scroll = GUILayout.BeginScrollView(_scroll, GUILayout.ExpandHeight(true));
            if (messages.Count == 0)
            {
                GUILayout.Label("No messages yet. Type below to talk to the AI crew.", _dim);
            }
            foreach (CapcomMessage m in messages)
            {
                string who = m.From == "player" ? "YOU" : "ASTRA";
                GUIStyle style = m.From == "player" ? _player : m.Level == "alert" ? _alert : m.Level == "warn" ? _warn : _info;
                GUILayout.Label("[" + m.Utc.ToLocalTime().ToString("HH:mm:ss", CultureInfo.InvariantCulture) + "] " + who + ": " + m.Text, style);
            }
            GUILayout.EndScrollView();

            Event e = Event.current;
            bool enter = e.type == EventType.KeyDown && (e.keyCode == KeyCode.Return || e.keyCode == KeyCode.KeypadEnter)
                && GUI.GetNameOfFocusedControl() == InputControl;
            if (enter)
            {
                Send();
                e.Use();
            }
            GUILayout.BeginHorizontal();
            GUI.SetNextControlName(InputControl);
            _input = GUILayout.TextField(_input ?? "", CapcomLog.MaxTextLength, GUILayout.ExpandWidth(true), GUILayout.MinHeight(24f));
            if (GUILayout.Button("Send", GUILayout.Width(70f), GUILayout.Height(24f)))
            {
                Send();
            }
            GUILayout.EndHorizontal();
            GUILayout.Label("F8 or the toolbar button toggles this panel. The AI reads your messages when it checks its inbox.", _dim);
            GUI.DragWindow(new Rect(0f, 0f, 10000f, 22f));
        }

        private void Send()
        {
            string text = (_input ?? "").Trim();
            if (text.Length == 0)
            {
                return;
            }
            CapcomLog.Post("player", "info", text, Util.SafeUt());
            _input = "";
        }

        private void BuildStyles()
        {
            _info = Style(new Color(0.92f, 0.92f, 0.92f));
            _warn = Style(new Color(1f, 0.85f, 0.3f));
            _alert = Style(new Color(1f, 0.4f, 0.35f));
            _player = Style(new Color(0.45f, 0.85f, 1f));
            _dim = Style(new Color(0.65f, 0.65f, 0.65f));
        }

        private static GUIStyle Style(Color color)
        {
            GUIStyle s = new GUIStyle(GUI.skin.label);
            s.wordWrap = true;
            s.richText = false; // message text is shown verbatim
            s.normal.textColor = color;
            return s;
        }

        private void SetVisible(bool visible)
        {
            _visible = visible;
            if (_button != null)
            {
                if (visible)
                {
                    _button.SetTrue(false);
                }
                else
                {
                    _button.SetFalse(false);
                }
            }
            if (!visible)
            {
                Unlock();
            }
        }

        private void SetLock(ControlTypes wanted)
        {
            if (wanted == _lock)
            {
                return;
            }
            InputLockManager.RemoveControlLock(LockId);
            if (wanted != ControlTypes.None)
            {
                InputLockManager.SetControlLock(wanted, LockId);
            }
            _lock = wanted;
        }

        private void Unlock()
        {
            SetLock(ControlTypes.None);
        }

        private void AddButton()
        {
            try
            {
                if (_button != null || ApplicationLauncher.Instance == null)
                {
                    return;
                }
                if (_icon == null)
                {
                    _icon = MakeIcon();
                }
                _button = ApplicationLauncher.Instance.AddModApplication(
                    delegate { _visible = true; }, delegate { _visible = false; Unlock(); },
                    null, null, null, null, ApplicationLauncher.AppScenes.ALWAYS, _icon);
                if (_visible)
                {
                    _button.SetTrue(false);
                }
            }
            catch (Exception ex)
            {
                BridgeLog.Warn("Could not add the CAPCOM toolbar button: " + ex.Message);
            }
        }

        private void ForgetButton()
        {
            _button = null;
        }

        /// <summary>A 38x38 headset-blue disc on a dark square, drawn at runtime so no asset ships.</summary>
        private static Texture2D MakeIcon()
        {
            const int size = 38;
            Texture2D tex = new Texture2D(size, size, TextureFormat.RGBA32, false);
            Color background = new Color(0.10f, 0.12f, 0.16f, 1f);
            Color disc = new Color(0.20f, 0.65f, 0.95f, 1f);
            float r = size * 0.36f;
            for (int y = 0; y < size; y++)
            {
                for (int x = 0; x < size; x++)
                {
                    float dx = x - size / 2f + 0.5f;
                    float dy = y - size / 2f + 0.5f;
                    tex.SetPixel(x, y, dx * dx + dy * dy <= r * r ? disc : background);
                }
            }
            tex.Apply();
            return tex;
        }
    }
}
