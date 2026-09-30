using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace KspAutomationBridge
{
    /// <summary>
    /// Minimal JSON reader and writer (C# 5, no dependencies).
    /// Reading yields Dictionary&lt;string, object&gt;, List&lt;object&gt;, string, double, bool or null.
    /// Writing accepts those plus every numeric type, enums, arrays/lists and dictionaries (Unity
    /// and KSP vectors must be converted with Util.Vec first: this file stays free of game types
    /// so it can be unit-tested outside KSP).
    /// NaN and Infinity are written as null so the output is always strict JSON.
    /// </summary>
    internal static class Json
    {
        private const int MaxDepth = 64;

        public static object Parse(string text)
        {
            if (text == null)
            {
                throw new FormatException("JSON text is null.");
            }
            int pos = 0;
            SkipWhitespace(text, ref pos);
            object value = ReadValue(text, ref pos, 0);
            SkipWhitespace(text, ref pos);
            if (pos != text.Length)
            {
                throw Error(text, pos, "unexpected trailing characters");
            }
            return value;
        }

        public static string Serialize(object value)
        {
            StringBuilder sb = new StringBuilder(256);
            Write(sb, value, 0);
            return sb.ToString();
        }

        // ------------------------------------------------------------------ reader

        private static object ReadValue(string s, ref int pos, int depth)
        {
            if (depth > MaxDepth)
            {
                throw Error(s, pos, "nesting too deep");
            }
            if (pos >= s.Length)
            {
                throw Error(s, pos, "unexpected end of input");
            }
            char c = s[pos];
            switch (c)
            {
                case '{':
                    return ReadObject(s, ref pos, depth);
                case '[':
                    return ReadArray(s, ref pos, depth);
                case '"':
                    return ReadString(s, ref pos);
                case 't':
                    ExpectLiteral(s, ref pos, "true");
                    return true;
                case 'f':
                    ExpectLiteral(s, ref pos, "false");
                    return false;
                case 'n':
                    ExpectLiteral(s, ref pos, "null");
                    return null;
                default:
                    if (c == '-' || (c >= '0' && c <= '9'))
                    {
                        return ReadNumber(s, ref pos);
                    }
                    throw Error(s, pos, "unexpected character '" + c + "'");
            }
        }

        private static Dictionary<string, object> ReadObject(string s, ref int pos, int depth)
        {
            Dictionary<string, object> result = new Dictionary<string, object>(StringComparer.Ordinal);
            pos++; // '{'
            SkipWhitespace(s, ref pos);
            if (pos < s.Length && s[pos] == '}')
            {
                pos++;
                return result;
            }
            while (true)
            {
                SkipWhitespace(s, ref pos);
                if (pos >= s.Length || s[pos] != '"')
                {
                    throw Error(s, pos, "expected a string key");
                }
                string key = ReadString(s, ref pos);
                SkipWhitespace(s, ref pos);
                if (pos >= s.Length || s[pos] != ':')
                {
                    throw Error(s, pos, "expected ':'");
                }
                pos++;
                SkipWhitespace(s, ref pos);
                result[key] = ReadValue(s, ref pos, depth + 1);
                SkipWhitespace(s, ref pos);
                if (pos >= s.Length)
                {
                    throw Error(s, pos, "unterminated object");
                }
                if (s[pos] == ',')
                {
                    pos++;
                    continue;
                }
                if (s[pos] == '}')
                {
                    pos++;
                    return result;
                }
                throw Error(s, pos, "expected ',' or '}'");
            }
        }

        private static List<object> ReadArray(string s, ref int pos, int depth)
        {
            List<object> result = new List<object>();
            pos++; // '['
            SkipWhitespace(s, ref pos);
            if (pos < s.Length && s[pos] == ']')
            {
                pos++;
                return result;
            }
            while (true)
            {
                SkipWhitespace(s, ref pos);
                result.Add(ReadValue(s, ref pos, depth + 1));
                SkipWhitespace(s, ref pos);
                if (pos >= s.Length)
                {
                    throw Error(s, pos, "unterminated array");
                }
                if (s[pos] == ',')
                {
                    pos++;
                    continue;
                }
                if (s[pos] == ']')
                {
                    pos++;
                    return result;
                }
                throw Error(s, pos, "expected ',' or ']'");
            }
        }

        private static string ReadString(string s, ref int pos)
        {
            pos++; // opening quote
            StringBuilder sb = null;
            int runStart = pos;
            while (pos < s.Length)
            {
                char c = s[pos];
                if (c == '"')
                {
                    string tail = s.Substring(runStart, pos - runStart);
                    pos++;
                    return sb == null ? tail : sb.Append(tail).ToString();
                }
                if (c == '\\')
                {
                    if (sb == null)
                    {
                        sb = new StringBuilder();
                    }
                    sb.Append(s, runStart, pos - runStart);
                    pos++;
                    if (pos >= s.Length)
                    {
                        break;
                    }
                    char e = s[pos];
                    switch (e)
                    {
                        case '"': sb.Append('"'); break;
                        case '\\': sb.Append('\\'); break;
                        case '/': sb.Append('/'); break;
                        case 'b': sb.Append('\b'); break;
                        case 'f': sb.Append('\f'); break;
                        case 'n': sb.Append('\n'); break;
                        case 'r': sb.Append('\r'); break;
                        case 't': sb.Append('\t'); break;
                        case 'u':
                            if (pos + 4 >= s.Length)
                            {
                                throw Error(s, pos, "truncated \\u escape");
                            }
                            int code;
                            if (!int.TryParse(s.Substring(pos + 1, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out code))
                            {
                                throw Error(s, pos, "invalid \\u escape");
                            }
                            sb.Append((char)code); // surrogate pairs arrive as two escapes and recombine naturally
                            pos += 4;
                            break;
                        default:
                            throw Error(s, pos, "invalid escape '\\" + e + "'");
                    }
                    pos++;
                    runStart = pos;
                    continue;
                }
                if (c < 0x20)
                {
                    throw Error(s, pos, "control character in string");
                }
                pos++;
            }
            throw Error(s, pos, "unterminated string");
        }

        private static double ReadNumber(string s, ref int pos)
        {
            int start = pos;
            if (s[pos] == '-')
            {
                pos++;
            }
            while (pos < s.Length)
            {
                char c = s[pos];
                if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-')
                {
                    pos++;
                }
                else
                {
                    break;
                }
            }
            double value;
            if (!double.TryParse(s.Substring(start, pos - start), NumberStyles.Float, CultureInfo.InvariantCulture, out value))
            {
                throw Error(s, start, "invalid number");
            }
            return value;
        }

        private static void ExpectLiteral(string s, ref int pos, string literal)
        {
            if (string.CompareOrdinal(s, pos, literal, 0, literal.Length) != 0)
            {
                throw Error(s, pos, "invalid literal");
            }
            pos += literal.Length;
        }

        private static void SkipWhitespace(string s, ref int pos)
        {
            while (pos < s.Length && (s[pos] == ' ' || s[pos] == '\t' || s[pos] == '\n' || s[pos] == '\r' || s[pos] == '\uFEFF'))
            {
                pos++;
            }
        }

        private static FormatException Error(string s, int pos, string what)
        {
            return new FormatException("Invalid JSON at character " + pos + ": " + what + ".");
        }

        // ------------------------------------------------------------------ writer

        private static void Write(StringBuilder sb, object value, int depth)
        {
            if (depth > MaxDepth)
            {
                throw new InvalidOperationException("JSON value nested too deeply (cycle?).");
            }
            if (value == null)
            {
                sb.Append("null");
                return;
            }
            string str = value as string;
            if (str != null)
            {
                WriteString(sb, str);
                return;
            }
            if (value is bool)
            {
                sb.Append((bool)value ? "true" : "false");
                return;
            }
            if (value is double)
            {
                WriteDouble(sb, (double)value);
                return;
            }
            if (value is float)
            {
                float f = (float)value;
                if (float.IsNaN(f) || float.IsInfinity(f))
                {
                    sb.Append("null");
                }
                else
                {
                    sb.Append(f.ToString("R", CultureInfo.InvariantCulture));
                }
                return;
            }
            if (value is int || value is long || value is uint || value is ulong || value is short
                || value is ushort || value is byte || value is sbyte || value is decimal)
            {
                sb.Append(Convert.ToString(value, CultureInfo.InvariantCulture));
                return;
            }
            if (value is Enum)
            {
                WriteString(sb, value.ToString());
                return;
            }
            IDictionary dict = value as IDictionary;
            if (dict != null)
            {
                sb.Append('{');
                bool first = true;
                foreach (DictionaryEntry entry in dict)
                {
                    if (!first)
                    {
                        sb.Append(',');
                    }
                    first = false;
                    WriteString(sb, Convert.ToString(entry.Key, CultureInfo.InvariantCulture));
                    sb.Append(':');
                    Write(sb, entry.Value, depth + 1);
                }
                sb.Append('}');
                return;
            }
            IEnumerable seq = value as IEnumerable;
            if (seq != null)
            {
                WriteArray(sb, seq, depth);
                return;
            }
            WriteString(sb, Convert.ToString(value, CultureInfo.InvariantCulture));
        }

        private static void WriteArray(StringBuilder sb, IEnumerable items, int depth)
        {
            sb.Append('[');
            bool first = true;
            foreach (object item in items)
            {
                if (!first)
                {
                    sb.Append(',');
                }
                first = false;
                Write(sb, item, depth + 1);
            }
            sb.Append(']');
        }

        private static void WriteDouble(StringBuilder sb, double d)
        {
            if (double.IsNaN(d) || double.IsInfinity(d))
            {
                sb.Append("null");
            }
            else
            {
                sb.Append(d.ToString("R", CultureInfo.InvariantCulture));
            }
        }

        private static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    case '\b': sb.Append("\\b"); break;
                    case '\f': sb.Append("\\f"); break;
                    default:
                        if (c < 0x20 || c == '\u2028' || c == '\u2029')
                        {
                            sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            sb.Append(c);
                        }
                        break;
                }
            }
            sb.Append('"');
        }
    }
}
