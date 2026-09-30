# YouTube upload kit

Video: `astra_showcase_en.mp4` on the [v2.0.0 release](https://github.com/shoal-rat/astra-ksp/releases/tag/v2.0.0)
(1920×1080, 30 fps, English narration, about 5 minutes). Thumbnail: `astra_cover_en.jpg` on the same page
(1920×1080 JPEG, under YouTube's 2 MB limit).

The video is built by [`scripts/showcase/make_video.py`](../../scripts/showcase/make_video.py) `--lang en` from the
in-game recordings, with the English narration in [`scripts/showcase/storyboard.en.json`](../../scripts/showcase/storyboard.en.json).
The narration is a synthetic voice (Microsoft `en-US-AndrewNeural` through edge-tts) and the music is synthesized by the
builder, so there is no third-party music to claim. The game's own interface in the footage is in Chinese, because the
recording install runs KSP localized as zh-cn; the overlays (subtitles, tool calls, telemetry, cards) are in English.

## Title (pick one)

- An AI Flies Kerbal Space Program: Mun and Mars Landings, No Scripts | ASTRA
- I Gave Claude a Rocket: It Designed It, Flew It, and Landed on Mars (KSP)
- An AI Astronaut Lands on the Mun and Duna, With No Mission Script | Kerbal Space Program

## Description

```text
An AI flew these missions itself. ASTRA turns a running game of Kerbal Space Program into 74 tools
(instruments, calculators, controls and short reflexes), and Claude uses them as astronaut and
mission control: it designs the rocket from the live part catalog, launches it, plans the transfer,
lands with its own powered-descent guidance, sends a kerbal out to plant a flag, and flies home.

There is no mission script anywhere in the project, and the AI is not allowed to write one. At every
step it reads the telemetry, computes the numbers, writes its decision in a flight log, acts, and
checks the result. The game pauses while it thinks.

In this video (all in-game footage; the top-right caption is the tool the AI was calling, the
bottom-left line is the telemetry at that moment):
- the AI designs a Mun rocket and flies it to orbit with MechJeb, every setting its own choice
- powered landing on the Mun at 0.03 m/s, the failures on the way there, and a flag
- a Lambert transfer to Duna (KSP's Mars), aerobraking, parachutes and a powered touchdown at 1.2 m/s
- Valentina Kerman plants a flag on Duna
- `astra mission`: one command, and a Claude crew with only these tools flies the whole thing

Open source (MIT): https://github.com/shoal-rat/astra-ksp
Flight logs of every mission, including the failed attempts: https://github.com/shoal-rat/astra-ksp/tree/main/docs/missions

00:00 A Flag on Duna
00:22 What Is ASTRA?
00:46 How the AI Thinks
01:17 The AI Designs a Rocket
01:42 MechJeb Launch to Orbit
02:01 Bound for the Mun
02:13 Powered Landing
02:39 Failures and Lessons
02:59 A Flag on the Mun
03:12 Return and Reentry
03:26 Landing on Duna (Mars)
04:17 One Command, Fully Autonomous
04:39 Open Source on GitHub

Narration: synthetic voice (Microsoft en-US-AndrewNeural via edge-tts). Music: synthesized for this video.
Built with kRPC, MechJeb2, Harmony and ModuleManager. Kerbal Space Program is a trademark of Take-Two
Interactive; this is a fan project, not affiliated with or endorsed by them.
```

## Tags

Kerbal Space Program, KSP, AI, Claude, Anthropic, artificial intelligence, AI agent, MCP, Model Context Protocol,
autonomous, space, rocket, Mun landing, Mars landing, Duna, MechJeb, kRPC, open source, programming, space flight

## Settings

- Category: Science & Technology (or Gaming).
- Audience: not made for kids.
- Chapters: YouTube builds them from the timestamps in the description (the first must be 00:00, each at least 10 s).
- Language: English; subtitles are burned in, so no caption file is needed.
